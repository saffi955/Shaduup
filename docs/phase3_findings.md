# Phase 3 — Shadow mode end-to-end

**Date:** 2026-07-18 · **Target:** InPage 2012 · **Result: works end-to-end.**

Phase 3 turns the Phase 2 snapshot function into a live recorder: a WinEvent hook
notices the UI changed, a debounce collapses the burst, a single COM worker
snapshots the right window and screenshots it, and the session graph folds it in
as a new node or just a new edge.

## What was built

| Piece | File | Role |
|---|---|---|
| WinEvent listener | `scrapers/windows_uia.py` → `WinEventListener` | `SetWinEventHook` on a background thread with a message loop, filtered to the target PID; posts events to the recorder |
| Debounce | `core/session.py` → `Debouncer` | 300 ms settle timer: one click = one state, not twenty |
| Diff engine | `core/session.py` → `tree_hash` + `SessionGraph` | position-independent fingerprint; append-or-link; edge + `leads_to` wiring; coverage |
| Screenshots | `capture/screenshot.py` → `ScreenshotGrabber` | mss grab of the window region only, `state_NNNN.png` |
| Orchestrator | `recorder.py` → `ShadowRecorder` | wires all four; headless; Phase 4's UI will drive this same class |

## Threading model (deliberate)
- **listener thread** — pumps window messages, does *no* COM, only kicks the debouncer
- **debounce timer** — fires once after the burst settles, enqueues a capture
- **capture worker** — the *only* thread that touches UIA/COM (initialised MTA once)

This matters: UIA is thread-sensitive, and confining every UIA call to one MTA
worker is what makes the recorder stable. Reusing the main-thread `IUIAutomation`
from the MTA worker works because UIA elements are agile (free-threaded).

## End-to-end test (step 12) — InPage, driven through its menus

```
state_0001 window     'Urdu InPage 2012'   (root)
state_0002 menu_open  'File'          <= menu_open / MenuItem:File from state_0001
   ...reopen File     -> [link] state_0002        # DEDUP: no duplicate node
state_0003 menu_open  'Preferences'   <= from state_0002
state_0004 menu_open  'Help'          <= from state_0003
state_0005 menu_open  'System'        <= from state_0004
coverage: 5 states, 29 controls, 24 unexplored + 5 screenshots
```

Confirmed:
- **Auto-capture** fires on menu-open / window / dialog events, filtered to the PID.
- **Edges** are wired: the File menu links back to `MenuItem:File` on the main
  window (matched by title), giving Claude the navigation *flow*, not just layout.
- **No duplicates**: reopening the File menu links to the existing `state_0002`
  instead of appending — the tree-hash diff engine works on the real app.
- **Coverage** is a fact, not a guess: the report lists the exact unexplored items
  per state (File's New/Open/…, Preferences' Application/Document/…, etc.).
- **Screenshots** land per state (menu popups are cropped to the popup region).

## Robustness fixes made during the test
1. **`MENUSTART` vs `MENUPOPUPSTART`.** `MENUSTART` (entering menu mode) carries
   the *main window* hwnd, not a popup — so it captures a `window` state;
   only `MENUPOPUPSTART` is a `menu_open`.
2. **Transient UIA error `0x80040201`.** InPage occasionally fails a UIA call
   while its menus are mid-transition. `_snapshot_retry` retries once, guarded by
   `IsWindow`; a second failure is swallowed so one bad frame never stops the run.

## Known limitations (fine for v0.1, noted for later)
- Edge attribution uses "previous captured state + title match". For a user
  clicking a menu from the menu bar it's exact; for keyboard menu-bar walking
  (Right/Right) the `on` can be empty because the destination title doesn't match
  a control in the previous *menu*. A precise INVOKED hook would tighten this.
- Fast navigation (< 300 ms between two different menus) can let the debounce
  skip the intermediate one — the manual-capture hotkey (Phase 4) is the fallback.
