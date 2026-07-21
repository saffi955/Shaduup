"""
Windows UIA scraper — Phase 2 deliverable.

Given a target process (or a specific window handle), walk the *currently
visible* UI Automation control tree and return one core ``State``. This is the
"prove it on the real target" step: no event hooks, no debounce, no screenshots
yet — just a fast, honest snapshot of what UIA can see.

Speed matters. A menu popup can vanish in a fraction of a second, so instead of
pywinauto's convenient-but-chatty per-element property reads (one cross-process
COM call *per property, per element*), this pulls the whole subtree in a single
``FindAllBuildCache`` round-trip with every property we need pre-cached. That is
what lets a menu-scoped read land inside the ~150 ms budget (risk R2).

Run it directly to try it on a real app:

    python -m scrapers.windows_uia --launch "C:\\Program Files (x86)\\InPage 2012\\InPage 2012.exe"
    python -m scrapers.windows_uia --pid 14232
    python -m scrapers.windows_uia --title-re ".*InPage.*"
"""

from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes
from typing import Callable, Optional

from config import REDACT_TYPES, set_dpi_awareness
from core.schema import Control, Edge, State
from scrapers.base import Scraper

_kernel32 = ctypes.windll.kernel32

# --- pywinauto / comtypes UIA plumbing --------------------------------------
# Imported lazily-at-module-load so a machine without the deps fails loudly and
# early rather than deep inside a walk.
from pywinauto.uia_defines import IUIA  # noqa: E402

_IUIA = IUIA()
_UIA = _IUIA.iuia          # IUIAutomation
_MOD = _IUIA.UIA_dll       # generated comtypes module holding the UIA_* constants


# Map control-type ids (50000, 50001, ...) to their short names ("Button",
# "MenuItem", ...) once, straight from the generated module.
def _build_control_type_names() -> dict[int, str]:
    names: dict[int, str] = {}
    suffix = "ControlTypeId"
    for attr in dir(_MOD):
        if attr.startswith("UIA_") and attr.endswith(suffix):
            short = attr[len("UIA_") : -len(suffix)]
            names[getattr(_MOD, attr)] = short
    return names


_CONTROL_TYPE_NAMES = _build_control_type_names()

# Properties we cache in the single round-trip. If you read a Cached* accessor
# for a property that isn't in this list, comtypes raises — so the two must stay
# in sync.
_CACHED_PROPERTIES = (
    _MOD.UIA_NamePropertyId,
    _MOD.UIA_ControlTypePropertyId,
    _MOD.UIA_AutomationIdPropertyId,
    _MOD.UIA_BoundingRectanglePropertyId,
    _MOD.UIA_IsEnabledPropertyId,
    _MOD.UIA_IsOffscreenPropertyId,
    _MOD.UIA_AcceleratorKeyPropertyId,
    _MOD.UIA_AccessKeyPropertyId,
)

_TREESCOPE_DESCENDANTS = _MOD.TreeScope_Descendants

# Title-bar / system-menu controls present on essentially every window. They are
# not app content, so they don't count towards "does this window expose real
# controls to UIA" when deciding whether the screenshot needs vision parsing.
_WINDOW_CHROME_NAMES = frozenset(
    {"Minimize", "Maximize", "Restore", "Close", "System", "Application"}
)


def _make_cache_request():
    cr = _UIA.CreateCacheRequest()
    for prop in _CACHED_PROPERTIES:
        cr.AddProperty(prop)
    return cr


def _split_accelerator(name: str, accelerator: str) -> tuple[str, str]:
    """Separate a menu label from a tab-embedded accelerator.

    Classic Win32 menus (InPage, and most plain-compiled apps) leave the UIA
    AcceleratorKey property empty and instead pack the shortcut into the label
    after a tab: ``"New...\\tCtrl+N"``. Split it so ``name`` stays clean and the
    ``shortcut`` field is populated the way an app menu should be. Names without
    a tab (e.g. recent-file entries) pass through untouched.

    >>> _split_accelerator("New...\\tCtrl+N", "")
    ('New...', 'Ctrl+N')
    >>> _split_accelerator("Open...", "")
    ('Open...', '')
    >>> _split_accelerator("Paste", "Ctrl+V")   # real property wins, not overwritten
    ('Paste', 'Ctrl+V')
    """
    if "\t" in name:
        label, _, embedded = name.partition("\t")
        if not accelerator:
            accelerator = embedded.strip()
        name = label
    return name.rstrip(), accelerator


