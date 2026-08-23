"""
ShadowRecorder — Phase 3 end-to-end orchestration.

Ties the four pieces together into "shadow mode": the WinEvent listener notices
the UI changed, the debouncer waits 300 ms for it to settle, a single capture
worker snapshots the right window (scoped to a menu popup when that's what
changed, so it stays fast) and screenshots it, and the session graph folds the
result in — a new node, or just a new edge to a node already on the map.

Threading model (deliberate, because COM is picky):
  * listener thread  — pumps window messages, does no COM, only pokes the debouncer
  * debounce timer   — fires once after the burst settles, enqueues a capture
  * capture worker   — the ONLY thread that touches UIA/COM; initialised MTA once

The recorder window (Phase 4) will drive this exact class; nothing here is
tkinter-aware, so it can also run headless from the CLI for the Phase 3 test.
"""

from __future__ import annotations

import ctypes
import queue
import threading
import time
from typing import Callable, Optional

from config import AUTOSAVE_SECONDS, DEBOUNCE_MS
from core.exporter import coverage_summary, write_app_map, write_session
from core.schema import State
from core.session import Debouncer, SessionGraph
from capture.screenshot import ScreenshotGrabber
from scrapers.windows_uia import (
    EVENT_OBJECT_SHOW,
    EVENT_SYSTEM_DIALOGSTART,
    EVENT_SYSTEM_FOREGROUND,
    EVENT_SYSTEM_MENUPOPUPSTART,
    EVENT_SYSTEM_MENUSTART,
    WindowsUIAScraper,
    WinEventListener,
    _pid_of_hwnd,
    main_hwnd_for_pid,
)

_user32 = ctypes.windll.user32

# event id -> (state kind, edge action)
# NOTE: MENUSTART fires when the app enters *menu mode* — its hwnd is the main
# window (menu bar), not a popup. So it captures a "window" state; only
# MENUPOPUPSTART, whose hwnd is the actual #32768 popup, is a "menu_open".
_EVENT_MAP: dict[int, tuple[str, str]] = {
    EVENT_SYSTEM_FOREGROUND: ("window", "foreground"),
    EVENT_SYSTEM_MENUSTART: ("window", "enter_menu"),
    EVENT_SYSTEM_MENUPOPUPSTART: ("menu_open", "menu_open"),
    EVENT_SYSTEM_DIALOGSTART: ("dialog", "dialog_open"),
    EVENT_OBJECT_SHOW: ("dialog", "show"),
}

# a capture request queued for the worker thread
_CaptureCtx = tuple  # (hwnd:int, kind:str, action:str, manual:bool)
_STOP = None  # sentinel put on the queue to end the worker

# Win32 window classes that tell us what a window is without an event to say so.
_CLASS_KINDS = {"#32768": "menu_open", "#32770": "dialog"}


def _window_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(64)
    _user32.GetClassNameW(hwnd, buf, 64)
    return buf.value


def _classify_window(hwnd: int) -> str:
    """What kind of state is this window? Used when no event told us."""
    return _CLASS_KINDS.get(_window_class(hwnd), "window")


