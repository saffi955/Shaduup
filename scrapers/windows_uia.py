"""
Windows UIA scraper — Phase 2 deliverable.

Given a target process (or a specific window handle), walk the *currently
visible* UI Automation control tree and return one core ``State``. This is the
"prove it on the real target" step: no event hooks, no debounce, no screenshots
yet — just a fast, honest snapshot of what UIA can see.

Speed matters. A menu popup can vanish in a fraction of a second, so instead of
pywinauto's convenient-but-chatty per-element property reads (one cross-process
COM call *per property, per element*), this pulls the whole subtree in a single
``FindAllBuildCache`` round-trip with every property we need pre-cached. That is
what lets a menu-scoped read land inside the ~150 ms budget (risk R2).

Run it directly to try it on a real app:

    python -m scrapers.windows_uia --launch "C:\\Program Files (x86)\\InPage 2012\\InPage 2012.exe"
    python -m scrapers.windows_uia --pid 14232
    python -m scrapers.windows_uia --title-re ".*InPage.*"
"""

from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes
from typing import Callable, Optional

from capture.screenshot import window_rect
from config import REDACT_TYPES, set_dpi_awareness
from core.schema import Control, Edge, State
from scrapers.base import Scraper

_kernel32 = ctypes.windll.kernel32

# --- pywinauto / comtypes UIA plumbing --------------------------------------
# Imported lazily-at-module-load so a machine without the deps fails loudly and
# early rather than deep inside a walk.
from pywinauto.uia_defines import IUIA  # noqa: E402

_IUIA = IUIA()
_UIA = _IUIA.iuia          # IUIAutomation
_MOD = _IUIA.UIA_dll       # generated comtypes module holding the UIA_* constants


# Map control-type ids (50000, 50001, ...) to their short names ("Button",
# "MenuItem", ...) once, straight from the generated module.
def _build_control_type_names() -> dict[int, str]:
    names: dict[int, str] = {}
    suffix = "ControlTypeId"
    for attr in dir(_MOD):
        if attr.startswith("UIA_") and attr.endswith(suffix):
            short = attr[len("UIA_") : -len(suffix)]
            names[getattr(_MOD, attr)] = short
    return names


_CONTROL_TYPE_NAMES = _build_control_type_names()

# Properties we cache in the single round-trip. If you read a Cached* accessor
# for a property that isn't in this list, comtypes raises — so the two must stay
# in sync.
_CACHED_PROPERTIES = (
    _MOD.UIA_NamePropertyId,
    _MOD.UIA_ControlTypePropertyId,
    _MOD.UIA_AutomationIdPropertyId,
    _MOD.UIA_BoundingRectanglePropertyId,
    _MOD.UIA_IsEnabledPropertyId,
    _MOD.UIA_IsOffscreenPropertyId,
    _MOD.UIA_AcceleratorKeyPropertyId,
    _MOD.UIA_AccessKeyPropertyId,
    # Presentation and state. These are what turn a list of names into something
    # you could actually rebuild an interface from.
    _MOD.UIA_ClassNamePropertyId,
    _MOD.UIA_HelpTextPropertyId,
    _MOD.UIA_IsPasswordPropertyId,
    _MOD.UIA_ToggleToggleStatePropertyId,
    _MOD.UIA_ExpandCollapseExpandCollapseStatePropertyId,
    _MOD.UIA_SelectionItemIsSelectedPropertyId,
    # Lets the walk notice a separate top-level window (an owned dialog) that
    # UIA nests inside its owner's tree.
    _MOD.UIA_NativeWindowHandlePropertyId,
)

_TREESCOPE_DESCENDANTS = _MOD.TreeScope_Descendants
_TREESCOPE_SUBTREE = _MOD.TreeScope_Subtree

