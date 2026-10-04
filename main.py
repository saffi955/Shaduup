"""
AppShadow recorder window — Phase 4.

A tiny always-on-top tkinter window (stdlib only, no UI deps) that drives the
Phase 3 ``ShadowRecorder``. It never covers the target app, stays out of your
way, and gives just enough feedback to know the shadow is keeping up:

  ⌖ Pick app   crosshair-drag onto any window (Spy++ style) → resolves hwnd → PID
  ● Start      attach the WinEvent hooks to that PID; REC dot starts blinking
  ⌖ Capture    force a snapshot right now (the fallback when auto-detect misses)
  ■ Stop       flush the session → write app_map.json, print the coverage summary

Feedback on every capture: the status strip flashes green, a small toast slides
in ("state_0023 captured"), and a soft tick plays (winsound). Global hotkeys
work while the *target* app has focus: Ctrl+Shift+C capture · Ctrl+Shift+X stop.

DPI awareness is set at import, before Tk() — so the crosshair's coordinates and
the captured rects agree with what is actually on screen.
"""

from __future__ import annotations

import ctypes
import os
import queue
import time
import tkinter as tk
import winsound
from ctypes import wintypes

from config import HOTKEY_CAPTURE, HOTKEY_STOP, OUTPUT_DIR, set_dpi_awareness

set_dpi_awareness()  # MUST run before the first window is created

from core.exporter import coverage_summary  # noqa: E402
from recorder import ShadowRecorder  # noqa: E402
from scrapers.windows_uia import content_pid, process_name  # noqa: E402

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
_user32.WindowFromPoint.argtypes = [wintypes.POINT]
_user32.WindowFromPoint.restype = wintypes.HWND

# blueprint palette
BG = "#141F35"
BAR = "#1B2A45"
DARK = "#0C1424"
BLUE = "#7FB4FF"
GREEN = "#6EE7A0"
AMBER = "#FFC65C"
RED = "#FF5252"
MUTED = "#5A6E93"
WHITE = "#FFFFFF"

_GA_ROOT = 2


def _app_exe(hwnd: int, pid: int) -> str:
    """Executable name of the app behind a window.

    For a UWP app this is the hosted app (CalculatorApp.exe), not the
    ApplicationFrameHost.exe that owns its frame.
    """
    return process_name(content_pid(hwnd) if hwnd else pid)


def _window_under_cursor() -> tuple[int, int]:
    """(top-level hwnd, pid) of the window currently under the mouse pointer."""
    pt = wintypes.POINT()
    _user32.GetCursorPos(ctypes.byref(pt))
    hwnd = _user32.WindowFromPoint(pt)
    if not hwnd:
        return 0, 0
    root = _user32.GetAncestor(hwnd, _GA_ROOT) or hwnd
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(root, ctypes.byref(pid))
    return int(root), int(pid.value)


