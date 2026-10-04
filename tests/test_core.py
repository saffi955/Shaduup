"""
Core invariants: the schema contract, the diff engine, the debouncer, exporters.

Everything here is deterministic and platform-agnostic, so it runs anywhere
without a live Windows application. The scraper's own behaviour needs a real
target and is covered by the findings in docs/.

    python -m pytest tests/            (if pytest is installed)
    python tests/test_core.py          (no dependencies, prints a summary)
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.exporter import (  # noqa: E402
    coverage_summary,
    write_mermaid,
    write_session,
    write_shards,
)
from core.schema import Control, Edge, Session, State  # noqa: E402
from core.session import Debouncer, SessionGraph, control_at, tree_hash  # noqa: E402


# --- helpers -----------------------------------------------------------------
def main_window() -> State:
    return State(
        state_id="",
        kind="window",
        window_title="Untitled - MyApp",
        controls=[
            Control(type="MenuItem", name="File", rect=[0, 0, 30, 20]),
            Control(type="MenuItem", name="Edit", rect=[30, 0, 60, 20]),
        ],
    )


def file_menu() -> State:
    return State(
        state_id="",
        kind="menu_open",
        window_title="File",
        controls=[
            Control(type="MenuItem", name="New...", rect=[3, 45, 400, 67], shortcut="Ctrl+N"),
            Control(type="MenuItem", name="Open...", rect=[3, 67, 400, 89], shortcut="Ctrl+O"),
        ],
    )


# --- schema ------------------------------------------------------------------
def test_control_prunes_default_fields():
    """to_dict() omits anything that carries no information (token economy)."""
    d = Control(type="MenuItem", name="New").to_dict()
    assert "enabled" not in d, "enabled=True is the default and should be omitted"
    assert "automation_id" not in d
    assert "value_redacted" not in d


def test_leads_to_only_on_actionable_controls():
    """A null leads_to is the coverage signal, so it must not appear as noise."""
    enabled = Control(type="MenuItem", name="Export").to_dict()
    assert "leads_to" in enabled and enabled["leads_to"] is None

    disabled = Control(type="MenuItem", name="Publish", enabled=False).to_dict()
    assert "leads_to" not in disabled, "a disabled control cannot lead anywhere"

    static = Control(type="Text", name="Ready").to_dict()
    assert "leads_to" not in static, "a static label cannot lead anywhere"


def test_edit_value_is_redacted_not_stored():
    """The privacy rule: the field exists in the map, its content never does."""
    d = Control(type="Edit", name="File name", value_redacted=True).to_dict()
    assert d["value_redacted"] is True
    assert "value" not in d
    assert "leads_to" not in d, "a redacted field is not a navigation target"


def test_edge_serialises_from_as_from():
    d = Edge(action="left_click", on="MenuItem:File", from_state="state_0001").to_dict()
    assert d["from"] == "state_0001", d
    assert "from_state" not in d


def test_window_chrome_is_excluded_from_coverage():
    """Otherwise "nothing left to explore" is only reachable by clicking Close."""
    state = State(
        state_id="state_0001",
        kind="window",
        controls=[
            Control(type="Button", name="Minimize", chrome=True),
            Control(type="Button", name="Close", chrome=True),
            Control(type="MenuItem", name="File"),
        ],
    )
    assert state.unexplored() == ["MenuItem:File"], state.unexplored()

    followed = State(state_id="state_0002", kind="window", controls=[
        Control(type="Button", name="Close", chrome=True),
        Control(type="MenuItem", name="File", leads_to="state_0003"),
    ])
    assert followed.unexplored() == [], "a complete pass must be able to reach zero"


def test_chrome_control_is_kept_in_the_map():
    """Excluded from coverage is not the same as deleted: the frame still exists."""
    d = Control(type="Button", name="Close", chrome=True).to_dict()
    assert d["name"] == "Close" and d["chrome"] is True
    assert "leads_to" not in d, "chrome must not read as an unexplored gap"


def test_redacted_field_is_not_an_unexplored_gap():
    state = State(state_id="state_0001", kind="dialog", controls=[
        Control(type="Edit", name="File name", value_redacted=True),
    ])
    assert state.unexplored() == []


def test_unexplored_lists_only_unfollowed_actionable_controls():
    state = State(
        state_id="state_0001",
        kind="menu_open",
        controls=[
            Control(type="MenuItem", name="Followed", leads_to="state_0002"),
            Control(type="MenuItem", name="Not followed"),
            Control(type="MenuItem", name="Disabled", enabled=False),
            Control(type="Text", name="Static"),
        ],
    )
    assert state.unexplored() == ["MenuItem:Not followed"], state.unexplored()


# --- diff engine -------------------------------------------------------------
def test_tree_hash_ignores_position():
    """The same menu popped at a different x is still the same menu."""
    a, b = file_menu(), file_menu()
    b.controls[0].rect = [999, 999, 1200, 1020]
    assert tree_hash(a) == tree_hash(b)


def test_tree_hash_distinguishes_content():
    a, b = file_menu(), file_menu()
    b.controls[0].name = "Save..."
    assert tree_hash(a) != tree_hash(b)


def test_revisiting_a_screen_links_instead_of_duplicating():
    g = SessionGraph(app="MyApp.exe", pid=1)
    first, new_first = g.record(main_window(), action="start")
    assert new_first and first.state_id == "state_0001"

    menu, new_menu = g.record(file_menu(), action="menu_open")
    assert new_menu and menu.state_id == "state_0002"

    back, is_new = g.record(main_window(), action="foreground")
    assert not is_new, "returning to a known screen must not create a node"
    assert back.state_id == "state_0001"

    again, is_new = g.record(file_menu(), action="menu_open")
    assert not is_new and again.state_id == "state_0002"
    assert len(g.session.states) == 2, "the map grew duplicates"


def test_edges_wire_and_fill_leads_to():
    """An edge turns a flat snapshot into a graph and makes coverage a fact."""
    g = SessionGraph(app="MyApp.exe", pid=1)
    home, _ = g.record(main_window(), action="start")
    menu, _ = g.record(file_menu(), action="menu_open")

    assert menu.reached_by is not None
    assert menu.reached_by.from_state == "state_0001"
    assert menu.reached_by.on == "MenuItem:File", menu.reached_by.on

    followed = next(c for c in home.controls if c.name == "File")
    assert followed.leads_to == "state_0002", "the followed control was not linked"
    untouched = next(c for c in home.controls if c.name == "Edit")
    assert untouched.leads_to is None, "an unclicked control must stay unexplored"


def test_revisit_keeps_the_original_parent():
    """Going back to a known screen must not rewrite how it was first reached.

    Rewriting it is what turned the Calculator map into a fake cycle: the first
    screen ended up "reached from" the screen that was reached from it.
    """
    g = SessionGraph(app="MyApp.exe", pid=1)
    home, _ = g.record(main_window(), action="start")
    menu, _ = g.record(file_menu(), action="menu_open")
    back, _ = g.record(main_window(), action="foreground")
    assert back is home
    assert home.reached_by is None, f"the root grew a parent: {home.reached_by}"
    g.record(file_menu(), action="menu_open")
    assert menu.reached_by.from_state == "state_0001"


def calculator(display: str, mode: str = "Standard") -> State:
    keys = ["5", "Plus"] if mode == "Standard" else ["5", "Plus", "Sine"]
    return State(
        state_id="",
        kind="window",
        window_title="Calculator",
        window_rect=[100, 100, 300, 500],
        controls=[Control(type="Text", name=f"Display is {display}", rect=[0, 0, 300, 80])]
        + [Control(type="Button", name=k, rect=[i * 60, 100, 60, 40])
           for i, k in enumerate(keys)],
    )


def test_in_place_value_change_marks_the_click_explored():
    """A Calculator key press changes the display, not the screen."""
    g = SessionGraph(app="CalculatorApp.exe", pid=1)
    calc, _ = g.record(calculator("0"), action="start")
    after, is_new = g.record(calculator("5"), action="left_click",
                             on="Button:5", in_place=True)
    assert not is_new and after is calc, "a new display value is not a new screen"
    assert len(g.session.states) == 1
    five = next(c for c in calc.controls if c.name == "5")
    assert five.leads_to == calc.state_id, "the pressed key must count as explored"
    assert "Button:5" not in calc.unexplored()


def test_in_place_layout_change_is_a_new_state():
    """Switching Calculator to Scientific swaps the buttons: a real transition."""
    g = SessionGraph(app="CalculatorApp.exe", pid=1)
    std, _ = g.record(calculator("0"), action="start")
    sci, is_new = g.record(calculator("0", mode="Scientific"), action="in_place",
                           in_place=True)
    assert is_new and sci is not std
    assert sci.reached_by.from_state == std.state_id


def test_in_place_return_reuses_the_earlier_screen():
    """Back to Standard with another number shown is still the Standard node."""
    g = SessionGraph(app="CalculatorApp.exe", pid=1)
    std, _ = g.record(calculator("0"), action="start")
    g.record(calculator("0", mode="Scientific"), action="in_place", in_place=True)
    back, is_new = g.record(calculator("7"), action="in_place", in_place=True)
    assert not is_new and back is std
    assert len(g.session.states) == 2


def test_control_at_hits_the_smallest_actionable_control():
    state = calculator("0")
    # window at (100, 100); "Plus" button is at x 60..120, y 100..140 inside it
    assert control_at(state, 100 + 70, 100 + 110) == "Button:Plus"
    assert control_at(state, 100 + 10, 100 + 10) is None, "static text is not a target"


# --- debouncer ---------------------------------------------------------------
def test_burst_collapses_to_one_call():
    """One click must produce one state, not twenty."""
    calls: list[float] = []
    d = Debouncer(120, lambda: calls.append(time.perf_counter()))
    for _ in range(20):
        d.trigger()
        time.sleep(0.005)
    time.sleep(0.35)
    assert len(calls) == 1, f"expected 1 debounced call, got {len(calls)}"


def test_flush_fires_immediately():
    fired = threading.Event()
    d = Debouncer(5000, fired.set)
    d.trigger()
    d.flush()
    assert fired.is_set(), "manual capture must not wait for the settle timer"


def test_cancel_prevents_firing():
    calls: list[int] = []
    d = Debouncer(80, lambda: calls.append(1))
    d.trigger()
    d.cancel()
    time.sleep(0.25)
    assert not calls, "a cancelled debounce must not fire after stop"


# --- exporter ----------------------------------------------------------------
def sample_session() -> Session:
    s = Session(app="MyApp.exe", pid=1)
    s.add_state(State(state_id="state_0001", kind="window", window_title="Untitled - MyApp",
                      controls=[Control(type="MenuItem", name="File", leads_to="state_0002")]))
    s.add_state(State(state_id="state_0002", kind="menu_open", window_title="Untitled - MyApp",
                      breadcrumb=["MainWindow", "File"],
                      reached_by=Edge("left_click", "MenuItem:File", "state_0001"),
                      controls=[Control(type="MenuItem", name="Export")]))
    s.add_state(State(state_id="state_0003", kind="dialog", window_title="Export",
                      breadcrumb=["MainWindow", "File", "Export"],
                      reached_by=Edge("left_click", "MenuItem:Export", "state_0002"),
                      controls=[Control(type="Edit", name="Path", value_redacted=True)]))
    return s


def test_write_session_produces_every_artefact():
    out = tempfile.mkdtemp(prefix="appshadow_test_")
    written = write_session(sample_session(), out)
    assert set(written) == {"app_map", "graph", "app_graph"}
    for path in written.values():
        assert os.path.exists(path) and os.path.getsize(path) > 0
    doc = json.load(open(written["app_map"], encoding="utf-8"))
    assert doc["state_count"] == 3


def test_mermaid_draws_every_state_and_edge():
    out = tempfile.mkdtemp(prefix="appshadow_test_")
    session = sample_session()
    text = open(write_mermaid(session, out), encoding="utf-8").read()
    assert text.startswith("graph TD")
    for state in session.states:
        assert state.state_id in text
    drawn = [line for line in text.splitlines() if "-->" in line]
    expected = sum(1 for s in session.states if s.reached_by and s.reached_by.from_state)
    assert len(drawn) == expected, (len(drawn), expected)


def test_mermaid_labels_survive_hostile_titles():
    """Mermaid has no escape syntax inside labels, so they must be sanitised."""
    out = tempfile.mkdtemp(prefix="appshadow_test_")
    s = Session(app="x", pid=1)
    s.add_state(State(state_id="state_0001", kind="window",
                      window_title='Report [draft] #3 "final" (v2)|x',
                      controls=[Control(type="Button", name="OK")]))
    text = open(write_mermaid(s, out, "hostile.mmd"), encoding="utf-8").read()
    label = text.splitlines()[1].split('"')[1]
    for ch in '[]{}()<>|"#':
        assert ch not in label, f"{ch!r} would break the Mermaid parser"


def test_mermaid_label_uses_breadcrumb_to_disambiguate():
    """Menus inherit the parent title, so the breadcrumb is what tells them apart."""
    out = tempfile.mkdtemp(prefix="appshadow_test_")
    text = open(write_mermaid(sample_session(), out, "crumbs.mmd"), encoding="utf-8").read()
    assert "state_0002: File" in text, text


def test_coverage_summary_reports_gaps_then_completion():
    gapped = coverage_summary(sample_session())
    assert "unexplored" in gapped
    assert "Revisit these" in gapped

    done = Session(app="x", pid=1)
    done.add_state(State(state_id="state_0001", kind="window",
                         controls=[Control(type="Button", name="OK", leads_to="state_0002")]))
    assert "Coverage complete" in coverage_summary(done)


def test_coverage_summary_surfaces_needs_vision():
    s = Session(app="x", pid=1)
    s.add_state(State(state_id="state_0009", kind="window", needs_vision=True,
                      controls=[Control(type="Button", name="Go", leads_to="s")]))
    assert "state_0009" in coverage_summary(s)


def test_shards_preserve_every_state():
    out = tempfile.mkdtemp(prefix="appshadow_test_")
    session = sample_session()
    paths = write_shards(session, out)
    assert paths
    total = sum(json.load(open(p, encoding="utf-8"))["state_count"] for p in paths)
    assert total == len(session.states), (total, len(session.states))


# --- geometry, hierarchy and presentation -------------------------------------
def test_geometry_carries_position_and_size():
    """[x, y, w, h] window-relative is what rebuilding a layout needs."""
    d = Control(type="Button", name="OK", rect=[30, 12, 80, 24]).to_dict()
    x, y, w, h = d["rect"]
    assert (x, y) == (30, 12)
    assert (w, h) == (80, 24), "width and height must be carried, not derived"


def test_window_rect_makes_absolute_positions_recoverable():
    s = State(state_id="state_0001", kind="window", window_rect=[100, 50, 800, 600],
              controls=[Control(type="Button", name="OK", rect=[30, 12, 80, 24])])
    d = s.to_dict()
    assert d["window_rect"] == [100, 50, 800, 600]
    wx, wy = d["window_rect"][0], d["window_rect"][1]
    cx, cy = d["controls"][0]["rect"][0], d["controls"][0]["rect"][1]
    assert (wx + cx, wy + cy) == (130, 62), "absolute position must be recoverable"


def test_depth_reconstructs_the_control_tree():
    """A flat list plus depth is enough to rebuild nesting exactly."""
    controls = [
        Control(type="ToolBar", name="Formatting", depth=0),
        Control(type="Button", name="Bold", depth=1),
        Control(type="Button", name="Italic", depth=1),
        Control(type="ToolBar", name="Clipboard", depth=0),
        Control(type="Button", name="Paste", depth=1),
    ]
    tree, stack = [], []
    for c in controls:
        node = {"name": c.name, "children": []}
        del stack[c.depth:]
        (stack[-1]["children"] if stack else tree).append(node)
        stack.append(node)
    assert [n["name"] for n in tree] == ["Formatting", "Clipboard"]
    assert [n["name"] for n in tree[0]["children"]] == ["Bold", "Italic"]
    assert [n["name"] for n in tree[1]["children"]] == ["Paste"]


def test_presentation_fields_are_emitted_only_when_known():
    plain = Control(type="Button", name="OK").to_dict()
    for absent in ("depth", "help_text", "checked", "expanded", "selected", "class_name"):
        assert absent not in plain, f"{absent} should stay out of the map when unknown"

    rich = Control(type="CheckBox", name="Word wrap", depth=2, class_name="Button",
                   help_text="Wrap text at the window edge", checked=True,
                   expanded=False, selected=True).to_dict()
    assert rich["depth"] == 2
    assert rich["class_name"] == "Button"
    assert rich["help_text"] == "Wrap text at the window edge"
    assert rich["checked"] is True
    assert rich["expanded"] is False
    assert rich["selected"] is True


def test_unchecked_box_is_recorded_as_false_not_omitted():
    """False and 'no such state' are different facts and must not collapse."""
    d = Control(type="CheckBox", name="Word wrap", checked=False).to_dict()
    assert d["checked"] is False


# --- app structure graph ------------------------------------------------------
def test_app_graph_shows_the_application_not_the_capture_order():
    from core.exporter import write_app_graph

    out = tempfile.mkdtemp(prefix="appshadow_test_")
    text = open(write_app_graph(sample_session(), out), encoding="utf-8").read()

    assert text.startswith("graph LR")
    assert '"MyApp.exe"' in text, "the application itself should be the root"
    # a followed control becomes a labelled edge into the screen it opens,
    # whether the link was recorded forwards (leads_to) or backwards (reached_by)
    assert '-->|"File"|' in text, text
    assert '-->|"Export"|' in text, text
    assert "state_0001 -->" in text and "state_0002 -->" in text, text


def test_app_graph_still_shows_controls_nobody_followed():
    """A gap has to be visible in the picture, not only in the coverage report."""
    from core.exporter import write_app_graph

    s = Session(app="MyApp.exe", pid=1)
    s.add_state(State(state_id="state_0001", kind="window", window_title="Main",
                      controls=[
                          Control(type="MenuItem", name="File", leads_to="state_0002"),
                          Control(type="MenuItem", name="Never opened"),
                      ]))
    s.add_state(State(state_id="state_0002", kind="menu_open", breadcrumb=["Main", "File"],
                      reached_by=Edge("left_click", "MenuItem:File", "state_0001"),
                      controls=[Control(type="MenuItem", name="Also never opened")]))

    out = tempfile.mkdtemp(prefix="appshadow_test_")
    text = open(write_app_graph(s, out, "gaps.mmd"), encoding="utf-8").read()
    assert "Never opened" in text and "Also never opened" in text
    assert "classDef unexplored" in text
    assert '-->|"File"|' in text, "the followed control is still a real edge"


def test_app_graph_labels_edges_with_control_names_not_state_ids():
    from core.exporter import write_app_graph

    out = tempfile.mkdtemp(prefix="appshadow_test_")
    text = open(write_app_graph(sample_session(), out, "labels.mmd"), encoding="utf-8").read()
    edges = [ln for ln in text.splitlines() if "-->|" in ln]
    assert edges, "expected labelled edges"
    for line in edges:
        label = line.split('|"')[1].split('"|')[0]
        assert not label.startswith("state_"), f"edge labelled with a state id: {line}"


# --- screenshot redaction -----------------------------------------------------
def test_paint_out_blanks_only_the_given_rect():
    """A screenshot is pixel-accurate, so redacted fields must be painted out."""
    from capture.screenshot import _paint_out

    w, h = 10, 4
    rgb = bytes([255]) * (w * h * 3)                      # all white
    # [x, y, w, h] window-relative: covers x 2..5, y 1..3
    out = _paint_out(rgb, w, h, [[2, 1, 3, 2]])

    def px(x, y):
        i = (y * w + x) * 3
        return out[i:i + 3]

    assert px(2, 1) == b"\x00\x00\x00", "inside the rect must be blanked"
    assert px(4, 2) == b"\x00\x00\x00", "inside the rect must be blanked"
    assert px(1, 1) == b"\xff\xff\xff", "left of the rect must be untouched"
    assert px(5, 1) == b"\xff\xff\xff", "right edge is exclusive"
    assert px(2, 0) == b"\xff\xff\xff", "above the rect must be untouched"
    assert px(2, 3) == b"\xff\xff\xff", "bottom edge is exclusive"
    assert len(out) == len(rgb)


def test_paint_out_clamps_rects_that_hang_off_the_window():
    """A field partly outside the window must not corrupt neighbouring rows."""
    from capture.screenshot import _paint_out

    w, h = 6, 3
    rgb = bytes([255]) * (w * h * 3)
    out = _paint_out(rgb, w, h, [[-50, -50, 500, 500]])
    assert out == b"\x00" * len(rgb), "a rect covering everything blanks everything"
    assert len(out) == len(rgb)

    untouched = _paint_out(rgb, w, h, [[900, 900, 50, 50]])
    assert untouched == rgb, "a rect fully outside must change nothing"


# --- runner (so the file works without pytest) --------------------------------
def _run_all() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {name}: {exc}")
        except Exception as exc:  # noqa: BLE001 - report, don't mask
            failed += 1
            print(f"  ERROR {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
