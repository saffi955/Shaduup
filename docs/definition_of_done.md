# Definition of Done: v0.1 verification

Measured against the four criteria in the project plan (§10). Three pass on real
captured data. One is reported honestly as partially met, with the reason.

| # | Criterion | Verdict |
|---|---|---|
| 1 | A session produces `app_map.json` under ~50K tokens covering what you visited | **Pass** |
| 2 | An agent, given only that JSON, can answer "what are all the export options and how do I reach them?" | **Pass**, with a caveat on edge labels |
| 3 | Coverage report shows 0 unexplored controls after a deliberate full pass | **Partially met**. Mechanism verified; a literal zero needs a human pass |
| 4 | No typed value appears anywhere in the output files | **Pass**, after a fix this check forced |

## 1. Map size: pass

A six-screen WordPad session:

```
states=6   bytes=50,531   ~14,437 tokens      (budget ~50,000)
```

Per node, measured rather than assumed:

| Node | Controls | Approx tokens |
|---|---|---|
| Small dialog | 1 | 92 |
| Save-as submenu | 7 | 290 |
| File menu | 28 | 801 |
| Full ribbon window | 83 | 2,389 |

The blueprint's "~90 tokens per node" holds for a simple menu or dialog. A dense
ribbon window is an order of magnitude larger, and the README now says so.

## 2. Answerable from the JSON alone: pass, with a caveat

The question was "what are all the export options and how do I reach them?".
Reading only `app_map.json`:

- `state_0004` lists **Rich Text document, Office Open XML document, OpenDocument
  text, Plain text document, Other formats**, grouped under "Save a copy of the
  document".
- Its `reached_by.from` is `state_0003`, whose controls include `Save as`.
- `state_0003` traces back through `state_0002` to the root `state_0001`, whose
  controls include `File tab`.

So the correct answer, *those five formats, reached by File tab then Save as*,
is recoverable from the JSON with no screenshots.

**Caveat:** every `reached_by.on` in this session is empty. Edge attribution
matches the destination's window title against a control name in the source, and
these ribbon popups report no title, so no match was found. The path is therefore
stated as "state_0004 came from state_0003" and the reader infers *which* control
from the source state's contents. That inference is easy here and was correct,
but it is inference, not recorded fact. Naming the control on every edge is the
single highest-value improvement left; a deliberately empty `on` is preferred over
a guessed one, per the project's rule against inventing edges.

## 3. Zero unexplored after a full pass: partially met

Two things had to be separated here.

**The metric was wrong, and is now fixed.** Window chrome (minimise, maximise,
close, the system menu) was counted as unexplored application surface. Because
"finishing" the map would then require clicking Close and ending the recording,
zero was unreachable by construction. Those controls are now flagged `chrome`,
kept in the map, and excluded from coverage. Twelve such controls were excluded
in the verification session. `tests/test_core.py` asserts that a state whose real
controls are all followed now reports zero.

**A literal zero on a whole application still needs a person.** The scripted pass
reached 121 unexplored of 221 controls, because it deliberately stopped short of
clicking every ribbon button. That restraint was not caution for its own sake: an
earlier blind pass activated *Send in email* and raised two error dialogs, which
is exactly the failure the plan predicts for autonomous crawlers and the reason
AppShadow is human-driven. A real zero means a person deliberately visiting every
screen, which is the intended workflow.

## 4. No typed value in the outputs: pass, after a fix

Tested by typing two distinctive strings into a live session (one into the
document body, one into the Save As filename field), capturing both screens while
the text was visible, then scanning every output file as UTF-8 and UTF-16.

The first run passed for `app_map.json` and `graph.mmd`: every text-bearing
control (`Document`, `RichEdit Control`, `File name:`, `Search Box`) was recorded
as `value_redacted: true` with no value.

**It failed for the screenshots.** A PNG is a pixel-accurate copy of the screen,
so the typed secret was plainly legible in the image even though it had been
stripped from the JSON. The privacy rule was true of the map and false of the
session.

Fixed by painting out the rectangle of every redacted field before the PNG is
written (`capture/screenshot.py`, `_paint_out`). The rects are already known, so
this costs nothing extra. Re-running the audit: **14 files scanned, 0 leaks**, and
the screenshot now shows the full ribbon and menu structure with the document body
blacked out. The structure that makes the map useful is kept; the content is not.

## Note on the original target application

The plan names a desktop publishing app as the target. Its menus were captured
successfully and are documented in `docs/phase2_findings.md` and
`docs/phase3_findings.md`, including labels, keyboard shortcuts and automation ids.

The end-to-end verification session above uses WordPad instead, because that build
of the publishing app opens with a modal, fully owner-drawn splash screen that
does not respond to synthetic keyboard or mouse input: Enter terminates the
process, Escape hides the splash without opening the main window, and clicks are
ignored. A person clicks past it in a second, which is precisely the argument for
human-driven recording made in the plan's opening section. Nothing about the
recorder needs to change for that app; it needs a human to start the session.
