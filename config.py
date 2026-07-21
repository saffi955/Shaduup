"""
AppShadow configuration + process-wide setup.

Small on purpose. Phases 1-2 only need the DPI-awareness call and a couple of
knobs; the recorder window and hook listener (later phases) read the rest.
"""

from __future__ import annotations

import ctypes

# --- Timing -----------------------------------------------------------------
DEBOUNCE_MS = 300          # settle window: one click = one state, not twenty
MENU_SNAPSHOT_BUDGET_MS = 150  # a menu-popup tree read must beat this (risk R2)
AUTOSAVE_SECONDS = 60      # flush to disk periodically so a crash loses nothing

# --- Hotkeys (global, work while the target app has focus) ------------------
HOTKEY_CAPTURE = "ctrl+shift+c"
HOTKEY_STOP = "ctrl+shift+x"

# --- Output -----------------------------------------------------------------
OUTPUT_DIR = "sessions"    # per-session folders are created under here

# --- Redaction --------------------------------------------------------------
# Any control whose type is in here is recorded as existing, never with its
# value. The privacy rule from the blueprint: the field exists in the map, its
# typed content never does.
REDACT_TYPES: frozenset[str] = frozenset({"Edit", "Document", "Password"})


def set_dpi_awareness() -> str:
    """Make this process per-monitor DPI aware BEFORE any UI/UIA work.

    Without this, every bounding rect UIA returns is silently wrong on a scaled
    display (risk R4). Must run before the first window is created or the first
    UIA call is made. Returns a short human-readable status string; never
    raises, because a missing shcore on very old Windows must not crash startup.
    """
    try:
        # 2 == PROCESS_PER_MONITOR_DPI_AWARE
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return "per-monitor-v1"
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
            return "system"
        except Exception:
            return "none"