# ToggleState / ExpandCollapseState enum values. Only the first two of each are
# real answers: a control with no toggle reports Indeterminate(2) and one with
# nothing to expand reports LeafNode(3), so those must read as "no such state"
# rather than as a genuine value. Measured on a live ribbon: every one of 93
# elements reported ToggleState 2, which is how we know 2 is the sentinel here.
_TOGGLE_OFF, _TOGGLE_ON = 0, 1
_EXPAND_COLLAPSED, _EXPAND_EXPANDED = 0, 1

# Pattern-backed properties are not exposed as Cached* attributes by the
# generated interface, so they are read by property id instead.
_PROP_TOGGLE = _MOD.UIA_ToggleToggleStatePropertyId
_PROP_EXPAND = _MOD.UIA_ExpandCollapseExpandCollapseStatePropertyId
_PROP_SELECTED = _MOD.UIA_SelectionItemIsSelectedPropertyId

# Title-bar / system-menu controls present on essentially every window. They are
# not app content, so they don't count towards "does this window expose real
# controls to UIA" when deciding whether the screenshot needs vision parsing.
_WINDOW_CHROME_NAMES = frozenset(
    {"Minimize", "Maximize", "Restore", "Close", "System", "Application"}
)

# The subset that is genuinely window management: drawn by Windows, present on
# almost every window, and not a feature of the application being mapped. These
# are flagged `chrome` so the coverage report ignores them — a map that could
# only reach "nothing left to explore" by clicking Close would never reach it.
# "Application" is excluded here: it is the name of the app's own menu bar.
_CHROME_CONTROL_NAMES = frozenset(
    {"Minimize", "Maximize", "Restore", "Close", "System"}
)
_CHROME_TYPES = frozenset({"TitleBar"})


def _make_cache_request(subtree: bool = False):
    """A cache request for one cross-process round trip.

    With ``subtree=True`` the *tree structure* is cached as well as the
    properties, so ``GetCachedChildren()`` walks real parent/child nesting
    without another cross-process call per element. That nesting is what makes
    the map usable for rebuilding a layout: a flat list of controls loses which
    button sits inside which toolbar inside which pane.
    """
    cr = _UIA.CreateCacheRequest()
    for prop in _CACHED_PROPERTIES:
        cr.AddProperty(prop)
    if subtree:
        cr.TreeScope = _TREESCOPE_SUBTREE
    return cr


def _split_accelerator(name: str, accelerator: str) -> tuple[str, str]:
    """Separate a menu label from a tab-embedded accelerator.

    Classic Win32 menus (InPage, and most plain-compiled apps) leave the UIA
    AcceleratorKey property empty and instead pack the shortcut into the label
    after a tab: ``"New...\\tCtrl+N"``. Split it so ``name`` stays clean and the
    ``shortcut`` field is populated the way an app menu should be. Names without
    a tab (e.g. recent-file entries) pass through untouched.

    >>> _split_accelerator("New...\\tCtrl+N", "")
    ('New...', 'Ctrl+N')
    >>> _split_accelerator("Open...", "")
    ('Open...', '')
    >>> _split_accelerator("Paste", "Ctrl+V")   # real property wins, not overwritten
    ('Paste', 'Ctrl+V')
    """
    if "\t" in name:
        label, _, embedded = name.partition("\t")
        if not accelerator:
            accelerator = embedded.strip()
        name = label
    return name.rstrip(), accelerator


# --- Win32 window enumeration (find the target's main hwnd) ------------------
_user32 = ctypes.windll.user32
_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def _pid_of_hwnd(hwnd: int) -> int:
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _win_rect(hwnd: int) -> tuple[int, int, int, int]:
    """(left, top, right, bottom) of a window's visible frame, screen pixels.

    Shared with the screenshot grabber so control rects and the PNG agree on
    where the window's top-left corner is.
    """
    return window_rect(hwnd)


_DWMWA_CLOAKED = 14
_GA_ROOT = 2
_FRAME_HOST_EXE = "applicationframehost.exe"


