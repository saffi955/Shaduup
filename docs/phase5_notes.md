# Phase 5 — Exporter, autosave, and coverage reporting

**Date:** 2026-07-21 · **Result: complete and verified on a live session.**

Phase 5 gives a finished session somewhere to go. Until now the recorder wrote a
raw JSON dump inline; the map, the diagram, and the honest "is this finished?"
report now all come from one place.

## What was built (plan step 15)

`core/exporter.py`, platform-agnostic like the rest of `core/`:

| Function | Produces |
|---|---|
| `write_app_map()` | `app_map.json`, the structured map you hand to an agent |
| `write_mermaid()` | `graph.mmd`, a Mermaid state diagram of the same graph |
| `coverage_summary()` | the plain-language report printed on Stop |
| `write_session()` | both artefacts at once, returning the paths written |
| `write_shards()` | one file per top-level menu, for very large apps (risk R5) |

`ShadowRecorder.stop()` now returns the dict of paths it wrote, so the recorder
window and the CLI report real paths instead of reconstructing them by hand.

### Mermaid diagram
Node shape encodes the state kind: rectangle for a window, stadium for a menu,
hexagon for a dialog, flag for a manual capture. States flagged `needs_vision`
get a dashed border, so the owner-drawn gaps are visible in the picture and not
only in the JSON.

Two details that took a second pass:

**Labels come from the breadcrumb, not the window title.** A menu popup inherits
its parent window's title, so labelling by title rendered three different menus
as three identical "Untitled - InPage 2012" nodes. The breadcrumb tail
(`File`, `Export`, `Export as PDF`) is what actually distinguishes them.

**Labels are sanitised, not escaped.** Mermaid has no escape syntax inside a
quoted label, and `[`, `]`, `{`, `}`, `(`, `)`, `<`, `>`, `|`, `"` all break the
parser while `#` starts a comment. Real application titles contain these
(`Report [draft] #3 "final" (v2)`), so they are replaced rather than escaped.

### Coverage summary
Answers one question honestly: is this map complete enough to hand over yet? It
reports states by kind, controls seen against controls never followed, and when
gaps remain it ranks the states with the biggest gaps first and prints the
navigation path to each, since that is where a second pass buys the most.

### Autosave
A 60 second timer (`AUTOSAVE_SECONDS`) flushes `app_map.json` while recording, so
a crash during a long mapping session never costs the whole session. The timer
is cancelled on Stop and rescheduled after each flush.

## A real bug this phase surfaced

The live test captured a state titled `Claude`, from a completely different
application. `manual_capture()` used `GetForegroundWindow()` with no ownership
check, and the hotkey is global, so pressing it while focus sat anywhere else
pulled that unrelated window, **and its screenshot**, into the map. That is a
correctness problem and a privacy one.

Fixed: the foreground window is now used only when `_pid_of_hwnd()` confirms it
belongs to the target process, otherwise the capture falls back to the target's
own main window. The automatic event path never had this hole, because
`SetWinEventHook` filters by process ID at the OS level.

## Verification
- Unit tests over the exporter: both artefacts written and non-empty, every state
  present as a node, every `reached_by` edge drawn, kind shapes correct, hostile
  characters stripped from labels, complete-coverage and gap wording both
  correct, `needs_vision` surfaced, shard state counts summing to the whole.
- Live session against Notepad: autosave confirmed firing mid-session (map on
  disk before Stop), both artefacts written, Mermaid valid, summary accurate,
  and manual capture now staying on the target process.
