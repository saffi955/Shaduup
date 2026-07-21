# Phase 4 — Recorder window + hotkeys

**Date:** 2026-07-18 · **Result: complete and verified.**

`main.py` is the tiny always-on-top tkinter window (stdlib only, no UI deps) that
drives the Phase 3 `ShadowRecorder`. It implements the blueprint's Panel A.

## What was built (plan steps 13–14)

- **340×210, always-on-top, borderless, draggable** — custom dark chrome per the
  blueprint palette; positioned top-right so it never covers the target app.
- **⌖ Pick app** — Spy++-style crosshair drag. Pressing the button starts an
  implicit Tk grab, so `<B1-Motion>` keeps firing while the pointer is over *other*
  windows; each move resolves the window under the cursor (`WindowFromPoint` →
  `GetAncestor(GA_ROOT)` → PID → exe name), and release locks the target. Our own
  recorder window is filtered out.
- **● Start / ⌖ Capture / ■ Stop** — Start attaches the hooks and flips button
  state; Capture forces `manual_capture()`; Stop flushes the session, writes
  `app_map.json`, and prints the coverage summary.
- **REC dot** — blinks red while listening, solid red while a snapshot is taken.
- **Status strip** — live `Captured: N states · M menus · K dialogs`, flashes
  green 300 ms on each capture.
- **LAST EVENT** line — `kind → 'title' (state_id)` on every capture.
- **Toast + tick** — a borderless toast slides in bottom-right ("state_0023
  captured") and `winsound.MessageBeep` plays a soft tick.
- **Global hotkeys** — `Ctrl+Shift+C` capture · `Ctrl+Shift+X` stop, via the
  `keyboard` lib, registered on Start and removed on Stop.

## Threading / Tk safety
`on_capture` fires on the recorder's capture worker thread, which must not touch
Tk. It pushes onto a `queue.Queue`; an 80 ms `root.after` poller drains it on the
main thread and does all UI work. Hotkey callbacks likewise enqueue actions rather
than mutating widgets directly.

## Verification
- Window renders at 340×210 with the correct idle and RECORDING states
  (screenshots captured).
- End-to-end: targeted a running Notepad, Start → 2 hotkeys registered + seed
  capture landed, `manual_capture()` worked, Stop wrote `app_map.json`
  (`app=notepad.exe`, 1 state, coverage computed) and reset the UI.

## Known limitation (risk R6)
Global hotkeys use the `keyboard` lib, which can require elevation to see
keystrokes destined for an elevated target app. The documented fallback is
`RegisterHotKey` via ctypes; not needed for the current target.