def _is_cloaked(hwnd: int) -> bool:
    """True for a window DWM is hiding even though it reports itself visible.

    Suspended UWP frames (a Settings window you closed, apps parked in the
    background) stay WS_VISIBLE with a real size but are cloaked, so without
    this check they look like perfectly good candidates for the main window.
    """
    cloaked = wintypes.DWORD()
    try:
        hr = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            wintypes.HWND(hwnd), _DWMWA_CLOAKED, ctypes.byref(cloaked),
            ctypes.sizeof(cloaked),
        )
    except OSError:
        return False
    return hr == 0 and cloaked.value != 0


def process_name(pid: int) -> str:
    """Basename of a process's executable, or '' if it can't be read."""
    import os

    h = _kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if _kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return ""
    finally:
        _kernel32.CloseHandle(h)


def is_frame_host(pid: int) -> bool:
    """Is ``pid`` ApplicationFrameHost, the shared shell around every UWP app?"""
    return process_name(pid).lower() == _FRAME_HOST_EXE


def root_window(hwnd: int) -> int:
    """The top-level window that owns ``hwnd`` (itself if already top-level)."""
    if not hwnd:
        return 0
    return int(_user32.GetAncestor(hwnd, _GA_ROOT) or hwnd)


def window_pids(hwnd: int) -> set[int]:
    """Every process drawing into ``hwnd``: its owner plus any child's owner.

    A Win32 app answers with one pid. A UWP app answers with two: the
    ApplicationFrameHost frame and the real app process (CalculatorApp.exe)
    whose CoreWindow is parented inside it. Events from the content arrive
    under the second pid, so the listener has to hook both.
    """
    pids = {_pid_of_hwnd(hwnd)}

    def _cb(child, _lparam):
        pids.add(_pid_of_hwnd(child))
        return True

    _user32.EnumChildWindows(hwnd, _WNDENUMPROC(_cb), 0)
    pids.discard(0)
    return pids


def content_pid(hwnd: int) -> int:
    """The process that actually *is* the app behind ``hwnd``.

    For a UWP frame that is the hosted app, not ApplicationFrameHost.
    """
    own = _pid_of_hwnd(hwnd)
    if is_frame_host(own):
        for pid in window_pids(hwnd):
            if pid != own:
                return pid
    return own


def _hosts_content(hwnd: int) -> bool:
    """A frame-host window that actually has an app parented inside it."""
    return len(window_pids(hwnd)) > 1


def _window_title(hwnd: int) -> str:
    length = _user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _on_screen(hwnd: int) -> bool:
    """Visible, not cloaked, not minimised: a window the user can actually see."""
    return bool(
        _user32.IsWindowVisible(hwnd)
        and not _user32.IsIconic(hwnd)
        and not _is_cloaked(hwnd)
    )


def top_level_windows(pid: int) -> list[int]:
    """On-screen, titled, top-level window handles owned by ``pid``."""
    found: list[int] = []

    def _cb(hwnd, _lparam):
        if _pid_of_hwnd(hwnd) == pid and _window_title(hwnd) and _on_screen(hwnd):
            found.append(hwnd)
        return True

    _user32.EnumWindows(_WNDENUMPROC(_cb), 0)
    return found


def main_hwnd_for_pid(pid: int) -> Optional[int]:
    """Best guess at the app's primary window.

    The foreground window wins when it belongs to ``pid``, because that is the
    one the user is looking at; otherwise the largest on-screen one. Cloaked
    and minimised windows never qualify. For ApplicationFrameHost, which owns
    the frame of *every* running UWP app, only frames that really host an app
    count, so a suspended Settings frame can't be mistaken for Calculator. Even
    then the pid alone can't say which UWP app is meant, so callers that know
    the window (the crosshair pick, a title match) should pass it directly.
    """
    wins = top_level_windows(pid)
    if is_frame_host(pid):
        wins = [w for w in wins if _hosts_content(w)]
    if not wins:
        return None

    fg = int(_user32.GetForegroundWindow() or 0)
    if fg in wins:
        return fg

    def _area(hwnd: int) -> int:
        left, top, right, bottom = _win_rect(hwnd)
        return max(0, right - left) * max(0, bottom - top)

    return max(wins, key=_area)


