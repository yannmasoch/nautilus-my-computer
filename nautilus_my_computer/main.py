"""Application state and Nautilus integration: MyComputerExtension itself.

This is the piece nautilus-my-computer.py (the hyphenated entry point Nautilus
loads directly) imports and re-exports. Everything else in this package is
stateless or takes `ext` as a parameter; this module is the one place that
holds GSettings handles, per-window state, and module-level caches.
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
import threading
import weakref

import gi

gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
gi.require_version("GObject", "2.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Nautilus, Pango

from nautilus_my_computer import (
    bookmarks,
    column_view,
    file_view_menu,
    location_filter,
    my_computer_view,
    nautilus_prefs,
    preferred_folders,
)
from nautilus_my_computer.common import (
    _,
    _all_widgets,
    _find_widget,
    _icon_name_renders,
    _log,
    _native,
    _pin_icon,
    _resolve_gtype,
    slot_view_owner,
)
from nautilus_my_computer.context_menu import (
    ContextMenu,
    ContextMenuItem,
    ContextMenuSection,
    open_section,
)
from nautilus_my_computer.my_computer_view import _GROUP_SPEC, DISKS_URI, VIEW_DISKINFO
from nautilus_my_computer.preferred_folders import PreferredFolder


# ── Per-site injection toggles (debugging) ────────────────────────────────────
# We catch/inject into Nautilus at four independent sites. Each flag gates EVERY
# entry point for that site so a site can be fully isolated while debugging the
# Nautilus templates-menu use-after-free (crash on navigation with non-empty
# ~/Templates). Set to False to disable that site entirely. Env override:
# e.g. MC_MAIN_VIEW=0. Default all on.
def _flag(name: str, default: bool = True) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.lower() in ("1", "true", "yes", "on")


def _sidebar_mode(native_enabled_default: bool) -> str:
    # We inject only the Computer row into Nautilus's own native listbox and
    # leave every other place native (hiding rows the user toggled off). The old
    # inner/outer-wrapper modes that rebuilt a mimic places group are retired.
    mode = (os.environ.get("MC_SIDEBAR_MODE") or "").strip().lower()
    if mode in ("native-list", "native-list-bottom"):
        return mode
    return "native-list"


DEBUG_MAIN_VIEW_ACTIVE = _flag("MC_MAIN_VIEW")  # main view: Computer panel injected per-slot
DEBUG_COMPUTER_BUTTON_ACTIVE = _flag("MC_COMPUTER_BUTTON")  # left sidebar: "Computer" row injection
DEBUG_NATIVE_SIDEBAR_ACTIVE = _flag("MC_NATIVE_SIDEBAR")  # native sidebar row, set 0 for fallback
DEBUG_SIDEBAR_MODE = _sidebar_mode(
    DEBUG_NATIVE_SIDEBAR_ACTIVE
)  # inner-wrapper default | native-list | native-list-bottom | outer-wrapper
DEBUG_PATHBAR_ACTIVE = _flag("MC_PATHBAR")  # top URL bar: chip icon pinning
DEBUG_SORT_WATCH_ACTIVE = _flag("MC_SORT_WATCH")  # top view-mode/sort buttons: sort metadata watch
DEBUG_LOCATION_FILTER_ACTIVE = _flag("MC_LOCATION_FILTER")  # address bar: card filter watch
DEBUG_SELFTEST = _flag("MC_SELFTEST", default=False)  # in-process navigation self-test driver
DETACH_SETTINGS_WINDOW = False  # testing toggle: True opens settings as a standalone window

# ── Extension metadata (keep in sync with pyproject.toml) ────────────────────
EXT_NAME = "My Computer for Nautilus"
EXT_VERSION = "0.13.1"
EXT_AUTHOR = "Yann Masoch"
EXT_LICENSE = "MIT"
EXT_GITHUB = "https://github.com/yannmasoch/nautilus-my-computer"


DISKS_URI = "computer:///"  # noqa: F811 -- intentional local redefinition, see CLAUDE.md merge log
_DISKS_FILE = Gio.File.new_for_uri(DISKS_URI)
# Edit this list to control which Nautilus locations do not offer Miller View.
# Keep the Network overview here, rather than mounted remote shares, which are
# ordinary browsable folders and should remain supported.
MILLER_VIEW_UNAVAILABLE_URIS = [
    DISKS_URI,
    "recent:///",
    "starred:///",
    "x-network-view:///",
    "trash:///",
]
COMPUTER_LABEL = _native("Computer")
COMPUTER_ICON = "computer-symbolic"  # icon used in sidebar and path bar
MENU_ITEM_LABEL = _("My Computer Settings")
PREFS_WIN_TITLE = _("My Computer Settings")
SCHEMA_ID = "io.github.yannmasoch.nautilus-my-computer"

# VIEW_DISKINFO (imported above; VIEW_FILES is my_computer_view-internal) is a
# per-slot "visible_view" token: whether a slot's own GtkStack currently
# shows our panel.
# Column View doesn't participate in visible_view (issue #118): it lives on
# the same per-slot GtkStack, tracked via column_view.slot_is_showing_column().

DBUS_FILE_MANAGER = "org.freedesktop.FileManager1"
DBUS_PATH_FILE_MANAGER = "/org/freedesktop/FileManager1"

# All updates are event-driven (VolumeMonitor signals, /proc/mounts POLLPRI,
# GSettings changed, Gio.FileMonitor, Gtk.Application window-added). The values
# below are one-shot retry/debounce intervals, not continuous poll periods.
_REFRESH_DEBOUNCE_MS = 300  # coalesce rapid mount/unmount/plug events
_WIN_INIT_RETRY_MS = 20  # retry interval while waiting for NautilusWindow widget tree
_WIN_INIT_MAX_ATTEMPTS = 100  # ~2 s budget waiting for the first view load to settle
_NAV_RETRY_MS = 60  # retry interval while navigating to computer:///
_TAB_WAIT_MS = 50  # retry interval while waiting for a new tab slot
_USAGE_GATE_MS = 1000  # idle cadence: try a statvfs sweep this often, skip while disk is busy
_USAGE_POLL_FAST_MS = 250  # fast cadence while writes are buffered (Dirty+Writeback elevated)
_USAGE_BUSY_RATIO = (
    0.50  # io_ticks delta / interval above this == disk busy → skip statvfs (avoid I/O contention)
)

_DIRTY_ACTIVE_THRESHOLD = (
    4 * 1000 * 1000
)  # /proc/meminfo Dirty+Writeback ≥ this → poll fast (above resting journal noise ~1–2 MB)
_USAGE_POLL_NETWORK_MS = 5000  # async D-Bus usage poll interval for GVfs/network mounts
_STALE_RELEASE_FRAMES = 2  # keep detached panel generations alive across this many frame ticks


# Resolve the display name Nautilus shows in the title bar when at DISKS_URI,
# so panel detection works regardless of which URI is configured.
try:
    _info = Gio.File.new_for_uri(DISKS_URI).query_info(
        "standard::display-name", Gio.FileQueryInfoFlags.NONE, None
    )
    _LOCATION_TITLE = _info.get_display_name()
except Exception:
    _LOCATION_TITLE = COMPUTER_LABEL

# Localized title Nautilus shows when browsing the user's home folder.
# Used to distinguish a "default new window" (opened at Home) from a window
# that was explicitly opened to a specific folder.
_HOME_TITLE: str = GLib.dgettext("nautilus", "Home")

# Transient title Nautilus shows while a location is still loading. Treated as
# "window not settled yet" so it never consumes the start-on-computer one-shot.
_LOADING_TITLE: str = GLib.dgettext("nautilus", "Loading…")


def _is_unsettled_title(title: str) -> bool:
    """True while the window hasn't resolved to a real location yet."""
    return not title or title == _LOADING_TITLE


REAL_FSTYPES = {
    "ext4",
    "ext3",
    "ext2",
    "xfs",
    "btrfs",
    "f2fs",
    "ntfs",
    "ntfs3",
    "vfat",
    "exfat",
    "zfs",
    "reiserfs",
    "apfs",
    "erofs",
    "fuseblk",
}

NETWORK_FSTYPES = {
    "nfs",
    "nfs4",
    "cifs",
    "smb",
    "smb2",
    "smbfs",
    "fuse",
    "fuse.sshfs",
    "fuse.rclone",
    "fuse.s3fs",
    "fuse.davfs2",
    "davfs",
    "sshfs",
    "ftpfs",
    "gvfsd-fuse",
}

OPTICAL_FSTYPES = {"iso9660", "udf"}

# Mountpoint prefixes that indicate removable / external media
EXTERNAL_PREFIXES = ("/media/", "/run/media/", "/mnt/")

# Sidebar place URIs that never accept a file drop, mirroring Nautilus'
# check_valid_drop_target (recent:/// is hardcoded invalid) and its
# drag_open_exclusion_list. Used to grey native rows during a drag when the
# pointer is over our own listbox (Nautilus only dims its own rows while its
# list_box is hovered).
_SIDEBAR_DROP_EXCLUDED_URIS = frozenset(
    {
        "recent:///",
        "starred:///",
        DISKS_URI,
        "x-network-view:///",
    }
)


@dataclasses.dataclass
class PlaceEntry:
    """Describes one fixed sidebar place (Computer, Home, Recent, Starred, Network, Trash)."""

    name: str  # internal key ("computer", "home", ...)
    position: int  # visual order (0 = top)
    label: str  # display label (translatable)
    icon: str  # themed icon name
    uri: str  # location URI
    visible: bool = True
    tooltip: str = ""
    order_index: int = 0  # passed to NautilusSidebarRow "order-index" property
    menu: object = None  # factory menu(ext, win, entry) -> ContextMenu, or None for no menu
    droppable: bool = False  # accepts file drops (copy/move destination)


def _computer_context_menu(ext, win, entry: PlaceEntry) -> ContextMenu:
    """Computer row: open actions + settings, with Open greyed out when already shown."""
    uri = entry.uri
    panel_state = ext._active_panel_state(win)
    on_computer = panel_state is not None and panel_state.get("visible_view") == VIEW_DISKINFO
    return ContextMenu(
        [
            open_section(
                lambda: ext._do_open(uri, win),
                open_tab_action=lambda: ext._do_open_tab(uri, win, make_active=False),
                open_window_action=lambda: ext._do_open_window(uri),
                open_enabled=not on_computer,
                submenu=False,
                shortcuts=False,
            ),
            ContextMenuSection(
                [ContextMenuItem(MENU_ITEM_LABEL, action=lambda: ext._launch_prefs(win))]
            ),
        ]
    )


# PLACES holds only Computer: the one place we still build our own row for. It
# has no native equivalent, so it cannot be handled like the others below.
PLACES: list[PlaceEntry] = [
    PlaceEntry(
        name="my_computer",
        position=0,
        label=_LOCATION_TITLE,
        icon=COMPUTER_ICON,
        uri=DISKS_URI,
        tooltip=_("Open My Computer"),
        order_index=0,
        menu=_computer_context_menu,
    ),
]

# NATIVE_PLACES describes places that stay fully NATIVE - we never build rows for
# them. Only `name` (maps to a sidebar-show-* key), `uri` (matches the native row)
# and `label`/`icon` (for the Preferences toggle row) are used, by
# _apply_native_place_visibility and the sidebar-visibility prefs page. `visible`
# is the default on/off state. Kept as PlaceEntry (same structure as PLACES) in
# case a future place needs the full row-building fields again.
NATIVE_PLACES: list[PlaceEntry] = [
    PlaceEntry(
        name="home",
        position=1,
        label=_native("Home"),
        icon="user-home-symbolic",
        uri=GLib.filename_to_uri(GLib.get_home_dir(), None),
    ),
    PlaceEntry(
        name="recent",
        position=2,
        label=_native("Recent"),
        icon="document-open-recent-symbolic",
        uri="recent:///",
        visible=False,
    ),
    PlaceEntry(
        name="starred",
        position=3,
        label=_native("Starred"),
        icon="starred-symbolic",
        uri="starred:///",
        visible=False,
    ),
    PlaceEntry(
        name="network",
        position=4,
        label=_native("Network"),
        icon="network-computer-symbolic",
        uri="x-network-view:///",
        visible=False,
    ),
    PlaceEntry(
        name="trash",
        position=5,
        label=_native("Trash"),
        icon="user-trash-symbolic",
        uri="trash:///",
    ),
]


# Maps each place name to its GSettings key controlling sidebar visibility.
# "my_computer" is intentionally absent -- it is always shown and has no toggle.
_PLACE_VISIBILITY_KEYS: dict[str, str] = {
    "home": "sidebar-show-home",
    "recent": "sidebar-show-recent",
    "starred": "sidebar-show-starred",
    "network": "sidebar-show-network",
    "trash": "sidebar-show-trash",
}


def _place_is_visible(entry: PlaceEntry, gsettings) -> bool:
    """Whether a place should appear in the custom sidebar group.

    Computer is always visible. Every other place is driven by its
    sidebar-show-* GSettings key, falling back to the static default.
    """
    gskey = _PLACE_VISIBILITY_KEYS.get(entry.name)
    if gskey is None:
        return True
    if gsettings is None:
        return entry.visible
    return gsettings.get_boolean(gskey)


