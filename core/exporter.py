"""
AppShadow exporters — what lands on disk when a session ends.

Platform-agnostic by design (no pywinauto, no ctypes): a session graph goes in,
three artefacts come out.

    app_map.json   the structured map you feed to an AI coding agent
    graph.mmd      a Mermaid state diagram, the visual version of the same graph
    summary        a plain-language coverage report printed on Stop

Sharding is here too: a very large application can be split into one file per
top-level menu, so an agent loads only the shard it needs instead of the whole
map (risk R5).
"""

from __future__ import annotations

import json
import os
import re
from typing import Optional

from core.schema import Session, State

__all__ = [
    "write_app_map",
    "write_mermaid",
    "coverage_summary",
    "write_session",
    "write_shards",
]

# Mermaid node shapes per state kind, so the diagram reads at a glance.
# How a state was *triggered* (manual vs automatic) is a flag on the state, not
# a kind, so it is styled separately below rather than taking a shape here.
_SHAPES: dict[str, tuple[str, str]] = {
    "window": ("[", "]"),          # rectangle
    "menu_open": ("([", "])"),     # stadium
    "dialog": ("{{", "}}"),        # hexagon
}
_DEFAULT_SHAPE = ("[", "]")


def _mermaid_id(state_id: str) -> str:
    """A Mermaid-safe node id (alphanumerics and underscores only)."""
    return re.sub(r"[^A-Za-z0-9_]", "_", state_id)


def _mermaid_text(text: str) -> str:
    """Escape a label so Mermaid renders it literally.

    Quotes and brackets break Mermaid's parser, and a hash starts a comment, so
    they are replaced rather than escaped (Mermaid has no escape syntax inside
    quoted labels). Long titles are trimmed to keep the diagram readable.
    """
    clean = text.replace('"', "'").replace("#", "no.")
    clean = re.sub(r"[\[\]{}()<>|]", "", clean)
    clean = " ".join(clean.split())  # collapse newlines/tabs/runs of spaces
    return clean[:40] if len(clean) > 40 else clean


def _state_label(state: State) -> str:
    """Human-readable node label for the diagram.

    Prefers the breadcrumb tail, because a menu popup usually inherits its
    parent window's title: three different menus would otherwise all read
    "Untitled - MyApp" and the diagram would say nothing. Falls back to the
    window title, then the kind.
    """
    if state.breadcrumb:
        base = state.breadcrumb[-1]
    else:
        base = state.window_title.strip() or state.kind
    return f"{state.state_id}: {_mermaid_text(base)}"


def write_app_map(session: Session, out_dir: str, filename: str = "app_map.json") -> str:
    """Write the structured map. Returns the path written."""
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, filename)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(session.to_dict(), fh, indent=2, ensure_ascii=False)
    return path


def write_mermaid(session: Session, out_dir: str, filename: str = "graph.mmd") -> str:
    """Write a Mermaid state diagram of the session graph.

    Nodes are captured states (shaped by kind); edges are the actions that led
    from one to the next, labelled with the control that was used. States whose
    UIA tree was too thin to trust are marked so the gap is visible in the
    picture, not just in the JSON.
    """
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, filename)

    lines: list[str] = ["graph TD"]
    needs_vision: list[str] = []
    manual: list[str] = []

    for state in session.states:
        nid = _mermaid_id(state.state_id)
        open_b, close_b = _SHAPES.get(state.kind, _DEFAULT_SHAPE)
        lines.append(f'    {nid}{open_b}"{_state_label(state)}"{close_b}')
        if state.needs_vision:
            needs_vision.append(nid)
        if state.manual:
            manual.append(nid)

    edge_lines: list[str] = []
    for state in session.states:
        edge = state.reached_by
        if edge is None or not edge.from_state:
            continue
        src = _mermaid_id(edge.from_state)
        dst = _mermaid_id(state.state_id)
        label = _mermaid_text(edge.on or edge.action)
        edge_lines.append(f'    {src} -->|"{label}"| {dst}')

    if edge_lines:
        lines.append("")
        lines.extend(edge_lines)

    if needs_vision or manual:
        lines.append("")
    if needs_vision:
        lines.append("    classDef needsVision stroke-dasharray: 5 5")
        lines.append(f"    class {','.join(needs_vision)} needsVision")
    if manual:
        lines.append("    classDef manual stroke-width: 3px")
        lines.append(f"    class {','.join(manual)} manual")

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


def coverage_summary(session: Session, max_hints: int = 5) -> str:
    """A plain-language report of what was mapped and what was missed.

    This is the text printed on Stop. Its job is to answer one question
    honestly: is this map complete enough to hand to an agent yet?
    """
    cov = session.coverage()
    kinds = {k[len("kind_"):]: v for k, v in cov.items() if k.startswith("kind_")}
    kind_bits = ", ".join(f"{n} {k}" for k, n in sorted(kinds.items())) or "none"

    lines = [
        f"Captured {cov['states']} states ({kind_bits})",
        f"Controls seen: {cov['controls']} · unexplored: {cov['unexplored']}",
    ]

    vision = [s.state_id for s in session.states if s.needs_vision]
    if vision:
        shown = ", ".join(vision[:max_hints])
        more = f" (+{len(vision) - max_hints} more)" if len(vision) > max_hints else ""
        lines.append(f"Owner-drawn, screenshot needed for: {shown}{more}")

    if cov["unexplored"] == 0:
        lines.append("Coverage complete: every control you saw was followed.")
    else:
        # Point at the states with the biggest gaps first — that is where a
        # second pass buys the most.
        gaps = sorted(
            ((s, len(s.unexplored())) for s in session.states if s.unexplored()),
            key=lambda pair: pair[1],
            reverse=True,
        )
        lines.append("Revisit these to finish the map:")
        for state, count in gaps[:max_hints]:
            where = " > ".join(state.breadcrumb) if state.breadcrumb else (
                state.window_title.strip() or state.kind
            )
            example = state.unexplored()[0]
            lines.append(f"  {state.state_id} ({where}): {count} unexplored, e.g. {example}")
        if len(gaps) > max_hints:
            lines.append(f"  ... and {len(gaps) - max_hints} more states with gaps")

    return "\n".join(lines)


def write_session(session: Session, out_dir: str) -> dict[str, str]:
    """Write every artefact for a finished session.

    Returns a dict of what was written, so a caller (the recorder window, the
    CLI) can report real paths instead of guessing at them.
    """
    return {
        "app_map": write_app_map(session, out_dir),
        "graph": write_mermaid(session, out_dir),
    }


def write_shards(session: Session, out_dir: str, subdir: str = "shards") -> list[str]:
    """Split the map into one file per top-level menu (risk R5).

    A state is filed under the first entry of its breadcrumb, falling back to
    its kind when there is no breadcrumb. Only worth calling on a large map;
    the agent then loads a single shard instead of the entire application.
    """
    shard_dir = os.path.join(out_dir, subdir)
    os.makedirs(shard_dir, exist_ok=True)

    buckets: dict[str, list[State]] = {}
    for state in session.states:
        key = state.breadcrumb[0] if state.breadcrumb else state.kind
        buckets.setdefault(key, []).append(state)

    written: list[str] = []
    for key, states in buckets.items():
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", key).strip("_") or "misc"
        path = os.path.join(shard_dir, f"{safe}.json")
        doc = {
            "app": session.app,
            "pid": session.pid,
            "shard": key,
            "state_count": len(states),
            "states": [s.to_dict() for s in states],
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, ensure_ascii=False)
        written.append(path)
    return written