class ShadowRecorder:
    def __init__(
        self,
        pid: int,
        app_name: str,
        out_dir: str,
        *,
        on_capture: Optional[Callable[[State, bool], None]] = None,
    ) -> None:
        self.pid = pid
        self.out_dir = out_dir
        self.scraper = WindowsUIAScraper()
        self.scraper.attach(pid)
        self.graph = SessionGraph(app=app_name, pid=pid)
        self.grabber = ScreenshotGrabber(out_dir)
        self.debouncer = Debouncer(DEBOUNCE_MS, self._on_settle)
        self.listener = WinEventListener(pid, self._on_event)
        self.on_capture = on_capture

        self._pending: Optional[_CaptureCtx] = None
        self._pending_lock = threading.Lock()
        self._capture_q: "queue.Queue[Optional[_CaptureCtx]]" = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._running = False
        self._autosave: Optional[threading.Timer] = None

    # -- lifecycle ------------------------------------------------------------
    def start(self) -> None:
        self._running = True
        self._worker = threading.Thread(
            target=self._worker_loop, name="appshadow-capture", daemon=True
        )
        self._worker.start()
        # Seed the map with the app's current main window before hooking events,
        # so the first menu/dialog has a "from" state to attach its edge to.
        main = main_hwnd_for_pid(self.pid)
        if main:
            self._capture_q.put((main, "window", "start", False))
        self.listener.start()
        self._schedule_autosave()

    def stop(self) -> dict[str, str]:
        """End the session, write every artefact, and return the paths written."""
        self._running = False
        if self._autosave is not None:
            self._autosave.cancel()
            self._autosave = None
        self.listener.stop()
        self.debouncer.cancel()
        self._capture_q.put(_STOP)
        if self._worker is not None:
            self._worker.join(3.0)
            self._worker = None
        self.grabber.close()
        return write_session(self.graph.session, self.out_dir)

    # -- autosave -------------------------------------------------------------
    def _schedule_autosave(self) -> None:
        if not self._running:
            return
        self._autosave = threading.Timer(AUTOSAVE_SECONDS, self._do_autosave)
        self._autosave.daemon = True
        self._autosave.start()

    def _do_autosave(self) -> None:
        """Flush the map to disk periodically so a crash never loses a session."""
        try:
            write_app_map(self.graph.session, self.out_dir)
        except OSError as exc:
            print(f"[autosave-error] {exc}")
        self._schedule_autosave()

    def manual_capture(self) -> None:
        """Force a capture of the target's foreground window (the fallback hotkey).

        The foreground window is only used when it actually belongs to the target
        process. The hotkey is global, so it fires wherever your focus happens to
        be, and capturing whatever unrelated app was in front would put a stranger's
        window (and its screenshot) into the map. When focus is elsewhere, fall
        back to the target's own main window.
        """
        hwnd = _user32.GetForegroundWindow()
        if not hwnd or _pid_of_hwnd(int(hwnd)) != self.pid:
            hwnd = main_hwnd_for_pid(self.pid)
        if hwnd:
            self._capture_q.put((int(hwnd), "manual", "manual_capture", True))

    # -- event path (listener thread) -----------------------------------------
    def _on_event(self, event: int, hwnd: int, id_object: int) -> None:
        mapping = _EVENT_MAP.get(event)
        if mapping is None:
            return
        kind, action = mapping
        # EVENT_OBJECT_SHOW is very chatty; only care about whole-window shows.
        if event == EVENT_OBJECT_SHOW and id_object != 0:
            return
        target = hwnd
        if kind == "window" and not target:
            target = main_hwnd_for_pid(self.pid) or 0
        if not target:
            return
        with self._pending_lock:
            self._pending = (target, kind, action, False)
        self.debouncer.trigger()

    def _on_settle(self) -> None:
        # Runs on the debounce timer thread: no COM here, just hand the latest
        # pending context to the capture worker.
        with self._pending_lock:
            ctx = self._pending
        if ctx is not None:
            self._capture_q.put(ctx)

    # -- capture path (single COM worker thread) ------------------------------
    def _worker_loop(self) -> None:
        import comtypes

        # MTA: UIA elements are agile, and an MTA thread needs no message pump.
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except OSError:
            pass
        try:
            while True:
                ctx = self._capture_q.get()
                if ctx is _STOP:
                    break
                try:
                    self._do_capture(ctx)
                except Exception as exc:  # never let one bad capture kill the loop
                    print(f"[capture-error] {exc}")
        finally:
            try:
                comtypes.CoUninitialize()
            except Exception:
                pass

    def _do_capture(self, ctx: _CaptureCtx) -> None:
        hwnd, kind, action, manual = ctx
        if not _user32.IsWindow(hwnd):
            return  # the window (often a menu) closed before we got to it
        # A manual capture has no event to say what it is, so classify it from
        # the window itself. Using a literal "manual" kind here would be worse
        # than useless: it tells a reading agent nothing about what the screen
        # IS, and because the kind is part of the tree hash it would stop a
        # manual capture from ever deduping against the same screen captured
        # automatically, filling the map with duplicate nodes. The `manual`
        # flag below is what records how the capture was triggered.
        snap_kind = _classify_window(hwnd) if manual else kind
        # Empty id -> the graph assigns a real one (or dedups to an existing node).
        state = self._snapshot_retry(hwnd, snap_kind)
        if state is None:
            return
        if manual:
            state.manual = True

        canonical, is_new = self.graph.record(state, action=action)

        # Only spend a PNG on genuinely new screens (revisits reuse the node's
        # existing shot). A menu that already closed yields no image — fine.
        if is_new and canonical.screenshot is None:
            rel = self.grabber.grab(hwnd, canonical.state_id)
            if rel:
                canonical.screenshot = rel

        tag = "NEW " if is_new else "link"
        print(
            f"  [{tag}] {canonical.state_id} {canonical.kind:10} "
            f"{canonical.window_title[:40]!r}  "
            f"({len(canonical.controls)} controls"
            + (", needs_vision" if canonical.needs_vision else "")
            + ")"
        )
        if self.on_capture is not None:
            self.on_capture(canonical, is_new)

    def _snapshot_retry(self, hwnd: int, kind: str) -> Optional[State]:
        """Snapshot, tolerating one transient UIA hiccup.

        Owner-drawn apps like InPage occasionally answer a UIA call with
        0x80040201 ("unable to invoke subscribers") while their UI is mid-
        transition (a menu closing as the next one opens). One short retry
        clears it; a second failure is swallowed so the recorder keeps running.
        """
        from comtypes import COMError

        for attempt in range(2):
            try:
                return self.scraper.snapshot("", kind=kind, hwnd=hwnd)
            except COMError:
                if attempt == 0 and _user32.IsWindow(hwnd):
                    time.sleep(0.05)
                    continue
                return None
        return None