# Seam between the separate My Computer listbox and Nautilus' native list
# directly below it. Both carry .navigation-sidebar (theme base padding 6px); the
# + combinator zeroes the touching edges so the two lists read as one column.
# boundary_separator (sidebar_my_computer_boundary_separator) sits between them
# in the widget tree at all times, but GTK's CSS sibling matching skips it for
# "+" purposes while it is hidden (the common case: at least one native place
# still visible) - so BOTH rules below are needed, not just one:
#   - my_computer_listbox + native_listbox: matches while the separator is
#     hidden (GTK treats my_computer_listbox as native_listbox's effective
#     predecessor).
#   - boundary_separator + native_listbox: matches once the separator becomes
#     visible (every native place hidden - see _apply_native_place_visibility),
#     since it then IS the structural predecessor.
# Without the first rule, hiding it back to "some places visible" reopens the
# gap this seam is meant to close.
#
# wrapper also carries .navigation-sidebar (added in code, see
# _inject_separate_computer_row) purely so libadwaita's own
# ".navigation-sidebar > separator { margin: 6px; }" rule (org.gnome.Adwaita
# gtk.css) applies to boundary_separator - the same rule that already spaces
# Nautilus's native row-header separators, so we do not hardcode that margin
# ourselves. wrapper's own top/bottom .navigation-sidebar padding is redundant
# (my_computer_listbox/native_listbox already manage their own) and is zeroed
# below.
_CSS_SIDEBAR = b"""
#sidebar_my_computer_listbox.navigation-sidebar {
    padding-bottom: 0;
}
#sidebar_my_computer_listbox.navigation-sidebar + .navigation-sidebar {
    padding-top: 0;
}
#sidebar_my_computer_boundary_separator + .navigation-sidebar {
    padding-top: 0;
}
#sidebar_my_computer_wrapper.navigation-sidebar {
    padding-top: 0;
    padding-bottom: 0;
}
"""


def _apply_native_place_visibility(
    native_listbox: Gtk.ListBox, gsettings, boundary_separator: Gtk.Separator | None = None
) -> None:
    """Show/hide native sidebar place rows per the user's sidebar-show-* settings.

    We do NOT mimic native rows anymore. Home/Recent/Starred/Network/Trash stay
    fully native (icons, tooltips, context menus, drag-and-drop, trash-full icon -
    all maintained by Nautilus). The only feature we add over them is a per-place
    on/off toggle, which is just selectively hiding the native row:

        sidebar-show-<place> == True  -> native row visible (untouched, native)
        sidebar-show-<place> == False -> native row hidden

    Computer has no native row (we inject our own), so it is not handled here.

    Matched by URI (not position) and applied with `set_visible()` on the row
    widget, so the state follows the row when Nautilus reorders the list (device
    mount/unmount, bookmark add/remove, async populate). A positional nth-child
    CSS rule did NOT survive reorders. We only ever touch rows whose URI is one of
    our places; Nautilus's own placeholder rows (e.g. the empty "Add a new
    bookmark" drop target) are never forced visible.

    Safe to call repeatedly; re-armed on every native list change via
    `observe_children()` items-changed (see _watch_native_list_changes)."""
    # uri -> should-be-visible, for the togglable native places.
    want_visible = {p.uri: _place_is_visible(p, gsettings) for p in NATIVE_PLACES}
    hidden = 0
    any_place_visible = False
    idx = 0
    while (row := native_listbox.get_row_at_index(idx)) is not None:
        try:
            uri = row.get_property("uri")
        except Exception:
            uri = None
        if uri in want_visible:
            visible = want_visible[uri]
            if row.get_visible() != visible:
                row.set_visible(visible)
            if visible:
                any_place_visible = True
            else:
                hidden += 1
        idx += 1
    _log(f"_apply_native_place_visibility: {hidden} native place row(s) hidden by setting")

    # Our "Computer" row lives in its own listbox, stacked above native_listbox
    # in the wrapper - Nautilus's own section-boundary separator (drawn as a
    # row header inside native_listbox, between the native places and
    # Bookmarks/Other Locations) has no idea Computer exists, so it only ever
    # separates native content from native content. That's fine while at
    # least one native place row is visible (the native separator still
    # appears further down, between that place and Bookmarks). But when every
    # togglable native place is hidden, Nautilus's own separator vanishes too
    # (nothing native left above Bookmarks to separate from), leaving Computer
    # touching Bookmarks with no line at all. boundary_separator is a
    # standing Gtk.Separator between the two listboxes in the wrapper that
    # covers exactly that gap; only ever shown in this one case.
    if boundary_separator is not None:
        boundary_separator.set_visible(not any_place_visible)


def _get_gsettings() -> Gio.Settings | None:
    try:
        return Gio.Settings.new(SCHEMA_ID)
    except Exception:
        return None


def _active_slot(win) -> Gtk.Widget | None:
    """The window's active NautilusWindowSlot widget, or None.

    Reads the "active"/"location" GObject properties on demand. No
    persistent signal, no set_child (safe re: issue #11). Prefers the slot
    flagged active so tabs are handled; falls back to the last slot found
    with a resolved location.
    """
    fallback = None
    for w in _all_widgets(win):
        if "Slot" not in type(w).__name__:
            continue
        try:
            loc = w.get_property("location")
        except TypeError:
            continue
        if loc is None:
            continue
        try:
            if w.get_property("active"):
                return w
        except TypeError:
            pass
        fallback = w
    return fallback


def _active_slot_location(win) -> Gio.File | None:
    """The window's active slot's current location, or None. See _active_slot."""
    slot = _active_slot(win)
    return slot.get_property("location") if slot is not None else None


def _is_file_chooser_window(win: Gtk.Window) -> bool:
    """True for NautilusFileChooser — the portal/file-picker window Nautilus
    opens when acting as another app's "open file" dialog. It is an AdwWindow
    subclass, not a NautilusWindow: no window-level "locations-changed"
    signal, no title updates on navigation, but it reuses the same
    OverlaySplitView/ToolbarView spine and carries a single NautilusWindowSlot."""
    return type(win).__name__ == "NautilusFileChooser"


def _is_nautilus_window(win: Gtk.Window) -> bool:
    """Identify a Nautilus application window by layered fallback.

    Tier 0: NautilusFileChooser (file-picker/portal window) — matched explicitly
    Tier 1: buildable_id == 'NautilusWindow'
    Tier 2: class name  == 'NautilusWindow'
    Tier 3: css class      'nautilus-window'
    Tier 4: structural  — contains Adw.OverlaySplitView
    """
    if _is_file_chooser_window(win):
        return True
    bid = win.get_buildable_id() if hasattr(win, "get_buildable_id") else None
    if bid and bid == "NautilusWindow":
        return True
    if type(win).__name__ == "NautilusWindow":
        if bid != "NautilusWindow":
            _log("is_nautilus_window: matched via class_name (buildable_id drift)")
        return True
    if hasattr(win, "has_css_class") and win.has_css_class("nautilus-window"):
        _log("is_nautilus_window: matched via css class (class/id drift)")
        return True
    if any(isinstance(w, Adw.OverlaySplitView) for w in _all_widgets(win)):
        _log("is_nautilus_window: matched via structural navigation (significant drift)")
        return True
    return False


# Window-level keyboard shortcuts this extension owns, dispatched from
# _on_window_key_capture before Nautilus's own type-ahead search gets a
# chance to eat the keystroke. Keyed by (modifiers, keyval), with modifiers
# restricted to _SHORTCUT_MODIFIER_MASK's four accelerator-relevant bits.
# Value is the name of a MyComputerExtension
# method taking just `win`, looked up via getattr and called from here. The
# method's return value decides whether the keypress is consumed: True (or
# None/no explicit return) stops it there, same as if we'd fully claimed the
# shortcut (see _show_column_view); False lets it keep propagating so
# Nautilus's own native handling for the same key still runs (see
# _leave_column_view_for_native_mode, which only piggybacks a side effect
# onto Nautilus's own Ctrl+1/Ctrl+2 without owning the shortcut itself).
# Add new shortcuts here rather than growing _on_window_key_capture with more
# inline keyval checks -- e.g. the planned Miller-columns arrow-key nav.
_SHORTCUT_MODIFIER_MASK = int(
    Gdk.ModifierType.CONTROL_MASK
    | Gdk.ModifierType.SHIFT_MASK
    | Gdk.ModifierType.ALT_MASK
    | Gdk.ModifierType.SUPER_MASK
)
_WINDOW_SHORTCUTS: dict[tuple[int, int], str] = {
    (int(Gdk.ModifierType.CONTROL_MASK), Gdk.KEY_1): "_leave_column_view_for_native_mode",
    (int(Gdk.ModifierType.CONTROL_MASK), Gdk.KEY_2): "_leave_column_view_for_native_mode",
    (int(Gdk.ModifierType.CONTROL_MASK), Gdk.KEY_3): "_show_column_view",
    (int(Gdk.ModifierType.CONTROL_MASK), Gdk.KEY_x): "_cut_column_focused_folder",
    (int(Gdk.ModifierType.CONTROL_MASK), Gdk.KEY_c): "_copy_column_focused_folder",
    (int(Gdk.ModifierType.CONTROL_MASK), Gdk.KEY_v): "_paste_into_column_focused_folder",
    (
        int(Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK),
        Gdk.KEY_N,
    ): "_new_folder_in_column_focused_folder",
    (0, Gdk.KEY_F2): "_rename_column_focused_folder",
    (0, Gdk.KEY_Delete): "_trash_column_focused_folder",
}

# Unmodified keys that must reach the toolbar's own MANAGED GtkShortcutController
# (nautilus-toolbar.c) so it can open the location entry, same as native Nautilus:
# "/" prompts the root location, "~" prompts home. Both are printable with no
# modifier, so without this list the disk panel's type-ahead swallow in
# _on_window_key_capture would eat them first.
_LOCATION_ENTRY_KEYVALS = frozenset(
    {
        Gdk.KEY_slash,
        Gdk.KEY_KP_Divide,
        Gdk.KEY_asciitilde,
        Gdk.KEY_dead_tilde,
    }
)


