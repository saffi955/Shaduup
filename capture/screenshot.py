"""
Screenshot capture — one PNG per state, window region only.

Grabs just the target window's rectangle (not the whole desktop, and never the
AppShadow recorder itself) via mss, and names it after the state. The PNG is the
safety net: when UIA can't see an owner-drawn toolbar (risk R1), the screenshot
is what a vision model parses later (backup plan §9).

Because the process is per-monitor DPI aware (see ``config.set_dpi_awareness``),
``GetWindowRect`` returns true physical pixels that line up with what mss grabs,
so the crop is correct on scaled displays.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from typing import Optional, Sequence

import mss
import mss.tools

_user32 = ctypes.windll.user32


def _window_rect(hwnd: int) -> tuple[int, int, int, int]:
    r = wintypes.RECT()
    _user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def _paint_out(
    rgb: bytes,
    width: int,
    height: int,
    rects: Sequence[Sequence[int]],
    origin_x: int,
    origin_y: int,
) -> bytes:
    """Fill each screen-coordinate rect with solid black in an RGB buffer.

    ``rgb`` is row-major, 3 bytes per pixel. Rects arrive in screen coordinates
    and are translated by the window origin, then clamped to the image, so a
    field hanging off the edge of the window cannot write out of bounds.
    """
    buf = bytearray(rgb)
    for rect in rects:
        if not rect or len(rect) < 4:
            continue
        left = max(0, min(width, int(rect[0]) - origin_x))
        top = max(0, min(height, int(rect[1]) - origin_y))
        right = max(0, min(width, int(rect[2]) - origin_x))
        bottom = max(0, min(height, int(rect[3]) - origin_y))
        if right <= left or bottom <= top:
            continue
        blank = b"\x00" * ((right - left) * 3)
        for y in range(top, bottom):
            start = (y * width + left) * 3
            buf[start:start + len(blank)] = blank
    return bytes(buf)


class ScreenshotGrabber:
    """Saves ``<out_dir>/screenshots/state_NNNN.png`` for a given window."""

    def __init__(self, out_dir: str, subdir: str = "screenshots") -> None:
        self.dir = os.path.join(out_dir, subdir)
        os.makedirs(self.dir, exist_ok=True)
        self.rel_prefix = subdir
        # One mss instance, reused. mss is not thread-safe, so the recorder must
        # only call grab() from a single worker thread (it does — the debounced
        # capture path).
        self._sct = mss.mss()

    def grab(
        self,
        hwnd: int,
        state_id: str,
        redact_rects: Optional[Sequence[Sequence[int]]] = None,
    ) -> Optional[str]:
        """Capture the window's region. Returns the map-relative PNG path.

        ``redact_rects`` are screen-coordinate rectangles of fields whose value
        is never recorded (see ``config.REDACT_TYPES``). They are painted out
        before the PNG is written, so the privacy rule holds for the picture as
        well as the JSON: a screenshot is a pixel-accurate copy of the screen,
        so without this a password or filename you typed would survive in the
        image even though it was stripped from the map.

        Returns ``None`` if the window has no drawable area (e.g. it closed
        between the event and the grab — common with menus).
        """
        left, top, right, bottom = _window_rect(hwnd)
        width, height = right - left, bottom - top
        if width <= 0 or height <= 0:
            return None

        region = {"left": left, "top": top, "width": width, "height": height}
        shot = self._sct.grab(region)
        pixels = shot.rgb
        if redact_rects:
            pixels = _paint_out(pixels, width, height, redact_rects, left, top)
        path = os.path.join(self.dir, f"{state_id}.png")
        mss.tools.to_png(pixels, shot.size, output=path)
        return f"{self.rel_prefix}/{state_id}.png"

    def close(self) -> None:
        try:
            self._sct.close()
        except Exception:
            pass
