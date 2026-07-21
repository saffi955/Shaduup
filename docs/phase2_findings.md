# Phase 2 — Scraper de-risking results

**Date:** 2026-07-18 · **Target:** InPage 2012 (`C:\Program Files (x86)\InPage 2012\InPage 2012.exe`)
**Environment:** Windows 10 Pro · Python 3.14.0 · pywinauto 0.6.9 · comtypes 1.4.16 · DPI awareness = `per-monitor-v1`

This is the honest inspection the plan (§7, step 7) asks for *before* the machinery
gets built around the scraper. Verdict: **go**, with the owner-drawn gap handled
exactly the way the blueprint anticipated.

## What was measured

The snapshot function (`scrapers/windows_uia.py`) was run against three windows.
`raw Length` is the number of UIA elements the single `FindAllBuildCache` call
returned; `controls` is what survived filtering into the map.

| Target | raw elems | controls | snapshot (best) | needs_vision | notes |
|---|---|---|---|---|---|
| Notepad (Phase 0 smoke) | ~30 | 24 | **30 ms** | false | clean tree; Edit body correctly redacted |
| InPage main window | 36 | 32 | **530 ms** | **true** | menus + chrome only; 16 nameless Panes |
| InPage **File menu** (scoped to popup hwnd) | 5 | 5 | **26 ms** | false | full labels + shortcuts, automation ids |

## Findings

### 1. Menus — GREEN light
InPage's menus are standard Win32 popups (window class `#32768`). UIA sees them
cleanly. The captured File menu:

```
MenuItem "New..."            shortcut Ctrl+N       automation_id 19013
MenuItem "Open..."           shortcut Ctrl+O       automation_id 20000
MenuItem "Printer Setup..."  shortcut Ctrl+Alt+P   automation_id 20006
MenuItem "Exit"              shortcut Alt+F4       automation_id 20007
MenuItem "1 C:\...\PANAFLEX DATA.INP"              automation_id 20117
```

Menu names, shortcuts, enabled state, rects and automation ids all come through —
this is exactly the data the JSON contract wants.

### 2. Toolbars + canvas — YELLOW light (risk R1, as predicted)
The InPage main window exposes **only** its menu bar (File / Preferences / Help)
and window chrome to UIA. Everything else — the formatting toolbar, the drawing
tools, the rulers, the document canvas — surfaces as **16 nameless `Pane`s**.
This is a plain-compiled, owner-drawn app: those regions are painted directly and
have no accessible structure.

Mitigation is already in the design: the scraper now flags such a window
`needs_vision: true`, so its screenshot gets parsed by a vision model later
(backup plan §9). No structured data is lost that was ever available.

### 3. Snapshot timing — R2 confirmed and mitigated
The InPage main-window walk takes ~530 ms for only **36 elements** — ~15 ms per
element. The cost is InPage's *own* UIA provider being slow to answer
cross-process calls, **not** tree size (the raw view of the same 36 elements is
just as slow). Scoping the walk to the menu popup's hwnd instead of the whole app
drops it to **26 ms** — 20× faster, comfortably inside the 150 ms menu budget.

**Conclusion for Phase 3:** the WinEventHook must hand the popup hwnd straight to
`snapshot(hwnd=...)` on a `MENUPOPUPSTART` event. Whole-app menu reads would miss
the window. Main-window / foreground snapshots tolerate the ~500 ms (they fire on
settle, not on a closing menu), but keep `max_controls` bounded.

## Schema adjustment made during Phase 2
Classic Win32 menus leave the UIA `AcceleratorKey` property empty and pack the
shortcut into the label after a tab (`"New...\tCtrl+N"`). Added
`_split_accelerator()` so `name` stays clean and `shortcut` is populated the way
an app menu should be. Covered by doctests; verified on the live InPage strings.

## Privacy check
The Notepad Edit body was recorded as `{"type":"Edit","value_redacted":true}` with
no text — the redaction rule holds. (Recent-file *menu labels* do contain a path,
but that is app UI structure, not typed input, so it is captured as-is by design.)