def find_hwnd_by_title(title_re: str) -> Optional[int]:
    """First on-screen top-level window whose title matches ``title_re``."""
    import re

    pattern = re.compile(title_re)
    match: list[int] = []

    def _cb(hwnd, _lparam):
        if _on_screen(hwnd):
            t = _window_title(hwnd)
            if t and pattern.search(t):
                match.append(hwnd)
                return False
        return True

    _user32.EnumWindows(_WNDENUMPROC(_cb), 0)
    return match[0] if match else None


# --- The scraper ------------------------------------------------------------
class WindowsUIAScraper(Scraper):
    """UIA-backed capture backend for Windows desktop apps."""

    def __init__(self) -> None:
        self.pid: Optional[int] = None
        self._cache = _make_cache_request()
        self._subtree_cache = _make_cache_request(subtree=True)

    def attach(self, pid: int) -> None:
        self.pid = pid

    # -- helpers --------------------------------------------------------------
    @staticmethod
    def _control_type_name(type_id: int) -> str:
        return _CONTROL_TYPE_NAMES.get(type_id, f"Custom({type_id})")

    @staticmethod
    def _cached(el, attr, default=None):
        """Read a cached property, tolerating providers that omit it."""
        try:
            return getattr(el, attr)
        except Exception:
            return default

    @staticmethod
    def _cached_prop(el, prop_id, default=None):
        """Read a cached property by id, for the pattern-backed ones."""
        try:
            return el.GetCachedPropertyValue(prop_id)
        except Exception:
            return default

    def _element_to_control(
        self, el, depth: int = 0, origin: tuple[int, int] = (0, 0)
    ) -> Optional[Control]:
        """Turn one cached UIA element into a Control, or None to drop it.

        Reads only cached properties (no fresh cross-process calls) and never
        touches the Value pattern, so a typed-in string is never even fetched,
        let alone stored. ``origin`` is the window's top-left corner, used to
        turn screen coordinates into window-relative position and size.
        """
        try:
            if el.CachedIsOffscreen:
                return None
            rect = el.CachedBoundingRectangle  # tagRECT: left/top/right/bottom
            left, top = int(rect.left), int(rect.top)
            right, bottom = int(rect.right), int(rect.bottom)
            if (left, top, right, bottom) == (0, 0, 0, 0):
                return None  # non-visual element, nothing to point at
            r = [left - origin[0], top - origin[1], right - left, bottom - top]

            type_name = self._control_type_name(el.CachedControlType)
            name, accelerator = _split_accelerator(
                el.CachedName or "", el.CachedAcceleratorKey or ""
            )
            automation_id = el.CachedAutomationId or ""
            enabled = bool(el.CachedIsEnabled)
            class_name = self._cached(el, "CachedClassName", "") or ""
            help_text = self._cached(el, "CachedHelpText", "") or ""

            # A password field is redacted whatever its control type claims.
            redacted = type_name in REDACT_TYPES or bool(
                self._cached(el, "CachedIsPassword", False)
            )

            toggle = self._cached_prop(el, _PROP_TOGGLE)
            checked = (
                toggle == _TOGGLE_ON
                if toggle in (_TOGGLE_OFF, _TOGGLE_ON)
                else None
            )
            expand = self._cached_prop(el, _PROP_EXPAND)
            expanded = (
                expand == _EXPAND_EXPANDED
                if expand in (_EXPAND_COLLAPSED, _EXPAND_EXPANDED)
                else None
            )
            # Recorded only when true. Nearly every element reports "not
            # selected", so writing that out would add a false field to almost
            # every control in the map to say nothing.
            selected = True if self._cached_prop(el, _PROP_SELECTED) is True else None

            # Drop nameless, non-actionable noise (separators, blank panes) to
            # keep the map lean — but always keep redacted fields (a nameless
            # Edit still matters) and anything carrying an id or a tooltip.
            if not name and not automation_id and not redacted and not help_text:
                if type_name in ("Pane", "Group", "Separator", "Text", "Image", "Custom"):
                    return None

            return Control(
                type=type_name,
                name=name,
                rect=r,
                enabled=enabled,
                # UWP frames name these "Minimize Calculator" etc., but keep
                # the plain word as the automation id, so check both.
                chrome=type_name in _CHROME_TYPES
                or name in _CHROME_CONTROL_NAMES
                or automation_id in _CHROME_CONTROL_NAMES,
                automation_id=automation_id or None,
                shortcut=accelerator or None,
                value_redacted=redacted,
                leads_to=None,  # edges are wired by the session graph (Phase 3)
                depth=depth,
                class_name=class_name or None,
                help_text=help_text or None,
                checked=checked,
                expanded=expanded,
                selected=selected,
            )
        except Exception:
            # A control that vanished mid-walk (menus do this) — just skip it.
            return None

    def _walk(
        self,
        root_element,
        max_controls: int,
        origin: tuple[int, int] = (0, 0),
        root_hwnd: int = 0,
    ) -> list[Control]:
        """Walk the cached subtree, preserving real parent/child nesting.

        The whole subtree came back in one round trip, so descending it costs
        nothing extra. Controls are emitted depth-first in document order, each
        carrying its nesting level, which is enough to rebuild the tree exactly.
        """
        controls: list[Control] = []

        def descend(el, depth: int) -> None:
            if len(controls) >= max_controls:
                return
            try:
                kids = el.GetCachedChildren()
            except Exception:
                return
            if kids is None:
                return  # a leaf: providers return NULL rather than an empty array
            try:
                count = kids.Length
            except Exception:
                return
            for i in range(count):
                if len(controls) >= max_controls:
                    return
                try:
                    child = kids.GetElement(i)
                except Exception:
                    continue
                if self._is_other_top_level(child, root_hwnd):
                    continue
                ctrl = self._element_to_control(child, depth, origin)
                # A dropped container must not orphan its children, so descend
                # at the same level rather than one deeper.
                if ctrl is not None:
                    controls.append(ctrl)
                    descend(child, depth + 1)
                else:
                    descend(child, depth)

        descend(root_element, 0)
        return controls

    def _is_other_top_level(self, el, root_hwnd: int) -> bool:
        """Is ``el`` a separate top-level window nested under the one walked?

        UIA lists a window's owned dialogs (Notepad's Font dialog) as its
        children, so without this the main window's state swallows the dialog's
        controls and turns into a new, wrong node while the dialog is open. The
        dialog gets its own capture. A UWP app's CoreWindow is a *child* window
        of its frame, not top-level, so it is kept.
        """
        if not root_hwnd:
            return False
        h = self._cached(el, "CachedNativeWindowHandle", 0) or 0
        return bool(h) and h != root_hwnd and root_window(h) == h

    # -- public API -----------------------------------------------------------
    def snapshot(
        self,
        state_id: str,
        *,
        kind: str = "window",
        reached_by: Optional[Edge] = None,
        hwnd: Optional[int] = None,
        max_controls: int = 600,
        vision_threshold: int = 3,
    ) -> State:
        """Capture one State from the target.

        ``hwnd`` scopes the walk to a specific window — pass the popup's handle
        for a menu so the subtree (and the time) stays tiny. With no ``hwnd``,
        the target process's main window is used.
        """
        if hwnd is None:
            if self.pid is None:
                raise RuntimeError("attach(pid) or pass hwnd= before snapshot()")
            hwnd = main_hwnd_for_pid(self.pid)
            if hwnd is None:
                raise RuntimeError(f"no visible top-level window for pid {self.pid}")

        root = _UIA.ElementFromHandle(hwnd)
        window_title = ""
        try:
            window_title = root.CurrentName or ""
        except Exception:
            window_title = _window_title(hwnd)

        # The window box, in absolute screen pixels. Control geometry is stored
        # relative to its top-left corner, so this is what makes absolute
        # positions recoverable without baking the monitor layout into the map.
        wl, wt, wr, wb = _win_rect(hwnd)
        window_rect = [wl, wt, wr - wl, wb - wt]

        # One round trip brings back the properties *and* the tree structure.
        cached_root = root.BuildUpdatedCache(self._subtree_cache)
        controls = self._walk(cached_root, max_controls, origin=(wl, wt), root_hwnd=hwnd)

        # Owner-drawn heuristic (risk R1). A plain-compiled app like InPage
        # exposes its menus and window chrome to UIA but draws its toolbars,
        # rulers and canvas itself — they surface only as nameless container
        # Panes. So a window is flagged for vision when it has almost no
        # *app-level* actionable control (menu items and the min/max/close
        # chrome don't count) yet several blank container panes. Menus are
        # exempt: a popup legitimately has few, unnamed-container-free items.
        app_actionable = [
            c
            for c in controls
            if c.is_interactive()
            and c.name
            and c.type != "MenuItem"
            and c.name not in _WINDOW_CHROME_NAMES
        ]
        blank_containers = [
            c for c in controls if c.type in ("Pane", "Custom", "Group") and not c.name
        ]
        needs_vision = (
            kind == "window"
            and len(app_actionable) < vision_threshold
            and len(blank_containers) >= 3
        )

        return State(
            state_id=state_id,
            kind=kind,
            window_title=window_title,
            reached_by=reached_by,
            window_rect=window_rect,
            controls=controls,
            needs_vision=needs_vision,
        )


