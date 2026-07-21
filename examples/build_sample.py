"""
Phase 1, step 5 — hand-build a fake session and emit app_map.json.

Purpose: lock the JSON contract by producing a realistic map *from the schema
dataclasses* (not hand-typed JSON), then confirm it is something an AI agent can
actually rebuild an app from. Reproduces the blueprint's canonical File > Export
node exactly, and adds the surrounding states (main window, File menu, an Export
dialog with a redacted filename field) so edges and coverage are exercised too.

Run:  python -m examples.build_sample   →  writes examples/app_map.sample.json
"""

from __future__ import annotations

import json
import os

from core.schema import Control, Edge, Session, State

HERE = os.path.dirname(__file__)
TS = "2026-07-12T14:03:22+05:00"


def build() -> Session:
    session = Session(app="InPage 2012.exe", pid=14232, started="2026-07-12T14:03:00+05:00")

    # state_0001 — main window (the root you start from)
    session.add_state(
        State(
            state_id="state_0001",
            kind="window",
            window_title="Untitled — InPage 2012",
            controls=[
                Control(type="MenuItem", name="File", rect=[8, 30, 44, 52], leads_to="state_0019"),
                Control(type="MenuItem", name="Edit", rect=[44, 30, 82, 52], leads_to="state_0025"),
                Control(type="MenuItem", name="View", rect=[82, 30, 124, 52], leads_to=None),
                Control(type="MenuItem", name="Format", rect=[124, 30, 178, 52], leads_to=None),
            ],
            ts="2026-07-12T14:03:00+05:00",
        )
    )

    # state_0019 — File menu open (reached by clicking File on the main window)
    session.add_state(
        State(
            state_id="state_0019",
            kind="menu_open",
            window_title="Untitled — InPage 2012",
            breadcrumb=["MainWindow", "File"],
            reached_by=Edge(action="left_click", on="MenuItem:File", from_state="state_0001"),
            controls=[
                Control(type="MenuItem", name="New", rect=[312, 60, 508, 84], shortcut="Ctrl+N", leads_to=None),
                Control(type="MenuItem", name="Open…", rect=[312, 88, 508, 112], shortcut="Ctrl+O", leads_to=None),
                Control(type="MenuItem", name="Export ▸", rect=[312, 116, 508, 140], leads_to="state_0023"),
                Control(type="MenuItem", name="Exit", rect=[312, 232, 508, 256], leads_to=None),
            ],
            ts="2026-07-12T14:03:19+05:00",
        )
    )

    # state_0023 — the canonical blueprint node: File > Export submenu
    session.add_state(
        State(
            state_id="state_0023",
            kind="menu_open",
            window_title="Untitled — InPage 2012",
            breadcrumb=["MainWindow", "File", "Export"],
            reached_by=Edge(action="left_click", on="MenuItem:Export", from_state="state_0019"),
            screenshot="screenshots/state_0023.png",
            controls=[
                Control(type="MenuItem", name="Export as PDF…", rect=[312, 148, 508, 172],
                        enabled=True, shortcut="Ctrl+Shift+E", leads_to="state_0024"),
                Control(type="MenuItem", name="Export as Image…", rect=[312, 176, 508, 200],
                        enabled=True, leads_to=None),
                Control(type="MenuItem", name="Publish to Web…", rect=[312, 204, 508, 228],
                        enabled=False),
            ],
            ts=TS,
        )
    )

    # state_0024 — the Export-as-PDF dialog (privacy rule: filename redacted)
    session.add_state(
        State(
            state_id="state_0024",
            kind="dialog",
            window_title="Export as PDF",
            breadcrumb=["MainWindow", "File", "Export", "Export as PDF"],
            reached_by=Edge(action="left_click", on="MenuItem:Export as PDF…", from_state="state_0023"),
            screenshot="screenshots/state_0024.png",
            controls=[
                Control(type="Edit", name="File name", rect=[120, 80, 460, 104], value_redacted=True),
                Control(type="ComboBox", name="Quality", rect=[120, 120, 460, 144], leads_to=None),
                Control(type="Button", name="Save", rect=[300, 200, 380, 228], leads_to=None),
                Control(type="Button", name="Cancel", rect=[390, 200, 470, 228], leads_to=None),
            ],
            ts="2026-07-12T14:03:41+05:00",
        )
    )

    return session


def main() -> None:
    session = build()
    out = os.path.join(HERE, "app_map.sample.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(session.to_dict(), fh, indent=2, ensure_ascii=False)
    cov = session.coverage()
    print(f"wrote {out}")
    print(f"states={cov['states']} controls={cov['controls']} unexplored={cov['unexplored']}")


if __name__ == "__main__":
    main()