# --- Win32 window enumeration (find the target's main hwnd) ------------------
_user32 = ctypes.windll.user32
_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def _pid_of_hwnd(hwnd: int) -> int:
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _window_title(hwnd: int) -> str:
    length = _user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def top_level_windows(pid: int) -> list[int]:
    """Visible, titled, top-level window handles owned by ``pid``."""
    found: list[int] = []

    def _cb(hwnd, _lparam):
        if (
            _user32.IsWindowVisible(hwnd)
            and _pid_of_hwnd(hwnd) == pid
            and _window_title(hwnd)
        ):
            found.append(hwnd)
        return True

    _user32.EnumWindows(_WNDENUMPROC(_cb), 0)
    return found


def main_hwnd_for_pid(pid: int) -> Optional[int]:
    """Best guess at the app's primary window: the largest visible one."""
    wins = top_level_windows(pid)
    if not wins:
        return None

    def _area(hwnd: int) -> int:
        r = wintypes.RECT()
        _user32.GetWindowRect(hwnd, ctypes.byref(r))
        return max(0, r.right - r.left) * max(0, r.bottom - r.top)

    return max(wins, key=_area)


def find_hwnd_by_title(title_re: str) -> Optional[int]:
    """First visible top-level window whose title matches ``title_re``."""
    import re

    pattern = re.compile(title_re)
    match: list[int] = []

    def _cb(hwnd, _lparam):
        if _user32.IsWindowVisible(hwnd):
            t = _window_title(hwnd)
            if t and pattern.search(t):
                match.append(hwnd)
                return False
        return True

    _user32.EnumWindows(_WNDENUMPROC(_cb), 0)
    return match[0] if match else None


