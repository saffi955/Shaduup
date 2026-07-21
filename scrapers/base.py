"""
The scraper seam — the one abstraction that keeps AppShadow cross-platform.

Every platform backend (Windows UIA today; web DOM, macOS AX, Android a11y
later) subclasses ``Scraper`` and returns core ``State`` objects. The core
(schema, session graph, exporter) never imports a backend directly, so adding a
platform is: write one subclass, change one factory line. Nothing above this
line knows what OS it is running on.
"""

from __future__ import annotations

import abc
from typing import Optional

from core.schema import Edge, State


class Scraper(abc.ABC):
    """Abstract capture backend: turn a live app's current UI into a State."""

    @abc.abstractmethod
    def attach(self, pid: int) -> None:
        """Bind this scraper to a target process id."""

    @abc.abstractmethod
    def snapshot(
        self,
        state_id: str,
        *,
        kind: str = "window",
        reached_by: Optional[Edge] = None,
    ) -> State:
        """Walk the target's *currently visible* UI and return one State node.

        Implementations must be fast enough for transient UI: a menu popup can
        vanish in well under a second, so a menu-scoped read has to finish
        inside ``MENU_SNAPSHOT_BUDGET_MS`` (risk R2).
        """