# --- WinEvent hook listener (Phase 3, step 9) -------------------------------
# The events that mean "the UI just changed to something worth capturing".
EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_SYSTEM_MENUSTART = 0x0004
EVENT_SYSTEM_MENUPOPUPSTART = 0x0006
EVENT_SYSTEM_DIALOGSTART = 0x0010
EVENT_OBJECT_SHOW = 0x8002

# In-place changes: the window stays put but what it shows changes (a
# Calculator key press, a toggled option, a pane switched inside a UWP app).
# None of the events above fire for those, so without these nothing past the
# first screen of a modern app would ever be captured. LOCATIONCHANGE (0x800B)
# sits inside this range but is deliberately skipped: it fires on every caret
# and mouse move and says nothing about content.
EVENT_OBJECT_FOCUS = 0x8005
EVENT_OBJECT_SELECTION = 0x8006
EVENT_OBJECT_STATECHANGE = 0x800A
EVENT_OBJECT_NAMECHANGE = 0x800C
EVENT_OBJECT_VALUECHANGE = 0x800E
EVENT_OBJECT_INVOKED = 0x8013
IN_PLACE_EVENTS = frozenset(
    {
        EVENT_OBJECT_FOCUS,
        EVENT_OBJECT_SELECTION,
        EVENT_OBJECT_STATECHANGE,
        EVENT_OBJECT_NAMECHANGE,
        EVENT_OBJECT_VALUECHANGE,
        EVENT_OBJECT_INVOKED,
    }
)

WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002
_WM_QUIT = 0x0012
_OBJID_WINDOW = 0  # a whole-window show, not a child-object show

# void CALLBACK WinEventProc(HWINEVENTHOOK, DWORD event, HWND, LONG idObject,
#                            LONG idChild, DWORD idEventThread, DWORD dwmsTime)
_WINEVENTPROC = ctypes.WINFUNCTYPE(
    None,
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.HWND,
    wintypes.LONG,
    wintypes.LONG,
    wintypes.DWORD,
    wintypes.DWORD,
)


_WH_MOUSE_LL = 14
_WM_LBUTTONUP = 0x0202
_LRESULT = ctypes.c_ssize_t


class _MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", wintypes.POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


_LOWLEVELMOUSEPROC = ctypes.WINFUNCTYPE(
    _LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
)
_user32.SetWindowsHookExW.argtypes = [
    ctypes.c_int, _LOWLEVELMOUSEPROC, wintypes.HINSTANCE, wintypes.DWORD
]
_user32.SetWindowsHookExW.restype = wintypes.HANDLE
_user32.CallNextHookEx.argtypes = [
    wintypes.HANDLE, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
]
_user32.CallNextHookEx.restype = _LRESULT
_user32.UnhookWindowsHookEx.argtypes = [wintypes.HANDLE]
_kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
_kernel32.GetModuleHandleW.restype = wintypes.HMODULE