# --- The scraper ------------------------------------------------------------
class WindowsUIAScraper(Scraper):
    """UIA-backed capture backend for Windows desktop apps."""

    def __init__(self) -> None:
        self.pid: Optional[int] = None
        self._cache = _make_cache_request()

    def attach(self, pid: int) -> None:
        self.pid = pid

    # -- helpers --------------------------------------------------------------
    @staticmethod
    def _control_type_name(type_id: int) -> str:
        return _CONTROL_TYPE_NAMES.get(type_id, f"Custom({type_id})")

    def _element_to_control(self, el) -> Optional[Control]:
        """Turn one cached UIA element into a Control, or None to drop it.

        Reads only cached properties (no fresh cross-process calls) and never
        touches the Value pattern, so a typed-in string is never even fetched,
        let alone stored.
        """
        try:
            if el.CachedIsOffscreen:
                return None
            rect = el.CachedBoundingRectangle  # tagRECT: left/top/right/bottom
            r = [int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)]
            if r == [0, 0, 0, 0]:
                return None  # non-visual element, nothing to point at

            type_name = self._control_type_name(el.CachedControlType)
            name, accelerator = _split_accelerator(
                el.CachedName or "", el.CachedAcceleratorKey or ""
            )
            automation_id = el.CachedAutomationId or ""
            enabled = bool(el.CachedIsEnabled)

            redacted = type_name in REDACT_TYPES
            # Drop nameless, non-actionable noise (separators, blank panes) to
            # keep the map lean — but always keep redacted fields (a nameless
            # Edit still matters) and anything with an automation id.
            if not name and not automation_id and not redacted:
                if type_name in ("Pane", "Group", "Separator", "Text", "Image", "Custom"):
                    return None

            return Control(
                type=type_name,
                name=name,
                rect=r,
                enabled=enabled,
                automation_id=automation_id or None,
                shortcut=accelerator or None,
                value_redacted=redacted,
                leads_to=None,  # edges are wired by the session graph (Phase 3)
            )
        except Exception:
            # A control that vanished mid-walk (menus do this) — just skip it.
            return None

    def _walk(self, root_element, max_controls: int) -> list[Control]:
        cond = getattr(_UIA, "ControlViewCondition", None) or _IUIA.true_condition
        array = root_element.FindAllBuildCache(
            _TREESCOPE_DESCENDANTS, cond, self._cache
        )
        controls: list[Control] = []
        length = array.Length
        for i in range(min(length, max_controls)):
            ctrl = self._element_to_control(array.GetElement(i))
            if ctrl is not None:
                controls.append(ctrl)
        return controls

    # -- public API -----------------------------------------------------------
    def snapshot(
        self,
        state_id: str,
        *,
        kind: str = "window",
        reached_by: Optional[Edge] = None,
        hwnd: Optional[int] = None,
        max_controls: int = 600,
        vision_threshold: int = 3,
    ) -> State:
        """Capture one State from the target.

        ``hwnd`` scopes the walk to a specific window — pass the popup's handle
        for a menu so the subtree (and the time) stays tiny. With no ``hwnd``,
        the target process's main window is used.
        """
        if hwnd is None:
            if self.pid is None:
                raise RuntimeError("attach(pid) or pass hwnd= before snapshot()")
            hwnd = main_hwnd_for_pid(self.pid)
            if hwnd is None:
                raise RuntimeError(f"no visible top-level window for pid {self.pid}")

        root = _UIA.ElementFromHandle(hwnd)
        window_title = ""
        try:
            window_title = root.CurrentName or ""
        except Exception:
            window_title = _window_title(hwnd)

        controls = self._walk(root, max_controls)

        # Owner-drawn heuristic (risk R1). A plain-compiled app like InPage
        # exposes its menus and window chrome to UIA but draws its toolbars,
        # rulers and canvas itself — they surface only as nameless container
        # Panes. So a window is flagged for vision when it has almost no
        # *app-level* actionable control (menu items and the min/max/close
        # chrome don't count) yet several blank container panes. Menus are
        # exempt: a popup legitimately has few, unnamed-container-free items.
        app_actionable = [
            c
            for c in controls
            if c.is_interactive()
            and c.name
            and c.type != "MenuItem"
            and c.name not in _WINDOW_CHROME_NAMES
        ]
        blank_containers = [
            c for c in controls if c.type in ("Pane", "Custom", "Group") and not c.name
        ]
        needs_vision = (
            kind == "window"
            and len(app_actionable) < vision_threshold
            and len(blank_containers) >= 3
        )

        return State(
            state_id=state_id,
            kind=kind,
            window_title=window_title,
            reached_by=reached_by,
            controls=controls,
            needs_vision=needs_vision,
        )


# --- WinEvent hook listener (Phase 3, step 9) -------------------------------
# The events that mean "the UI just changed to something worth capturing".
EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_SYSTEM_MENUSTART = 0x0004
EVENT_SYSTEM_MENUPOPUPSTART = 0x0006
EVENT_SYSTEM_DIALOGSTART = 0x0010
EVENT_OBJECT_SHOW = 0x8002

WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002
_WM_QUIT = 0x0012
_OBJID_WINDOW = 0  # a whole-window show, not a child-object show

# void CALLBACK WinEventProc(HWINEVENTHOOK, DWORD event, HWND, LONG idObject,
#                            LONG idChild, DWORD idEventThread, DWORD dwmsTime)
_WINEVENTPROC = ctypes.WINFUNCTYPE(
    None,
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.HWND,
    wintypes.LONG,
    wintypes.LONG,
    wintypes.DWORD,
    wintypes.DWORD,
)