# --- CLI: the Phase 3 end-to-end test loop ----------------------------------
def _main(argv: Optional[list[str]] = None) -> int:
    import argparse
    import os

    from config import OUTPUT_DIR, set_dpi_awareness
    from scrapers.windows_uia import (
        _launch_and_wait,
        _pid_of_hwnd,
        find_hwnd_by_title,
    )

    parser = argparse.ArgumentParser(description="AppShadow shadow-mode recorder (Phase 3)")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--pid", type=int)
    src.add_argument("--launch", metavar="EXE")
    src.add_argument("--title-re", metavar="REGEX")
    parser.add_argument("--seconds", type=int, default=25, help="how long to record")
    parser.add_argument("--app", default="target", help="app name for the session meta")
    args = parser.parse_args(argv)

    set_dpi_awareness()

    if args.launch:
        pid = _launch_and_wait(args.launch, settle_s=4.0)
    elif args.pid:
        pid = args.pid
    else:
        hwnd = find_hwnd_by_title(args.title_re)
        if hwnd is None:
            print(f"[error] no window matched /{args.title_re}/")
            return 2
        pid = _pid_of_hwnd(hwnd)

    stamp = time.strftime("%Y-%m-%d_%H%M")
    out_dir = os.path.join(OUTPUT_DIR, f"session_{stamp}")
    os.makedirs(out_dir, exist_ok=True)

    rec = ShadowRecorder(pid, args.app, out_dir)
    print(f"[shadow] recording pid {pid} for {args.seconds}s -> {out_dir}")
    print("[shadow] drive the app now: open menus, dialogs, switch windows...\n")
    rec.start()
    try:
        time.sleep(args.seconds)
    except KeyboardInterrupt:
        pass
    written = rec.stop()

    print("\n--- session summary ---")
    print(coverage_summary(rec.graph.session))
    for label, path in written.items():
        print(f"wrote {label}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