class RecorderApp:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("AppShadow")
        self.root.geometry("340x210")
        self.root.overrideredirect(True)  # draw our own chrome (blueprint look)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=BG)
        self._place_top_right()

        self.own_pid = os.getpid()
        self.target_hwnd = 0
        self.target_pid = 0
        self.target_name = ""

        self.rec: ShadowRecorder | None = None
        self.recording = False
        self.out_dir = ""
        self.ui_queue: "queue.Queue[tuple]" = queue.Queue()
        self.counts = {"states": 0, "menus": 0, "dialogs": 0}
        self._dot_on = True
        self._busy = False  # solid-red dot while a snapshot is being taken
        self._hotkeys: list = []

        self._build_ui()
        self.root.after(80, self._poll)

    # -- layout ---------------------------------------------------------------
    def _place_top_right(self) -> None:
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        self.root.geometry(f"340x210+{sw - 360}+{40}")

    def _build_ui(self) -> None:
        # title bar (draggable) ------------------------------------------------
        bar = tk.Frame(self.root, bg=BAR, height=30)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        self.dot = tk.Canvas(bar, width=14, height=14, bg=BAR, highlightthickness=0)
        self.dot.pack(side="left", padx=(10, 6), pady=8)
        self._dot_id = self.dot.create_oval(2, 2, 12, 12, fill=MUTED, outline="")
        self.title_lbl = tk.Label(bar, text="AppShadow — idle", bg=BAR, fg=WHITE,
                                  font=("Segoe UI", 9, "bold"))
        self.title_lbl.pack(side="left")
        close = tk.Label(bar, text="✕", bg=BAR, fg=MUTED, font=("Segoe UI", 10),
                         cursor="hand2")
        close.pack(side="right", padx=10)
        close.bind("<Button-1>", lambda _e: self._on_close())
        for w in (bar, self.title_lbl):
            w.bind("<ButtonPress-1>", self._start_move)
            w.bind("<B1-Motion>", self._on_move)

        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True, padx=12, pady=8)

        # target row -----------------------------------------------------------
        tk.Label(body, text="TARGET PROCESS", bg=BG, fg=MUTED,
                 font=("Segoe UI", 7)).pack(anchor="w")
        row = tk.Frame(body, bg=BG)
        row.pack(fill="x", pady=(2, 8))
        self.target_lbl = tk.Label(row, text="— no target —", bg=DARK, fg=GREEN,
                                   font=("Consolas", 9), anchor="w", padx=8)
        self.target_lbl.pack(side="left", fill="x", expand=True, ipady=4)
        self.pick_btn = self._chip(row, "⌖ Pick app", BLUE, side="right")
        # crosshair drag: implicit grab keeps motion/release ours over any window
        self.pick_btn.bind("<ButtonPress-1>", self._pick_press)
        self.pick_btn.bind("<B1-Motion>", self._pick_motion)
        self.pick_btn.bind("<ButtonRelease-1>", self._pick_release)

        # buttons --------------------------------------------------------------
        btns = tk.Frame(body, bg=BG)
        btns.pack(fill="x", pady=(0, 8))
        self.start_btn = self._chip(btns, "● Start", GREEN, side="left",
                                    command=self._start)
        self.capture_btn = self._chip(btns, "⌖ Capture", AMBER, side="left",
                                      command=self._manual_capture, state="off")
        self.stop_btn = self._chip(btns, "■ Stop", RED, side="left",
                                   command=self._stop, state="off")

        # status strip ---------------------------------------------------------
        self.status = tk.Label(body, text="Captured: 0 states · 0 menus · 0 dialogs",
                               bg=DARK, fg=WHITE, font=("Consolas", 9), anchor="w",
                               padx=8)
        self.status.pack(fill="x", ipady=5)

        tk.Label(body, text="LAST EVENT", bg=BG, fg=MUTED,
                 font=("Segoe UI", 7)).pack(anchor="w", pady=(8, 0))
        self.last_evt = tk.Label(body, text="—", bg=BG, fg=BLUE,
                                 font=("Consolas", 9), anchor="w")
        self.last_evt.pack(fill="x")
        tk.Label(body, text=f"hotkeys: {HOTKEY_CAPTURE} capture · {HOTKEY_STOP} stop",
                 bg=BG, fg=MUTED, font=("Segoe UI", 7)).pack(anchor="w", pady=(4, 0))

    def _chip(self, parent, text, color, side, command=None, state="on"):
        """A themed, clickable label acting as a button (reliable colors on Win)."""
        lbl = tk.Label(parent, text=text, bg=BAR, fg=color, font=("Segoe UI", 9, "bold"),
                       padx=10, pady=6, cursor="hand2")
        lbl.pack(side=side, padx=3)
        lbl._color = color
        lbl._command = command
        lbl._enabled = state == "on"
        if command is not None:
            lbl.bind("<Button-1>", lambda _e, w=lbl: w._enabled and w._command())
        self._set_chip_enabled(lbl, lbl._enabled)
        return lbl

    @staticmethod
    def _set_chip_enabled(lbl, enabled: bool) -> None:
        lbl._enabled = enabled
        lbl.configure(fg=lbl._color if enabled else MUTED,
                      cursor="hand2" if enabled else "arrow")

    # -- window drag ----------------------------------------------------------
    def _start_move(self, e) -> None:
        self._mx, self._my = e.x, e.y

    def _on_move(self, e) -> None:
        self.root.geometry(f"+{e.x_root - self._mx}+{e.y_root - self._my}")

    # -- pick app (crosshair) -------------------------------------------------
    def _pick_press(self, _e) -> None:
        if self.recording:
            return
        self.root.configure(cursor="crosshair")
        self.title_lbl.configure(text="AppShadow — drop on target…")

    def _pick_motion(self, _e) -> None:
        hwnd, pid = self._live_target()
        if pid:
            self.target_lbl.configure(text=f"{_app_exe(hwnd, pid)}  (pid {pid})")

    def _pick_release(self, _e) -> None:
        self.root.configure(cursor="arrow")
        self.title_lbl.configure(text="AppShadow — idle")
        hwnd, pid = self._live_target()
        if pid:
            self.target_hwnd, self.target_pid = hwnd, pid
            self.target_name = _app_exe(hwnd, pid) or f"pid_{pid}"
            self.target_lbl.configure(text=f"{self.target_name}  (pid {pid})")
        else:
            self.target_lbl.configure(
                text=f"{self.target_name}  (pid {self.target_pid})"
                if self.target_pid else "— no target —")

    def _live_target(self) -> tuple[int, int]:
        """Window under the cursor, ignoring our own recorder window."""
        hwnd, pid = _window_under_cursor()
        if pid == self.own_pid:
            return 0, 0
        return hwnd, pid

    # -- recording lifecycle --------------------------------------------------
    def _start(self) -> None:
        if self.recording or not self.target_pid:
            if not self.target_pid:
                self._flash_status("pick a target app first", RED)
            return
        stamp = time.strftime("%Y-%m-%d_%H%M%S")
        self.out_dir = os.path.join(OUTPUT_DIR, f"session_{stamp}")
        os.makedirs(self.out_dir, exist_ok=True)
        self.counts = {"states": 0, "menus": 0, "dialogs": 0}
        self.rec = ShadowRecorder(self.target_pid, self.target_name, self.out_dir,
                                  hwnd=self.target_hwnd,
                                  on_capture=self._on_capture)
        self.rec.start()
        self.recording = True
        self._register_hotkeys()

        self.title_lbl.configure(text="AppShadow — RECORDING")
        self._set_chip_enabled(self.start_btn, False)
        self._set_chip_enabled(self.pick_btn, False)
        self._set_chip_enabled(self.capture_btn, True)
        self._set_chip_enabled(self.stop_btn, True)
        self._blink()

    def _manual_capture(self) -> None:
        if self.recording and self.rec is not None:
            self.rec.manual_capture()

    def _stop(self) -> None:
        if not self.recording or self.rec is None:
            return
        self._unregister_hotkeys()
        written = self.rec.stop()  # flushes app_map.json + graph.mmd
        self.recording = False

        app_map = written["app_map"]
        cov = self.rec.graph.coverage()
        unexplored = cov.get("unexplored", 0)

        self.title_lbl.configure(text="AppShadow — idle")
        self.dot.itemconfigure(self._dot_id, fill=MUTED)
        self._set_chip_enabled(self.start_btn, True)
        self._set_chip_enabled(self.pick_btn, True)
        self._set_chip_enabled(self.capture_btn, False)
        self._set_chip_enabled(self.stop_btn, False)
        self.status.configure(
            text=f"Saved {cov['states']} states · {unexplored} unexplored → app_map.json",
            fg=GREEN)
        self.last_evt.configure(text=os.path.abspath(app_map))
        for label, path in written.items():
            print(f"[stop] wrote {label}: {path}")
        print(coverage_summary(self.rec.graph.session))

    # -- capture feedback (worker thread → UI via queue) ----------------------
    def _on_capture(self, state, is_new: bool) -> None:
        # Runs on the recorder's capture worker thread — do NO tkinter here.
        self.ui_queue.put(("capture", state.state_id, state.kind,
                           state.window_title or "", bool(is_new)))

    def _poll(self) -> None:
        try:
            while True:
                item = self.ui_queue.get_nowait()
                self._handle(item)
        except queue.Empty:
            pass
        self.root.after(80, self._poll)

    def _handle(self, item: tuple) -> None:
        kind = item[0]
        if kind == "hotkey_capture":
            self._manual_capture()
        elif kind == "hotkey_stop":
            self._stop()
        elif kind == "capture":
            _c, state_id, state_kind, title, is_new = item
            if is_new:
                self.counts["states"] += 1
                if state_kind == "menu_open":
                    self.counts["menus"] += 1
                elif state_kind == "dialog":
                    self.counts["dialogs"] += 1
            self.status.configure(
                text=(f"Captured: {self.counts['states']} states · "
                      f"{self.counts['menus']} menus · {self.counts['dialogs']} dialogs"))
            self.last_evt.configure(
                text=f"{state_kind} → {title[:28]!r} ({state_id})")
            self._flash_status(None, GREEN)
            self._toast(f"{state_id} captured")
            winsound.MessageBeep(winsound.MB_OK)  # soft tick, async

    # -- small UI effects -----------------------------------------------------
    def _blink(self) -> None:
        if not self.recording:
            return
        if self._busy:
            self.dot.itemconfigure(self._dot_id, fill=RED)
        else:
            self._dot_on = not self._dot_on
            self.dot.itemconfigure(self._dot_id, fill=RED if self._dot_on else "#5A1A1A")
        self.root.after(550, self._blink)

    def _flash_status(self, text, color) -> None:
        if text is not None:
            self.status.configure(text=text)
        self.status.configure(fg=color)
        self.root.after(300, lambda: self.status.configure(fg=WHITE))

    def _toast(self, text: str) -> None:
        t = tk.Toplevel(self.root)
        t.overrideredirect(True)
        t.attributes("-topmost", True)
        tk.Label(t, text=f"  {text}  ", bg=BAR, fg=GREEN,
                 font=("Segoe UI", 9, "bold"), padx=6, pady=8).pack()
        t.update_idletasks()
        sw, sh = t.winfo_screenwidth(), t.winfo_screenheight()
        w, h = t.winfo_width(), t.winfo_height()
        t.geometry(f"+{sw - w - 24}+{sh - h - 60}")
        t.after(1400, t.destroy)

    # -- global hotkeys -------------------------------------------------------
    def _register_hotkeys(self) -> None:
        import keyboard

        self._hotkeys = [
            keyboard.add_hotkey(HOTKEY_CAPTURE,
                                lambda: self.ui_queue.put(("hotkey_capture",))),
            keyboard.add_hotkey(HOTKEY_STOP,
                                lambda: self.ui_queue.put(("hotkey_stop",))),
        ]

    def _unregister_hotkeys(self) -> None:
        import keyboard

        for hk in self._hotkeys:
            try:
                keyboard.remove_hotkey(hk)
            except (KeyError, ValueError):
                pass
        self._hotkeys = []

    # -- shutdown -------------------------------------------------------------
    def _on_close(self) -> None:
        if self.recording:
            self._stop()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    RecorderApp().run()


if __name__ == "__main__":
    main()
