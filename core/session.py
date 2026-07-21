"""
AppShadow — the session graph: dedup, edge wiring, debounce, coverage.

Platform-agnostic on purpose (no pywinauto, no ctypes). A scraper hands this
module finished ``State`` objects; this module decides whether each one is a
*new* node or a *revisit* of one already in the map, wires the navigation edge
that led to it, and keeps the coverage picture up to date.

Three pieces:

* ``tree_hash``     — a position-independent fingerprint of a state's controls,
                      so reopening the same menu is recognised as the same node
                      instead of piling up duplicates (the diff engine).
* ``Debouncer``     — collapses a burst of window events into a single capture
                      (300 ms settle: one click = one state, not twenty).
* ``SessionGraph``  — the live map: append-or-link on each capture, plus the
                      Stop-time coverage report.
"""

from __future__ import annotations

import hashlib
import threading
from typing import Callable, Optional

from core.schema import Edge, Session, State

__all__ = ["tree_hash", "Debouncer", "SessionGraph"]


def tree_hash(state: State) -> str:
    """A fingerprint that identifies a *screen*, independent of where it sits.

    Deliberately excludes bounding rects (a menu popped at x=310 vs x=312 is the
    same menu) and includes the kind, window title and the ordered list of
    control type/name/enabled — enough to tell the File menu from the Edit menu,
    but stable across reopening the same one. This is what stops the map filling
    with duplicates when you revisit a screen.
    """
    parts = [state.kind, "\x1f", state.window_title, "\x1e"]
    for c in state.controls:
        parts.append(c.type)
        parts.append(":")
        parts.append(c.name)
        parts.append("+" if c.enabled else "-")
        parts.append("\x1d")
    blob = "".join(parts).encode("utf-8", "replace")
    return hashlib.sha1(blob).hexdigest()[:16]


class Debouncer:
    """Collapse an event burst into one deferred call.

    Every ``trigger()`` (re)starts a settle timer; the callback fires once, only
    after the events stop arriving for ``delay_ms``. Thread-safe: window events
    arrive on the hook thread, the timer fires on its own thread. ``flush()``
    forces an immediate fire (the manual-capture path), ``cancel()`` stops it
    (Stop).
    """

    def __init__(self, delay_ms: int, callback: Callable[[], None]) -> None:
        self._delay = delay_ms / 1000.0
        self._callback = callback
        self._timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()

    def trigger(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._delay, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def flush(self) -> None:
        """Fire now, skipping the wait (manual capture)."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        self._callback()

    def cancel(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    def _fire(self) -> None:
        with self._lock:
            self._timer = None
        self._callback()


class SessionGraph:
    """The live map: append new states, link revisits, wire edges, count coverage."""

    def __init__(self, app: str = "", pid: Optional[int] = None) -> None:
        self.session = Session(app=app, pid=pid)
        self._by_hash: dict[str, str] = {}  # tree_hash -> state_id
        self._by_id: dict[str, State] = {}
        self._counter = 0
        self.current_id: Optional[str] = None  # where the user "is" right now
        self._lock = threading.Lock()

    # -- id allocation --------------------------------------------------------
    def next_state_id(self) -> str:
        self._counter += 1
        return f"state_{self._counter:04d}"

    # -- the diff engine ------------------------------------------------------
    def record(
        self,
        state: State,
        *,
        action: str = "navigate",
        on: Optional[str] = None,
    ) -> tuple[State, bool]:
        """Fold a freshly captured state into the map.

        Returns ``(canonical_state, is_new)``. If the state's fingerprint was
        seen before, the existing node is reused (no duplicate) and only the
        navigation edge is (re)wired; otherwise the state is appended. Either
        way the edge from the previous state is recorded and — where it can be
        matched — the source control's ``leads_to`` is filled in, which is what
        turns ``unexplored`` from a guess into a fact.
        """
        with self._lock:
            h = state.tree_hash = tree_hash(state)
            existing_id = self._by_hash.get(h)

            if existing_id is not None:
                canonical = self._by_id[existing_id]
                is_new = False
            else:
                # give it a real id and adopt it as a new node
                if not state.state_id or state.state_id in self._by_id:
                    state.state_id = self.next_state_id()
                self._by_hash[h] = state.state_id
                self._by_id[state.state_id] = state
                self.session.add_state(state)
                canonical = state
                is_new = True

            # Wire the edge that led here (skip a self-loop: an event that just
            # re-captures the screen you're already on isn't navigation).
            if self.current_id is not None and self.current_id != canonical.state_id:
                source = self._by_id.get(self.current_id)
                edge_on = on or self._guess_source_control(source, canonical)
                canonical.reached_by = Edge(
                    action=action, on=edge_on or "", from_state=self.current_id
                )
                if source is not None and edge_on:
                    self._link_leads_to(source, edge_on, canonical.state_id)

            self.current_id = canonical.state_id
            return canonical, is_new

    @staticmethod
    def _guess_source_control(source: Optional[State], dest: State) -> Optional[str]:
        """Best-effort: which control in `source` opened `dest`?

        A menu/dialog usually carries the label of the item that spawned it
        (the File menu popup is titled "File"), so match the destination's
        window title against a control name in the source. Good enough to build
        real edges without a mouse hook; a precise INVOKED event, when present,
        overrides this via the ``on=`` argument.
        """
        if source is None or not dest.window_title:
            return None
        title = dest.window_title.strip().lower()
        for c in source.controls:
            if c.name and c.name.strip().lower() == title:
                return c.ref()
        return None

    @staticmethod
    def _link_leads_to(source: State, control_ref: str, dest_id: str) -> None:
        for c in source.controls:
            if c.ref() == control_ref:
                c.leads_to = dest_id
                return

    def set_current(self, state_id: Optional[str]) -> None:
        with self._lock:
            self.current_id = state_id

    # -- reporting ------------------------------------------------------------
    def coverage(self) -> dict[str, int]:
        return self.session.coverage()

    def unexplored_report(self) -> list[tuple[str, list[str]]]:
        """(state_id, [unexplored control refs]) for every state with gaps."""
        out = []
        for s in self.session.states:
            gaps = s.unexplored()
            if gaps:
                out.append((s.state_id, gaps))
        return out