class WinEventListener:
    """Subscribe to the target process's UI-change events, off the main thread.

    ``SetWinEventHook`` with ``OUTOFCONTEXT`` only delivers callbacks while the
    subscribing thread is pumping messages, so the hook lives on its own thread
    running a ``GetMessage`` loop. Each relevant event is handed to ``on_event``
    (which must be cheap and do no COM work — it just notes what changed and
    kicks the debounce timer). The listener is filtered to ``pid`` at the OS
    level, so unrelated apps generate no traffic here at all.
    """

    def __init__(self, pid: int, on_event: Callable[[int, int, int], None]) -> None:
        self.pid = pid
        self.on_event = on_event
        self._proc = _WINEVENTPROC(self._callback)  # keep a ref, or it is GC'd
        self._thread: Optional[threading.Thread] = None
        self._thread_id: int = 0
        self._hooks: list[int] = []
        self._ready = threading.Event()

    def _callback(self, _hook, event, hwnd, id_object, _id_child, _tid, _time) -> None:
        try:
            self.on_event(int(event), int(hwnd) if hwnd else 0, int(id_object))
        except Exception:
            pass  # a listener must never die on one bad event

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="appshadow-winevent", daemon=True
        )
        self._thread.start()
        self._ready.wait(2.0)

    def _run(self) -> None:
        self._thread_id = _kernel32.GetCurrentThreadId()
        flags = WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS
        # One contiguous range covers foreground/menu/dialog; object-show is a
        # separate id far up the range, so it needs its own hook.
        for emin, emax in (
            (EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_DIALOGSTART),
            (EVENT_OBJECT_SHOW, EVENT_OBJECT_SHOW),
        ):
            h = _user32.SetWinEventHook(emin, emax, 0, self._proc, self.pid, 0, flags)
            if h:
                self._hooks.append(h)
        self._ready.set()

        msg = wintypes.MSG()
        while _user32.GetMessageW(ctypes.byref(msg), 0, 0, 0) > 0:
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))

        for h in self._hooks:
            _user32.UnhookWinEvent(h)
        self._hooks.clear()

    def stop(self) -> None:
        if self._thread_id:
            _user32.PostThreadMessageW(self._thread_id, _WM_QUIT, 0, 0)
        if self._thread is not None:
            self._thread.join(2.0)
            self._thread = None


# --- CLI: quick Phase 2 probe -----------------------------------------------
def _launch_and_wait(path: str, settle_s: float = 3.0) -> int:
    import subprocess
    import time

    proc = subprocess.Popen([path])
    time.sleep(settle_s)  # let the splash/main window come up
    return proc.pid


def _main(argv: Optional[list[str]] = None) -> int:
    import argparse
    import json
    import time

    parser = argparse.ArgumentParser(description="AppShadow Windows UIA snapshot probe")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--pid", type=int, help="attach to an already-running process id")
    src.add_argument("--launch", metavar="EXE", help="launch this exe, then snapshot")
    src.add_argument("--title-re", metavar="REGEX", help="find a window by title regex")
    parser.add_argument("--max-controls", type=int, default=600)
    parser.add_argument("--repeat", type=int, default=3, help="timing samples to take")
    args = parser.parse_args(argv)

    dpi = set_dpi_awareness()
    print(f"[dpi] awareness = {dpi}")

    hwnd: Optional[int] = None
    pid: Optional[int] = None

    if args.launch:
        pid = _launch_and_wait(args.launch)
        print(f"[launch] pid = {pid}")
    elif args.pid:
        pid = args.pid
    elif args.title_re:
        hwnd = find_hwnd_by_title(args.title_re)
        if hwnd is None:
            print(f"[error] no window matched /{args.title_re}/")
            return 2
        pid = _pid_of_hwnd(hwnd)
        print(f"[find] hwnd = {hwnd}  pid = {pid}")

    scraper = WindowsUIAScraper()
    if pid is not None:
        scraper.attach(pid)

    # Timing: take a few samples so the number is honest, not a cold-cache fluke.
    timings: list[float] = []
    state: Optional[State] = None
    for i in range(max(1, args.repeat)):
        t0 = time.perf_counter()
        state = scraper.snapshot(
            f"state_{i:04d}", hwnd=hwnd, max_controls=args.max_controls
        )
        timings.append((time.perf_counter() - t0) * 1000.0)

    assert state is not None
    doc = state.to_dict()
    print(json.dumps(doc, indent=2, ensure_ascii=False))

    print("\n--- summary ---")
    print(f"window_title : {state.window_title!r}")
    print(f"controls     : {len(state.controls)}")
    print(f"unexplored   : {len(state.unexplored())}")
    print(f"needs_vision : {state.needs_vision}")
    print(
        "snapshot ms  : "
        + ", ".join(f"{t:.1f}" for t in timings)
        + f"   (best {min(timings):.1f} ms)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
