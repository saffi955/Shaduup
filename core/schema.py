"""
AppShadow — the JSON contract (schema).

This module is the single source of truth for what a captured session looks
like on disk. It is deliberately platform-agnostic: no pywinauto, no Windows,
no screenshots imports here. A scraper (Windows UIA now, web/macOS later) is
responsible for *filling* these dataclasses; this module only defines their
shape and how they serialise to the ``app_map.json`` Claude reads.

The design goal is token economy. A described screenshot costs ~1,500 tokens;
one State node here costs ~90. ``to_dict()`` therefore prunes every field that
carries no information (empty strings, ``None`` optionals, ``False`` flags) so
the emitted JSON stays lean without losing anything an AI agent needs to
reconstruct the app.

Field reference (mirrors the blueprint, panel C):

    Control  one UI element inside a state (a menu item, button, edit box, ...)
    Edge     the action that got you *into* a state  (state.reached_by)
    State    one captured snapshot of the app = one node in the map
    Session  the whole recording = the graph of states + their edges
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

__all__ = ["Control", "Edge", "State", "Session", "now_ts", "INTERACTIVE_TYPES"]


def now_ts() -> str:
    """Local, timezone-aware ISO-8601 timestamp to the second.

    Local time (not UTC) matches how the person driving the app experiences
    the session, and the offset keeps it unambiguous. Example:
    ``2026-07-12T14:03:22+05:00``.
    """
    return datetime.now().astimezone().isoformat(timespec="seconds")


# Control types a user can act on. Only these get a ``leads_to`` slot (and thus
# participate in coverage tracking) — a static Text label leads nowhere, so
# emitting ``leads_to: null`` for it would be noise. Kept as UIA-style names
# because that is what the first scraper produces; other scrapers map onto them.
INTERACTIVE_TYPES: frozenset[str] = frozenset(
    {
        "MenuItem",
        "Button",
        "SplitButton",
        "MenuButton",
        "CheckBox",
        "RadioButton",
        "ComboBox",
        "ListItem",
        "TreeItem",
        "TabItem",
        "Hyperlink",
        "Slider",
        "Spinner",
    }
)


@dataclass
class Control:
    """One interactive-or-static element captured inside a State.

    ``leads_to`` is the heart of coverage tracking:
      * ``None`` on an enabled interactive control  → you *saw* it but never
        clicked it (it shows up in ``State.unexplored``).
      * a ``state_id`` string                       → clicking it took you to
        that state; the map now has a real navigation edge.

    ``value_redacted`` enforces the privacy rule: an Edit/Password field is
    recorded as *existing*, never with its typed contents.
    """

    type: str
    name: str = ""
    rect: Optional[list[int]] = None  # [left, top, right, bottom], logical px
    enabled: bool = True
    automation_id: Optional[str] = None
    shortcut: Optional[str] = None  # e.g. "Ctrl+Shift+E", when UIA exposes it
    value_redacted: bool = False  # True for Edit/Password: field exists, value never stored
    leads_to: Optional[str] = None  # target state_id, or None = unexplored
    interactive: Optional[bool] = None  # None = infer from `type`

    def is_interactive(self) -> bool:
        """Whether this control participates in navigation / coverage."""
        if self.interactive is not None:
            return self.interactive
        return self.type in INTERACTIVE_TYPES

    def ref(self) -> str:
        """Stable short reference used in edges and unexplored lists.

        Example: ``"MenuItem:Export as Image…"``.
        """
        return f"{self.type}:{self.name}" if self.name else self.type

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"type": self.type}
        if self.name:
            d["name"] = self.name
        if self.rect is not None:
            d["rect"] = list(self.rect)
        # `enabled` is only worth stating when it deviates from the default.
        if not self.enabled:
            d["enabled"] = False
        if self.automation_id:
            d["automation_id"] = self.automation_id
        if self.shortcut:
            d["shortcut"] = self.shortcut
        if self.value_redacted:
            d["value_redacted"] = True
        # leads_to (possibly null) is meaningful ONLY for enabled, actionable
        # controls — that null is what powers the "unexplored" coverage report.
        # A disabled or static control can't lead anywhere, so we omit it.
        elif self.enabled and self.is_interactive():
            d["leads_to"] = self.leads_to
        return d


@dataclass
class Edge:
    """The action that led *into* a state. Stored as ``State.reached_by``.

    ``from_state`` serialises to ``"from"`` (a Python keyword, so it can't be
    the attribute name). Together the edges turn flat snapshots into the
    navigable, Graphify-style graph — Claude learns the *flow*, not just the
    per-screen layout.
    """

    action: str  # "left_click", "menu_open", "hover", "key", ...
    on: str  # control ref this action targeted, e.g. "MenuItem:Export"
    from_state: Optional[str] = None  # source state_id

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"action": self.action, "on": self.on}
        if self.from_state is not None:
            d["from"] = self.from_state
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Edge":
        return cls(action=d["action"], on=d["on"], from_state=d.get("from"))


@dataclass
class State:
    """One captured snapshot of the app = one node in the map."""

    state_id: str
    kind: str  # "window" | "menu_open" | "dialog" | "manual" | ...
    window_title: str = ""
    breadcrumb: list[str] = field(default_factory=list)
    reached_by: Optional[Edge] = None
    screenshot: Optional[str] = None  # relative path, e.g. "screenshots/state_0023.png"
    controls: list[Control] = field(default_factory=list)
    manual: bool = False  # captured via the manual hotkey/button, not auto-detect
    needs_vision: bool = False  # UIA was lossy here; the screenshot must be parsed too
    ts: str = field(default_factory=now_ts)
    # Diff-engine bookkeeping. Not part of the JSON contract — the session graph
    # uses it to recognise a state it has seen before. Excluded from to_dict().
    tree_hash: Optional[str] = None

    def unexplored(self) -> list[str]:
        """Refs of enabled, interactive controls that were never followed.

        Drives the Stop-time coverage report ("11 controls unexplored —
        revisit File > Export?"). An empty list means a complete pass.
        """
        return [
            c.ref()
            for c in self.controls
            if c.enabled and c.is_interactive() and c.leads_to is None
        ]

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"state_id": self.state_id, "kind": self.kind}
        if self.window_title:
            d["window_title"] = self.window_title
        if self.breadcrumb:
            d["breadcrumb"] = list(self.breadcrumb)
        if self.reached_by is not None:
            d["reached_by"] = self.reached_by.to_dict()
        if self.screenshot:
            d["screenshot"] = self.screenshot
        d["controls"] = [c.to_dict() for c in self.controls]
        unexplored = self.unexplored()
        if unexplored:
            d["unexplored"] = unexplored
        if self.manual:
            d["manual"] = True
        if self.needs_vision:
            d["needs_vision"] = True
        d["ts"] = self.ts
        return d


@dataclass
class Session:
    """The whole recording: metadata + the ordered list of captured states.

    The graph itself (dedup, edge wiring, autosave) lives in ``core/session.py``
    for Phase 3; this dataclass is the serialisable container those pieces fill
    and the exporter reads.
    """

    app: str = ""  # target executable, e.g. "InPage 2012.exe"
    pid: Optional[int] = None
    started: str = field(default_factory=now_ts)
    states: list[State] = field(default_factory=list)

    def add_state(self, state: State) -> None:
        self.states.append(state)

    def coverage(self) -> dict[str, int]:
        """Quick totals for the Stop-time summary and the status strip."""
        total_controls = sum(len(s.controls) for s in self.states)
        unexplored = sum(len(s.unexplored()) for s in self.states)
        kinds: dict[str, int] = {}
        for s in self.states:
            kinds[s.kind] = kinds.get(s.kind, 0) + 1
        return {
            "states": len(self.states),
            "controls": total_controls,
            "unexplored": unexplored,
            **{f"kind_{k}": v for k, v in kinds.items()},
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "app": self.app,
            "pid": self.pid,
            "started": self.started,
            "state_count": len(self.states),
            "states": [s.to_dict() for s in self.states],
        }