class MyComputerExtension(GObject.GObject, Nautilus.MenuProvider):
    def __init__(self):
        super().__init__()
        # Maps each NautilusWindow to its per-window chrome state dict (sidebar
        # row, pathbar/sort watches, start_on_computer, native place hiding).
        # The Computer panel itself is per-slot state (slot._mc_computer, see
        # my_computer_view.py's injection machinery, issue #133) -- use
        # _active_panel_state()/_iter_panel_states() to reach it.
        self._windows: dict = {}
        # Weak registry of every slot with an injected panel, so broadcasts
        # (usage updates, folder icon/caption refreshes) don't need to walk
        # every window's whole widget tree to find them. Self-cleaning: a
        # slot drops out once GTK finalizes it and no other Python reference
        # remains (tab close).
        self._panel_slots: weakref.WeakSet = weakref.WeakSet()
        self._polling_started = False
        self._refresh_pending = False  # debounce flag for live-refresh
        self._local_poll_stop: threading.Event | None = None
        self._net_poll_timer_id: int | None = None
        self._net_poll_cancellable: Gio.Cancellable | None = None
        self._folder_refresh_cancellable = Gio.Cancellable()
        self._folder_monitors: dict[str, Gio.FileMonitor] = {}  # keyed by parent dir URI
        # Resolved folder URI -> exact preferred-folders GSettings entry.
        # These differ for portable file://~/… entries.
        self._watched_folder_keys: dict[str, str] = {}
        self._last_selected_folder_uri: str | None = None  # see get_file_items()

        self._sort_column: str = "name"
        self._sort_reverse: bool = False
        self._view_mode: str = "icon-view"
        self._click_policy: str = "double"  # Nautilus "click-policy": 'single' or 'double'
        # Sort is read from per-folder GVfs metadata. There is no usable event
        # for it (the metadata daemon writes via mmap so file monitors never
        # fire, and the GTK4 Python bindings don't expose get_action_group, so
        # we can't subscribe to Nautilus's "view.sort" GAction). We therefore
        # poll — but only while the pointer is over the header bar (where the
        # sort menu lives) and the Computer panel is visible.
        # _sort_hover tracks whether the pointer is currently inside the navbar.
        # The poll arms on enter and disarms on leave, with a short grace period
        # to cover the gap when the pointer moves from the navbar into the sort
        # popover (which is a separate native surface and triggers a leave event).
        self._sort_poll_id = None  # GLib source id while polling, else None
        self._sort_hover = False  # True while pointer is inside the navbar
        self._view_mode_gsettings = None  # Gio.Settings for org.gnome.nautilus.preferences
        # Column View's own settings adapter (view mode/click policy/sort/zoom/hidden
        # files), independent of the disk view's ad hoc fields above. See
        # nautilus_prefs.NautilusPrefs.
        self._nautilus_prefs = nautilus_prefs.NautilusPrefs()
        self._bar_css_provider = Gtk.CssProvider()
        self._bar_css_display = None

        self._gsettings = _get_gsettings()
        if self._gsettings:
            self._start_on_disks: bool = self._gsettings.get_boolean("start-on-disks")
            self._gsettings.connect("changed", self._on_settings_changed)
        else:
            self._start_on_disks = False

        # Adw.StyleManager's "accent-color" property (libadwaita 1.6+) changes
        # when the user picks a new system accent in GNOME Settings. GTK's
        # named color @accent_bg_color is updated in its global symbol table
        # at that point, but already-realized widgets referencing it through
        # our own CssProvider (_apply_bar_color's ".diskinfo-bar block.filled"
        # rule) don't get repainted automatically -- unlike GTK's own themed
        # widgets, which are restyled as part of the same settings-change
        # cascade. Reloading the provider (same CSS text) forces that repaint.
        # Guarded: the property doesn't exist on libadwaita < 1.6.
        style_manager = Adw.StyleManager.get_default()
        if hasattr(style_manager.props, "accent_color"):
            style_manager.connect("notify::accent-color", lambda *_a: self._apply_bar_color())

        my_computer_view.init_data_watchers(self)
        column_view.init_icon_watcher(self)
        GLib.idle_add(self._late_init)

    # ── My Computer view delegation ─────────────────────────────────────────────
    # Thin wrappers so external code (widgets.py, signal connections elsewhere in
    # this file) can keep calling ext._method(...) while the implementation lives
    # in my_computer_view.py. See CLAUDE.md "Project structure".

    def _populate(self, win: Gtk.Window) -> None:
        my_computer_view._populate(self, win)

    def _build_panel(self, win: Gtk.Window) -> tuple:
        return my_computer_view._build_panel(self, win)

    def _apply_bar_color(self) -> None:
        my_computer_view._apply_bar_color(self)

    def _read_sort_metadata(self) -> bool:
        return my_computer_view._read_sort_metadata(self)

    def _attach_sort_button_watch(self, nautilus_win: Gtk.Window) -> None:
        my_computer_view._attach_sort_button_watch(self, nautilus_win)

    def _apply_card_filter(self, nautilus_win: Gtk.Window, query: str) -> None:
        my_computer_view.apply_card_filter(self, nautilus_win, query)

    def _attach_location_filter_watch(self, nautilus_win: Gtk.Window) -> None:
        location_filter.attach_location_filter_watch(self, nautilus_win)

    def _read_view_mode(self) -> None:
        my_computer_view._read_view_mode(self)

    def _watch_view_mode(self) -> None:
        my_computer_view._watch_view_mode(self)

    def _schedule_live_refresh(self) -> None:
        my_computer_view._schedule_live_refresh(self)

    # _repopulate_visible is defined further below; it also refreshes every
    # tab's Column View via column_view.refresh_all_column_views.

    def _ensure_usage_poll_running(self) -> None:
        my_computer_view._ensure_usage_poll_running(self)

    def _stop_usage_poll_if_idle(self) -> None:
        my_computer_view._stop_usage_poll_if_idle(self)

    def _on_window_active_changed(self, win: Gtk.Window) -> None:
        my_computer_view._on_window_active_changed(self, win)

    def _on_card_activated(self, flow_box, child: Gtk.FlowBoxChild, win: Gtk.Window) -> None:
        my_computer_view._on_card_activated(self, flow_box, child, win)

    def _on_flow_selection_changed(self, flow_box: Gtk.FlowBox, win: Gtk.Window) -> None:
        my_computer_view._on_flow_selection_changed(self, flow_box, win)

    def _attach_flow_shortcuts(self, flow_box: Gtk.FlowBox, win: Gtk.Window) -> None:
        my_computer_view._attach_flow_shortcuts(self, flow_box, win)

    def _on_card_pressed(self, gesture, n, x, y, win: Gtk.Window, card: Gtk.Box) -> None:
        my_computer_view._on_card_pressed(self, gesture, n, x, y, win, card)

    # ── Initialisation ────────────────────────────────────────────────────────

    def _late_init(self) -> bool:
        # Catch any windows that already existed before we connected signals.
        self._check_new_windows()

        if not self._polling_started:
            self._polling_started = True
            # Instant detection of new windows via signal (no polling needed).
            app = Gtk.Application.get_default()
            if app:
                app.connect("window-added", self._on_window_added)
            self._read_sort_metadata()
            self._read_view_mode()
            self._watch_view_mode()
            self._nautilus_prefs.refresh_folder_sort(DISKS_URI)
            self._nautilus_prefs.refresh_view_mode()
            self._nautilus_prefs.watch_global(self)

        return False

    def _on_window_added(self, _app, win: Gtk.Window) -> None:
        """Instant handler for new Nautilus windows — defers injection until load settles."""
        self._schedule_window_init(win)

    def _schedule_window_init(self, win: Gtk.Window) -> None:
        """Wait for the window's first view load to settle, then inject on a
        low-priority idle.

        Injecting our Gtk.Overlay reparents the AdwToolbarView content. Doing that
        during Nautilus's files_view_begin_loading races with its templates
        context-menu rebuild: with a non-empty ~/Templates,
        slot_on_templates_menu_changed rebuilds a GtkPopoverMenu whose internal
        GtkStack our tree surgery destabilises, hitting a Nautilus-core
        use-after-free that segfaults on GTK 4.22 / GNOME 50 (GTK_IS_STACK
        assertion → SIGSEGV). Deferring until the load has finished, and running
        the injection at PRIORITY_LOW (after Nautilus's loading idles drain),
        removes the overlap. See issue #4. Empty ~/Templates never triggers it.
        """
        if not _is_nautilus_window(win) or win in self._windows:
            return
        attempts = [0]

        def _try() -> bool:
            if win in self._windows:
                return GLib.SOURCE_REMOVE
            attempts[0] += 1
            # Hold off until the first load has settled (title resolved to a
            # real location, not "Loading…"). Measured: title-settle is the
            # latest of the real readiness signals (tree/mapped/title), lagging
            # by ~20-40ms — typically settling within ~20-65ms of window-added.
            # No fixed floor: PRIORITY_LOW on the injection idle (below) is what
            # actually avoids the issue #4 templates-menu race, not extra delay.
            if _is_unsettled_title(win.get_title() or ""):
                if attempts[0] > _WIN_INIT_MAX_ATTEMPTS:
                    # Window never settled (rare) — inject anyway so the
                    # extension still works; route through the low-prio idle.
                    GLib.idle_add(self._deferred_init_window, win, priority=GLib.PRIORITY_LOW)
                    return GLib.SOURCE_REMOVE
                return GLib.SOURCE_CONTINUE
            GLib.idle_add(self._deferred_init_window, win, priority=GLib.PRIORITY_LOW)
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(_WIN_INIT_RETRY_MS, _try)

    def _deferred_init_window(self, win: Gtk.Window) -> bool:
        """Low-priority idle wrapper around _init_window (always one-shot)."""
        if win not in self._windows and _is_nautilus_window(win):
            self._init_window(win)
        return GLib.SOURCE_REMOVE

    def _init_window(self, win: Gtk.Window) -> bool:
        css = Gtk.CssProvider()
        css.load_from_data(my_computer_view._CSS)
        display = win.get_display()
        Gtk.StyleContext.add_provider_for_display(
            display,
            css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        css_sidebar = Gtk.CssProvider()
        css_sidebar.load_from_data(_CSS_SIDEBAR)
        Gtk.StyleContext.add_provider_for_display(
            display,
            css_sidebar,
            Gtk.STYLE_PROVIDER_PRIORITY_USER + 1,
        )
        if self._bar_css_display is None:
            self._bar_css_display = display
            self._apply_bar_color()

        self._windows[win] = {
            "window": win,
            "native_split_button": None,
            "view_switcher": None,
            "view_options_menu_button": None,
            "header_motion": None,  # Gtk.EventControllerMotion on the header bar
            "location_filter_watch_attached": False,
            # A file-picker dialog should never auto-navigate itself to
            # computer:/// on open - that heuristic is normal-window-only.
            "start_on_computer": self._start_on_disks and not _is_file_chooser_window(win),
            "native_hide_model": None,  # observe_children() model of native listbox
            "native_hide_handler": None,  # items-changed handler id on that model
            "native_hide_pending": False,  # coalesces re-hide bursts into one idle pass
        }

        # Capture-phase key guard on the window: Nautilus's "type to search"
        # type-ahead is hooked above keyboard focus, so neither hiding nor
        # de-focusing the covered file view stops it. A controller at the top
        # of the capture chain sees keystrokes first and swallows plain
        # printable ones while the active slot's panel is shown — so typing
        # doesn't reopen the vanilla computer:/// search. Modified shortcuts
        # (Ctrl/Alt/Super) and control keys (arrows, Tab, Enter, Esc) always
        # pass through.
        key_guard = Gtk.EventControllerKey()
        key_guard.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key_guard.connect("key-pressed", self._on_window_key_capture, win)
        win.add_controller(key_guard)

        win.connect("destroy", self._on_window_destroyed)
        if _is_file_chooser_window(win):
            # No window-level "locations-changed" on this class — watch the
            # slot's own "location" property directly (same ground truth
            # _window_is_at_disks() reads, symmetric with normal windows).
            for w in _all_widgets(win):
                if "Slot" in type(w).__name__:
                    w.connect(
                        "notify::location",
                        lambda _slot, _pspec, w=win: self._on_navigation(w),
                    )
                    break
        elif GObject.signal_lookup("locations-changed", type(win)):
            # "locations-changed" fires when a slot's own location changes
            # (navigating within a tab), but NOT on tab switch: switching
            # tabs only reassigns NautilusWindow's "active-slot" property
            # (set_active_slot() -> g_object_notify_by_pspec(active-slot),
            # confirmed in nautilus-window.c) without touching any slot's
            # location. Both must be watched or the window chrome goes stale
            # the moment the active tab changes without a navigation.
            win.connect("locations-changed", self._on_navigation)
            win.connect("notify::active-slot", lambda w, _pspec: self._on_navigation(w))
        else:
            # Nautilus <= 47 has no "locations-changed" signal on
            # NautilusWindow (issue #61). Fall back to watching the active
            # slot's own "location" property directly (same technique as
            # the file-chooser branch above), re-subscribing whenever
            # "active-slot" changes (tab switch or new tab). URI-based,
            # like _window_is_at_disks() itself, rather than locale-string
            # title matching.
            watch = {"slot": None, "handler": None}

            def _rewatch_active_slot(w, _pspec=None, watch=watch):
                try:
                    slot = w.get_property("active-slot")
                except TypeError:
                    slot = None
                if slot is not watch["slot"]:
                    if watch["slot"] is not None and watch["handler"] is not None:
                        try:
                            watch["slot"].disconnect(watch["handler"])
                        except Exception:
                            pass
                    watch["slot"] = slot
                    watch["handler"] = (
                        slot.connect(
                            "notify::location",
                            lambda _s, _p, w=w: self._on_navigation(w),
                        )
                        if slot is not None
                        else None
                    )
                self._on_navigation(w)

            win.connect("notify::active-slot", _rewatch_active_slot)
            _rewatch_active_slot(win)

        if DEBUG_COMPUTER_BUTTON_ACTIVE:
            self._inject_sidebar_link(win)
        self._attach_pathbar_menu_watch(win)
        self._attach_file_view_context_menu(win)
        self._inject_column_view_entry(win)
        column_view.watch_tab_view(self, win)
        if DEBUG_MAIN_VIEW_ACTIVE:
            my_computer_view.watch_tab_view(self, win)
        win.connect("notify::is-active", lambda w, _pspec: self._on_window_active_changed(w))
        self._on_navigation(win)

        if DEBUG_SELFTEST and not getattr(self, "_selftest_started", False):
            self._selftest_started = True
            GLib.timeout_add(3000, lambda: self._run_selftest(win))

        return True

    def _run_selftest(self, win) -> bool:
        """Debug-only: drive in-process navigation (no keyboard/focus needed) so
        the templates-menu crash can be reproduced deterministically."""
        home = os.path.expanduser
        steps = [
            DISKS_URI,
            Gio.File.new_for_path(home("~/Downloads")).get_uri(),
            DISKS_URI,
            Gio.File.new_for_path(home("~/Documents")).get_uri(),
            DISKS_URI,
            Gio.File.new_for_path(home("~/Downloads")).get_uri(),
        ]
        idx = [0]

        def step():
            if win not in self._windows:
                _log("SELFTEST: window gone")
                return GLib.SOURCE_REMOVE
            if idx[0] >= len(steps):
                _log("SELFTEST DONE: survived all navigations")
                return GLib.SOURCE_REMOVE
            uri = steps[idx[0]]
            idx[0] += 1
            _log(f"SELFTEST step -> {uri}")
            for w in _all_widgets(win):
                if "Slot" in type(w).__name__:
                    try:
                        if w.activate_action("open-location", GLib.Variant("s", uri)):
                            break
                    except Exception:
                        pass
            return GLib.SOURCE_CONTINUE

        GLib.timeout_add(2500, step)
        return GLib.SOURCE_REMOVE

    def _check_new_windows(self) -> bool:
        toplevels = Gtk.Window.list_toplevels()
        found_any = False
        for win in toplevels:
            if _is_nautilus_window(win) and win not in self._windows:
                found_any = True
                # Route through the deferred path: a window present at
                # extension-load time may still be mid-load (see issue #4).
                self._schedule_window_init(win)
        if toplevels and not found_any and not self._windows:
            names = [type(w).__name__ for w in toplevels]
            _log(f"check_new_windows: no NautilusWindow found among {names} — class renamed?")
        return True

    def _on_window_destroyed(self, win: Gtk.Window) -> None:
        state = self._windows.pop(win, None)
        if state:
            column_view.detach_column_view_entry(self, win, state)
            model = state.get("native_hide_model")
            handler = state.get("native_hide_handler")
            if model is not None and handler:
                try:
                    model.disconnect(handler)
                except Exception:
                    pass
            state["native_hide_model"] = None
            state["native_hide_handler"] = None
        # Stop usage poll workers if this was the last slot showing our panel.
        self._stop_usage_poll_if_idle()

    def _set_computer_sidebar_selected(self, state: dict, selected: bool) -> bool:
        my_computer_listbox = state.get("sidebar_my_computer_listbox")
        sidebar_row = state.get("sidebar_row")
        if my_computer_listbox is None or sidebar_row is None:
            return GLib.SOURCE_REMOVE
        try:
            if sidebar_row.get_parent() is not my_computer_listbox:
                return GLib.SOURCE_REMOVE
            if selected:
                if my_computer_listbox.get_selected_row() is not sidebar_row:
                    my_computer_listbox.select_row(sidebar_row)
            elif my_computer_listbox.get_selected_row() is sidebar_row:
                my_computer_listbox.unselect_all()
        except Exception:
            pass
        return GLib.SOURCE_REMOVE

    def _on_settings_changed(self, settings: Gio.Settings, key: str) -> None:
        if key == "start-on-disks":
            self._start_on_disks = settings.get_boolean(key)
        elif key in (
            "color-mode",
            "custom-color",
            "custom-gradient-color-1",
            "custom-gradient-color-2",
        ):
            self._apply_bar_color()
        elif key == "show-system-partitions":
            # Needs a rescan because filtered mounts must be re-collected
            self._schedule_live_refresh()
        elif key.startswith("visibility-"):
            # Grouping change only -- no rescan needed, just re-render
            self._repopulate_visible()
        elif key == "preferred-folders":
            self._repopulate_visible()
        elif key == "show-preferred-folder-captions":
            self._reapply_folder_captions()
        elif key.startswith("sidebar-show-"):
            # Sidebar place toggle -- re-apply native row visibility in every window.
            GLib.idle_add(self._reapply_sidebar_visibility)
        elif key == "custom-bookmark-icons":
            # Another window customized a bookmark icon -- re-apply everywhere.
            GLib.idle_add(self._reapply_bookmark_icons_all_windows)
        elif key == "computer-icon":
            # Distro override or dconf edit -- re-pin the Computer row/chip icon.
            GLib.idle_add(self._reapply_computer_icon_all_windows)

    def _get_computer_icon(self) -> str:
        """Symbolic icon name for the Computer row, overridable via the
        computer-icon GSettings key (e.g. a distro .gschema.override)."""
        if self._gsettings is None:
            return COMPUTER_ICON
        icon = self._gsettings.get_string("computer-icon")
        return icon or COMPUTER_ICON

    def _reapply_computer_icon_all_windows(self) -> bool:
        """Re-pin the Computer sidebar row and path bar chip icon in every
        window after a computer-icon settings change."""
        icon_name = self._get_computer_icon()
        for win, state in list(self._windows.items()):
            sidebar_row = state.get("sidebar_row")
            if sidebar_row is not None:
                for w in _all_widgets(sidebar_row):
                    if isinstance(w, Gtk.Image):
                        _pin_icon(w, icon_name)
                try:
                    sidebar_row.set_property("start-icon", Gio.ThemedIcon.new(icon_name))
                except Exception:
                    pass
            self._fix_pathbar_icon(win)
        return GLib.SOURCE_REMOVE

    # ── Live-refresh helpers ──────────────────────────────────────────────────

    def _repopulate_visible(self) -> bool:
        """Repopulate every slot with the panel elected, and refresh Column
        View wherever it's active."""
        for state in list(self._iter_panel_states()):
            if state.get("visible_view") == VIEW_DISKINFO:
                my_computer_view._populate_slot(self, state["slot"])
        for win in list(self._windows):
            column_view.refresh_all_column_views(self, win)
        return GLib.SOURCE_REMOVE

    def _reapply_folder_captions(self) -> None:
        """Preferred Folders "captions" GSettings key changed (NautilusPrefs).
        Instantly re-render every rendered card from whatever caption data is
        already cached, then kick a fresh async fetch to fill in any field a
        newly-selected token needs that was never queried before."""
        for pf in list(my_computer_view._folder_data.values()):
            my_computer_view._show_folder_captions(self, pf.key)
            my_computer_view._refresh_folder_captions_async(self, pf)

    def _repopulate_disk_view_only(self) -> bool:
        """Narrower sibling of _repopulate_visible for click-policy changes
        (see NautilusPrefs.refresh_click_policy): only the disk-view grid
        needs rebuilding to pick up the new activate-on-single-click flag.
        Column View is deliberately skipped -- refreshing it would
        re-enumerate and re-sort every open Miller column for a setting its
        columns don't use."""
        for state in list(self._iter_panel_states()):
            if state.get("visible_view") == VIEW_DISKINFO:
                my_computer_view._populate_slot(self, state["slot"])
        return GLib.SOURCE_REMOVE

    def _repopulate_column_view_only(self) -> bool:
        """Narrower sibling of _repopulate_visible for sort-directories-first
        changes (see NautilusPrefs.sort_directories_first): only Column View
        mixes folders and files in one listing, so only it needs
        re-sorting."""
        for win in list(self._windows):
            column_view.refresh_all_column_views(self, win)
        return GLib.SOURCE_REMOVE

    def _slot_location(self, win: Gtk.Window) -> Gio.File | None:
        return _active_slot_location(win)

    def _active_slot_widget(self, win: Gtk.Window) -> Gtk.Widget | None:
        return _active_slot(win)

    def _active_panel_state(self, win: Gtk.Window) -> dict | None:
        """Per-slot Computer panel state for `win`'s active slot, or None if
        that slot hasn't been injected (or has no panel elected). See
        my_computer_view._slot_panel_state / slot._mc_computer."""
        return my_computer_view._slot_panel_state(_active_slot(win))

    def _iter_panel_states(self):
        """Every injected Computer panel state, active tab or not -- for
        broadcasts that must reach every open tab (usage updates, folder
        icon/caption refreshes, live re-populate). Backed by a weak registry
        (self._panel_slots) rather than a widget-tree walk, since this is
        called from the usage-poll hot path."""
        for slot in list(self._panel_slots):
            state = getattr(slot, "_mc_computer", None)
            if state is not None:
                yield state

    def _active_slot_showing_column(self, win: Gtk.Window) -> bool:
        return column_view.is_active_slot_showing_column(self, win)

    def _resolve_sort_target(self, win: Gtk.Window):
        """(uri, on_changed) for whichever of our views is visible in `win`
        right now, or None if neither is -- see NautilusPrefs.watch_sort_button.
        The disk panel always watches DISKS_URI. Column View watches the real
        Nautilus slot's current location -- the native sort popover writes
        GVfs metadata for wherever Nautilus itself is actually navigated to,
        which is exactly Column View's root (see _show_column_view /
        column_view.enter_column_view: entering always reseeds the Miller
        chain from the real current location, and internal drill-down never
        moves the real Nautilus slot)."""
        state = self._active_panel_state(win)
        if state and state.get("visible_view") == VIEW_DISKINFO:
            return (DISKS_URI, self._repopulate_visible)
        if self._active_slot_showing_column(win):
            loc = _active_slot_location(win)
            if loc is None:
                return None
            return (loc.get_uri(), self._repopulate_visible)
        return None

    def _arm_sort_watch(self, win: Gtk.Window) -> None:
        if not DEBUG_SORT_WATCH_ACTIVE:
            return
        GLib.idle_add(
            lambda w=win: (
                self._nautilus_prefs.watch_sort_button(
                    self, w, resolve_sort_target=lambda _ext, ww: self._resolve_sort_target(ww)
                )
                or False
            )
        )

    def _show_column_view(self, win: Gtk.Window) -> None:
        """Ctrl+3: tmp shortcut, replaces the old location-trigger hack. Not
        a toggle -- always (re)opens Column View, reconciled to wherever
        Nautilus really is right now (see column_view.enter_column_view).
        Since drill-downs commit slot.open-location, that location is
        normally the deepest column already open, so this preserves the
        whole chain on a round-trip through Ctrl+1/Ctrl+2 instead of
        collapsing it to a single column (see _ColumnViewHost.sync_to_uri for
        the ancestor-truncate / re-root cases). See
        _leave_column_view_for_native_mode for the mirror-image case."""
        slot = self._active_slot_widget(win)
        if slot is None or getattr(slot, "_mc_column_view", None) is None:
            _log("_show_column_view: active slot not ready")
            return
        loc = _active_slot_location(win)
        if not self._column_view_available_at(loc):
            _log("_show_column_view: unavailable for the current virtual location")
            return False
        root_uri = loc.get_uri() if loc is not None else column_view.default_root_uri()
        _log(f"_show_column_view: entering column view at root_uri={root_uri!r}")
        column_view.enter_column_view(self, win, root_uri)
        column_view.refresh_column_view_chrome(self, win)
        self._arm_sort_watch(win)
        self._set_default_view(column_view.VIEW_COLUMN)

    @staticmethod
    def _column_view_available_at(location: Gio.File | None) -> bool:
        """Whether Miller view can be used for a Nautilus slot location."""
        return location is None or location.get_uri() not in MILLER_VIEW_UNAVAILABLE_URIS

    def _column_view_available_for_window(self, win: Gtk.Window) -> bool:
        return self._column_view_available_at(_active_slot_location(win))

    def _auto_elect_view_for_slot(self, win: Gtk.Window) -> str | None:
        """Which of our own views a slot should open into, or None to leave
        Nautilus's native view alone. File choosers must never be auto-elected
        into a non-native view -- they carry a single NautilusWindowSlot and
        need their native model for file selection (see _is_file_chooser_window)."""
        if _is_file_chooser_window(win) or not self._gsettings:
            return None
        value = self._gsettings.get_string("default-view")
        return None if value == "native" else value

    def _set_default_view(self, value: str) -> None:
        if self._gsettings and self._gsettings.get_string("default-view") != value:
            self._gsettings.set_string("default-view", value)

    def _rename_column_focused_folder(self, win: Gtk.Window) -> bool:
        """F2: rename the selected local item in the focused Miller column."""
        if not self._active_slot_showing_column(win):
            return False
        # While the rename entry itself owns focus, leave F2 to that editor
        # rather than creating a second popover.
        if isinstance(win.get_focus(), Gtk.Editable):
            return False
        return column_view.rename_focused_folder(self, win)

    def _trash_column_focused_folder(self, win: Gtk.Window) -> bool:
        """Delete: move the selected local Miller item to Nautilus's trash."""
        if not self._active_slot_showing_column(win):
            return False
        if isinstance(win.get_focus(), Gtk.Editable):
            return False
        return column_view.trash_focused_folder(self, win)

    def _cut_column_focused_folder(self, win: Gtk.Window) -> bool:
        """Ctrl+X: put the selected Miller folder on the clipboard as a cut."""
        return self._copy_column_focused_folder(win, cut=True)

    def _copy_column_focused_folder(self, win: Gtk.Window, *, cut: bool = False) -> bool:
        """Ctrl+C: copy the selected Miller item to the system clipboard."""
        if not self._active_slot_showing_column(win):
            return False
        if isinstance(win.get_focus(), Gtk.Editable):
            return False
        return column_view.copy_focused_folder_to_clipboard(self, win, cut=cut)

    def _paste_into_column_focused_folder(self, win: Gtk.Window) -> bool:
        """Ctrl+V: paste the Miller clipboard into the selected folder."""
        if not self._active_slot_showing_column(win):
            return False
        if isinstance(win.get_focus(), Gtk.Editable):
            return False
        return column_view.paste_into_focused_folder(self, win)

    def _new_folder_in_column_focused_folder(self, win: Gtk.Window) -> bool:
        """Shift+Ctrl+N: create a folder in the focused Miller column."""
        if not self._active_slot_showing_column(win):
            return False
        if isinstance(win.get_focus(), Gtk.Editable):
            return False
        return column_view.create_folder_in_focused_column(self, win)

    def _stop_hidden_native_slot(self, win: Gtk.Window, slot: Gtk.Widget) -> bool:
        """Cancel Nautilus's covered files-view load on `slot` without
        touching widgets. Targets `slot` directly (rather than the window's
        current active slot) since per-slot injection (Column View #118,
        Computer View #133) means the slot needing this is not necessarily
        the active one -- e.g. a background tab elected via "Open in New
        Tab". File choosers need their single native model throughout
        navigation (issue #55 picker path deliberately uses location
        observation only)."""
        if _is_file_chooser_window(win):
            return False
        return slot.activate_action("slot.stop", None)

    def _leave_computer_panel_for_slot(self, win: Gtk.Window, slot: Gtk.Widget) -> None:
        """Bridge for column_view.py: release the Computer panel's own state
        on `slot` before Column View claims the shared per-slot GtkStack.
        column_view.py may not import my_computer_view.py directly (CLAUDE.md
        target-module isolation), so this thin delegate is the only path
        between them (issue #137's per-slot view-election arbiter)."""
        if slot_view_owner(slot) == "computer" and getattr(slot, "_mc_computer", None) is not None:
            my_computer_view._leave_panel(self, win, slot)

    def _leave_column_view_for_slot(self, slot: Gtk.Widget) -> None:
        """Bridge for my_computer_view.py: release Column View's own state
        on `slot` before the Computer panel claims the shared per-slot
        GtkStack (issue #137's per-slot view-election arbiter)."""
        if slot_view_owner(slot) == "column":
            column_view.leave_column_view(slot)

    def _reload_native_slot_after_column(self, win: Gtk.Window, slot: Gtk.Widget) -> bool:
        """Reload a native model that Column View previously stopped for `slot`.

        This runs from an idle after Ctrl+1/Ctrl+2 propagation, so Nautilus
        changes the inner List/Grid widget before reloading its shared model.
        If Column View became visible again first, keep the stopped marker for
        the next real exit instead of restarting hidden native work.
        """
        if not getattr(slot, "_mc_column_native_stopped", False):
            return GLib.SOURCE_REMOVE
        if column_view.slot_is_showing_column(slot):
            return GLib.SOURCE_REMOVE
        if _is_file_chooser_window(win):
            return GLib.SOURCE_REMOVE
        activated = slot.activate_action("slot.reload", None)
        if activated:
            slot._mc_column_native_stopped = False
        return GLib.SOURCE_REMOVE

    def _leave_column_view_for_native_mode(self, win: Gtk.Window) -> bool:
        """Reveal the native view before a native List/Grid transition.

        Ctrl+1/Ctrl+2 call this as a side effect and then propagate to
        Nautilus. The extension-owned three-state primary action also calls it
        before explicitly activating Nautilus's untouched native toggle.
        """
        # Every caller here is an explicit "I want native" pick, so this is
        # written unconditionally, not only when slot_is_showing_column below
        # is true -- e.g. the user forced-fallback to a Column-unavailable
        # location (trash:///) and then presses Ctrl+1 there. If the write
        # were gated on slot_is_showing_column, the key would stay 'column'
        # and leaving that location would silently re-elect Column View
        # against the explicit pick just made.
        self._set_default_view("native")
        slot = self._active_slot_widget(win)
        if slot is not None and column_view.slot_is_showing_column(slot):
            _log("_leave_column_view_for_native_mode: dropping back to native view")
            column_view.leave_column_view(slot)
            if getattr(slot, "_mc_column_native_stopped", False):
                GLib.idle_add(self._reload_native_slot_after_column, win, slot)
        return False

    def _on_window_key_capture(self, _ctrl, keyval, _keycode, gtk_state, win) -> bool:
        """Limit Column View keys and suppress panel type-ahead search."""
        win_state = self._windows.get(win)
        if not win_state:
            _log(f"key_capture: no state for win, keyval={Gdk.keyval_name(keyval)}")
            return False
        panel_state = self._active_panel_state(win)
        _log(
            f"key_capture: keyval={Gdk.keyval_name(keyval)} "
            f"ctrl={bool(gtk_state & Gdk.ModifierType.CONTROL_MASK)} "
            f"alt={bool(gtk_state & Gdk.ModifierType.ALT_MASK)} "
            f"super={bool(gtk_state & Gdk.ModifierType.SUPER_MASK)} "
            f"visible_view={panel_state.get('visible_view') if panel_state else None!r}"
        )
        # Shared shortcut table (see _WINDOW_SHORTCUTS) -- checked from any
        # visible_view, must run before the VIEW_DISKINFO gate below (that
        # gate is only for the type-ahead-swallow behavior further down).
        handler_name = _WINDOW_SHORTCUTS.get((int(gtk_state) & _SHORTCUT_MODIFIER_MASK, keyval))
        if handler_name is not None:
            consumed = getattr(self, handler_name)(win) is not False
            _log(
                f"key_capture: shortcut match ({Gdk.keyval_name(keyval)}) -> "
                f"{handler_name} consumed={consumed}"
            )
            return consumed
        if not panel_state or panel_state.get("visible_view") != VIEW_DISKINFO:
            return False
        # Let modified shortcuts through (Ctrl+L, Alt+Left, Super, …).
        if gtk_state & (
            Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SUPER_MASK
        ):
            # Ctrl+L opens the address bar for real navigation, not card
            # filtering -- disown it so _on_location_text_changed ignores
            # the resulting "changed" signals. See location_filter_owned.
            panel_state["location_filter_owned"] = False
            return False
        # Address-bar trigger keys must reach the toolbar's MANAGED shortcut
        # controller (nautilus-toolbar.c): "/" -> root, "~" -> home, like native.
        # Without this the printable-char swallow below eats them first.
        # These open the bar for real navigation, not card filtering, so
        # disown it the same way Ctrl+L does above.
        if keyval in _LOCATION_ENTRY_KEYVALS:
            panel_state["location_filter_owned"] = False
            return False
        # Only handle printable characters (>= space). Control keys — arrows,
        # Tab, Enter, Esc, function keys — map to unicode < 0x20 and pass through.
        if Gdk.keyval_to_unicode(keyval) < 0x20:
            return False
        # If the user opened a text entry (Ctrl+L location bar, Ctrl+F search),
        # the focused widget is an Editable — let it receive the keystroke.
        focused = win.get_focus()
        if focused is not None and isinstance(focused, Gtk.Editable):
            return False
        # Plain printable, address bar not open yet: reveal it (same
        # toolbar.edit-location action Ctrl+L uses) and seed it with this
        # character, so typing filters the panel's cards live instead of
        # triggering Nautilus's own type-ahead search. See location_filter.py.
        # Gated by the same flag as the watch so MC_LOCATION_FILTER=0 fully
        # restores the plain type-ahead swallow (no keystroke hijack).
        if DEBUG_LOCATION_FILTER_ACTIVE:
            char = chr(Gdk.keyval_to_unicode(keyval))
            # Must be set before reveal_and_seed(): entry.set_text() inside it
            # fires "changed" synchronously, before this call even returns, so
            # setting the flag from the return value would miss that first
            # keystroke's filter application entirely.
            panel_state["location_filter_owned"] = True
            if not location_filter.reveal_and_seed(self, win, char):
                panel_state["location_filter_owned"] = False
        return True

    # ── Location change handler ───────────────────────────────────────────────

    def _on_navigation(self, win: Gtk.Window) -> None:
        """Sync window-singleton chrome (sidebar highlight, pathbar chip,
        sort watch, location filter watch) to whichever slot is active.

        Showing/hiding the panel itself is no longer this function's job:
        each slot owns that decision independently, driven directly by its
        own "notify::location" (see my_computer_view._on_slot_location_changed,
        issue #133) rather than by window-title polling. This function only
        follows the ACTIVE slot to keep the window's shared chrome widgets in
        sync with it -- it fires on both real navigation and tab switches."""
        state = self._windows.get(win)
        if not state:
            return

        current_title = win.get_title() or ""
        # A transient/empty title ("Loading…") means the window hasn't resolved
        # its location yet. Never act on it: it must not consume the one-shot
        # start-on-computer flag.
        if _is_unsettled_title(current_title):
            return

        if state.get("start_on_computer"):
            state["start_on_computer"] = False
            if current_title == _HOME_TITLE:
                self._navigate_to_disks(win)
                return

        panel_state = self._active_panel_state(win)
        # Read the slot's own location rather than the panel's visible_view
        # flag: Nautilus emits "locations-changed" from inside its own
        # notify::location handler (nautilus-window.c on_slot_location_changed),
        # which it connected long before my_computer_view's per-slot watcher,
        # so this runs *before* _enter_panel/_leave_panel has updated that
        # flag. The location is the same ground truth both of them act on,
        # and it is already committed by the time either handler runs.
        location = _active_slot_location(win)
        at_disks = location is not None and location.equal(_DISKS_FILE)
        in_view = panel_state is not None and at_disks
        if in_view:
            GLib.idle_add(self._set_computer_sidebar_selected, state, True)

            # Re-pin the chrome icons (path-bar chip + sidebar row) every time
            # the active tab arrives at the computer view.
            if DEBUG_PATHBAR_ACTIVE:
                GLib.idle_add(lambda w=win: self._fix_pathbar_icon(w) or False)
            if DEBUG_SORT_WATCH_ACTIVE:
                GLib.idle_add(lambda w=win: self._attach_sort_button_watch(w) or False)
            if DEBUG_LOCATION_FILTER_ACTIVE:
                GLib.idle_add(lambda w=win: self._attach_location_filter_watch(w) or False)
        elif state.get("_chrome_in_view"):
            # Only on the actual transition away from the panel (not on every
            # subsequent files-view navigation) -- re-derive our sidebar
            # highlight from the aggregate native selection rather than
            # blindly unselecting. The Computer row is selected manually on
            # entry (no live native row to mirror), so on exit nothing fires
            # a native signal to clear it. Re-running the mirror sync clears
            # our row when the destination (e.g. /tmp/) is not one of our
            # places, and leaves it intact when the mirror already picked an
            # owned place.
            sync = state.get("sidebar_sync")
            if callable(sync):
                GLib.idle_add(lambda s=sync: (s(), False)[1])
            else:
                GLib.idle_add(self._set_computer_sidebar_selected, state, False)
        state["_chrome_in_view"] = in_view

        # Nautilus keeps one view-options control and menu model per window.
        # Refresh our chrome and stable menu section when the active slot moves
        # into or out of a location where Column View is unavailable.
        column_view.refresh_column_view_chrome(self, win)

    def _do_open_with(
        self, nav_uri: str, win: Gtk.Window, *, content_type: str = "inode/directory"
    ) -> None:
        """Show an app chooser for nav_uri as an in-window sheet (Adw.Dialog),
        matching how native Nautilus presents its own "Open With…" - a custom
        AdwDialog compiled into the nautilus binary, with no public API we
        could call directly. Built directly from Gio.AppInfo rather than
        Gtk.AppChooserDialog: that stock widget is a Gtk.Dialog (always a
        separate top-level window, never an attached sheet), and its
        "View All Apps…" / "Find New Apps…" extras plus collapsed search
        toggle are private template internals with no supported way to
        customize or remove. Used by local-file "Open With…" menu items."""
        if not nav_uri.startswith("file://"):
            return

        recommended = list(Gio.AppInfo.get_recommended_for_type(content_type))
        recommended_ids = {info.get_id() for info in recommended}
        other = [
            info
            for info in Gio.AppInfo.get_all()
            if info.get_id() not in recommended_ids and info.should_show()
        ]
        recommended.sort(key=lambda i: i.get_display_name().lower())
        other.sort(key=lambda i: i.get_display_name().lower())

        file_name = Gio.File.new_for_uri(nav_uri).get_basename() or nav_uri

        dialog = Adw.Dialog()
        dialog.set_title(
            _native("Open Folder") if content_type == "inode/directory" else _native("Open File")
        )
        dialog.set_content_width(420)
        dialog.set_content_height(560)

        toolbar_view = Adw.ToolbarView()
        dialog.set_child(toolbar_view)

        header = Adw.HeaderBar()
        header.set_show_start_title_buttons(False)
        header.set_show_end_title_buttons(False)
        cancel_button = Gtk.Button(label=_native("Cancel"))
        cancel_button.connect("clicked", lambda *_a: dialog.close())
        header.pack_start(cancel_button)
        open_button = Gtk.Button(label=_native("Open"))
        open_button.add_css_class("suggested-action")
        open_button.set_sensitive(False)
        header.pack_end(open_button)
        toolbar_view.add_top_bar(header)

        # Search entry as its own toolbar row (native Nautilus: an Adw.Bin with
        # the "toolbar" style class), not inside the margined content box - this
        # is what makes it span edge-to-edge, aligned with the header buttons.
        search_bin = Adw.Bin()
        search_bin.add_css_class("toolbar")
        search_entry = Gtk.SearchEntry()
        search_entry.set_hexpand(True)
        search_bin.set_child(search_entry)
        toolbar_view.add_top_bar(search_bin)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.set_margin_start(18)
        content.set_margin_end(18)
        content.set_margin_top(18)
        content.set_margin_bottom(18)
        toolbar_view.set_content(content)

        description = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        description.set_markup(
            _native("Choose an app to open <b>%s</b>") % GLib.markup_escape_text(file_name)
        )
        content.append(description)

        # has-frame gives the native rounded-corner/bordered look (matches
        # NautilusAppChooserWidget's ScrolledWindow); the listbox itself stays
        # unstyled so rows render without the .boxed-list per-row separators.
        scroller = Gtk.ScrolledWindow()
        scroller.set_has_frame(True)
        scroller.set_vexpand(True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        content.append(scroller)

        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        listbox.set_activate_on_single_click(False)
        scroller.set_child(listbox)

        def _make_header_row(text: str, *, first: bool) -> Gtk.ListBoxRow:
            row = Gtk.ListBoxRow()
            row.set_selectable(False)
            row.set_activatable(False)
            label = Gtk.Label(label=text, xalign=0.0)
            label.add_css_class("heading")
            label.set_margin_start(6)
            label.set_margin_top(6 if first else 16)
            label.set_margin_bottom(6)
            row.set_child(label)
            return row

        def _make_app_row(info: Gio.AppInfo) -> Gtk.ListBoxRow:
            row = Gtk.ListBoxRow()
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            box.set_margin_top(6)
            box.set_margin_bottom(6)
            box.set_margin_start(6)
            box.set_margin_end(6)
            icon = info.get_icon()
            image = (
                Gtk.Image.new_from_gicon(icon)
                if icon
                else Gtk.Image.new_from_icon_name("application-x-executable-symbolic")
            )
            image.set_pixel_size(32)
            box.append(image)
            label = Gtk.Label(label=info.get_display_name(), xalign=0.0)
            box.append(label)
            row.set_child(box)
            row._app_info = info
            return row

        def _populate(filter_text: str = "") -> None:
            child = listbox.get_first_child()
            while child:
                nxt = child.get_next_sibling()
                listbox.remove(child)
                child = nxt
            filt = filter_text.strip().lower()
            first_app_row = None
            is_first_group = True
            for title, apps in ((_("Recommended Apps"), recommended), (_("Other Apps"), other)):
                matches = [a for a in apps if not filt or filt in a.get_display_name().lower()]
                if not matches:
                    continue
                listbox.append(_make_header_row(title, first=is_first_group))
                is_first_group = False
                for info in matches:
                    app_row = _make_app_row(info)
                    listbox.append(app_row)
                    if first_app_row is None:
                        first_app_row = app_row
            if first_app_row is not None:
                listbox.select_row(first_app_row)
            else:
                empty = Gtk.ListBoxRow()
                empty.set_selectable(False)
                empty.set_activatable(False)
                label = Gtk.Label(label=_native("No applications found."))
                label.add_css_class("dim-label")
                label.set_margin_top(24)
                label.set_margin_bottom(24)
                empty.set_child(label)
                listbox.append(empty)

        def _selected_app_info():
            row = listbox.get_selected_row()
            return getattr(row, "_app_info", None) if row else None

        def _launch_and_close() -> None:
            info = _selected_app_info()
            dialog.close()
            if info:
                try:
                    info.launch_uris([nav_uri], None)
                except GLib.Error as e:
                    _log(f"Open With launch failed: {e}")

        search_entry.connect("search-changed", lambda e: _populate(e.get_text()))
        search_entry.connect("activate", lambda *_a: _launch_and_close())
        listbox.connect(
            "row-selected",
            lambda _lb, row: open_button.set_sensitive(getattr(row, "_app_info", None) is not None),
        )
        listbox.connect(
            "row-activated",
            lambda _lb, row: _launch_and_close() if getattr(row, "_app_info", None) else None,
        )
        open_button.connect("clicked", lambda *_a: _launch_and_close())

        _populate()
        dialog.present(win)
        search_entry.grab_focus()

    def _do_open(self, nav_uri: str, win: Gtk.Window) -> None:
        GLib.idle_add(self._navigate_to, nav_uri, win)

    def _do_open_tab(self, nav_uri: str, win: Gtk.Window, make_active: bool = True) -> None:
        """Open nav_uri in a new tab, built the way Nautilus itself builds one
        (nautilus_window_create_and_init_slot, nautilus-window.c:406): create a
        NautilusWindowSlot directly and hand it to AdwTabView, rather than firing
        the "new-tab" action (the Ctrl+T path) and polling for the resulting slot.
        Falls back to the action+poll path if the slot type can't be resolved.

        make_active=False mirrors NAUTILUS_OPEN_FLAG_DONT_MAKE_ACTIVE
        (nautilus-window.c:471): the tab is created and navigated but never
        selected, matching every native middle-click and "Open in New Tab" site.
        Nautilus itself defaults to leaving a freshly created tab unselected
        (adw_tab_view_add_page never selects); selecting happens only in
        set_active_slot, which we skip here when make_active is False.
        """
        uri = nav_uri

        tab_view = next(
            (w for w in _all_widgets(win) if isinstance(w, Adw.TabView)),
            None,
        )
        if tab_view is None:
            return

        slot_gtype = _resolve_gtype("NautilusWindowSlot")
        if slot_gtype is None:
            self._do_open_tab_via_action(uri, win, tab_view, make_active)
            return

        slot = GObject.new(slot_gtype, mode=0)  # NAUTILUS_MODE_BROWSE
        current_page = tab_view.get_selected_page()
        page = tab_view.add_page(slot, current_page)

        # Without these bindings the tab has no title and never shows the
        # loading spinner — nautilus_window_create_and_init_slot binds the same
        # two properties (nautilus-window.c:417-421).
        slot.bind_property("title", page, "title", GObject.BindingFlags.SYNC_CREATE)
        slot.bind_property("allow-stop", page, "loading", GObject.BindingFlags.SYNC_CREATE)

        slot.activate_action("slot.open-location", GLib.Variant("s", uri))
        if make_active:
            tab_view.set_selected_page(page)

    def _do_open_tab_via_action(
        self, uri: str, win: Gtk.Window, tab_view: Adw.TabView, make_active: bool = True
    ) -> None:
        pages_before = tab_view.get_n_pages()
        previous_page = None if make_active else tab_view.get_selected_page()

        attempt = [0]

        def _fire_and_wait():
            Gio.ActionGroup.activate_action(win, "new-tab", None)

            def _wait_for_tab():
                n = tab_view.get_n_pages()
                if n <= pages_before:
                    attempt[0] += 1
                    if attempt[0] >= 20:
                        return GLib.SOURCE_REMOVE
                    return GLib.SOURCE_CONTINUE

                # Navigate by index, not selected page — avoids racing with
                # concurrent rapid tab-opens that share the same pages_before.
                page = tab_view.get_nth_page(pages_before)
                if page:
                    slot = page.get_child()
                    if slot and slot.activate_action("slot.open-location", GLib.Variant("s", uri)):
                        # The "new-tab" action always selects the new tab (it's
                        # the Ctrl+T path) — restore the previous selection to
                        # emulate DONT_MAKE_ACTIVE in this fallback.
                        if previous_page is not None:
                            tab_view.set_selected_page(previous_page)
                        return GLib.SOURCE_REMOVE

                attempt[0] += 1
                if attempt[0] >= 40:
                    return GLib.SOURCE_REMOVE
                return GLib.SOURCE_CONTINUE

            GLib.timeout_add(_TAB_WAIT_MS, _wait_for_tab)
            return GLib.SOURCE_REMOVE

        GLib.idle_add(_fire_and_wait)

    def _do_open_window(self, mountpoint: str) -> None:
        subprocess.Popen(["nautilus", "--new-window", mountpoint])

    def _do_properties(self, nav_uri: str, win: Gtk.Window) -> None:
        uri = nav_uri

        # The native properties window is created in-process by Nautilus via the
        # D-Bus ShowItemProperties call. It is NOT registered with the
        # GtkApplication, so "window-added" never fires — we must poll
        # list_toplevels() to find it. Once found, set it transient-for our
        # window and modal so the compositor visually binds it to the parent
        # (centered, above, moves/closes with it) instead of floating free.
        before_ids = {id(w) for w in Gtk.Window.list_toplevels()}
        state = {"done": False}

        def _try_parent(attempt=0):
            if state["done"]:
                return GLib.SOURCE_REMOVE
            for w in Gtk.Window.list_toplevels():
                if id(w) not in before_ids and w is not win:
                    w.set_transient_for(win)
                    w.set_modal(True)
                    state["done"] = True
                    return GLib.SOURCE_REMOVE
            if attempt < 40:
                GLib.timeout_add(25, lambda: _try_parent(attempt + 1))
            return GLib.SOURCE_REMOVE

        def _on_call(bus, result, _):
            try:
                bus.call_finish(result)
            except Exception:
                pass

        def _on_bus(_, result):
            try:
                bus = Gio.bus_get_finish(result)
                bus.call(
                    DBUS_FILE_MANAGER,
                    DBUS_PATH_FILE_MANAGER,
                    DBUS_FILE_MANAGER,
                    "ShowItemProperties",
                    GLib.Variant("(ass)", ([uri], "")),
                    None,
                    Gio.DBusCallFlags.NONE,
                    5000,
                    None,
                    _on_call,
                    None,
                )
            except Exception:
                pass

        # Start polling immediately so we catch the window as early as possible.
        _try_parent()
        Gio.bus_get(Gio.BusType.SESSION, None, _on_bus)

    def _launch_settings_panel(self, panel: str) -> None:
        """Open a gnome-control-center panel (e.g. 'privacy'), matching native rows."""
        try:
            Gio.Subprocess.new(["gnome-control-center", panel], Gio.SubprocessFlags.NONE)
        except GLib.Error as e:
            _log(f"settings launch failed ({panel}): {e.message}")

    def _launch_prefs(self, win: Gtk.Window | None = None) -> None:
        if not self._gsettings:
            return

        detached = DETACH_SETTINGS_WINDOW or win is None
        pref_win = Adw.PreferencesWindow() if detached else Adw.PreferencesDialog()
        pref_win.set_title(PREFS_WIN_TITLE)
        pref_win.set_search_enabled(False)
        if detached:
            pref_win.set_default_size(680, 760)

        page_general = Adw.PreferencesPage()
        page_general.set_title(_("General"))
        page_general.set_icon_name("preferences-system-symbolic")
        pref_win.add(page_general)

        page_computer = Adw.PreferencesPage()
        page_computer.set_title(_("Computer view"))
        page_computer.set_icon_name("computer-symbolic")
        pref_win.add(page_computer)

        page_sidebar = Adw.PreferencesPage()
        page_sidebar.set_title(_native("Sidebar"))
        # Icon RTL handling differs by pack. view-left-pane-symbolic (Colloid,
        # Papirus, Tela, WhiteSur, kora...) and sidebar-show-symbolic (Adwaita)
        # name their mirror with a "-rtl" SUFFIX, which GTK swaps in automatically.
        # Yaru uses a "-rtl" INFIX (sidebar-show-rtl-symbolic) with no suffix
        # companion, so GTK's auto-lookup never mirrors it -- probe that name
        # ourselves in RTL. Verify each candidate rather than trusting any single
        # name to exist.
        candidates = ["view-left-pane-symbolic"]
        if Gtk.Widget.get_default_direction() == Gtk.TextDirection.RTL:
            candidates.append("sidebar-show-rtl-symbolic")
        candidates.append("sidebar-show-symbolic")
        for candidate in candidates:
            if _icon_name_renders(candidate):
                page_sidebar.set_icon_name(candidate)
                break
        pref_win.add(page_sidebar)

        page_about = Adw.PreferencesPage()
        page_about.set_title(_native("About"))
        page_about.set_icon_name("help-about-symbolic")
        pref_win.add(page_about)

        gen_group = Adw.PreferencesGroup()
        gen_group.set_title(_("General"))
        page_general.add(gen_group)

        start_row = Adw.SwitchRow()
        start_row.set_title(_("Start on the Computer view"))
        start_row.set_subtitle(_("Launch directly to the Computer view instead of Home"))
        self._gsettings.bind("start-on-disks", start_row, "active", Gio.SettingsBindFlags.DEFAULT)
        gen_group.add(start_row)

        vis_group = Adw.PreferencesGroup()
        vis_group.set_title(_("Panel visibility"))
        vis_group.set_description(
            _(
                "Choose how each group appears. "
                "Visible: shows the group as a normal separated section. "
                "Merged: folds the group into the On this Computer group. "
                "Hidden: hides the group entirely."
            )
        )
        page_computer.add(vis_group)

        _vis_map = ["visible", "merged", "hidden"]
        _vis_labels = [_("Visible"), _("Merged"), _("Hidden")]
        _folders_vis_map = ["visible", "hidden"]
        _folders_vis_labels = [_("Visible"), _("Hidden")]

        folders_combo = Adw.ComboRow()
        folders_combo.set_title(_("Preferred Folders"))
        folders_combo.set_model(Gtk.StringList.new(_folders_vis_labels))
        current_folders_vis = self._gsettings.get_string("visibility-preferred-folders")
        folders_combo.set_selected(
            _folders_vis_map.index(current_folders_vis)
            if current_folders_vis in _folders_vis_map
            else 0
        )

        def _on_folders_vis_changed(c, _param):
            idx = c.get_selected()
            if 0 <= idx < len(_folders_vis_map):
                self._gsettings.set_string("visibility-preferred-folders", _folders_vis_map[idx])

        folders_combo.connect("notify::selected", _on_folders_vis_changed)
        vis_group.add(folders_combo)

        for gkey, glabel, gskey in _GROUP_SPEC:
            if gskey is None:
                continue  # "On this Computer" is always visible -- no control needed

            combo = Adw.ComboRow()
            combo.set_title(_(glabel))
            combo.set_model(Gtk.StringList.new(_vis_labels))
            current = self._gsettings.get_string(gskey)
            combo.set_selected(_vis_map.index(current) if current in _vis_map else 0)

            def _on_vis_changed(c, _param, _gskey=gskey):
                idx = c.get_selected()
                if 0 <= idx < len(_vis_map):
                    self._gsettings.set_string(_gskey, _vis_map[idx])

            combo.connect("notify::selected", _on_vis_changed)
            vis_group.add(combo)

        show_sys_parts_row = Adw.SwitchRow()
        show_sys_parts_row.set_title(_("Show system partitions"))
        show_sys_parts_row.set_subtitle(_("Include boot and EFI partitions in the System group"))
        self._gsettings.bind(
            "show-system-partitions", show_sys_parts_row, "active", Gio.SettingsBindFlags.DEFAULT
        )
        vis_group.add(show_sys_parts_row)

        sidebar_vis_group = Adw.PreferencesGroup()
        sidebar_vis_group.set_title(_("Sidebar visibility"))
        sidebar_vis_group.set_description(_("Choose which locations appear on the sidebar."))
        page_sidebar.add(sidebar_vis_group)

        # One toggle per native place (Computer is always shown, no key, not here).
        for entry in NATIVE_PLACES:
            gskey = _PLACE_VISIBILITY_KEYS[entry.name]
            place_row = Adw.SwitchRow()
            place_row.set_title(entry.label)
            icon_img = Gtk.Image.new_from_icon_name(entry.icon)
            icon_img.set_icon_size(Gtk.IconSize.NORMAL)
            place_row.add_prefix(icon_img)
            self._gsettings.bind(gskey, place_row, "active", Gio.SettingsBindFlags.DEFAULT)
            sidebar_vis_group.add(place_row)

        color_group = Adw.PreferencesGroup()
        color_group.set_title(_("Bar Color"))
        color_group.set_description(_("Select or customize the bar color."))
        page_computer.add(color_group)

        mode_row = Adw.ComboRow()
        mode_row.set_title(_("Color mode"))
        mode_model = Gtk.StringList.new(
            [_("System accent"), _("Custom color"), _("Custom gradient")]
        )
        mode_row.set_model(mode_model)
        _mode_map = ["accent", "flat", "gradient"]
        current_mode = self._gsettings.get_string("color-mode")
        mode_row.set_selected(_mode_map.index(current_mode) if current_mode in _mode_map else 0)
        color_group.add(mode_row)

        color_dialog = Gtk.ColorDialog()
        color_dialog.set_with_alpha(False)

        def _hex_to_rgba(hex_str: str) -> Gdk.RGBA:
            rgba = Gdk.RGBA()
            rgba.parse(hex_str)
            return rgba

        def _rgba_to_hex(rgba: Gdk.RGBA) -> str:
            r = int(rgba.red * 255)
            g = int(rgba.green * 255)
            b = int(rgba.blue * 255)
            return f"#{r:02X}{g:02X}{b:02X}"

        flat_row = Adw.ActionRow()
        flat_row.set_title(_("Color"))
        flat_btn = Gtk.ColorDialogButton(dialog=color_dialog)
        flat_btn.set_valign(Gtk.Align.CENTER)
        flat_btn.set_rgba(_hex_to_rgba(self._gsettings.get_string("custom-color")))
        flat_btn.connect(
            "notify::rgba",
            lambda btn, _: self._gsettings.set_string("custom-color", _rgba_to_hex(btn.get_rgba())),
        )
        flat_row.add_suffix(flat_btn)
        color_group.add(flat_row)

        grad_row1 = Adw.ActionRow()
        grad_row1.set_title(_("Start color"))
        grad_btn1 = Gtk.ColorDialogButton(dialog=color_dialog)
        grad_btn1.set_valign(Gtk.Align.CENTER)
        grad_btn1.set_rgba(_hex_to_rgba(self._gsettings.get_string("custom-gradient-color-1")))
        grad_btn1.connect(
            "notify::rgba",
            lambda btn, _: self._gsettings.set_string(
                "custom-gradient-color-1", _rgba_to_hex(btn.get_rgba())
            ),
        )
        grad_row1.add_suffix(grad_btn1)
        color_group.add(grad_row1)

        grad_row2 = Adw.ActionRow()
        grad_row2.set_title(_("End color"))
        grad_btn2 = Gtk.ColorDialogButton(dialog=color_dialog)
        grad_btn2.set_valign(Gtk.Align.CENTER)
        grad_btn2.set_rgba(_hex_to_rgba(self._gsettings.get_string("custom-gradient-color-2")))
        grad_btn2.connect(
            "notify::rgba",
            lambda btn, _: self._gsettings.set_string(
                "custom-gradient-color-2", _rgba_to_hex(btn.get_rgba())
            ),
        )
        grad_row2.add_suffix(grad_btn2)
        color_group.add(grad_row2)

        def _update_color_rows(selected: int) -> None:
            flat_row.set_visible(selected == 1)
            grad_row1.set_visible(selected == 2)
            grad_row2.set_visible(selected == 2)

        def _on_mode_changed(row, _) -> None:
            idx = row.get_selected()
            self._gsettings.set_string("color-mode", _mode_map[idx])
            _update_color_rows(idx)

        mode_row.connect("notify::selected", _on_mode_changed)
        _update_color_rows(mode_row.get_selected())

        pf_group = Adw.PreferencesGroup()
        pf_group.set_title(_("Preferred Folders"))
        page_computer.add(pf_group)

        pf_captions_row = Adw.SwitchRow()
        pf_captions_row.set_title(_("Show captions"))
        pf_captions_row.set_subtitle(
            _("Shows or hides the caption lines already configured in Nautilus")
        )
        self._gsettings.bind(
            "show-preferred-folder-captions",
            pf_captions_row,
            "active",
            Gio.SettingsBindFlags.DEFAULT,
        )
        pf_group.add(pf_captions_row)

        about_group = Adw.PreferencesGroup()
        about_group.set_title(_native("About"))
        page_about.add(about_group)

        def _about_row(title: str, value: str) -> Adw.ActionRow:
            row = Adw.ActionRow()
            row.set_title(title)
            lbl = Gtk.Label(label=value)
            lbl.get_style_context().add_class("dim-label")
            lbl.set_valign(Gtk.Align.CENTER)
            row.add_suffix(lbl)
            return row

        about_group.add(_about_row(_("Extension"), EXT_NAME))
        about_group.add(_about_row(_("Version"), EXT_VERSION))
        about_group.add(_about_row(_("Author"), EXT_AUTHOR))
        about_group.add(_about_row(_native("License"), EXT_LICENSE))

        github_row = Adw.ActionRow()
        github_row.set_title(_("Source code"))
        github_btn = Gtk.LinkButton(uri=EXT_GITHUB, label=_("GitHub"))
        github_btn.get_style_context().add_class("flat")
        github_btn.set_valign(Gtk.Align.CENTER)
        github_row.add_suffix(github_btn)
        about_group.add(github_row)

        if detached:
            pref_win.present()
        else:
            pref_win.present(win)

    def _navigate_to_disks(self, win: Gtk.Window) -> None:
        """Navigate a window to computer:/// at startup, retrying until the slot
        is ready. The slot often isn't navigable the instant the window settles
        on Home, so a single open-location call silently no-ops; we retry on a
        short bounded poll and stop as soon as the location actually changes
        (the active slot's own per-slot handler shows the panel at that point,
        see my_computer_view._on_slot_location_changed)."""
        attempts = [0]

        def _try() -> bool:
            if win not in self._windows:
                return GLib.SOURCE_REMOVE
            if my_computer_view._window_is_at_disks(win):
                return GLib.SOURCE_REMOVE
            attempts[0] += 1
            if attempts[0] > 25:  # ~1.5 s budget, then give up
                return GLib.SOURCE_REMOVE
            self._navigate_to(DISKS_URI, win)
            return GLib.SOURCE_CONTINUE

        GLib.timeout_add(_NAV_RETRY_MS, _try)

    def _navigate_to(self, uri: str, win: Gtk.Window) -> bool:
        # Target the active slot directly rather than walking every "Slot"
        # widget in the window: with 2+ tabs open, a blind walk can hand the
        # action to a background tab's slot first, silently navigating the
        # wrong tab while the visible one looks frozen (issue #132).
        slot = _active_slot(win)
        if slot is not None:
            try:
                if slot.activate_action("open-location", GLib.Variant("s", uri)):
                    return False
            except Exception:
                pass
        try:
            if win.activate_action("slot.open-location", GLib.Variant("s", uri)):
                return False
        except Exception:
            pass

        def _on_proxy(_, result):
            try:
                proxy = Gio.DBusProxy.new_for_bus_finish(result)
                proxy.call(
                    "ShowFolders",
                    GLib.Variant("(ass)", ([uri], "")),
                    Gio.DBusCallFlags.NONE,
                    -1,
                    None,
                    None,
                )
            except Exception:
                pass

        Gio.DBusProxy.new_for_bus(
            Gio.BusType.SESSION,
            Gio.DBusProxyFlags.NONE,
            None,
            DBUS_FILE_MANAGER,
            DBUS_PATH_FILE_MANAGER,
            DBUS_FILE_MANAGER,
            None,
            _on_proxy,
        )
        return False

    # ── Chrome icon fix (path bar chip) ─────────────────────────────────────

    def _find_sidebar_listbox(self, nautilus_sidebar) -> Gtk.ListBox | None:
        places_sidebar = None
        for w in _all_widgets(nautilus_sidebar):
            buildable_id = w.get_buildable_id() if hasattr(w, "get_buildable_id") else None
            widget_name = w.get_name() if hasattr(w, "get_name") else None
            if buildable_id == "places_sidebar" or widget_name == "places_sidebar":
                places_sidebar = w
                break
        if places_sidebar is None:
            places_sidebar = _find_widget(
                nautilus_sidebar,
                class_name="NautilusSidebar",
                site="_find_sidebar_listbox",
            )
        search_root = places_sidebar or nautilus_sidebar

        for w in _all_widgets(search_root):
            if isinstance(w, Gtk.ListBox) and w.has_css_class("navigation-sidebar"):
                return w

        for w in _all_widgets(search_root):
            if isinstance(w, Gtk.ListBox) and w.has_css_class("places-sidebar-list"):
                return w

        for w in _all_widgets(search_root):
            if isinstance(w, Gtk.ListBox):
                _log("_find_sidebar_listbox: no known sidebar class found, using first GtkListBox")
                return w
        return None

    def _build_place_sidebar_row(
        self, win: Gtk.Window, entry: PlaceEntry, nautilus_sidebar: Gtk.Widget | None = None
    ) -> Gtk.ListBoxRow:
        # Only the Computer row is built here (it has no native equivalent). Every
        # other place stays native; we just toggle its native row's visibility.
        row_label = entry.label
        row_tooltip = entry.tooltip
        icon_name = self._get_computer_icon() if entry.uri == DISKS_URI else entry.icon

        # Instantiate the native row directly from Nautilus's GObject type system.
        # Nautilus 47 calls it NautilusGtkSidebarRow; 48+ calls it NautilusSidebarRow.
        # Both are registered before extensions load and expose the same
        # construct-only uri and sidebar properties. uri is construct-only.
        list_row = None
        row_gtype = _resolve_gtype("NautilusSidebarRow", "NautilusGtkSidebarRow")
        if row_gtype is not None:
            try:
                row_props = {
                    "uri": entry.uri,
                    "place-type": 0,  # NAUTILUS_SIDEBAR_ROW_INVALID, sorts before built-in rows
                    "section-type": 1,  # NAUTILUS_SIDEBAR_SECTION_DEFAULT_LOCATIONS
                    "order-index": entry.order_index,
                    "label": row_label,
                    "tooltip": row_tooltip,
                    "eject-tooltip": _native("Unmount"),
                    "start-icon": Gio.ThemedIcon.new(icon_name),
                }
                if nautilus_sidebar is not None:
                    row_props["sidebar"] = nautilus_sidebar

                list_row = GObject.new(row_gtype, **row_props)
                list_row.set_name(f"place_{entry.name}")
                list_row.set_has_tooltip(True)
                _log(
                    f"_build_place_sidebar_row: {GObject.type_name(row_gtype)} created"
                    f" (uri={entry.uri})"
                )
            except Exception as e:
                _log(
                    f"_build_place_sidebar_row: native row construction failed ({e}),"
                    " using GtkListBoxRow"
                )
        else:
            _log(
                "_build_place_sidebar_row: no known sidebar row type registered,"
                " using GtkListBoxRow"
            )

        if list_row is None:
            list_row = Gtk.ListBoxRow()
            list_row.set_name(f"place_{entry.name}")
            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row_box.set_name(f"place_{entry.name}_box")
            icon_img = Gtk.Image.new_from_icon_name(icon_name)
            icon_img.set_name(f"place_{entry.name}_icon")
            icon_img.add_css_class("sidebar-icon")
            icon_img.set_icon_size(Gtk.IconSize.NORMAL)
            lbl = Gtk.Label(label=row_label)
            lbl.set_name(f"place_{entry.name}_label")
            lbl.add_css_class("sidebar-label")
            lbl.set_xalign(0.0)
            lbl.set_hexpand(True)
            lbl.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            row_box.append(icon_img)
            row_box.append(lbl)
            list_row.set_child(row_box)

        list_row.add_css_class("activatable")

        def _pin_row_icon():
            for w in _all_widgets(list_row):
                if not isinstance(w, Gtk.Image):
                    continue
                parent = w.get_parent()
                in_button = False
                while parent and parent is not list_row:
                    if isinstance(parent, Gtk.Button):
                        in_button = True
                        break
                    parent = parent.get_parent()
                if not in_button:
                    _pin_icon(w, icon_name)
                    break
            return GLib.SOURCE_REMOVE

        GLib.idle_add(_pin_row_icon)

        # Click dispatch, mirroring NautilusSidebar (nautilus-sidebar.c:3215): one
        # gesture on all buttons, dispatched from "released" (not "pressed" - the
        # sidebar's "pressed" handler only records drag-reorder coordinates), no
        # n_press guard. Middle opens a background tab (Ctrl+middle a new window,
        # nautilus-sidebar.c:3237); secondary opens the row's popover menu
        # (Computer carries _computer_context_menu).
        def _on_place_released(gesture, _n, x, y):
            button = gesture.get_current_button()
            if button == Gdk.BUTTON_MIDDLE:
                gesture.set_state(Gtk.EventSequenceState.CLAIMED)
                state = gesture.get_current_event_state()
                if state & Gdk.ModifierType.CONTROL_MASK:
                    self._do_open_window(entry.uri)
                else:
                    self._do_open_tab(entry.uri, win, make_active=False)
            elif button == Gdk.BUTTON_SECONDARY:
                if not callable(entry.menu):
                    return
                gesture.set_state(Gtk.EventSequenceState.CLAIMED)
                ctx_menu = entry.menu(self, win, entry)
                popover = ctx_menu.build_popover(list_row, f"place_{entry.name}")
                rect = Gdk.Rectangle()
                rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
                popover.set_pointing_to(rect)
                popover.popup()

        click = Gtk.GestureClick()
        click.set_button(0)
        click.connect("released", _on_place_released)
        list_row.add_controller(click)

        # Hide the eject button - not applicable for our injected entries.
        btn = _find_widget(list_row, buildable_id="eject_button")
        if isinstance(btn, Gtk.Button):
            btn.set_visible(False)

        return list_row

    def _find_sidebar_scrolled_window(
        self, nautilus_sidebar: Gtk.Widget, native_listbox: Gtk.ListBox | None = None
    ) -> Gtk.ScrolledWindow | None:
        target_listbox = native_listbox or self._find_sidebar_listbox(nautilus_sidebar)
        for w in _all_widgets(nautilus_sidebar):
            if not isinstance(w, Gtk.ScrolledWindow):
                continue
            if target_listbox is None:
                return w
            parent = target_listbox.get_parent()
            while parent is not None:
                if parent is w:
                    return w
                parent = parent.get_parent()
        return None

    def _inject_separate_computer_row(
        self,
        win: Gtk.Window,
        nautilus_sidebar: Gtk.Widget,
        native_scrolled_window: Gtk.ScrolledWindow,
        native_listbox: Gtk.ListBox,
    ) -> bool:
        """Put a one-row 'Computer' listbox ABOVE Nautilus' native list, inside
        the sidebar's scrolled window. Computer is visually its own section; it
        never enters Nautilus' managed listbox, so Nautilus rebuilds (bookmark
        drag-and-drop, mounts) never move/remove it - no flicker. The native
        places stay native; we only hide the ones toggled off via settings.

        Builds one row per entry in PLACES (currently just Computer), rather than
        hardcoding the single row, so a future custom place only needs adding to
        PLACES."""
        my_computer_listbox = Gtk.ListBox()
        my_computer_listbox.set_name("sidebar_my_computer_listbox")
        my_computer_listbox.add_css_class("navigation-sidebar")
        my_computer_listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        row_uris: dict[Gtk.ListBoxRow, str] = {}
        for entry in PLACES:
            row = self._build_place_sidebar_row(win, entry, nautilus_sidebar)
            my_computer_listbox.append(row)
            row_uris[row] = entry.uri
        my_computer_listbox.connect(
            "row-activated", lambda _lb, row: self._navigate_to(row_uris.get(row, DISKS_URI), win)
        )
        computer_row = my_computer_listbox.get_row_at_index(0)

        # Wrap: the My Computer one-row list on top, the native list below, in the existing
        # scrolled window so both scroll together. boundary_separator stands between
        # them, hidden by default, and only shown when every native place row is
        # hidden (see _apply_native_place_visibility for why).
        wrapper = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        wrapper.set_name("sidebar_my_computer_wrapper")
        wrapper.add_css_class("navigation-sidebar")
        native_scrolled_window.set_child(wrapper)
        boundary_separator = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        boundary_separator.set_name("sidebar_my_computer_boundary_separator")
        boundary_separator.set_visible(False)
        wrapper.append(my_computer_listbox)
        wrapper.append(boundary_separator)
        wrapper.append(native_listbox)

        # Cross-deselect: the My Computer selection and any native section selection
        # are mutually exclusive (GTK does not deselect across separate listboxes).
        all_lbs = [my_computer_listbox] + [
            w for w in _all_widgets(nautilus_sidebar) if isinstance(w, Gtk.ListBox)
        ]
        _deselecting = [False]

        def _on_any_lb_selected(selected_lb, row):
            if _deselecting[0] or row is None:
                return
            _deselecting[0] = True
            try:
                for lb in all_lbs:
                    if lb is not selected_lb:
                        lb.unselect_all()
            finally:
                _deselecting[0] = False

        for lb in all_lbs:
            lb.connect("row-selected", _on_any_lb_selected)

        state = self._windows.get(win)
        if state is not None:
            state["sidebar_listbox"] = native_listbox
            state["sidebar_my_computer_listbox"] = my_computer_listbox
            state["sidebar_boundary_separator"] = boundary_separator
            state["sidebar_row"] = computer_row
            state["sidebar_native"] = True
            state["sidebar_native_widget"] = nautilus_sidebar

        # Hide native place rows the user toggled off, and re-apply on rebuilds.
        self._apply_native_place_visibility(native_listbox, boundary_separator)
        self._attach_bookmark_context_menus(win, native_listbox)
        self._apply_bookmark_icons(native_listbox)
        self._watch_native_list_changes(win, native_listbox)

        self._wire_computer_drop_dimming(wrapper, computer_row)
        return True

    def _wire_computer_drop_dimming(self, area: Gtk.Widget, computer_row: Gtk.ListBoxRow) -> None:
        """Grey out (desensitize) the Computer row while a file drag is over the
        sidebar, matching Nautilus' own invalid-drop-target feedback. Computer
        has no Gtk.DropTarget of its own (it is not a real folder, like
        recent:/// or starred:///), so a desensitized row simply never receives
        pointer/drop events - that IS the rejection mechanism, not a DropTarget
        that claims and refuses the drag.

        Latches to the Gdk.Drag's lifetime rather than raw enter/leave: a child
        widget's own Gtk.DropTarget (Nautilus' native listbox has one) steals
        the drop and fires a spurious `leave` on this controller as the pointer
        crosses into it, which would flicker the dimming off. See
        tmp/Logs/2026-06-16 - 1719 - [Feature] Drag-and-drop sidebar visual
        feedback.md for the full investigation - this is the proven fix."""
        drag_state = {"drag": None}

        def _set_dimmed(dimmed: bool) -> None:
            computer_row.set_sensitive(not dimmed)

        def _undim(*_a):
            drag_state["drag"] = None
            _set_dimmed(False)

        def _on_enter(controller, *_a):
            _set_dimmed(True)
            if drag_state["drag"] is not None:
                return
            drop = controller.get_drop()
            drag = drop.get_drag() if drop is not None else None
            if drag is None:
                return
            drag_state["drag"] = drag
            drag.connect("dnd-finished", _undim)
            drag.connect("cancel", lambda *_a: _undim())

        def _on_leave(controller, *_a):
            def _check():
                if not controller.contains_pointer():
                    _set_dimmed(False)
                return GLib.SOURCE_REMOVE

            GLib.idle_add(_check)

        motion = Gtk.DropControllerMotion.new()
        motion.connect("enter", _on_enter)
        motion.connect("motion", lambda *_a: _set_dimmed(True))
        motion.connect("leave", _on_leave)
        area.add_controller(motion)

    def _watch_native_list_changes(self, win: Gtk.Window, native_listbox: Gtk.ListBox) -> None:
        """Re-apply native place visibility whenever Nautilus mutates the list.

        `observe_children()` returns a live GListModel of the listbox's child
        rows; its items-changed fires on add, remove AND reorder. Because we set
        row visibility with `set_visible()` (a property Nautilus can overwrite
        when it rebuilds a row), a one-shot pass is not enough - this watcher
        re-applies it.

        Changes arrive in bursts (Nautilus rebuilds several rows at once), so we
        coalesce into a single GLib.idle_add pass via a per-window pending flag
        (idle, not a timeout - no polling)."""
        state = self._windows.get(win)
        if state is None:
            return
        model = native_listbox.observe_children()

        def _rescan() -> bool:
            state["native_hide_pending"] = False
            self._apply_native_place_visibility(
                native_listbox, state.get("sidebar_boundary_separator")
            )
            self._attach_bookmark_context_menus(win, native_listbox)
            self._apply_bookmark_icons(native_listbox)
            return GLib.SOURCE_REMOVE

        def _on_items_changed(*_a) -> None:
            if state.get("native_hide_pending"):
                return
            state["native_hide_pending"] = True
            GLib.idle_add(_rescan)

        handler_id = model.connect("items-changed", _on_items_changed)
        # Keep refs alive for the window's lifetime so the model is not collected.
        state["native_hide_model"] = model
        state["native_hide_handler"] = handler_id

    def _gtk_bookmark_uris(self) -> set:
        return bookmarks.bookmark_uris()

    def _get_bookmark_icons(self) -> dict:
        return bookmarks.get_bookmark_icons(self._gsettings)

    def _set_bookmark_icon(self, uri: str, icon_name: str) -> None:
        bookmarks.set_bookmark_icon(self._gsettings, uri, icon_name)

    def _clear_bookmark_icon(self, uri: str) -> None:
        bookmarks.clear_bookmark_icon(self._gsettings, uri)

    def _apply_bookmark_icons(self, native_listbox: Gtk.ListBox) -> None:
        bookmarks.apply_bookmark_icons(self, native_listbox)

    def _reapply_bookmark_icons_all_windows(self) -> bool:
        return bookmarks.reapply_bookmark_icons_all_windows(self)

    def _attach_bookmark_context_menus(self, win: Gtk.Window, native_listbox: Gtk.ListBox) -> None:
        bookmarks.attach_bookmark_context_menus(self, win, native_listbox)

    # ── Preferred Folders (issue #30) ───────────────────────────────────────────

    def _get_preferred_folders(self) -> list:
        return preferred_folders.get_preferred_entries(self._gsettings)

    def _add_preferred_folder(self, uri: str) -> None:
        preferred_folders.add_preferred(self._gsettings, uri)

    def _do_remove_preferred_folder(self, pf: "PreferredFolder", win: Gtk.Window) -> None:
        preferred_folders.remove_preferred(self._gsettings, pf.key)

    def _commit_preferred_order(self, keys: list[str]) -> bool:
        """Persist a drag-reordered Preferred Folders sequence. Writing the
        gsettings key fires _on_settings_changed -> _repopulate_visible, which
        rebuilds the cards from the saved order (clearing any drag-preview
        state). Called via GLib.idle_add from the drop handler so the rebuild
        happens after the drag has fully torn down."""
        preferred_folders.save_order(self._gsettings, keys)
        return GLib.SOURCE_REMOVE

    # ── "Pin to My Computer" injection (issue #30) ────────────────────────────

    def _attach_pathbar_menu_watch(self, win: Gtk.Window) -> None:
        preferred_folders.attach_pathbar_menu_watch(self, win)

    def _open_bookmark_icon_picker(self, uri: str, label: str, row) -> None:
        bookmarks.open_bookmark_icon_picker(self, uri, label, row)

    def _apply_native_place_visibility(
        self, native_listbox: Gtk.ListBox, boundary_separator: Gtk.Separator | None = None
    ) -> None:
        """Instance shim so call sites can use self; delegates to the helper."""
        _apply_native_place_visibility(native_listbox, self._gsettings, boundary_separator)

    def _reapply_sidebar_visibility(self) -> bool:
        """Re-apply native place visibility in every window after a settings change."""
        for _win, state in list(self._windows.items()):
            native_listbox = state.get("sidebar_listbox")
            if native_listbox is not None:
                self._apply_native_place_visibility(
                    native_listbox, state.get("sidebar_boundary_separator")
                )
        return GLib.SOURCE_REMOVE

    def _inject_sidebar_link(self, win: Gtk.Window) -> bool:
        """Inject a separate one-row 'Computer' section above Nautilus' native
        sidebar list. Computer lives in its own listbox (never in Nautilus'
        managed list), so Nautilus rebuilds never move it. Every other place
        stays native; we only hide the ones toggled off via settings.
        """
        split_view = next(
            (w for w in _all_widgets(win) if isinstance(w, Adw.OverlaySplitView)), None
        )
        sidebar_toolbar = split_view.get_sidebar() if split_view else None
        if not isinstance(sidebar_toolbar, Adw.ToolbarView):
            _log(
                f"_inject_sidebar_link: expected AdwToolbarView from get_sidebar(), "
                f"got {type(sidebar_toolbar).__name__ if sidebar_toolbar else 'None'}"
            )
            return False

        nautilus_sidebar = sidebar_toolbar.get_content()
        if nautilus_sidebar is None:
            _log("_inject_sidebar_link: AdwToolbarView content is None")
            return False

        _log(f"_inject_sidebar_link: content={type(nautilus_sidebar).__name__}")

        native_listbox = self._find_sidebar_listbox(nautilus_sidebar)
        if native_listbox is None:
            _log("_inject_sidebar_link: native listbox unavailable")
            return False

        native_scrolled_window = self._find_sidebar_scrolled_window(
            nautilus_sidebar, native_listbox
        )
        if native_scrolled_window is None:
            _log("_inject_sidebar_link: native scrolled window unavailable")
            return False

        # Guard: skip if we already wrapped this sidebar (double-injection).
        existing = native_scrolled_window.get_child()
        if existing is not None and existing.get_name() == "sidebar_my_computer_wrapper":
            _log("_inject_sidebar_link: wrapper already present, skipping")
            return True

        return self._inject_separate_computer_row(
            win, nautilus_sidebar, native_scrolled_window, native_listbox
        )

    def _fix_pathbar_icon(self, win: Gtk.Window) -> bool:
        """Non-invasive chip icon update. Called on each title-change arrival at
        computer:///. Scans the window for the chip label, finds the existing
        Gtk.Image in the chip, and pins it to computer-symbolic.

        Never connects signals to Nautilus's internal pathbar GtkStack or box
        models, and never calls set_child() — those caused the GTK_IS_STACK crash
        (issue #11). The navigation trigger already fires on every location
        change, so no persistent watcher is needed.

        On some Nautilus/GVfs combinations (confirmed: Nautilus 47.0 + gvfs 1.56
        on Fedora 41) nautilus_is_root_for_scheme() fails to
        recognise computer:/// as a root location and falls back to its generic
        path-segment ("NORMAL_BUTTON") rendering: no Gtk.Image is ever created
        for the chip, and a leading "/" separator label is shown before it. In
        that case there is no existing image to pin, so one is created and
        prepended, and the stray separator is hidden."""
        target_labels = {COMPUTER_LABEL, _LOCATION_TITLE}

        for w in _all_widgets(win):
            if not isinstance(w, Gtk.Label):
                continue
            label_text = w.get_label()
            if not label_text or label_text.strip() not in target_labels:
                continue

            # Skip labels inside the sidebar or the tab bar. A tab whose page
            # title happens to be "Computer" (i.e. that tab is showing
            # computer:///) has an AdwTab containing its own Label + Image;
            # without this check the walk below would pin the computer icon
            # onto that tab's icon, which then stays frozen there even after
            # the tab navigates elsewhere (issue #29).
            ancestor = w.get_parent()
            in_sidebar = False
            button = None
            while ancestor:
                cls = type(ancestor).__name__
                if "Sidebar" in cls or "PlacesView" in cls or "Tab" in cls:
                    in_sidebar = True
                    break
                if cls in ("NautilusPathBarButton", "GtkButton", "AdwButton", "Button"):
                    button = ancestor
                    break
                ancestor = ancestor.get_parent()
            if in_sidebar:
                continue

            # Walk up to the chip container
            container = w.get_parent()
            while container and type(container).__name__ not in (
                "NautilusPathBarButton",
                "GtkButton",
                "GtkBox",
                "Button",
                "Box",
            ):
                container = container.get_parent()

            if not container:
                continue

            # Pin the existing chip image — no structural changes to Nautilus's tree
            image = None
            for sub in _all_widgets(container):
                if isinstance(sub, Gtk.Image):
                    image = sub
                    break

            if image is not None:
                _pin_icon(image, self._get_computer_icon())
            else:
                # Fallback layout: no Gtk.Image exists in the chip at all.
                # Create one so the chip isn't left blank. This container was
                # built by Nautilus with spacing=2 (meant for a lone label,
                # per the NORMAL_BUTTON case in nautilus-pathbar.c) rather
                # than the spacing=6 every other root chip's icon+label box
                # uses, so match that native value instead of leaving the
                # icon flush against the text.
                image = Gtk.Image.new_from_icon_name(self._get_computer_icon())
                container.prepend(image)
                if isinstance(container, Gtk.Box):
                    container.set_spacing(6)

            # The fallback layout also prefixes the chip with a leading "/"
            # separator, since Nautilus is treating computer:/// as an
            # ordinary path segment instead of a filesystem root. A root
            # location should never show a path separator ahead of it.
            if button is not None:
                outer = button.get_parent()
                if outer is not None:
                    sep = outer.get_first_child()
                    if sep is not button and isinstance(sep, Gtk.Label) and sep.get_label() == "/":
                        sep.set_visible(False)

        return False

    # ── MenuProvider ─────────────────────────────────────────────────────────
    # get_file_items() is (ab)used purely as Nautilus's official, reliable feed
    # of "what's currently selected" -- it fires on every selection change, and
    # nautilus_files_view_pop_up_selection_context_menu() forces a pending
    # update before popping up, so the cache below is guaranteed fresh at
    # right-click time. We do NOT return items through this API: every
    # extension's get_file_items() results land in one shared, separator-less
    # GMenu section (selection-extensions-section), so our two lines could end
    # up jammed against another extension's with no visual break. Instead we
    # inject directly into the native selection popover (see
    # _attach_file_view_context_menu below), exactly like the existing
    # bookmark-row and pathbar injections, which gives full control over
    # placement and a real separator.

    def get_file_items(self, *args):
        files = args[-1] if args else []
        self._last_selected_folder_uri = None
        if len(files) == 1 and files[0].is_directory():
            self._last_selected_folder_uri = files[0].get_uri()
        return []

    def get_background_items(self, *args):
        return []

    # ── Folder selection: Bookmarks/Preferred injection (native file view) ──

    def _attach_file_view_context_menu(self, win: Gtk.Window) -> None:
        file_view_menu.attach_file_view_context_menu(self, win)

    # ── Column view prototype (native view-options popover) ─────────────────

    def _inject_column_view_entry(self, win: Gtk.Window) -> None:
        column_view.inject_column_view_entry(self, win)
