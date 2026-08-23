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
from core.session import Debouncer, SessionGraph, tree_hash  # noqa: E402


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


def test_write_session_produces_both_artefacts():
    out = tempfile.mkdtemp(prefix="appshadow_test_")
    written = write_session(sample_session(), out)
    assert set(written) == {"app_map", "graph"}
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