class WinEventListener:
    """Subscribe to the target app's UI-change events, off the main thread.

    ``SetWinEventHook`` with ``OUTOFCONTEXT`` only delivers callbacks while the
    subscribing thread is pumping messages, so the hook lives on its own thread
    running a ``GetMessage`` loop. Each relevant event is handed to ``on_event``
    (which must be cheap and do no COM work — it just notes what changed and
    kicks the debounce timer). The hooks are filtered to the app's processes at
    the OS level, so unrelated apps generate no traffic here at all. A UWP app
    spans two processes (its frame host and the app itself), hence ``pids``.

    With ``on_click`` set, a low-level mouse hook on the same thread reports
    where each left click landed, so the recorder can tell *which* control a
    transition came from instead of guessing from window titles.
    """

    def __init__(
        self,
        pids,
        on_event: Callable[[int, int, int], None],
        on_click: Optional[Callable[[int, int], None]] = None,
    ) -> None:
        self.pids = sorted({pids} if isinstance(pids, int) else set(pids))
        self.on_event = on_event
        self.on_click = on_click
        self._proc = _WINEVENTPROC(self._callback)  # keep a ref, or it is GC'd
        self._mouse_proc = _LOWLEVELMOUSEPROC(self._mouse_callback)
        self._mouse_hook = None
        self._thread: Optional[threading.Thread] = None
        self._thread_id: int = 0
        self._hooks: list[int] = []
        self._ready = threading.Event()

    def _callback(self, _hook, event, hwnd, id_object, _id_child, _tid, _time) -> None:
        try:
            self.on_event(int(event), int(hwnd) if hwnd else 0, int(id_object))
        except Exception:
            pass  # a listener must never die on one bad event

    def _mouse_callback(self, n_code, w_param, l_param):
        # Low-level hooks stall *all* mouse input until they return, so this
        # only copies the point out and passes the event straight on.
        if n_code >= 0 and w_param == _WM_LBUTTONUP and self.on_click is not None:
            try:
                info = ctypes.cast(l_param, ctypes.POINTER(_MSLLHOOKSTRUCT)).contents
                self.on_click(int(info.pt.x), int(info.pt.y))
            except Exception:
                pass
        return _user32.CallNextHookEx(None, n_code, w_param, l_param)

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="appshadow-winevent", daemon=True
        )
        self._thread.start()
        self._ready.wait(2.0)

    def _run(self) -> None:
        self._thread_id = _kernel32.GetCurrentThreadId()
        flags = WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS
        # One contiguous range covers foreground/menu/dialog; object-show and
        # the in-place events are far up the range, so they get their own
        # hooks (split around LOCATIONCHANGE, which is pure noise).
        ranges = (
            (EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_DIALOGSTART),
            (EVENT_OBJECT_SHOW, EVENT_OBJECT_SHOW),
            (EVENT_OBJECT_FOCUS, EVENT_OBJECT_STATECHANGE),
            (EVENT_OBJECT_NAMECHANGE, EVENT_OBJECT_VALUECHANGE),
            (EVENT_OBJECT_INVOKED, EVENT_OBJECT_INVOKED),
        )
        for pid in self.pids:
            for emin, emax in ranges:
                h = _user32.SetWinEventHook(emin, emax, 0, self._proc, pid, 0, flags)
                if h:
                    self._hooks.append(h)
        if self.on_click is not None:
            self._mouse_hook = _user32.SetWindowsHookExW(
                _WH_MOUSE_LL, self._mouse_proc, _kernel32.GetModuleHandleW(None), 0
            )
        self._ready.set()

        msg = wintypes.MSG()
        while _user32.GetMessageW(ctypes.byref(msg), 0, 0, 0) > 0:
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))

        for h in self._hooks:
            _user32.UnhookWinEvent(h)
        self._hooks.clear()
        if self._mouse_hook:
            _user32.UnhookWindowsHookEx(self._mouse_hook)
            self._mouse_hook = None

    def stop(self) -> None:
        if self._thread_id:
            _user32.PostThreadMessageW(self._thread_id, _WM_QUIT, 0, 0)
        if self._thread is not None:
            self._thread.join(2.0)
            self._thread = None


# --- CLI: quick Phase 2 probe -----------------------------------------------
def _launch_and_wait(path: str, settle_s: float = 3.0) -> int:
    import subprocess
    import time

    proc = subprocess.Popen([path])
    time.sleep(settle_s)  # let the splash/main window come up
    return proc.pid


def launch_app(path: str, settle_s: float = 3.0) -> tuple[int, Optional[int]]:
    """Launch ``path`` and return ``(pid, hwnd)`` of the window it opened.

    Some launchers are only stubs: ``calc.exe`` starts CalculatorApp.exe inside
    ApplicationFrameHost and exits at once, so its own pid never owns a window.
    When that happens the window that just came to the foreground is taken
    instead, which is the app the stub opened.
    """
    pid = _launch_and_wait(path, settle_s)
    hwnd = main_hwnd_for_pid(pid)
    if hwnd is None:
        fg = int(_user32.GetForegroundWindow() or 0)
        if fg:
            hwnd = fg
            pid = _pid_of_hwnd(fg)
    return pid, hwnd


def _main(argv: Optional[list[str]] = None) -> int:
    import argparse
    import json
    import time

    parser = argparse.ArgumentParser(description="AppShadow Windows UIA snapshot probe")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--pid", type=int, help="attach to an already-running process id")
    src.add_argument("--launch", metavar="EXE", help="launch this exe, then snapshot")
    src.add_argument("--title-re", metavar="REGEX", help="find a window by title regex")
    parser.add_argument("--max-controls", type=int, default=600)
    parser.add_argument("--repeat", type=int, default=3, help="timing samples to take")
    args = parser.parse_args(argv)

    dpi = set_dpi_awareness()
    print(f"[dpi] awareness = {dpi}")

    hwnd: Optional[int] = None
    pid: Optional[int] = None

    if args.launch:
        pid, hwnd = launch_app(args.launch)
        print(f"[launch] pid = {pid}  hwnd = {hwnd}")
    elif args.pid:
        pid = args.pid
    elif args.title_re:
        hwnd = find_hwnd_by_title(args.title_re)
        if hwnd is None:
            print(f"[error] no window matched /{args.title_re}/")
            return 2
        pid = _pid_of_hwnd(hwnd)
        print(f"[find] hwnd = {hwnd}  pid = {pid}")

    scraper = WindowsUIAScraper()
    if pid is not None:
        scraper.attach(pid)

    # Timing: take a few samples so the number is honest, not a cold-cache fluke.
    timings: list[float] = []
    state: Optional[State] = None
    for i in range(max(1, args.repeat)):
        t0 = time.perf_counter()
        state = scraper.snapshot(
            f"state_{i:04d}", hwnd=hwnd, max_controls=args.max_controls
        )
        timings.append((time.perf_counter() - t0) * 1000.0)

    assert state is not None
    doc = state.to_dict()
    print(json.dumps(doc, indent=2, ensure_ascii=False))

    print("\n--- summary ---")
    print(f"window_title : {state.window_title!r}")
    print(f"controls     : {len(state.controls)}")
    print(f"unexplored   : {len(state.unexplored())}")
    print(f"needs_vision : {state.needs_vision}")
    print(
        "snapshot ms  : "
        + ", ".join(f"{t:.1f}" for t in timings)
        + f"   (best {min(timings):.1f} ms)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
