"""Column View: Miller (macOS Finder-style) columns injected into Nautilus."""

import fnmatch
import functools
import os
import shutil
import stat

import gi

gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Gio", "2.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk

from nautilus_my_computer import bookmarks, common, components, preferred_folders
from nautilus_my_computer.common import (
    _COLUMN_MAX_WIDTH,
    _COLUMN_PREVIEW_WIDTH,
    _COLUMN_WIDTH,
    N_,
    _,
    _all_widgets,
    _bundled_gicon,
    _focus_owns_text_selection,
    _icon_name_renders,
    _log,
    _native,
)
from nautilus_my_computer.context_menu import (
    ContextMenu,
    ContextMenuItem,
    ContextMenuSection,
    background_clipboard_section,
    background_creation_section,
    background_terminal_section,
    clipboard_actions_section,
    file_actions_section,
    my_computer_additions_section,
    open_section,
    properties_section,
)
from nautilus_my_computer.widgets import (
    MyComputerColumn,
    MyComputerPreviewColumn,
    MyComputerToggleButton,
)

try:
    gi.require_version("GnomeAutoar", "0.1")
    from gi.repository import GnomeAutoar
except (ValueError, ImportError):
    GnomeAutoar = None

VIEW_COLUMN = "column"

# Name Column View is added under on each slot's own GtkStack (see
# watch_tab_view/_do_inject_into_slot below). Nautilus's own two stack
# children (vbox, global_search_page) are added via gtk_stack_add_child
# with no name, so this name can never collide with anything of theirs.
_SLOT_STACK_CHILD_NAME = "mc-column"
_SLOT_INIT_RETRY_MS = 20  # retry interval while waiting for a new slot to settle
_SLOT_INIT_MAX_ATTEMPTS = 100  # ~2s budget, mirrors main.py's _WIN_INIT_MAX_ATTEMPTS
_TYPEAHEAD_RESET_MS = 1200
_NAUTILUS_SCRIPT_MAX_ITEMS = 256
_NAUTILUS_SCRIPT_MAX_DEPTH = 8

# Whether a navigation event moves into a subfolder of where browsing
# currently is (NAV_DOWN), back toward a parent (NAV_UP), or re-selects
# within the same column that was already focused (NAV_SELF). Detected from
# the shape of the change rather than comparing URIs directly: NAV_DOWN is
# the row activated living in the currently deepest open column (so the new
# folder is a child of the current one). NAV_SELF is a click landing
# in the same column that already held focus (self.focused_index) when that
# column isn't the deepest one -- re-choosing a sibling in a column the user
# is already working in, not backing out of it, so it should scroll like
# NAV_DOWN rather than jump left. NAV_UP is anything else -- an earlier,
# not-previously-focused column was clicked, or the new location doesn't
# extend the existing chain -- which always collapses the columns beyond
# that point, since the new location is no longer a descendant of what's
# currently open. Exists so callers (scroll/fade rules) can branch on it
# without re-deriving it from index/common bookkeeping each time.
NAV_DOWN = "down"
NAV_UP = "up"
NAV_SELF = "self"

_HORIZONTAL_SCROLL_OWNER_CLASS = "mc-horizontal-scroll-owner"


def _widget_or_ancestor_has_css_class(widget, css_class: str) -> bool:
    """Return whether *widget* is inside a surface that owns horizontal scroll.

    GTK does not expose the picked event widget directly in Python, so the
    capture handler picks it from the event coordinates and then walks upward.
    Keeping the walk separate makes the propagation rule independent of widget
    realization state.
    """
    while widget is not None:
        if widget.has_css_class(css_class):
            return True
        widget = widget.get_parent()
    return False


def _scroll_event_targets_css_class(controller, css_class: str) -> bool:
    """Return whether the current pointer event is over a matching widget."""
    event = controller.get_current_event()
    if event is None:
        return False
    try:
        has_position, surface_x, surface_y = event.get_position()
        surface = event.get_surface()
        native = Gtk.Native.get_for_surface(surface) if surface is not None else None
        if not has_position or native is None:
            return False

        # Raw GdkEvent positions use surface coordinates. GtkWidget.pick()
        # expects the GtkNative's widget coordinates, and this translation is
        # precisely the offset between those two coordinate systems.
        offset_x, offset_y = native.get_surface_transform()
        picked = native.pick(
            surface_x + offset_x,
            surface_y + offset_y,
            Gtk.PickFlags.DEFAULT,
        )
    except (AttributeError, RuntimeError, TypeError):
        # A widget may disappear while an async preview is being replaced.
        return False
    return _widget_or_ancestor_has_css_class(picked or native, css_class)


def _scroll_event_is_over_widget(controller, widget: Gtk.Widget) -> bool:
    """Check event coordinates against *widget*, including nested surfaces."""
    event = controller.get_current_event()
    if event is None:
        return False
    try:
        has_position, surface_x, surface_y = event.get_position()
        surface = event.get_surface()
        native = Gtk.Native.get_for_surface(surface) if surface is not None else None
        if not has_position or native is None:
            return False

        # WebKit can render through its own GtkNative. In that case the event
        # surface itself is already inside the preview and no bounds transform
        # against the toplevel is needed.
        if native is widget or widget.is_ancestor(native):
            return True

        offset_x, offset_y = native.get_surface_transform()
        has_bounds, bounds = widget.compute_bounds(native)
        if not has_bounds:
            return False
        x = surface_x + offset_x
        y = surface_y + offset_y
        return (
            bounds.get_x() <= x < bounds.get_x() + bounds.get_width()
            and bounds.get_y() <= y < bounds.get_y() + bounds.get_height()
        )
    except (AttributeError, RuntimeError, TypeError):
        return False


def col_nav_direction(from_index: int, to_index: int) -> str:
    """NAV_DOWN/SELF/UP purely from the column index moved from and to --
    e.g. from_index=self.focused_index, to_index=the clicked column. Deeper
    than from_index is DOWN even if to_index isn't the deepest column open
    (see _on_real_row_activated: comparing against "deepest" instead of
    focused_index mislabeled that case as UP and scrolled backward on a
    click that was actually moving forward)."""
    if to_index > from_index:
        return NAV_DOWN
    elif to_index == from_index:
        return NAV_SELF
    else:
        return NAV_UP


def row_nav_direction(from_index: int, to_index: int) -> str:
    """Same comparison as col_nav_direction, for a row index within a single
    column once something tracks an active row index there -- no caller yet."""
    if to_index > from_index:
        return NAV_DOWN
    elif to_index == from_index:
        return NAV_SELF
    else:
        return NAV_UP


# Retained for the existing focus/selection bookkeeping, but keyboard
# navigation itself is currently disabled by the Column View's key controller.
_COLUMN_KEYBOARD_NAV = False

# Master switch for user drag-to-resize on folder columns. On: dragging a
# column's handle updates that column's width, clamped to _COLUMN_MAX_WIDTH
# (see _on_paned_position_changed) -- no manual floor clamp needed, GTK's own
# Gtk.Paned already refuses to shrink a column below MyComputerColumn's
# size_request(_COLUMN_MIN_WIDTH) floor (widgets.py), but size_request only
# ever sets a minimum, so the ceiling still needs a manual reassert. Off
# reverts to a fixed COLUMN_WIDTH per column (see _on_paned_position_fixed).
_COLUMN_RESIZE_ENABLED = True

# Icon used for the Column segment of the Grid/List/Column switcher, and to
# read back which side of the native split button's TARGET-view icon-name
# binding (nautilus_files_view_get_toggle_icon_name) is currently showing:
# "view-grid-symbolic" while list is showing, "view-list-symbolic" while grid
# is showing.
# Most third-party packs ship view-column-symbolic (Papirus, Colloid, Tela,
# WhiteSur, kora, MacTahoe, Reversal, Vimix, elementary, Qogir, Fluent,
# Numix); Adwaita, Yaru and Breeze do not (issue #101, see
# tmp/issue-101-icon-theme-research.md). Those fall through to our own
# bundled copy of the same name (_resolve_column_icon() below) rather than
# to view-dual-symbolic -- that name resolves everywhere but Yaru draws it
# as an open book, and we cannot audit every icon pack in existence.
_COLUMN_ICON_NAME = "view-column-symbolic"
_ICON_TARGET_GRID = "view-grid-symbolic"  # shown while sitting on list
_ICON_TARGET_LIST = "view-list-symbolic"  # shown while sitting on grid (default fallback)
_NATIVE_TOGGLE_ACTION = "slot.files-view-mode-toggle"

# Single sources of truth are common._COLUMN_WIDTH for folder columns and
# common._COLUMN_PREVIEW_WIDTH for the trailing preview column. These aliases
# keep the layout arithmetic below legible.
COLUMN_WIDTH = _COLUMN_WIDTH
PREVIEW_WIDTH = _COLUMN_PREVIEW_WIDTH
HANDLE_WIDTH_ESTIMATE = 12
# Generous hit margin (in px, either side of a paned's current position)
# used to tell a genuine press-and-drag on the handle apart from GTK
# repositioning the handle itself during layout (see
# _on_paned_handle_pressed/_on_paned_position_changed). Errs wide rather
# than narrow since HANDLE_WIDTH_ESTIMATE above is itself only an estimate
# and the rendered handle width varies by theme.
HANDLE_HIT_SLOP = 16
# Gtk.Adjustment can emit several "changed" events in a quick burst when
# several rebuilds/relayouts land close together (e.g. a resize settling
# right before an add/trim fires) -- acting on the *first* one grabs a stale
# intermediate state instead of the final settled one. Debounce: wait this
# long after the *last* "changed" event before actually applying whichever
# scroll action is pending.
SCROLL_SETTLE_DEBOUNCE_MS = 50
# Duration/easing for the programmatic hadjustment scroll (see
# _animate_scroll_to) -- matches GTK's own kinetic-scroll deceleration feel
# (Gtk.ScrolledWindow animates panel-size-driven adjustment changes, e.g.
# when a column closes, the same way) rather than an instant jump.
SCROLL_ANIMATION_DURATION_MS = 200
# Shorter than the new-column fade: the preview column is rebuilt on every
# single click (not just drill-downs), so a full 200ms would feel sluggish --
# this just needs to be enough to smooth over the replace-flash, not to read
# as a deliberate reveal.
PREVIEW_FADE_DURATION_MS = 100
# Keyboard row changes move the selection immediately but defer the actual
# commit (rebuild the chain, replace the preview, push Nautilus's real slot
# location) by this long, restarting the clock on each further key. Held
# arrow keys repeat roughly every 30ms once X's repeat delay elapses, so
# without this every intermediate row on the way to the target one paid for a
# full _rebuild_chain plus its own slot.open-location navigation. Short enough
# that a single deliberate press still feels immediate.
ROW_COMMIT_DEBOUNCE_MS = 100
# How many not-yet-echoed slot.open-location pushes to remember (see
# _sync_slot_location / sync_to_uri). Bounded because an echo is not
# guaranteed to arrive at all -- Nautilus can refuse or supersede a
# navigation -- and a permanently pinned entry would swallow a later, real
# navigation to the same folder. Drill-downs land 0.7-9s apart in practice,
# so anything beyond a couple of outstanding pushes is already pathological.
_MAX_PENDING_SLOT_URIS = 8
# A slot push which never echoes must not suppress a genuine navigation to
# the same URI much later. Timestamps keep the loop guard useful only for the
# short async window in which Nautilus can reasonably deliver its echo.
_PENDING_SLOT_URI_TTL_US = 3_000_000
# How many frame ticks to keep re-asserting keyboard focus onto a freshly
# drilled-into column after a commit (see _arm_focus_retry) -- long enough to
# outlast Nautilus's own async re-focus of its real, hidden GtkGridView for
# the newly navigated slot.
_FOCUS_RETRY_FRAMES = 30

# Nautilus treats archives specially when Files itself is their default
# handler: activating one extracts it instead of launching the desktop file.
# The desktop file deliberately uses ``nautilus --new-window %U``, so sending
# an archive through Gio.AppInfo (as Miller View historically did) always
# creates another window and bypasses Nautilus's extraction branch.
_NAUTILUS_DESKTOP_ID = "org.gnome.Nautilus.desktop"
_ARCHIVE_SUFFIXES = tuple(
    sorted(
        (
            ".tar.bz2",
            ".tar.gz",
            ".tar.lz",
            ".tar.lzma",
            ".tar.lzo",
            ".tar.xz",
            ".tar.zst",
            ".tbz2",
            ".tgz",
            ".txz",
            ".7z",
            ".bz2",
            ".cab",
            ".cpio",
            ".gz",
            ".iso",
            ".lz",
            ".lzma",
            ".rar",
            ".tar",
            ".xz",
            ".zip",
            ".zst",
        ),
        key=len,
        reverse=True,
    )
)


def _archive_folder_name(basename: str) -> str:
    """Return the directory name used for an extracted archive."""
    folded = basename.casefold()
    for suffix in _ARCHIVE_SUFFIXES:
        if folded.endswith(suffix) and len(basename) > len(suffix):
            return basename[: -len(suffix)]
    stem, _extension = os.path.splitext(basename)
    return stem or basename or _native("Archive")


def _should_extract_archive(
    content_type: str | None,
    default_app_id: str | None,
    autoar_supported: bool,
) -> bool:
    """Mirror Nautilus's archive activation gate without treating ZIP-based
    documents (EPUB, DOCX, and similar formats) as archives."""
    return bool(content_type and autoar_supported and default_app_id == _NAUTILUS_DESKTOP_ID)


def default_root_uri() -> str:
    """Fallback root for the very first (pre-navigation) widget build, before
    any real location is known -- never seen by the user in practice, since
    entering Column View (Ctrl+3) always re-seeds from the real current
    location via enter_column_view()."""
    return Gio.File.new_for_path(GLib.get_home_dir()).get_uri()


def _parse_nautilus_clipboard_data(data: bytes) -> tuple[list[str], bool] | None:
    """Return (URIs, is_cut) for x-special/gnome-copied-files payloads."""
    lines = data.decode("utf-8", errors="replace").splitlines()
    if len(lines) < 2 or lines[0] not in ("copy", "cut"):
        return None
    uris = [line for line in lines[1:] if line]
    return (uris, lines[0] == "cut") if uris else None


def _parse_uri_list_data(data: bytes) -> list[str]:
    """Decode RFC-style text/uri-list data, ignoring comments and blank lines."""
    return [
        line.strip()
        for line in data.decode("utf-8", errors="replace").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


class _MillerCanvas(Gtk.Fixed, Gtk.Scrollable):
    """The Miller chain's single-child scrolling surface. Implements
    Gtk.Scrollable itself so Gtk.ScrolledWindow never wraps it in an
    internal Gtk.Viewport -- that Viewport is what was auto-recomputing and
    clamping the hadjustment's value on every allocate whose measured
    natural width changed (e.g. trimming stale columns a beat before a new
    one is appended), producing a native, instant, unanimated jump a frame
    ahead of our own scroll animation (_ColumnViewHost._animate_scroll_to).

    As a Gtk.Scrollable, Gtk.ScrolledWindow still creates the hadjustment
    for us (on set_child) but never touches its bounds or value again on
    its own -- _ColumnViewHost owns lower/upper/page-size/value entirely
    from here on (see _sync_root_width), so every visible scroll-position
    change from here on is one it explicitly chose, never one GTK computed
    behind its back."""

    __gtype_name__ = "MillerCanvas"

    hadjustment = GObject.Property(type=Gtk.Adjustment, default=None)
    vadjustment = GObject.Property(type=Gtk.Adjustment, default=None)
    hscroll_policy = GObject.Property(
        type=Gtk.ScrollablePolicy, default=Gtk.ScrollablePolicy.MINIMUM
    )
    vscroll_policy = GObject.Property(
        type=Gtk.ScrollablePolicy, default=Gtk.ScrollablePolicy.MINIMUM
    )

    def __init__(self) -> None:
        super().__init__()
        # Clip whatever's outside our own allocated (viewport-sized) bounds
        # -- without an internal Viewport, nothing else does this for us.
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self._content: Gtk.Widget | None = None
        self.connect("notify::hadjustment", self._on_hadjustment_set)

    def _on_hadjustment_set(self, _widget, _pspec) -> None:
        adj = self.get_hadjustment()
        if adj is not None:
            adj.connect("value-changed", lambda _adj: self._reposition())

    def set_content(self, widget: Gtk.Widget | None) -> None:
        """Replace the single child this canvas scrolls. Always placed at
        logical x=0 -- the current hadjustment value supplies the visible
        offset (see _reposition), so callers never need to think about
        scroll position when swapping content."""
        if self._content is not None:
            self.remove(self._content)
        self._content = widget
        if widget is not None:
            self.put(widget, 0, 0)
            self._reposition()

    def _reposition(self) -> None:
        if self._content is None:
            return
        adj = self.get_hadjustment()
        offset = -adj.get_value() if adj is not None else 0
        self.move(self._content, offset, 0)


class NativeCutStateObserver:
    """Observe Nautilus's decoded ``NautilusViewItem:is-cut`` model state.

    This is deliberately read-only and never touches GdkClipboard. Nautilus
    remains responsible for decoding its private clipboard object; Miller only
    mirrors the resulting item properties that are already present in the
    covered native List/Grid view.
    """

    def __init__(self, win: Gtk.Window, on_changed) -> None:
        self._win = win
        self._on_changed = on_changed
        self._view = None
        self._view_handler_id = 0
        self._model = None
        self._model_handler_id = 0
        self._item_handlers: list[tuple[GObject.Object, int]] = []

    def start(self) -> None:
        self.stop()
        candidates = [
            widget
            for widget in _all_widgets(self._win)
            if type(widget).__name__ in ("NautilusGridView", "NautilusListView")
        ]
        if not candidates:
            self._on_changed(set())
            return
        self._view = next((view for view in candidates if view.get_mapped()), candidates[0])
        try:
            self._view_handler_id = self._view.connect("notify::model", self._on_model_changed)
            model = self._view.get_property("model")
        except (AttributeError, TypeError):
            self.stop()
            self._on_changed(set())
            return
        self._bind_model(model)

    def stop(self) -> None:
        self._disconnect_items()
        if self._model is not None and self._model_handler_id:
            self._model.disconnect(self._model_handler_id)
        self._model = None
        self._model_handler_id = 0
        if self._view is not None and self._view_handler_id:
            self._view.disconnect(self._view_handler_id)
        self._view = None
        self._view_handler_id = 0

    def _on_model_changed(self, view, _pspec) -> None:
        self._bind_model(view.get_property("model"))

    def _bind_model(self, model) -> None:
        self._disconnect_items()
        if self._model is not None and self._model_handler_id:
            self._model.disconnect(self._model_handler_id)
        self._model = model
        self._model_handler_id = 0
        if model is None:
            self._on_changed(set())
            return
        self._model_handler_id = model.connect("items-changed", self._on_items_changed)
        self._bind_items()

    def _on_items_changed(self, _model, _position, _removed, _added) -> None:
        self._bind_items()

    def _disconnect_items(self) -> None:
        for item, handler_id in self._item_handlers:
            try:
                item.disconnect(handler_id)
            except TypeError:
                pass
        self._item_handlers.clear()

    def _bind_items(self) -> None:
        self._disconnect_items()
        if self._model is None:
            return
        for index in range(self._model.get_n_items()):
            tree_row = self._model.get_item(index)
            item = tree_row.get_item() if isinstance(tree_row, Gtk.TreeListRow) else tree_row
            if item is None or item.find_property("is-cut") is None:
                continue
            handler_id = item.connect("notify::is-cut", self._on_item_cut_changed)
            self._item_handlers.append((item, handler_id))
        self._emit_cut_uris()

    def _on_item_cut_changed(self, _item, _pspec) -> None:
        self._emit_cut_uris()

    def _emit_cut_uris(self) -> None:
        uris = set()
        for item, _handler_id in self._item_handlers:
            if not item.get_property("is-cut"):
                continue
            file_obj = item.get_property("file")
            uri = self._file_uri(file_obj)
            if uri:
                uris.add(uri)
        self._on_changed(uris)

    @staticmethod
    def _file_uri(file_obj) -> str | None:
        if file_obj is None:
            return None
        get_uri = getattr(file_obj, "get_uri", None)
        if callable(get_uri):
            return get_uri()
        get_location = getattr(file_obj, "get_location", None)
        if callable(get_location):
            location = get_location()
            return location.get_uri() if location is not None else None
        return None


class _ColumnViewHost:
    def __init__(self, ext, win: Gtk.Window, display: Gdk.Display | None, root_uri: str) -> None:
        self._ext = ext
        self._win = win
        self._root_uri = root_uri
        self._clipboard_uris: list[str] = []
        self._clipboard_is_cut = False
        self._clipboard = win.get_clipboard()
        self._clipboard_handler_id = self._clipboard.connect("changed", self._on_clipboard_changed)
        self._operation_monitors: list[Gio.FileMonitor] = []
        self._operation_timeout_ids: set[int] = set()
        self._archive_operations: dict[object, Gio.Cancellable] = {}
        self._extracting_archive_uris: set[str] = set()
        # Output names selected by concurrent extractors but not necessarily
        # created on disk yet. Without this reservation, two archives with
        # the same stem can both pass the async existence probe and race to
        # write the same folder.
        self._reserved_archive_output_uris: set[str] = set()
        self._navigation_generation = 0
        self._destroyed = False
        self._suspended = False
        self._suspended_preview_uris: list[str] = []
        self._pending_created_renames: dict[Gtk.Widget, str] = {}
        self._native_cut_observer = NativeCutStateObserver(win, self._apply_native_cut_uris)
        self._sort = ext._nautilus_prefs.resolve_column_sort(root_uri)

        self.columns = self._build_columns()
        self.preview_column = self._make_preview_column()
        self.paneds = []
        self._clamping = False
        # Index of the column whose paned handle is currently pressed and
        # held by the user (see _on_paned_handle_pressed/_released), or None
        # -- lets _on_paned_position_changed tell an actual drag apart from
        # GTK repositioning the same handle on its own during layout.
        self._active_drag_index: int | None = None
        # (kind, widget, position): widget/position are None unless kind
        # needs them -- "align_end"/"align_start" use widget only,
        # "align_pos" uses both, "scroll_end"/"scroll_start" use neither,
        # "scroll_pos" uses position only. See _align_to_viewport_end/
        # _align_to_viewport_start/_align_to_viewport_pos/
        # _scroll_to_viewport_end/_scroll_to_viewport_start/
        # _scroll_to_viewport_pos.
        self._pending_scroll_intent: tuple[str, Gtk.Widget | None, float | None] | None = None
        self._scroll_settle_debounce_id = 0
        # Keyboard row change waiting to be committed (see _arm_row_commit).
        self._pending_row_commit: tuple[Gtk.Widget, Gtk.Widget] | None = None
        self._row_commit_id = 0
        # Reveal-the-preview scroll waiting to fire (see _arm_preview_scroll).
        self._preview_scroll_id = 0
        self._focus_retry_id = 0
        self._typeahead_query = ""
        self._typeahead_clear_id = 0
        # Locations this chain pushed onto Nautilus's real slot that have not
        # come back through notify::location yet (see _sync_slot_location).
        self._pending_slot_uris: list[tuple[str, int]] = []
        # Kept alive on self so the Adw.TimedAnimation isn't GC'd mid-flight
        # (see _animate_scroll_to) -- Adw.TimedAnimation.play() does not hold
        # its own reference.
        self._scroll_animation = None
        self._last_viewport_size = (0, 0)
        # Which column currently owns the accent-coloured selection. This is
        # updated from mouse navigation and deliberately independent of GTK's
        # keyboard focus.
        self.focused_index = 0
        self._apply_focused_column_style()

        scroller = Gtk.ScrolledWindow()
        # Vertical NEVER makes this scroller's height follow the canvas's
        # latest height request. After a window grows, that request becomes
        # the new vertical minimum, so the viewport cannot shrink and the
        # resize tick never observes the smaller height needed to lower it.
        # Keep the viewport independently shrinkable instead. Folder columns
        # scroll vertically themselves, so an outer vertical scrollbar only
        # appears as a safe fallback for an unexpectedly tall child.
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_propagate_natural_height(False)
        scroller.set_min_content_height(0)
        self.scroller = scroller
        scroller.set_hexpand(True)
        scroller.set_vexpand(True)
        scroller.set_focusable(True)

        aligner = _MillerCanvas()
        aligner.set_hexpand(True)
        aligner.set_vexpand(True)
        aligner.set_valign(Gtk.Align.FILL)
        self.aligner = aligner
        # A Gtk.Scrollable child gets its hadjustment created and bound by
        # set_child() itself, synchronously -- available to read back right
        # after this call (confirmed: no need to wait for realize/map).
        scroller.set_child(aligner)

        # Nothing native derives a "line"/"row" size for us here (that's
        # normally what a GtkTextView/GtkListView reports) -- step-increment
        # is left at GTK's raw default of 0. There's no GNOME setting for
        # this either (mouse/touchpad "speed" is pointer acceleration,
        # unrelated); the native convention is simply that whoever owns the
        # scrollable content defines it. A third of a column width reads as
        # a natural, visible pan per discrete wheel notch (see
        # _on_capture_scroll, which reads this back rather than keeping its
        # own separate constant).
        scroller.get_hadjustment().set_step_increment(COLUMN_WIDTH / 3)

        scroller.get_hadjustment().connect("changed", self._on_hadjustment_changed)
        # GtkWidget has no live "width"/"height" GObject property in this GTK
        # version (only "width-request"/"height-request" -- confirmed via
        # GObject.list_properties), so notify::width/height never fires, and
        # Gtk.Fixed's layout goes through a GtkLayoutManager rather than the
        # widget's own size_allocate vfunc, so overriding do_size_allocate on
        # a Fixed subclass is never called either (confirmed by logging --
        # zero calls). A per-frame tick callback that only acts when the
        # viewport size actually changed is the reliable fallback: it's cheap
        # (one tuple comparison per frame) and only runs while the window is
        # mapped.
        self._viewport_tick_id = scroller.add_tick_callback(self._poll_viewport_size)

        key_controller = Gtk.EventControllerKey()
        key_controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key_controller.connect("key-pressed", self._on_key_pressed)
        scroller.add_controller(key_controller)

        # Horizontal-intent panning of the whole Miller chain. Wired in the
        # CAPTURE phase on this outer scroller (an ancestor of every column),
        # so it sees the scroll event BEFORE any column's own ScrolledWindow
        # -- confirmed necessary: over a column whose list overflows, the
        # column would otherwise consume a Shift+wheel (delivered as a plain
        # vertical dy carrying a Shift modifier, per a live probe -- GTK does
        # NOT pre-swap it to dx) as its own vertical scroll, so the chain
        # never panned. Claiming it here first, only for horizontal intent,
        # leaves plain (non-Shift) vertical scrolling to the columns untouched.
        pan_controller = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.BOTH_AXES)
        pan_controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        pan_controller.connect("scroll", self._on_capture_scroll)
        scroller.add_controller(pan_controller)

        self._rebuild_chain()
        # Land the initial view scrolled fully right so the last folder
        # column and the preview are visible by default -- the preview's
        # own right edge is the true end of the canvas here.
        self._align_to_viewport_end(self.preview_column)

    def reset(self, root_uri: str) -> None:
        self._navigation_generation += 1
        old_columns = list(self.columns)
        old_preview = self.preview_column
        self._detach_root()
        for column in old_columns:
            column.destroy_enumeration()
        old_preview.destroy_enumeration()
        self._pending_created_renames.clear()
        # Nothing from the old chain may commit into, or echo into, the new one.
        self._cancel_row_commit()
        self._cancel_preview_scroll()
        self._cancel_focus_retry()
        self._clear_typeahead()
        self._pending_slot_uris.clear()

        self._root_uri = root_uri
        self._sort = self._ext._nautilus_prefs.resolve_column_sort(root_uri)
        self.columns = self._build_columns()
        self.preview_column = self._make_preview_column()
        self.paneds = []
        self._clamping = False
        # Index of the column whose paned handle is currently pressed and
        # held by the user (see _on_paned_handle_pressed/_released), or None
        # -- lets _on_paned_position_changed tell an actual drag apart from
        # GTK repositioning the same handle on its own during layout.
        self._active_drag_index: int | None = None
        # Cancels any pending debounce/animation and snaps the hadjustment
        # back to 0 -- see _reset_viewport_width for why this must happen
        # before _rebuild_chain() rather than after.
        self._reset_viewport_width()
        self._last_viewport_size = (0, 0)
        self.focused_index = 0
        self._apply_focused_column_style()
        self._rebuild_chain()

    def destroy(self) -> None:
        """Release every callback and background resource owned by this host."""
        if self._destroyed:
            return
        self._destroyed = True
        self._native_cut_observer.stop()
        self._cancel_row_commit()
        self._cancel_preview_scroll()
        self._cancel_focus_retry()
        self._clear_typeahead()
        self._reset_viewport_width()
        if self._viewport_tick_id:
            self.scroller.remove_tick_callback(self._viewport_tick_id)
            self._viewport_tick_id = 0
        if self._clipboard_handler_id:
            try:
                self._clipboard.disconnect(self._clipboard_handler_id)
            except (TypeError, RuntimeError):
                pass
            self._clipboard_handler_id = 0
        for monitor in self._operation_monitors:
            monitor.cancel()
        self._operation_monitors.clear()
        for source_id in self._operation_timeout_ids:
            GLib.source_remove(source_id)
        self._operation_timeout_ids.clear()
        for cancellable in self._archive_operations.values():
            cancellable.cancel()
        self._archive_operations.clear()
        self._extracting_archive_uris.clear()
        self._reserved_archive_output_uris.clear()
        for column in self.columns:
            column.destroy_enumeration()
        self.preview_column.destroy_enumeration()
        self._detach_root()
        self.columns.clear()
        self._pending_created_renames.clear()

    def suspend(self) -> None:
        """Pause filesystem and preview work while this slot's view is hidden."""
        if self._suspended or self._destroyed:
            return
        self._suspended = True
        self._navigation_generation += 1
        self._native_cut_observer.stop()
        self._cancel_row_commit()
        self._cancel_preview_scroll()
        self._cancel_focus_retry()
        self._reset_viewport_width()
        for column in self.columns:
            column.destroy_enumeration()
        self._suspended_preview_uris = list(self.preview_column.file_uris)
        self.preview_column.destroy_enumeration()

    def resume(self) -> None:
        """Recreate the canceled preview; populate_column_view reloads folders."""
        if not self._suspended or self._destroyed:
            return
        uris = self._suspended_preview_uris
        preview_target: str | list[str] | None
        if len(uris) == 1:
            preview_target = uris[0]
        else:
            preview_target = uris or None
        self.preview_column = MyComputerPreviewColumn(
            self._ext, preview_target, self._show_open_error
        )
        self._suspended_preview_uris = []
        self._suspended = False
        self._rebuild_chain()

    def _poll_viewport_size(self, _widget, _clock) -> bool:
        size = (self.scroller.get_width(), self.scroller.get_height())
        if size != self._last_viewport_size:
            self._last_viewport_size = size
            self._sync_root_width()
        return True

    def _build_columns(self) -> list[Gtk.Widget]:
        # The Miller chain starts at a single root column; every deeper column
        # is appended live as the user drills into folders (see
        # _on_real_row_activated).
        return [self._make_real_column(self._root_uri)]

    def _make_real_column(self, folder_uri: str) -> Gtk.Widget:
        column = MyComputerColumn(
            self._ext,
            folder_uri,
            self._on_real_row_activated,
            on_loaded=self._on_column_loaded,
            on_row_created=self._on_column_row_created,
            on_files_dropped=self._on_files_dropped,
            on_open_error=self._show_open_error,
            on_file_open=self._open_file,
            on_child_renamed=self._on_external_child_renamed,
            on_child_changed=self._on_column_child_changed,
            on_folder_moved=self._on_open_folder_moved,
            on_folder_unavailable=self._on_column_unavailable,
            sort=self._sort,
        )
        right_click = Gtk.GestureClick(button=3)
        right_click.connect("pressed", self._on_column_background_right_clicked, column)
        column.add_controller(right_click)
        background_click = Gtk.GestureClick(button=1)
        background_click.connect("pressed", self._on_column_background_primary_clicked, column)
        column.add_controller(background_click)
        return column

    @staticmethod
    def _column_point_is_on_row(column: Gtk.Widget, x: float, y: float) -> bool:
        picked = column.pick(x, y, Gtk.PickFlags.DEFAULT)
        while picked is not None and picked is not column:
            if hasattr(picked, "uri") and hasattr(picked, "is_dir"):
                return True
            picked = picked.get_parent()
        return False

    def _clear_column_background_selection(self, column: Gtk.Widget) -> None:
        if column not in self.columns:
            return
        column.clear_pinned_selection()
        column.list_box.unselect_all()
        column._anchor_row = None
        column._cursor_row = None
        self._collapse_below(column)

    def _on_column_background_primary_clicked(
        self,
        gesture: Gtk.GestureClick,
        _n_press: int,
        x: float,
        y: float,
        column: Gtk.Widget,
    ) -> None:
        if self._column_point_is_on_row(column, x, y):
            return
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self._clear_column_background_selection(column)

    def _on_files_dropped(self, source_uris: list[str], destination_uri: str, *, cut: bool) -> None:
        self._paste_uris_into_folder(source_uris, destination_uri, cut=cut)

    def _column_is_live(self, column: Gtk.Widget | None) -> bool:
        """Whether an async callback may still mutate this column's UI."""
        return (
            column is not None
            and not self._destroyed
            and not self._suspended
            and column in self.columns
        )

    def _on_column_loaded(self, _column) -> None:
        """A freshly created column's enumerate_children_async just finished
        populating its rows. _sync_column_selections runs synchronously right
        after a column is created (in _on_real_row_activated / sync_to_uri),
        before that async load has had any chance to complete -- for a brand
        new column (list_box still empty at that point), select_child_for_uri
        silently finds nothing to select. Re-apply now that the rows
        actually exist, so the committed-path highlight (and which column
        reads as "current", see _apply_focused_column_style) isn't lost for
        a chain that got freshly rebuilt rather than reusing existing
        columns."""
        self._sync_column_selections()
        self._apply_focused_column_style()

        self._reconcile_loaded_column(_column)
        self._show_pending_created_rename(_column)

    def _on_column_row_created(self, column: Gtk.Widget, row: Gtk.Widget) -> None:
        """Wire one streamed row immediately, without rescanning the column."""
        click = Gtk.GestureClick(button=0)
        # Keep primary press unclaimed so a touch/kinetic drag can become a
        # scroll. Miller claims a valid click on release, before GtkListBox's
        # own release handler can replace the multi-selection (#161).
        click.connect("pressed", self._on_row_pressed, column, row)
        click.connect("released", self._on_row_released, column, row)
        row.add_controller(click)
        row._mc_click_wired = True
        if self._clipboard_is_cut and row.uri in self._clipboard_uris:
            row.set_cut(True)

    def _show_pending_created_rename(self, column: Gtk.Widget) -> None:
        """Start inline rename once a newly created file or folder has a row."""
        uri = self._pending_created_renames.get(column)
        if uri is None or column not in self.columns:
            return
        target = Gio.File.new_for_uri(uri)
        row = next(
            (row for row in column.rows() if Gio.File.new_for_uri(row.uri).equal(target)), None
        )
        if row is None:
            return
        self._pending_created_renames.pop(column, None)
        column.list_box.unselect_all()
        column.list_box.select_row(row)
        self._prepare_context_selection(column, row)
        self._show_rename_popover(column, row)

    def _reconcile_loaded_column(self, column: Gtk.Widget) -> None:
        """Collapse state that points at an item removed by an external change."""
        if column not in self.columns or not column.load_succeeded():
            return
        index = self.columns.index(column)
        if index + 1 < len(self.columns):
            target_uri = self.columns[index + 1].folder_uri
            if column.contains_uri(target_uri):
                return
            for stale in self.columns[index + 1 :]:
                stale.destroy_enumeration()
            del self.columns[index + 1 :]
            self.focused_index = min(self.focused_index, index)
            self._set_preview(None)
            self._cancel_preview_scroll()
            self._reset_viewport_width()
            self._sync_column_selections()
            self._apply_focused_column_style()
            self._rebuild_chain()
            return
        preview_uris = self.preview_column.file_uris
        if preview_uris:
            selected_uris = column.selected_uris()
            surviving = [uri for uri in selected_uris if column.contains_uri(uri)]
            requested: str | list[str] | None
            if len(surviving) == 1:
                requested = surviving[0]
            else:
                requested = surviving or None
            if surviving != preview_uris:
                self._set_preview(requested)
                self._cancel_preview_scroll()
                self._sync_column_selections()
                self._rebuild_chain()

    def _on_column_background_right_clicked(
        self,
        gesture: Gtk.GestureClick,
        _n_press: int,
        x: float,
        y: float,
        column: Gtk.Widget,
    ) -> None:
        """Show actions for the unused space of one Miller folder column.

        Row gestures claim their own secondary clicks, so this bubble-phase
        controller runs only for the column background.
        """
        if self._column_point_is_on_row(column, x, y):
            return
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self._clear_column_background_selection(column)
        self._show_column_background_menu(column, x, y)

    def _show_column_background_menu(self, column: Gtk.Widget, x: float, y: float) -> None:
        """Show the folder-background menu for pointer or keyboard access."""
        uri = column.folder_uri
        sections = [
            background_creation_section(
                new_folder_action=lambda: self._create_folder(column),
                new_document_items=self._new_document_items(uri),
                open_with_action=(lambda: self._ext._do_open_with(uri, self._win))
                if uri.startswith("file://")
                else None,
            ),
            background_clipboard_section(
                paste_action=(lambda: self._paste_into_folder(uri))
                if self._clipboard_has_pasteable_files()
                else None,
                paste_link_action=(lambda: self._create_links_at(self._clipboard_uris, uri))
                if self._clipboard_uris
                else None,
            ),
            ContextMenuSection(
                [
                    ContextMenuItem(
                        _native("Select All"),
                        action=lambda: self._select_all_in_column(column),
                        shortcut="<Control>a",
                    )
                ]
            ),
        ]
        terminal_action = self._terminal_action(uri)
        if terminal_action is not None:
            sections.append(background_terminal_section(open_terminal_action=terminal_action))
        sections.append(properties_section(lambda: self._ext._do_properties(uri, self._win)))
        # Parent the column-background menu to the MillerCanvas rather than
        # its GtkPaned child chain. The canvas is the actual view surface and
        # is not subject to an individual column's scroll/paned allocation.
        popover_parent = self.aligner
        point = column.translate_coordinates(popover_parent, x, y)
        point_x, point_y = point if point is not None else (x, y)
        popover = ContextMenu(sections).build_popover(popover_parent, "millerbackground")
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(point_x), int(point_y), 1, 1
        popover.set_pointing_to(rect)
        popover.popup()

    def _select_all_in_column(self, column: Gtk.Widget) -> None:
        rows = column.rows()
        if not rows:
            return
        column.list_box.select_all()
        column._anchor_row = rows[0]
        column._cursor_row = rows[-1]
        self._activate_selection(column, rows[-1])

    def _new_document_items(self, destination_uri: str) -> list[ContextMenuItem]:
        """Build the native-style New Document submenu from ~/Templates."""
        templates_dir = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_TEMPLATES)
        if not templates_dir:
            return []
        try:
            entries = sorted(os.scandir(templates_dir), key=lambda entry: entry.name.lower())
        except OSError:
            return []

        items = []
        for entry in entries:
            if not entry.is_file(follow_symlinks=False):
                continue
            template_uri = Gio.File.new_for_path(entry.path).get_uri()
            items.append(
                ContextMenuItem(
                    entry.name,
                    action=lambda template_uri=template_uri: self._create_document_from_template(
                        template_uri, destination_uri
                    ),
                )
            )
        return items

    def _create_document_from_template(self, template_uri: str, destination_uri: str) -> None:
        """Copy a template with a unique name, then start inline rename."""
        template = Gio.File.new_for_uri(template_uri)
        parent = Gio.File.new_for_uri(destination_uri)
        basename = template.get_basename() or _native("New Document")
        stem, extension = os.path.splitext(basename)
        destination_column = next(
            (
                column
                for column in self.columns
                if Gio.File.new_for_uri(column.folder_uri).equal(parent)
            ),
            None,
        )

        def copy_named(name: str, suffix: int) -> None:
            destination = parent.get_child(name)
            if self._column_is_live(destination_column):
                destination_column.expect_child_creation(destination.get_uri())

            def on_copied(source: Gio.File, result: Gio.AsyncResult) -> None:
                try:
                    source.copy_finish(result)
                except GLib.Error as error:
                    if self._column_is_live(destination_column):
                        destination_column.finish_expected_child_creation(
                            destination.get_uri(), created=False
                        )
                    if error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.EXISTS):
                        copy_named(f"{stem} {suffix}{extension}", suffix + 1)
                        return
                    _log(
                        f"Could not create document from {template_uri!r} in "
                        f"{destination_uri!r}: {error.message}"
                    )
                    if not self._suspended and not self._destroyed:
                        self._show_file_operation_error(error.message)
                    return
                if self._column_is_live(destination_column):
                    self._pending_created_renames[destination_column] = destination.get_uri()
                    destination_column.finish_expected_child_creation(
                        destination.get_uri(), created=True
                    )

            template.copy_async(
                destination,
                Gio.FileCopyFlags.NONE,
                GLib.PRIORITY_DEFAULT,
                None,
                None,
                on_copied,
            )

        copy_named(basename, 2)

    @staticmethod
    def _terminal_app() -> Gio.AppInfo | None:
        """Resolve the installed GNOME terminal once per menu construction."""
        terminal_ids = {
            "org.gnome.Console.desktop",
            "org.gnome.Terminal.desktop",
            "org.gnome.Ptyxis.desktop",
        }
        return next(
            (app for app in Gio.AppInfo.get_all() if app.get_id() in terminal_ids),
            None,
        )

    def _terminal_action_for_uris(self, uris: list[str]):
        if not uris or not all(uri.startswith("file://") for uri in uris):
            return None
        terminal = self._terminal_app()
        if terminal is None:
            return None

        def open_terminal() -> None:
            for uri in uris:
                try:
                    terminal.launch_uris([uri], None)
                except GLib.Error as error:
                    _log(f"Could not open terminal for {uri!r}: {error.message}")

        return open_terminal

    def _terminal_action(self, uri: str):
        """Return a launcher for an installed GNOME terminal, if available."""
        return self._terminal_action_for_uris([uri])

    def _run_programs(self, rows: list[Gtk.Widget]) -> None:
        """Launch executable local files exactly when the user requests it."""
        for row in rows:
            path = Gio.File.new_for_uri(row.uri).get_path()
            if path is None or not getattr(row, "can_execute", False):
                continue
            launcher = Gio.SubprocessLauncher.new(Gio.SubprocessFlags.NONE)
            parent = os.path.dirname(path)
            if parent:
                launcher.set_cwd(parent)
            try:
                launcher.spawnv([path])
            except GLib.Error as error:
                self._show_file_operation_error(error.message)

    def _launch_nautilus_script(
        self, script_path: str, selected_uris: list[str], current_uri: str
    ) -> None:
        """Run one executable from the standard Nautilus Scripts directory."""
        selected_paths = [Gio.File.new_for_uri(uri).get_path() for uri in selected_uris]
        if any(path is None for path in selected_paths):
            return
        paths = [path for path in selected_paths if path is not None]
        launcher = Gio.SubprocessLauncher.new(Gio.SubprocessFlags.NONE)
        current_path = Gio.File.new_for_uri(current_uri).get_path()
        if current_path:
            launcher.set_cwd(current_path)
        geometry = f"{max(1, self._win.get_width())}x{max(1, self._win.get_height())}+0+0"
        launcher.setenv("NAUTILUS_SCRIPT_SELECTED_FILE_PATHS", "\n".join(paths), True)
        launcher.setenv("NAUTILUS_SCRIPT_SELECTED_URIS", "\n".join(selected_uris), True)
        launcher.setenv("NAUTILUS_SCRIPT_CURRENT_URI", current_uri, True)
        launcher.setenv("NAUTILUS_SCRIPT_WINDOW_GEOMETRY", geometry, True)
        try:
            launcher.spawnv([script_path, *paths])
        except GLib.Error as error:
            self._show_file_operation_error(error.message)

    def _nautilus_script_items(
        self,
        directory: str,
        selected_uris: list[str],
        current_uri: str,
        *,
        depth: int = 0,
        budget: list[int] | None = None,
    ) -> list[ContextMenuItem]:
        """Build the bounded Scripts tree without following directory links."""
        if depth >= _NAUTILUS_SCRIPT_MAX_DEPTH:
            return []
        if budget is None:
            budget = [_NAUTILUS_SCRIPT_MAX_ITEMS]
        try:
            with os.scandir(directory) as iterator:
                # Bound directory traversal itself, not only the number of
                # executable results. A Scripts folder containing thousands
                # of irrelevant files must not stall a context-menu click.
                entries = []
                for entry in iterator:
                    if len(entries) >= budget[0]:
                        break
                    entries.append(entry)
                entries = sorted(
                    entries,
                    key=lambda entry: GLib.utf8_collate_key_for_filename(entry.name, -1),
                )
        except OSError:
            return []

        items: list[ContextMenuItem] = []
        for entry in entries:
            if budget[0] <= 0:
                break
            budget[0] -= 1
            try:
                if entry.is_dir(follow_symlinks=False):
                    children = self._nautilus_script_items(
                        entry.path,
                        selected_uris,
                        current_uri,
                        depth=depth + 1,
                        budget=budget,
                    )
                    if children:
                        items.append(
                            ContextMenuItem(
                                entry.name,
                                submenu=ContextMenu([ContextMenuSection(children)]),
                            )
                        )
                    continue
                mode = entry.stat(follow_symlinks=True).st_mode
            except OSError:
                continue
            if not stat.S_ISREG(mode) or not mode & 0o111:
                continue
            script_path = entry.path
            items.append(
                ContextMenuItem(
                    entry.name,
                    action=lambda path=script_path: self._launch_nautilus_script(
                        path, selected_uris, current_uri
                    ),
                )
            )
        return items

    def _nautilus_scripts_section(
        self, selected_uris: list[str], current_uri: str
    ) -> ContextMenuSection | None:
        if not selected_uris or not all(uri.startswith("file://") for uri in selected_uris):
            return None
        scripts_root = os.path.join(GLib.get_user_data_dir(), "nautilus", "scripts")
        items = self._nautilus_script_items(scripts_root, selected_uris, current_uri)
        if not items:
            return None
        return ContextMenuSection(
            [
                ContextMenuItem(
                    _native("Scripts"),
                    submenu=ContextMenu([ContextMenuSection(items)]),
                )
            ]
        )

    def _create_folder(self, column: Gtk.Widget) -> None:
        """Create a new folder in one column, then refresh its listing."""
        parent = Gio.File.new_for_uri(column.folder_uri)
        base_name = _native("New Folder")

        def create_named(name: str, suffix: int) -> None:
            candidate = parent.get_child(name)
            if self._column_is_live(column):
                column.expect_child_creation(candidate.get_uri())

            def on_folder_created(source, result, _data) -> None:
                try:
                    source.make_directory_finish(result)
                except GLib.Error as error:
                    if self._column_is_live(column):
                        column.finish_expected_child_creation(candidate.get_uri(), created=False)
                    if error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.EXISTS):
                        create_named(f"{base_name} {suffix}", suffix + 1)
                        return
                    _log(f"Could not create folder in {column.folder_uri!r}: {error.message}")
                    if not self._suspended and not self._destroyed:
                        self._show_file_operation_error(error.message)
                    return
                if self._column_is_live(column):
                    self._pending_created_renames[column] = source.get_uri()
                    column.finish_expected_child_creation(source.get_uri(), created=True)

            candidate.make_directory_async(GLib.PRIORITY_DEFAULT, None, on_folder_created, None)

        create_named(base_name, 2)

    def create_folder_in_focused_column(self) -> bool:
        """Create a folder in the focused Miller column (Shift+Ctrl+N)."""
        column = self._focused_column()
        if column is None:
            return False
        self._create_folder(column)
        return True

    def _on_row_pressed(
        self,
        gesture: Gtk.GestureClick,
        n_press: int,
        x: float,
        y: float,
        column: Gtk.Widget,
        row: Gtk.Widget,
    ) -> None:
        """Dispatch a Miller row press by button, mirroring the single
        button=0 GtkGestureClick native cells install (nautilus-list-base.c
        on_item_click_pressed, :242-296) instead of one gesture per button.

        Ctrl+middle on a folder row opens a new window, the same affordance
        the injected sidebar row got in #116 (nautilus-sidebar.c:3236-3241).
        Native file-view cells do not do this -- they ignore modifiers on
        middle-click (:284-291, activate_selection(self, TRUE) is called
        unconditionally) -- but Miller rows are a browsing surface, so the
        sidebar's modifier reads more naturally here than cell parity.

        Unlike native's select_single_item_if_not_selected before
        activating, middle-click here does not select or :active-anchor the
        row. A blue :selected row means "part of the committed Miller path"
        (_sync_column_selections) and would be wrong for a row being opened
        elsewhere; there is also no popover to anchor to, unlike the
        right-click case below.
        """
        button = gesture.get_current_button()
        # A pin protects only the modifier action that created it. Any new
        # pointer press is an explicit selection action and must be free to
        # replace that old state before GtkListBox handles this sequence.
        column.clear_pinned_selection()
        # The pointer takes over from here: a keyboard commit still waiting
        # out its debounce would otherwise land on top of this click.
        self._cancel_row_commit()
        # Also drop any still-pending preview-reveal scroll from an earlier
        # click -- this press might be the second half of a double-click on
        # that same row, and _arm_preview_scroll depends on exactly this
        # happening before its timer fires (see its docstring).
        self._cancel_preview_scroll()
        if button == Gdk.BUTTON_MIDDLE and n_press == 1:
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            ctrl = bool(gesture.get_current_event_state() & Gdk.ModifierType.CONTROL_MASK)
            if not row.is_dir:
                # A file has no location to open a window on, so Ctrl is
                # irrelevant: hand it to its default application either way.
                self._open_file(row.uri)
            elif ctrl:
                self._ext._do_open_window(row.uri)
            else:
                self._ext._do_open_tab(row.uri, self._win, make_active=False)
            return
        if button == Gdk.BUTTON_SECONDARY and n_press == 1:
            self._on_row_right_clicked(gesture, n_press, x, y, column, row)
            return

        if button == Gdk.BUTTON_PRIMARY:
            # #161: leave primary unclaimed until release so a press that
            # turns into touch/kinetic scrolling never commits selection.
            # _on_row_released owns both plain and modifier selection.
            return

        # Reset the all-buttons gesture for any unsupported button instead
        # of letting its sequence linger into the next press.
        gesture.set_state(Gtk.EventSequenceState.DENIED)

    def _activate_selection(
        self,
        column: Gtk.Widget,
        clicked_row: Gtk.Widget,
        *,
        pin_native_echo: bool = False,
    ) -> None:
        """Run Miller activation for whatever `column` has selected *after* a
        modifier click, rather than for the row that was clicked.

        Ctrl+clicking a selected row deselects it, so the clicked row is
        exactly the one that must not drive the preview/chain: feeding it to
        _on_real_row_activated made _sync_column_selections re-select it as
        the previewed file, silently undoing the deselection. With several
        rows selected the clicked row is only a tie-break -- the multi
        branch of _on_real_row_activated reads the whole selection itself.

        Modifier selections are pinned (see MyComputerColumn.pin_selection)
        through the GTK work caused by rebuilding and reparenting the column.
        The release-time row controller prevents GtkListBox's own click from
        replacing it; the pin also covers later focus/reparent settling."""
        selected = column.selected_rows()
        if not selected:
            self._collapse_below(column)
        elif len(selected) == 1:
            self._on_real_row_activated(column, selected[0])
        else:
            self._on_real_row_activated(column, clicked_row)
        if pin_native_echo:
            column.pin_selection()
        else:
            # Non-modifier selection paths do not need a settling guard.
            column.clear_pinned_selection()

    def _open_selection(self, column: Gtk.Widget) -> bool:
        """Open what is selected in `column` -- the Return/Enter target.

        Enter was previously left to propagate, on the assumption that
        Gtk.ListBox's activate-cursor-row binding would pick it up. It only
        fires when a row itself holds keyboard focus, which is not the case
        for most of Column View's life: entering the view focuses the view
        widget (see populate_column_view), and the accent selection this
        chain tracks is deliberately independent of GTK focus. So Enter
        usually did nothing at all. Handled explicitly here instead, off the
        same selection every other shortcut uses.

        Folders open the way clicking them does -- drill into a new column,
        which is what "open" means in a Miller view. Files go to their
        default application, matching native Nautilus, where Enter opens the
        selection rather than merely previewing it. For a multi-selection,
        files are launched in batches and every selected folder is opened in
        a background tab because several folders cannot all become the next
        Miller column."""
        selected = column.selected_rows()
        if not selected:
            cursor = getattr(column, "_cursor_row", None)
            if cursor is None or cursor not in column.rows():
                return False
            selected = [cursor]

        if len(selected) == 1:
            row = selected[0]
            if row.is_dir:
                self._cancel_row_commit()
                self._on_real_row_activated(column, row)
            else:
                self._open_file(row.uri, row.content_type)
            return True

        files = [(row.uri, row.content_type) for row in selected if not row.is_dir]
        folder_uris = [row.uri for row in selected if row.is_dir]
        if files:
            self._open_files(files)
        for uri in folder_uris:
            self._ext._do_open_tab(uri, self._win, make_active=False)
        return True

    def _arm_row_commit(self, column: Gtk.Widget, row: Gtk.Widget) -> None:
        """Schedule the Miller commit for a keyboard row change, replacing
        any commit still pending.

        The selection itself has already moved by the time this is called --
        this only defers the expensive half (see ROW_COMMIT_DEBOUNCE_MS), so
        walking through a folder with the arrow keys stays a pure cursor
        movement until the user settles on a row."""
        self._pending_row_commit = (column, row)
        if self._row_commit_id != 0:
            GLib.source_remove(self._row_commit_id)
        self._row_commit_id = GLib.timeout_add(ROW_COMMIT_DEBOUNCE_MS, self._apply_row_commit)

    def _apply_row_commit(self) -> bool:
        self._row_commit_id = 0
        pending = self._pending_row_commit
        self._pending_row_commit = None
        if pending is None:
            return GLib.SOURCE_REMOVE
        column, row = pending
        # The chain can have moved on while this was waiting (an external
        # navigation, a reload recreating the rows) -- commit only what is
        # still really there.
        if column not in self.columns or row not in column.rows():
            return GLib.SOURCE_REMOVE
        self._on_real_row_activated(column, row)
        return GLib.SOURCE_REMOVE

    def _cancel_row_commit(self) -> None:
        """Drop a pending keyboard commit, for when something else takes over
        the navigation (a click, a re-root)."""
        if self._row_commit_id != 0:
            GLib.source_remove(self._row_commit_id)
            self._row_commit_id = 0
        self._pending_row_commit = None

    def _arm_preview_scroll(self) -> None:
        """Delay the scroll that brings a freshly previewed file's preview
        into view, replacing any scroll still pending.

        Previewing a file always jumps the Miller canvas to show it (see the
        preview_added branch of _on_real_row_activated), and that scroll is
        what a fast double-click needs to open the file rather than just
        preview it -- the second click has to land back on the same row, but
        an immediate scroll can slide that row out from under the pointer
        mid-animation before the click arrives. Waiting out the same double-click window
        _on_row_activated_internal already uses to detect a repeat click means
        a genuine double-click never sees the row move at all: the very next
        press cancels this via _on_row_pressed before the timer ever fires."""
        if self._preview_scroll_id != 0:
            GLib.source_remove(self._preview_scroll_id)
        double_click_ms = Gtk.Settings.get_default().get_property("gtk-double-click-time")
        self._preview_scroll_id = GLib.timeout_add(double_click_ms, self._apply_preview_scroll)

    def _apply_preview_scroll(self) -> bool:
        self._preview_scroll_id = 0
        self._scroll_to_viewport_end()
        return GLib.SOURCE_REMOVE

    def _cancel_preview_scroll(self) -> None:
        """Drop a pending preview-reveal scroll -- a new press (including the
        second click of a double-click) always supersedes it."""
        if self._preview_scroll_id != 0:
            GLib.source_remove(self._preview_scroll_id)
            self._preview_scroll_id = 0

    def _collapse_below(self, column: Gtk.Widget) -> None:
        """Drop everything deeper than `column` and clear the preview, for a
        selection that just became empty (ctrl+clicking the last selected row
        off). Nothing is selected any more, so there is no row left to derive
        a child column or a preview from."""
        index = self.columns.index(column)
        self.focused_index = index
        had_deeper = len(self.columns) > index + 1
        for stale_column in self.columns[index + 1 :]:
            stale_column.destroy_enumeration()
        del self.columns[index + 1 :]
        self._set_preview(None)
        self._sync_slot_location(column.folder_uri)
        self._sync_column_selections()
        self._apply_focused_column_style()
        if had_deeper:
            # Same collapse as NAV_UP in _on_real_row_activated -- reset the
            # scroll position before the rebuild so the stale, far-scrolled
            # value isn't baked into the new, narrower canvas (see
            # _reset_viewport_width).
            self._reset_viewport_width()
        self._rebuild_chain()

    def _select_range(self, column: Gtk.Widget, target_row: Gtk.Widget) -> None:
        rows = column.rows() if hasattr(column, "rows") else []
        if not rows:
            return
        anchor = getattr(column, "_anchor_row", None)
        if anchor is None or anchor not in rows:
            column.list_box.unselect_all()
            column.list_box.select_row(target_row)
            column._anchor_row = target_row
            column._cursor_row = target_row
            return

        idx1 = rows.index(anchor)
        idx2 = rows.index(target_row)
        start, end = min(idx1, idx2), max(idx1, idx2)
        column.list_box.unselect_all()
        for i in range(start, end + 1):
            column.list_box.select_row(rows[i])
        column._cursor_row = target_row

    def _on_row_released(
        self,
        gesture: Gtk.GestureClick,
        _n_press: int,
        x: float,
        y: float,
        column: Gtk.Widget,
        row: Gtk.Widget,
    ) -> None:
        """Primary-click release counterpart to _on_row_pressed (#161).

        Press leaves the sequence unclaimed so the enclosing ScrolledWindow
        can still scroll. Claiming a valid click here cancels GtkListBox's own
        release handler before it can replace Miller's multi-selection.
        Plain clicks still run MyComputerColumn's activation state machine;
        Ctrl/Shift clicks commit the resulting selection as a group.

        Two details mirror GtkListBox's own released handler rather than
        simplifying past it:
        - **Release must still be over the pressed row.** Native explicitly
          re-checks (`box->active_row == gtk_list_box_get_row_at_y(box, y)`)
          before selecting or activating, so pressing one row and releasing
          over another (or off the list) does nothing. Our controller is on
          the row itself, so the equivalent test is a bounds check on the
          row's own allocation.
        - **Every press count dispatches, not just the first.**
          `_on_row_activated_internal` detects repeat clicks by *timing*, not
          `n_press`, precisely because a chain rebuild resets GTK's press-count
          tracking mid-double-click (see MyComputerColumn's own
          `_last_activated_uri` comment). A second release can therefore arrive
          as either n_press 1 or 2 depending on whether the row survived, so
          filtering on n_press would silently swallow the open-already-previewed-file
          click in the cases where it does survive.
        """
        if gesture.get_current_button() != Gdk.BUTTON_PRIMARY:
            return
        if not (0 <= x < row.get_width() and 0 <= y < row.get_height()):
            # Released off the row it was pressed on: not a click. Deny rather
            # than just return, so the sequence ends now instead of lingering
            # into the next press (see _on_row_pressed's DENIED branch).
            gesture.set_state(Gtk.EventSequenceState.DENIED)
            return
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        state = gesture.get_current_event_state()
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        if shift:
            self._select_range(column, row)
            column._cursor_row = row
            self._activate_selection(column, row, pin_native_echo=True)
        elif ctrl:
            if row in column.selected_rows():
                column.list_box.unselect_row(row)
            else:
                column.list_box.select_row(row)
            column._anchor_row = row
            column._cursor_row = row
            self._activate_selection(column, row, pin_native_echo=True)
        else:
            column.clear_pinned_selection()
            column.list_box.unselect_all()
            column.list_box.select_row(row)
            column._anchor_row = row
            column._cursor_row = row
            column._on_row_activated_internal(column.list_box, row)

    def _on_row_right_clicked(
        self,
        gesture: Gtk.GestureClick | None,
        _n_press: int,
        x: float,
        y: float,
        column: Gtk.Widget,
        row: Gtk.Widget,
    ) -> None:
        """Show a live menu for one Miller folder or file row.

        These rows are not part of Nautilus's private FilesView selection,
        so native ``view.*`` actions cannot safely be reused here. Open uses
        the Miller activation path; the remaining entries are actions this
        extension owns directly.
        """
        if gesture is not None:
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        components.set_row_active(row, True)

        selected_rows = column.selected_rows() if hasattr(column, "selected_rows") else []
        if row not in selected_rows:
            column.list_box.unselect_all()
            column.list_box.select_row(row)
            column._anchor_row = row
            column._cursor_row = row
            selected_rows = [row]
            self._prepare_context_selection(column, row)

        selected_uris = [r.uri for r in selected_rows]
        is_multi = len(selected_rows) > 1
        local_selection = all(uri.startswith("file://") for uri in selected_uris)
        compress_action = (
            (lambda: self._show_compress_dialog(column, selected_uris))
            if local_selection and GnomeAutoar is not None
            else None
        )
        email_action = (
            (lambda: self._email_files(selected_uris))
            if local_selection
            and not any(selected_row.is_dir for selected_row in selected_rows)
            and shutil.which("xdg-email")
            else None
        )

        if is_multi:

            def is_extractable(selected_row: Gtk.Widget) -> bool:
                if selected_row.is_dir:
                    return False
                selected_type = selected_row.content_type or "application/octet-stream"
                selected_app = Gio.AppInfo.get_default_for_type(selected_type, False)
                return _should_extract_archive(
                    selected_type,
                    selected_app.get_id() if selected_app else None,
                    GnomeAutoar is not None,
                )

            archive_rows = [
                selected_row for selected_row in selected_rows if is_extractable(selected_row)
            ]
            file_content_types = {
                selected_row.content_type or "application/octet-stream"
                for selected_row in selected_rows
                if not selected_row.is_dir
            }
            multi_open_with = (
                (
                    lambda: self._ext._do_open_with(
                        selected_uris,
                        self._win,
                        content_type=next(iter(file_content_types)),
                    )
                )
                if not any(row.is_dir for row in selected_rows) and len(file_content_types) == 1
                else None
            )
            multi_terminal = (
                self._terminal_action_for_uris(selected_uris)
                if all(selected_row.is_dir for selected_row in selected_rows)
                else None
            )
            executable_rows = [
                selected_row
                for selected_row in selected_rows
                if not selected_row.is_dir
                and selected_row.uri.startswith("file://")
                and getattr(selected_row, "can_execute", False)
            ]
            run_programs = (
                (lambda: self._run_programs(executable_rows))
                if len(executable_rows) == len(selected_rows)
                else None
            )
            sections = [
                open_section(
                    lambda: self._open_selection(column),
                    open_with_action=multi_open_with,
                    submenu=multi_open_with is not None,
                ),
                clipboard_actions_section(
                    cut_action=lambda: self._copy_to_clipboard(selected_uris, cut=True),
                    copy_action=lambda: self._copy_to_clipboard(selected_uris, cut=False),
                    paste_action=(lambda: self._paste_into_folder(column.folder_uri))
                    if self._clipboard_has_pasteable_files()
                    else None,
                    paste_link_action=(
                        lambda: self._create_links_at(self._clipboard_uris, column.folder_uri)
                    )
                    if self._clipboard_uris
                    else None,
                    move_to_action=lambda: self._show_destination_picker(selected_uris, move=True),
                    copy_to_action=lambda: self._show_destination_picker(selected_uris, move=False),
                ),
                file_actions_section(
                    rename_action=None,
                    create_link_action=(
                        lambda: self._create_links_at(selected_uris, column.folder_uri)
                    )
                    if local_selection
                    else None,
                    extract_action=(lambda: self._extract_rows_here(archive_rows))
                    if archive_rows
                    else None,
                    extract_to_action=(lambda: self._show_extract_destination(archive_rows))
                    if archive_rows
                    else None,
                    open_terminal_action=multi_terminal,
                    run_as_program_action=run_programs,
                    move_to_trash_action=(
                        (lambda: self._move_to_trash(column, selected_uris))
                        if all(u.startswith("file://") for u in selected_uris)
                        else None
                    ),
                    delete_permanently_action=(
                        (lambda: self._delete_permanently(column, selected_uris))
                        if all(u.startswith("file://") for u in selected_uris)
                        else None
                    ),
                    compress_action=compress_action,
                    email_action=email_action,
                ),
                properties_section(lambda: self._ext._do_properties(selected_uris, self._win)),
            ]
            scripts_section = self._nautilus_scripts_section(selected_uris, column.folder_uri)
            if scripts_section is not None:
                sections.insert(-1, scripts_section)
        else:
            uri = row.uri
            content_type = row.content_type or "application/octet-stream"
            default_app = Gio.AppInfo.get_default_for_type(content_type, False)
            # Native catalog wording (#120). Some locales translate this
            # entry without a %s placeholder, so the app name is only
            # substituted when the template actually carries one.
            open_with_template = _native("Open With %s")
            file_open_label = (
                (
                    open_with_template % default_app.get_display_name()
                    if "%s" in open_with_template
                    else default_app.get_display_name()
                )
                if default_app
                else _native("Open")
            )

            open_actions = (
                open_section(
                    lambda: self._on_real_row_activated(column, row),
                    open_tab_action=lambda: self._ext._do_open_tab(
                        uri, self._win, make_active=False
                    ),
                    open_window_action=lambda: self._ext._do_open_window(uri),
                    open_with_action=(
                        (lambda: self._ext._do_open_with(uri, self._win, content_type=content_type))
                        if uri.startswith("file://")
                        else None
                    ),
                )
                if row.is_dir
                else open_section(
                    lambda: self._open_file(uri, content_type),
                    open_label=file_open_label,
                    open_with_action=(
                        (lambda: self._ext._do_open_with(uri, self._win, content_type=content_type))
                        if uri.startswith("file://")
                        else None
                    ),
                    submenu=False,
                )
            )
            sections = [
                open_actions,
                clipboard_actions_section(
                    cut_action=lambda: self._copy_to_clipboard(uri, cut=True),
                    copy_action=lambda: self._copy_to_clipboard(uri, cut=False),
                    paste_action=(lambda: self._paste_into_folder(uri))
                    if row.is_dir and self._clipboard_has_pasteable_files()
                    else None,
                    paste_link_action=(lambda: self._create_links_at(self._clipboard_uris, uri))
                    if row.is_dir and self._clipboard_uris
                    else None,
                    move_to_action=lambda: self._show_destination_picker(uri, move=True),
                    copy_to_action=lambda: self._show_destination_picker(uri, move=False),
                ),
                file_actions_section(
                    rename_action=(
                        (lambda: self._show_rename_popover(column, row))
                        if uri.startswith("file://")
                        else None
                    ),
                    create_link_action=(lambda: self._create_links_at([uri], column.folder_uri))
                    if uri.startswith("file://")
                    else None,
                    extract_action=(
                        lambda: self._extract_archive_in_current_window(
                            uri, content_type, open_when_done=False
                        )
                    )
                    if not row.is_dir
                    and _should_extract_archive(
                        content_type,
                        default_app.get_id() if default_app else None,
                        GnomeAutoar is not None,
                    )
                    else None,
                    extract_to_action=(lambda: self._show_extract_destination([row]))
                    if not row.is_dir
                    and _should_extract_archive(
                        content_type,
                        default_app.get_id() if default_app else None,
                        GnomeAutoar is not None,
                    )
                    else None,
                    set_as_background_action=(lambda: self._set_as_background(uri))
                    if uri.startswith("file://") and content_type.startswith("image/")
                    else None,
                    open_terminal_action=self._terminal_action(uri) if row.is_dir else None,
                    run_as_program_action=(lambda: self._run_programs([row]))
                    if not row.is_dir
                    and uri.startswith("file://")
                    and getattr(row, "can_execute", False)
                    else None,
                    move_to_trash_action=(
                        (lambda: self._move_to_trash(column, uri))
                        if uri.startswith("file://")
                        else None
                    ),
                    delete_permanently_action=(
                        (lambda: self._delete_permanently(column, [uri]))
                        if uri.startswith("file://")
                        else None
                    ),
                    compress_action=compress_action,
                    email_action=email_action,
                ),
            ]
            if row.is_dir and uri.startswith("file://"):
                sections.append(
                    my_computer_additions_section(
                        bookmarked=bookmarks.is_bookmarked(uri),
                        preferred=preferred_folders.is_preferred(self._ext._gsettings, uri),
                        toggle_bookmark_action=lambda: bookmarks.toggle_bookmark(uri),
                        toggle_preferred_action=lambda: preferred_folders.toggle_preferred(
                            self._ext._gsettings, uri
                        ),
                    )
                )
            scripts_section = self._nautilus_scripts_section([uri], column.folder_uri)
            if scripts_section is not None:
                sections.append(scripts_section)
            sections.append(properties_section(lambda: self._ext._do_properties(uri, self._win)))

        popover = ContextMenu(sections).build_popover(row, "millerrow")
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        popover.set_pointing_to(rect)
        popover.connect(
            "unmap",
            lambda *_args: self._clear_context_active_row(row),
        )
        popover.popup()

        def keep_anchor_active() -> bool:
            # The button release arrives after the popover maps and clears
            # :active. Match Rename's anchor behavior by reasserting it on
            # the next main-loop turn for the menu's full lifetime.
            if popover.get_mapped():
                components.set_row_active(row, True)
            return GLib.SOURCE_REMOVE

        GLib.idle_add(keep_anchor_active)

    def _prepare_context_selection(self, column: Gtk.Widget, row: Gtk.Widget) -> None:
        """Make a context-clicked row the visible state without opening folders."""
        if column not in self.columns:
            return
        self._cancel_row_commit()
        self._cancel_preview_scroll()
        index = self.columns.index(column)
        stale = self.columns[index + 1 :]
        for stale_column in stale:
            stale_column.destroy_enumeration()
        del self.columns[index + 1 :]
        preview_changed = self._set_preview(None if row.is_dir else row.uri)
        self.focused_index = index
        self._sync_slot_location(column.folder_uri)
        self._apply_focused_column_style()
        if stale or preview_changed:
            self._reset_viewport_width()
            self._rebuild_chain()

    @staticmethod
    def _rewrite_uri(uri: str, old_uri: str, new_uri: str) -> str:
        target = Gio.File.new_for_uri(uri)
        old = Gio.File.new_for_uri(old_uri)
        if target.equal(old):
            return new_uri
        if not target.has_prefix(old):
            return uri
        relative = old.get_relative_path(target)
        if relative is None:
            return uri
        return Gio.File.new_for_uri(new_uri).resolve_relative_path(relative).get_uri()

    def _on_item_renamed(
        self,
        source_column: Gtk.Widget,
        old_uri: str,
        new_uri: str,
        *,
        refresh_source: bool = True,
    ) -> None:
        """Apply a completed shared rename operation to the Miller chain."""
        old_deepest = self.columns[-1].folder_uri if self.columns else None
        for open_column in self.columns:
            rewritten = self._rewrite_uri(open_column.folder_uri, old_uri, new_uri)
            if rewritten != open_column.folder_uri:
                open_column.set_folder_uri(rewritten)
        self._root_uri = self._rewrite_uri(self._root_uri, old_uri, new_uri)
        self._clipboard_uris = [
            self._rewrite_uri(uri, old_uri, new_uri) for uri in self._clipboard_uris
        ]
        self._suspended_preview_uris = [
            self._rewrite_uri(uri, old_uri, new_uri) for uri in self._suspended_preview_uris
        ]
        self._pending_slot_uris = [
            (self._rewrite_uri(uri, old_uri, new_uri), created_at)
            for uri, created_at in self._pending_slot_uris
        ]
        for column, uri in tuple(self._pending_created_renames.items()):
            self._pending_created_renames[column] = self._rewrite_uri(uri, old_uri, new_uri)

        if refresh_source and source_column in self.columns:
            source_column.rename_child_uri(old_uri, new_uri, notify_host=False)

        preview_uris = [
            self._rewrite_uri(uri, old_uri, new_uri) for uri in self.preview_column.file_uris
        ]
        if preview_uris != self.preview_column.file_uris:
            requested = preview_uris[0] if len(preview_uris) == 1 else preview_uris
            self._set_preview(requested)
            self._rebuild_chain()
            self._fade_in(self.preview_column, duration=PREVIEW_FADE_DURATION_MS)
        if self.columns and self.columns[-1].folder_uri != old_deepest:
            self._sync_slot_location(self.columns[-1].folder_uri)

    def _on_external_child_renamed(
        self, source_column: Gtk.Widget, old_uri: str, new_uri: str
    ) -> None:
        if self._suspended or self._destroyed:
            return
        self._on_item_renamed(source_column, old_uri, new_uri, refresh_source=False)

    def _on_column_child_changed(self, _column: Gtk.Widget, uri: str) -> None:
        if self._suspended or self._destroyed or uri not in self.preview_column.file_uris:
            return
        requested = (
            self.preview_column.file_uris[0]
            if len(self.preview_column.file_uris) == 1
            else list(self.preview_column.file_uris)
        )
        if self._set_preview(requested, force=True):
            self._rebuild_chain()

    def _on_open_folder_moved(self, column: Gtk.Widget, old_uri: str, new_uri: str) -> None:
        if self._suspended or self._destroyed or column not in self.columns:
            return
        self._on_item_renamed(column, old_uri, new_uri, refresh_source=False)

    def _on_column_unavailable(self, column: Gtk.Widget, _uri: str) -> None:
        if self._suspended or self._destroyed or column not in self.columns:
            return
        index = self.columns.index(column)
        self._navigation_generation += 1
        for stale in self.columns[index + (1 if index == 0 else 0) :]:
            stale.destroy_enumeration()
        if index == 0:
            del self.columns[1:]
            if getattr(column, "_file_monitor", None) is not None:
                column._file_monitor.cancel()
                column._file_monitor = None
            column.reload()
        else:
            del self.columns[index:]
        self.focused_index = max(0, min(self.focused_index, len(self.columns) - 1))
        self._set_preview(None)
        self._cancel_preview_scroll()
        self._sync_column_selections()
        self._apply_focused_column_style()
        self._reset_viewport_width()
        self._rebuild_chain()

    def _show_rename_popover(self, column: Gtk.Widget, row: Gtk.Widget) -> None:
        components.show_rename_popover(
            row,
            row.uri,
            lambda old_uri, new_uri: self._on_item_renamed(column, old_uri, new_uri),
            item_kind="folder" if row.is_dir else "file",
        )

    def _move_to_trash(self, source_column: Gtk.Widget, uris: str | list[str]) -> None:
        """Run Nautilus's own trash operation, including its undo manager."""
        if isinstance(uris, str):
            uris = [uris]
        file_uris = [u for u in uris if u.startswith("file://")]
        if not file_uris:
            return
        self._call_nautilus_file_operation(
            "TrashURIs", GLib.Variant("(asa{sv})", (file_uris, {})), file_uris[0]
        )

    def _call_nautilus_file_operation(
        self, method: str, parameters: GLib.Variant, uri: str, *, on_started=None
    ) -> None:
        """Start a native Nautilus file operation through its session D-Bus API."""

        def on_operation_started(proxy, result, _data) -> None:
            try:
                proxy.call_finish(result)
            except GLib.Error as error:
                _log(f"Nautilus {method} failed for {uri!r}: {error.message}")
                self._show_file_operation_error(error.message)
                return
            if callable(on_started):
                on_started()

        def on_proxy_ready(_source, result: Gio.AsyncResult, _data) -> None:
            try:
                operations = Gio.DBusProxy.new_for_bus_finish(result)
            except GLib.Error as error:
                _log(f"Could not start Nautilus {method} for {uri!r}: {error.message}")
                self._show_file_operation_error(error.message)
                return
            operations.call(
                method,
                parameters,
                Gio.DBusCallFlags.NONE,
                -1,
                None,
                on_operation_started,
                None,
            )

        Gio.DBusProxy.new_for_bus(
            Gio.BusType.SESSION,
            Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES,
            None,
            "org.gnome.Nautilus",
            "/org/gnome/Nautilus/FileOperations2",
            "org.gnome.Nautilus.FileOperations2",
            None,
            on_proxy_ready,
            None,
        )

    def _show_file_operation_error(self, message: str) -> None:
        self._show_error(_("File operation failed"), message)

    def _show_open_error(self, message: str) -> None:
        self._show_error(_("Could not open file"), message)

    def _show_error(self, heading: str, message: str) -> None:
        if self._destroyed:
            return
        if hasattr(Adw, "AlertDialog"):
            dialog = Adw.AlertDialog.new(heading, message)
            dialog.add_response("close", _native("Close"))
            dialog.set_close_response("close")
            dialog.present(self._win)
            return
        dialog = Adw.MessageDialog(transient_for=self._win, heading=heading, body=message)
        dialog.add_response("close", _native("Close"))
        dialog.set_close_response("close")
        dialog.present()

    def _copy_to_clipboard(self, uris: str | list[str], *, cut: bool) -> None:
        """Publish Miller items as standard and Nautilus clipboard data."""
        if isinstance(uris, str):
            uris = [uris]
        gfiles = [Gio.File.new_for_uri(u) for u in uris]
        file_list = Gdk.FileList.new_from_list(gfiles)
        value = GObject.Value()
        value.init(Gdk.FileList)
        value.set_boxed(file_list)
        file_provider = Gdk.ContentProvider.new_for_value(value)
        action_prefix = "cut" if cut else "copy"
        nautilus_data = (action_prefix + "\n" + "\n".join(uris)).encode()
        nautilus_provider = Gdk.ContentProvider.new_for_bytes(
            "x-special/gnome-copied-files", GLib.Bytes.new(nautilus_data)
        )
        provider = Gdk.ContentProvider.new_union([file_provider, nautilus_provider])
        self._clipboard.set_content(provider)
        self._set_miller_clipboard_state(uris, cut=cut)

    def _open_file(self, uri: str, content_type: str | None = None) -> None:
        """Open a file, preserving Nautilus's special archive activation.

        When Files is the default archive handler, native Nautilus extracts
        the archive. Launching its desktop file instead would execute
        ``nautilus --new-window``. Miller View owns its rows, so it performs
        that extraction itself and opens the result in this host's slot.
        """
        if self._extract_archive_in_current_window(uri, content_type):
            return
        try:
            Gio.AppInfo.launch_default_for_uri(uri, None)
        except GLib.Error as error:
            _log(f"Could not open {uri!r}: {error.message}")
            self._show_open_error(error.message)

    @staticmethod
    def _choose_archive_output_async(
        parent: Gio.File,
        basename: str,
        cancellable: Gio.Cancellable,
        on_chosen,
        on_error,
        is_reserved=None,
    ) -> None:
        """Find a collision-free extraction folder without blocking GTK."""
        stem = _archive_folder_name(basename)

        def probe(suffix: int) -> None:
            name = stem if suffix == 0 else f"{stem} ({suffix})"
            candidate = parent.get_child(name)
            if callable(is_reserved) and is_reserved(candidate):
                probe(suffix + 1)
                return

            def on_info_ready(file: Gio.File, result: Gio.AsyncResult) -> None:
                try:
                    file.query_info_finish(result)
                except GLib.Error as error:
                    if error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.NOT_FOUND):
                        on_chosen(file)
                    elif not error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                        on_error(error)
                    return
                probe(suffix + 1)

            candidate.query_info_async(
                Gio.FILE_ATTRIBUTE_STANDARD_TYPE,
                Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS,
                GLib.PRIORITY_DEFAULT,
                cancellable,
                on_info_ready,
            )

        probe(0)

    def _extract_archive_in_current_window(
        self, uri: str, content_type: str | None, *, open_when_done: bool = True
    ) -> bool:
        """Extract an archive handled by Nautilus and enter it in this slot.

        Returns True when the URI was recognized and consumed. Other files
        continue through the normal default-application launcher.
        """
        if GnomeAutoar is None or not uri.startswith("file://"):
            return False

        source = Gio.File.new_for_uri(uri)
        basename = source.get_basename() or ""
        if not content_type:
            content_type, _uncertain = Gio.content_type_guess(basename, None)
        try:
            autoar_supported = bool(GnomeAutoar.check_mime_type_supported(content_type))
        except (TypeError, ValueError):
            autoar_supported = False
        default_app = (
            Gio.AppInfo.get_default_for_type(content_type, False) if content_type else None
        )
        default_app_id = default_app.get_id() if default_app is not None else None
        if not _should_extract_archive(content_type, default_app_id, autoar_supported):
            return False

        # Held/repeated Return must not start competing extractions before the
        # first Extractor has created its output directory.
        if uri in self._extracting_archive_uris:
            return True
        cancellable = Gio.Cancellable()
        navigation_generation = self._navigation_generation
        self._extracting_archive_uris.add(uri)
        pending_key = object()
        self._archive_operations[pending_key] = cancellable
        reserved_output_uri: str | None = None

        def finish(operation) -> None:
            nonlocal reserved_output_uri
            self._archive_operations.pop(operation, None)
            self._extracting_archive_uris.discard(uri)
            if reserved_output_uri is not None:
                self._reserved_archive_output_uris.discard(reserved_output_uri)
                reserved_output_uri = None

        def on_output_error(error: GLib.Error) -> None:
            finish(pending_key)
            if not self._destroyed and not self._suspended:
                self._show_file_operation_error(error.message)

        def on_output_chosen(output: Gio.File) -> None:
            nonlocal reserved_output_uri
            self._archive_operations.pop(pending_key, None)
            if self._destroyed or cancellable.is_cancelled():
                self._extracting_archive_uris.discard(uri)
                return
            reserved_output_uri = output.get_uri()
            self._reserved_archive_output_uris.add(reserved_output_uri)
            try:
                extractor = GnomeAutoar.Extractor.new(source, output)
                # Always give the archive a folder of its own. Besides
                # matching the requested folder-like activation, this gives
                # us one stable location to enter even for a single file.
                extractor.set_output_is_dest(True)
            except (GLib.Error, TypeError, ValueError) as error:
                finish(pending_key)
                _log(f"Could not prepare archive extraction for {uri!r}: {error}")
                if not self._suspended and not self._destroyed:
                    self._show_file_operation_error(str(error))
                return

            source_parent = source.get_parent()
            watched_parent = next(
                (
                    column
                    for column in self.columns
                    if source_parent is not None
                    and Gio.File.new_for_uri(column.folder_uri).equal(source_parent)
                ),
                None,
            )
            if self._column_is_live(watched_parent):
                watched_parent.expect_child_creation(output.get_uri())

            self._archive_operations[extractor] = cancellable

            def on_completed(_extractor) -> None:
                finish(extractor)
                if self._column_is_live(watched_parent):
                    watched_parent.finish_expected_child_creation(output.get_uri(), created=True)
                if self._destroyed:
                    return
                if (
                    open_when_done
                    and not self._suspended
                    and navigation_generation == self._navigation_generation
                ):
                    self._open_extracted_folder(uri, output.get_uri())

            def on_error(_extractor, error: GLib.Error) -> None:
                finish(extractor)
                if self._column_is_live(watched_parent):
                    watched_parent.finish_expected_child_creation(output.get_uri(), created=False)
                if not self._destroyed and not self._suspended:
                    self._show_file_operation_error(error.message)

            def on_cancelled(_extractor) -> None:
                finish(extractor)
                if self._column_is_live(watched_parent):
                    watched_parent.finish_expected_child_creation(output.get_uri(), created=False)

            extractor.connect("completed", on_completed)
            extractor.connect("cancelled", on_cancelled)
            extractor.connect("error", on_error)
            extractor.start_async(cancellable)

        parent = source.get_parent()
        if parent is None:
            finish(pending_key)
            return False
        self._choose_archive_output_async(
            parent,
            basename,
            cancellable,
            on_output_chosen,
            on_output_error,
            lambda candidate: candidate.get_uri() in self._reserved_archive_output_uris,
        )
        return True

    def _open_extracted_folder(self, source_uri: str, target_uri: str) -> None:
        """Open an extracted folder as the next Miller column.

        ``sync_to_uri()`` is intentionally an external-navigation reset: it
        replaces the whole chain with one column. Extraction is different --
        the output is a new child of the column containing the archive, so it
        should behave like activating a folder row in that column and retain
        the current folder on the left.
        """
        source_parent = Gio.File.new_for_uri(source_uri).get_parent()
        parent_index = None
        if source_parent is not None:
            parent_index = next(
                (
                    index
                    for index, column in enumerate(self.columns)
                    if Gio.File.new_for_uri(column.folder_uri).equal(source_parent)
                ),
                None,
            )

        if parent_index is None or self._suspended or self._destroyed:
            # Extraction may finish after the user navigated elsewhere. The
            # operation still completes and its row is monitored, but it must
            # never steal the active slot or rebuild an unrelated chain.
            return

        self._cancel_row_commit()
        self._cancel_preview_scroll()
        stale = self.columns[parent_index + 1 :]
        reused_width = stale[0].width if stale else None
        for stale_column in stale:
            stale_column.destroy_enumeration()
        del self.columns[parent_index + 1 :]

        new_width = reused_width if reused_width is not None else COLUMN_WIDTH
        fits = self._new_content_fits(new_width)
        new_column = self._make_real_column(target_uri)
        new_column.width = new_width
        self.columns.append(new_column)
        self.focused_index = parent_index
        preview_replaced = self._set_preview(None)

        # The extraction path suppresses the anticipated monitor event and
        # inserts only the finished output row. Do not call reload() here:
        # that would clear every row a second time at completion and can
        # overwrite the column's vertical scroll restoration while this chain
        # rebuild is settling.
        self._sync_slot_location(target_uri)
        self._sync_column_selections()
        self._apply_focused_column_style()
        self._rebuild_chain()
        self._fade_in(new_column)
        if preview_replaced:
            self._fade_in(self.preview_column, duration=PREVIEW_FADE_DURATION_MS)
        if not fits:
            self._align_to_viewport_end(new_column)

    def _open_files(self, files: list[str | tuple[str, str | None]]) -> None:
        """Open a mixed selection with the same semantics as single items.

        Normal files are grouped per default application (the efficient and
        native multi-open path). Archives whose default handler is Files are
        consumed by the in-place extractor instead, so a multi-selection
        cannot fall through to ``nautilus --new-window``. Only the first such
        archive enters its result; the others extract alongside it.
        """
        if not files:
            return
        normalized = [item if isinstance(item, tuple) else (item, None) for item in files]
        if len(normalized) == 1:
            self._open_file(*normalized[0])
            return

        app_map: dict[str, tuple[Gio.AppInfo, list[str]]] = {}
        fallback_uris: list[str] = []
        archive_count = 0

        for uri, content_type in normalized:
            gfile = Gio.File.new_for_uri(uri)
            ctype = content_type
            if not ctype:
                ctype, _uncertain = Gio.content_type_guess(gfile.get_basename(), None)

            if self._extract_archive_in_current_window(
                uri, ctype, open_when_done=archive_count == 0
            ):
                archive_count += 1
                continue

            app_info = Gio.AppInfo.get_default_for_type(ctype, False) if ctype else None
            if app_info is not None:
                app_id = app_info.get_id() or app_info.get_executable() or "default"
                if app_id not in app_map:
                    app_map[app_id] = (app_info, [])
                app_map[app_id][1].append(uri)
            else:
                fallback_uris.append(uri)

        for _app_id, (app_info, group_uris) in app_map.items():
            try:
                app_info.launch_uris(group_uris, None)
            except GLib.Error as error:
                _log(f"Could not launch app {app_info.get_name()} for {group_uris!r}: {error}")
                for uri in group_uris:
                    self._open_file(uri)

        for u in fallback_uris:
            self._open_file(u)

    def _on_clipboard_changed(self, _clipboard: Gdk.Clipboard) -> None:
        """Drop stale Miller sources when another app replaces the clipboard.

        Reading clipboard bytes from this notification previously caused a
        Nautilus re-entrancy freeze. The notification itself is safe: it only
        clears our cached source list, while the live menu queries formats.
        """
        self._set_miller_clipboard_state([], cut=False)

    def _clipboard_has_pasteable_files(self) -> bool:
        """Whether the current Nautilus/GTK clipboard offers a file list.

        This intentionally inspects metadata only. Nautilus publishes its
        Cut and Copy data as ``Gdk.FileList``; the actual file list is read
        only after the user chooses Paste.
        """
        formats = self._clipboard.get_formats()
        return (
            formats.contain_gtype(Gdk.FileList.__gtype__)
            or formats.contain_mime_type("x-special/gnome-copied-files")
            or formats.contain_mime_type("text/uri-list")
        )

    def _set_miller_clipboard_state(self, uris: list[str], *, cut: bool) -> None:
        """Synchronize paste availability and visible cut rows with GTK clipboard state."""
        self._clear_cut_rows()
        self._clipboard_uris = uris
        self._clipboard_is_cut = cut and bool(uris)
        if self._clipboard_is_cut:
            self._set_cut_rows()

    def set_native_cut_observer_active(self, active: bool) -> None:
        """Mirror native ``is-cut`` state only while Miller is visible."""
        if active:
            self._native_cut_observer.start()
        else:
            self._native_cut_observer.stop()

    def _apply_native_cut_uris(self, uris: set[str]) -> None:
        self._set_miller_clipboard_state(sorted(uris), cut=bool(uris))

    def _set_cut_rows(self) -> None:
        """Apply persistent cut state to rows represented by our clipboard."""
        for column in self.columns:
            for row in column.rows():
                if row.uri in self._clipboard_uris:
                    row.set_cut(True)

    def _clear_cut_rows(self) -> None:
        """Clear all visible Miller cut-row styling."""
        for column in self.columns:
            for row in column.rows():
                row.set_cut(False)

    def _clear_context_active_row(self, row: Gtk.Widget) -> None:
        """Clear the temporary :active state used while a menu is open."""
        components.set_row_active(row, False)

    def _paste_into_folder(self, destination_uri: str) -> None:
        """Paste known Miller sources, or read an external file list on demand."""
        if self._clipboard_uris:
            self._paste_uris_into_folder(
                self._clipboard_uris, destination_uri, cut=self._clipboard_is_cut
            )
            return
        if not self._clipboard_has_pasteable_files():
            return

        formats = self._clipboard.get_formats()
        if formats.contain_mime_type("x-special/gnome-copied-files"):
            self._clipboard.read_async(
                ["x-special/gnome-copied-files"],
                GLib.PRIORITY_DEFAULT,
                None,
                self._on_external_nautilus_clipboard_read,
                destination_uri,
            )
            return

        if not formats.contain_gtype(Gdk.FileList.__gtype__) and formats.contain_mime_type(
            "text/uri-list"
        ):
            self._clipboard.read_async(
                ["text/uri-list"],
                GLib.PRIORITY_DEFAULT,
                None,
                self._on_external_uri_list_read,
                destination_uri,
            )
            return

        self._clipboard.read_value_async(
            Gdk.FileList.__gtype__,
            GLib.PRIORITY_DEFAULT,
            None,
            self._on_external_file_list_read,
            destination_uri,
        )

    def _on_external_nautilus_clipboard_read(
        self, clipboard: Gdk.Clipboard, result: Gio.AsyncResult, destination_uri: str
    ) -> None:
        """Decode Nautilus's private clipboard payload so external cuts stay moves."""

        def on_data(data: bytes) -> None:
            decoded = _parse_nautilus_clipboard_data(data)
            if decoded is None:
                _log("Nautilus clipboard data had an unsupported format")
                return
            uris, is_cut = decoded
            self._paste_uris_into_folder(uris, destination_uri, cut=is_cut)

        self._read_clipboard_stream(clipboard, result, on_data)

    def _on_external_uri_list_read(
        self, clipboard: Gdk.Clipboard, result: Gio.AsyncResult, destination_uri: str
    ) -> None:
        """Paste a standard URI-list offered by a non-GTK application."""

        def on_data(data: bytes) -> None:
            uris = _parse_uri_list_data(data)
            if uris:
                self._paste_uris_into_folder(uris, destination_uri, cut=False)

        self._read_clipboard_stream(clipboard, result, on_data)

    def _read_clipboard_stream(self, clipboard: Gdk.Clipboard, result, on_data) -> None:
        """Read one explicitly requested clipboard stream without blocking GTK."""
        try:
            stream, _mime_type = clipboard.read_finish(result)
        except GLib.Error as error:
            _log(f"Could not read clipboard data: {error.message}")
            return
        chunks: list[bytes] = []

        def on_chunk_ready(
            input_stream: Gio.InputStream, chunk_result: Gio.AsyncResult, _data
        ) -> None:
            try:
                chunk = input_stream.read_bytes_finish(chunk_result).get_data()
            except GLib.Error as error:
                input_stream.close_async(GLib.PRIORITY_DEFAULT, None, lambda *_args: None)
                _log(f"Could not read clipboard stream: {error.message}")
                return
            if chunk:
                chunks.append(bytes(chunk))
                input_stream.read_bytes_async(
                    64 * 1024, GLib.PRIORITY_DEFAULT, None, on_chunk_ready, None
                )
                return
            input_stream.close_async(GLib.PRIORITY_DEFAULT, None, lambda *_args: None)
            on_data(b"".join(chunks))

        stream.read_bytes_async(64 * 1024, GLib.PRIORITY_DEFAULT, None, on_chunk_ready, None)

    def _on_external_file_list_read(
        self, clipboard: Gdk.Clipboard, result: Gio.AsyncResult, destination_uri: str
    ) -> None:
        """Paste a Nautilus Copy file list after an explicit user request."""
        try:
            value = clipboard.read_value_finish(result)
            file_list = value.get_boxed()
            uris = [file.get_uri() for file in file_list.get_files()]
        except (GLib.Error, AttributeError, TypeError) as error:
            _log(f"Could not read clipboard files for paste: {error}")
            return
        if uris:
            # Nautilus's private cut bit is mirrored through
            # NativeCutStateObserver when its source is in this window. A
            # plain external Gdk.FileList is a Copy, matching GTK semantics.
            self._paste_uris_into_folder(uris, destination_uri, cut=self._clipboard_is_cut)

    def _paste_uris_into_folder(
        self, source_uris: list[str], destination_uri: str, *, cut: bool
    ) -> None:
        """Submit a resolved Copy/Move operation and refresh affected columns."""
        source_parents = []
        for source_uri in source_uris:
            parent = Gio.File.new_for_uri(source_uri).get_parent()
            if parent is not None:
                source_parents.append(parent.get_uri())
        self._watch_operation_directories([*source_parents, destination_uri])
        method = "MoveURIs" if cut else "CopyURIs"
        parameters = GLib.Variant("(assa{sv})", (source_uris, destination_uri, {}))
        self._call_nautilus_file_operation(
            method,
            parameters,
            destination_uri,
            on_started=self._clear_clipboard_after_paste if cut else None,
        )

    def _clear_clipboard_after_paste(self) -> None:
        """Drop copied-file ownership once Nautilus has accepted a paste."""
        self._clipboard.set_content(None)

    def _watch_operation_directories(self, directory_uris: list[str]) -> None:
        """Reload open source/destination columns after a native operation changes them."""
        monitors = []
        candidates = {uri for uri in directory_uris}
        watched = set()
        for uri in candidates:
            target = Gio.File.new_for_uri(uri)
            for column in self.columns:
                if Gio.File.new_for_uri(column.folder_uri).equal(target):
                    # The permanent column monitor already coalesces and reloads
                    # this location. A second operation monitor would trigger a
                    # duplicate enumeration for the same filesystem event.
                    if getattr(column, "_file_monitor", None) is None:
                        watched.add(uri)
                    break
        if not watched:
            return
        watched_files = [Gio.File.new_for_uri(uri) for uri in watched]
        refresh_id = 0
        expiry_id = 0

        def finish_refresh(*, expired: bool = False) -> bool:
            nonlocal refresh_id, expiry_id
            if refresh_id:
                if expired:
                    GLib.source_remove(refresh_id)
                self._operation_timeout_ids.discard(refresh_id)
            refresh_id = 0
            if expiry_id:
                if not expired:
                    GLib.source_remove(expiry_id)
                self._operation_timeout_ids.discard(expiry_id)
                expiry_id = 0
            if not self._suspended and not self._destroyed:
                for column in self.columns:
                    column_file = Gio.File.new_for_uri(column.folder_uri)
                    if any(column_file.equal(watched_file) for watched_file in watched_files):
                        column.reload()
            for monitor in monitors:
                monitor.cancel()
                if monitor in self._operation_monitors:
                    self._operation_monitors.remove(monitor)
            return GLib.SOURCE_REMOVE

        def expire_monitors() -> bool:
            return finish_refresh(expired=True)

        def on_changed(*_args) -> None:
            nonlocal refresh_id
            if refresh_id:
                GLib.source_remove(refresh_id)
                self._operation_timeout_ids.discard(refresh_id)
            refresh_id = GLib.timeout_add(150, finish_refresh)
            self._operation_timeout_ids.add(refresh_id)

        for directory_uri in watched:
            try:
                monitor = Gio.File.new_for_uri(directory_uri).monitor_directory(
                    Gio.FileMonitorFlags.NONE, None
                )
            except GLib.Error as error:
                _log(f"Could not monitor operation directory {directory_uri!r}: {error.message}")
                continue
            monitor.connect("changed", on_changed)
            monitors.append(monitor)
            self._operation_monitors.append(monitor)
        # A backend may accept the operation without emitting a monitor event.
        # Refresh once after a short grace period and always release monitors.
        expiry_id = GLib.timeout_add_seconds(2, expire_monitors)
        self._operation_timeout_ids.add(expiry_id)

    def _show_destination_picker(self, uris: str | list[str], *, move: bool) -> None:
        """Choose a destination folder in Nautilus's modal native file dialog."""
        if isinstance(uris, str):
            uris = [uris]
        if not uris:
            return
        dialog = Gtk.FileDialog()
        dialog.set_title(
            _native("Select Move Destination") if move else _native("Select Copy Destination")
        )
        dialog.set_accept_label(_("Select"))
        dialog.set_initial_folder(Gio.File.new_for_uri(uris[0]).get_parent())

        def on_destination_selected(source, result, _data) -> None:
            try:
                destination = source.select_folder_finish(result)
            except GLib.Error as error:
                if not error.matches(Gtk.DialogError, Gtk.DialogError.DISMISSED):
                    _log(f"Could not select destination for {uris!r}: {error.message}")
                return
            if self._suspended or self._destroyed:
                return
            method = "MoveURIs" if move else "CopyURIs"
            parameters = GLib.Variant("(assa{sv})", (uris, destination.get_uri(), {}))
            watch_uris = [destination.get_uri()]
            for uri in uris:
                source_parent = Gio.File.new_for_uri(uri).get_parent()
                if source_parent is not None:
                    watch_uris.append(source_parent.get_uri())
            self._watch_operation_directories(watch_uris)
            self._call_nautilus_file_operation(method, parameters, uris[0])

        dialog.select_folder(self._win, None, on_destination_selected, None)

    def _show_compress_dialog(self, column: Gtk.Widget, uris: list[str]) -> None:
        """Choose a ZIP destination and create it asynchronously with GNOME Autoar."""
        if GnomeAutoar is None or not uris:
            return
        sources = [Gio.File.new_for_uri(uri) for uri in uris]
        parent = Gio.File.new_for_uri(column.folder_uri)
        first_name = sources[0].get_basename() or _native("Archive")
        stem = os.path.splitext(first_name)[0]
        initial_name = f"{stem}.zip" if len(sources) == 1 else _native("Archive.zip")

        dialog = Gtk.FileDialog()
        dialog.set_title(_("Create Archive"))
        dialog.set_accept_label(_("Create"))
        dialog.set_initial_folder(parent)
        dialog.set_initial_name(initial_name)

        def on_destination_selected(source, result, _data) -> None:
            try:
                destination = source.save_finish(result)
            except GLib.Error as error:
                if not error.matches(Gtk.DialogError, Gtk.DialogError.DISMISSED):
                    self._show_file_operation_error(error.message)
                return
            if self._suspended or self._destroyed:
                return
            self._create_archive(column, sources, destination)

        dialog.save(self._win, None, on_destination_selected, None)

    def _create_archive(
        self,
        source_column: Gtk.Widget,
        sources: list[Gio.File],
        destination: Gio.File,
    ) -> None:
        """Run one selected ZIP operation and retain it until termination."""
        destination_parent = destination.get_parent()
        destination_column = next(
            (
                column
                for column in self.columns
                if destination_parent is not None
                and Gio.File.new_for_uri(column.folder_uri).equal(destination_parent)
            ),
            None,
        )
        if self._column_is_live(destination_column):
            destination_column.expect_child_creation(destination.get_uri())
        compressor = GnomeAutoar.Compressor.new(
            sources,
            destination,
            GnomeAutoar.Format.ZIP,
            GnomeAutoar.Filter.NONE,
            len(sources) > 1,
        )
        compressor.set_output_is_dest(True)
        cancellable = Gio.Cancellable()
        self._archive_operations[compressor] = cancellable

        def finish() -> None:
            self._archive_operations.pop(compressor, None)

        def on_completed(_compressor) -> None:
            finish()
            if self._column_is_live(destination_column):
                destination_column.finish_expected_child_creation(
                    destination.get_uri(), created=True
                )

        def on_error(_compressor, error: GLib.Error) -> None:
            finish()
            if self._column_is_live(destination_column):
                destination_column.finish_expected_child_creation(
                    destination.get_uri(), created=False
                )
            if not self._suspended and not self._destroyed:
                self._show_file_operation_error(error.message)

        def on_cancelled(*_args) -> None:
            finish()
            if self._column_is_live(destination_column):
                destination_column.finish_expected_child_creation(
                    destination.get_uri(), created=False
                )

        compressor.connect("completed", on_completed)
        compressor.connect("cancelled", on_cancelled)
        compressor.connect("error", on_error)
        compressor.start_async(cancellable)

    def _email_files(self, uris: list[str]) -> None:
        """Open the default mail composer with every selected local attachment."""
        xdg_email = shutil.which("xdg-email")
        paths = [Gio.File.new_for_uri(uri).get_path() for uri in uris]
        if xdg_email is None or any(path is None for path in paths):
            return
        argv = [xdg_email]
        for path in paths:
            argv.extend(("--attach", path))
        try:
            process = Gio.Subprocess.new(argv, Gio.SubprocessFlags.STDERR_PIPE)
        except GLib.Error as error:
            self._show_open_error(error.message)
            return

        def on_finished(subprocess: Gio.Subprocess, result: Gio.AsyncResult) -> None:
            try:
                _ok, _stdout, stderr = subprocess.communicate_utf8_finish(result)
            except GLib.Error as error:
                self._show_open_error(error.message)
                return
            if not subprocess.get_successful():
                self._show_open_error((stderr or _("Could not open the mail composer")).strip())

        process.communicate_utf8_async(None, None, on_finished)

    def trash_focused_folder(self) -> bool:
        """Move the focused local Miller item(s) to trash (the Delete target)."""
        column = self._focused_column()
        if column is None:
            return False
        uris = column.selected_uris() if hasattr(column, "selected_uris") else []
        if not uris:
            row = column.selected_row()
            if row is not None:
                uris = [row.uri]
        file_uris = [u for u in uris if u.startswith("file://")]
        if not file_uris or len(file_uris) != len(uris):
            return False
        self._move_to_trash(column, file_uris)
        return True

    def delete_permanently_focused_folder(self) -> bool:
        """Permanently delete focused local Miller item(s) (the Shift+Delete target)."""
        column = self._focused_column()
        if column is None:
            return False
        uris = column.selected_uris() if hasattr(column, "selected_uris") else []
        if not uris:
            row = column.selected_row()
            if row is not None:
                uris = [row.uri]
        file_uris = [u for u in uris if u.startswith("file://")]
        if not file_uris or len(file_uris) != len(uris):
            return False
        # DeleteURIs owns the native confirmation. Presenting an extension
        # dialog here causes a second confirmation when Nautilus accepts the
        # request, despite there being only one Shift+Delete key dispatch.
        self._delete_permanently(column, file_uris)
        return True

    def _delete_permanently(self, source_column: Gtk.Widget, uris: list[str]) -> None:
        """Run DeleteURIs; Nautilus presents its own confirmation dialog."""
        file_uris = [u for u in uris if u.startswith("file://")]
        if not file_uris:
            return
        self._call_nautilus_file_operation(
            "DeleteURIs", GLib.Variant("(asa{sv})", (file_uris, {})), file_uris[0]
        )

    def copy_focused_folder_to_clipboard(self, *, cut: bool) -> bool:
        """Copy or cut the focused Miller item(s) (the Ctrl+X/Ctrl+C targets)."""
        column = self._focused_column()
        if column is None:
            return False
        uris = column.selected_uris() if hasattr(column, "selected_uris") else []
        if not uris:
            row = column.selected_row()
            if row is not None:
                uris = [row.uri]
        if not uris:
            return False
        self._copy_to_clipboard(uris, cut=cut)
        return True

    def paste_into_focused_folder(self) -> bool:
        """Paste into the focused Miller folder (the Ctrl+V target)."""
        column = self._focused_column()
        row = column.selected_row() if column is not None else None
        if column is None or not self._clipboard_has_pasteable_files():
            return False
        destination_uri = row.uri if row is not None and row.is_dir else column.folder_uri
        self._paste_into_folder(destination_uri)
        return True

    def rename_focused_folder(self) -> bool:
        """Open Rename for the focused local Miller item (the F2 target).
        Renaming targets exactly one item, so a multi-selection declines the
        shortcut rather than picking one of them -- same rule the row menu
        already follows (see _on_row_right_clicked's is_multi branch)."""
        column = self._focused_column()
        if column is not None and len(column.selected_rows()) > 1:
            return False
        row = column.selected_row() if column is not None else None
        if row is None or not row.uri.startswith("file://"):
            return False
        self._show_rename_popover(column, row)
        return True

    def _focused_rows(self) -> tuple[Gtk.Widget | None, list[Gtk.Widget]]:
        column = self._focused_column()
        if column is None:
            return None, []
        rows = column.selected_rows()
        if not rows:
            cursor = getattr(column, "_cursor_row", None)
            if cursor in column.rows():
                rows = [cursor]
        return column, rows

    def open_focused_selection(self, disposition: str = "current") -> bool:
        column, rows = self._focused_rows()
        if column is None or not rows:
            return False
        if disposition == "current":
            return self._open_selection(column)

        files = [(row.uri, row.content_type) for row in rows if not row.is_dir]
        folders = [row.uri for row in rows if row.is_dir]
        if files:
            self._open_files(files)
        for uri in folders:
            if disposition == "tab":
                self._ext._do_open_tab(uri, self._win, make_active=False)
            else:
                self._ext._do_open_window(uri)
        return True

    def show_focused_properties(self) -> bool:
        _column, rows = self._focused_rows()
        if not rows:
            return False
        self._ext._do_properties([row.uri for row in rows], self._win)
        return True

    def reload_focused_view(self) -> bool:
        column = self._focused_column()
        if column is None:
            return False
        # F5 refreshes the folder the user is operating on. Reloading every
        # ancestor in a deep chain needlessly re-enumerates and re-sorts
        # unrelated directories, which is especially visible on large or
        # remote folders. MyComputerColumn.reload() preserves this column's
        # selection and vertical scroll in place.
        column.reload()
        requested = list(self.preview_column.file_uris)
        if requested:
            self._set_preview(requested[0] if len(requested) == 1 else requested, force=True)
            self._rebuild_chain()
        return True

    def invert_focused_selection(self) -> bool:
        column = self._focused_column()
        if column is None:
            return False
        rows = column.rows()
        if not rows:
            return False
        selected = set(column.selected_rows())
        column.list_box.unselect_all()
        inverted = [row for row in rows if row not in selected]
        for row in inverted:
            column.list_box.select_row(row)
        column._anchor_row = inverted[0] if inverted else None
        column._cursor_row = inverted[-1] if inverted else None
        if inverted:
            self._activate_selection(column, inverted[-1])
        else:
            self._collapse_below(column)
        return True

    def select_matching_items(self) -> bool:
        column = self._focused_column()
        if column is None:
            return False
        popover = Gtk.Popover()
        popover.set_has_arrow(True)
        popover.set_parent(column)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)
        label = Gtk.Label(label=_native("Select Items Matching"), xalign=0.0)
        label.add_css_class("heading")
        entry = Gtk.Entry()
        entry.set_placeholder_text(_("Pattern, for example *.jpg"))
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        actions.set_halign(Gtk.Align.END)
        cancel = Gtk.Button(label=_native("Cancel"))
        select = Gtk.Button(label=_native("Select"))
        select.add_css_class("suggested-action")
        actions.append(cancel)
        actions.append(select)
        box.append(label)
        box.append(entry)
        box.append(actions)
        popover.set_child(box)

        def apply_pattern(*_args) -> None:
            pattern = entry.get_text().strip()
            if not pattern:
                return
            matches = [
                row
                for row in column.rows()
                if fnmatch.fnmatchcase(row.display_name.casefold(), pattern.casefold())
            ]
            column.list_box.unselect_all()
            for row in matches:
                column.list_box.select_row(row)
            column._anchor_row = matches[0] if matches else None
            column._cursor_row = matches[-1] if matches else None
            if matches:
                self._activate_selection(column, matches[-1])
            else:
                self._collapse_below(column)
            popover.popdown()

        cancel.connect("clicked", lambda *_args: popover.popdown())
        select.connect("clicked", apply_pattern)
        entry.connect("activate", apply_pattern)
        popover.connect("unmap", lambda widget: GLib.idle_add(widget.unparent))
        popover.popup()
        entry.grab_focus()
        return True

    def show_focused_context_menu(self) -> bool:
        preview_context = getattr(self.preview_column, "show_text_context_menu", None)
        focus = self._win.get_focus() if getattr(self, "_win", None) is not None else None
        preview_has_focus = False
        ancestor = focus
        while ancestor is not None:
            if ancestor is self.preview_column:
                preview_has_focus = True
                break
            ancestor = ancestor.get_parent()
        if preview_has_focus and callable(preview_context) and preview_context():
            return True
        column, rows = self._focused_rows()
        if column is None:
            return False
        row = getattr(column, "_cursor_row", None)
        if row not in rows:
            row = rows[0] if rows else None
        if row is None:
            allocation = column.get_allocation()
            self._show_column_background_menu(
                column,
                max(1.0, allocation.width / 2),
                max(1.0, allocation.height / 2),
            )
            return True
        allocation = row.get_allocation()
        self._on_row_right_clicked(
            None,
            1,
            max(1.0, allocation.width / 2),
            max(1.0, allocation.height / 2),
            column,
            row,
        )
        return True

    def _set_as_background(self, uri: str) -> None:
        try:
            settings = Gio.Settings.new("org.gnome.desktop.background")
            settings.set_string("picture-uri", uri)
            if "picture-uri-dark" in settings.list_keys():
                settings.set_string("picture-uri-dark", uri)
            Gio.Settings.sync()
        except GLib.Error as error:
            self._show_file_operation_error(error.message)

    def _extract_rows_here(self, rows: list[Gtk.Widget]) -> None:
        for row in rows:
            self._extract_archive_in_current_window(row.uri, row.content_type, open_when_done=False)

    def _show_extract_destination(self, rows: list[Gtk.Widget]) -> None:
        if GnomeAutoar is None or not rows:
            return
        dialog = Gtk.FileDialog()
        dialog.set_title(_native("Select Extract Destination"))
        dialog.set_accept_label(_native("Select"))
        first_parent = Gio.File.new_for_uri(rows[0].uri).get_parent()
        if first_parent is not None:
            dialog.set_initial_folder(first_parent)

        def on_selected(source: Gtk.FileDialog, result: Gio.AsyncResult, _data=None) -> None:
            try:
                destination = source.select_folder_finish(result)
            except GLib.Error as error:
                if not error.matches(Gtk.DialogError, Gtk.DialogError.DISMISSED):
                    self._show_file_operation_error(error.message)
                return
            for row in rows:
                self._extract_archive_to_parent(row.uri, destination)

        dialog.select_folder(self._win, None, on_selected, None)

    def _extract_archive_to_parent(self, uri: str, parent: Gio.File) -> None:
        source = Gio.File.new_for_uri(uri)
        cancellable = Gio.Cancellable()
        pending_key = object()
        self._archive_operations[pending_key] = cancellable
        reserved_uri: str | None = None

        def finish(operation) -> None:
            nonlocal reserved_uri
            self._archive_operations.pop(operation, None)
            if reserved_uri is not None:
                self._reserved_archive_output_uris.discard(reserved_uri)
                reserved_uri = None

        def on_choose_error(error: GLib.Error) -> None:
            finish(pending_key)
            self._show_file_operation_error(error.message)

        def on_chosen(output: Gio.File) -> None:
            nonlocal reserved_uri
            self._archive_operations.pop(pending_key, None)
            if self._destroyed or cancellable.is_cancelled():
                return
            reserved_uri = output.get_uri()
            self._reserved_archive_output_uris.add(reserved_uri)
            try:
                extractor = GnomeAutoar.Extractor.new(source, output)
                extractor.set_output_is_dest(True)
            except (GLib.Error, TypeError, ValueError) as error:
                finish(pending_key)
                self._show_file_operation_error(str(error))
                return
            destination_column = next(
                (
                    column
                    for column in self.columns
                    if Gio.File.new_for_uri(column.folder_uri).equal(parent)
                ),
                None,
            )
            if self._column_is_live(destination_column):
                destination_column.expect_child_creation(output.get_uri())
            self._archive_operations[extractor] = cancellable

            def completed(_extractor) -> None:
                finish(extractor)
                if self._column_is_live(destination_column):
                    destination_column.finish_expected_child_creation(
                        output.get_uri(), created=True
                    )

            def failed(_extractor, error: GLib.Error) -> None:
                finish(extractor)
                if self._column_is_live(destination_column):
                    destination_column.finish_expected_child_creation(
                        output.get_uri(), created=False
                    )
                self._show_file_operation_error(error.message)

            def cancelled(_extractor) -> None:
                finish(extractor)
                if self._column_is_live(destination_column):
                    destination_column.finish_expected_child_creation(
                        output.get_uri(), created=False
                    )

            extractor.connect("completed", completed)
            extractor.connect("error", failed)
            extractor.connect("cancelled", cancelled)
            extractor.start_async(cancellable)

        self._choose_archive_output_async(
            parent,
            source.get_basename() or _native("Archive"),
            cancellable,
            on_chosen,
            on_choose_error,
            lambda candidate: candidate.get_uri() in self._reserved_archive_output_uris,
        )

    def _create_links_at(self, source_uris: list[str], destination_uri: str) -> bool:
        local_sources = [
            Gio.File.new_for_uri(uri) for uri in source_uris if uri.startswith("file://")
        ]
        destination = Gio.File.new_for_uri(destination_uri)
        if not local_sources or destination.get_path() is None:
            return False
        destination_column = next(
            (
                column
                for column in self.columns
                if Gio.File.new_for_uri(column.folder_uri).equal(destination)
            ),
            None,
        )

        def create_one(source: Gio.File, suffix: int = 1) -> None:
            source_path = source.get_path()
            if source_path is None:
                return
            basename = source.get_basename() or _native("Link")
            label = _("Link to {name}").format(name=basename)
            name = (
                label if suffix == 1 else _("{name} ({number})").format(name=label, number=suffix)
            )
            link = destination.get_child(name)
            if self._column_is_live(destination_column):
                destination_column.expect_child_creation(link.get_uri())

            def on_created(target: Gio.File, result: Gio.AsyncResult, _data=None) -> None:
                try:
                    target.make_symbolic_link_finish(result)
                except GLib.Error as error:
                    if self._column_is_live(destination_column):
                        destination_column.finish_expected_child_creation(
                            target.get_uri(), created=False
                        )
                    if error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.EXISTS):
                        create_one(source, suffix + 1)
                    else:
                        self._show_file_operation_error(error.message)
                    return
                if self._column_is_live(destination_column):
                    destination_column.finish_expected_child_creation(
                        target.get_uri(), created=True
                    )

            link.make_symbolic_link_async(
                source_path,
                GLib.PRIORITY_DEFAULT,
                None,
                on_created,
                None,
            )

        for source in local_sources:
            create_one(source)
        return True

    def create_links_for_focused_selection(self) -> bool:
        column, rows = self._focused_rows()
        return bool(
            column is not None
            and rows
            and self._create_links_at([row.uri for row in rows], column.folder_uri)
        )

    def paste_links_in_focused_folder(self) -> bool:
        column = self._focused_column()
        if column is None or not self._clipboard_uris:
            return False
        row = column.selected_row()
        destination_uri = row.uri if row is not None and row.is_dir else column.folder_uri
        return self._create_links_at(self._clipboard_uris, destination_uri)

    def undo_file_operation(self) -> bool:
        self._call_nautilus_file_operation("Undo", GLib.Variant("(a{sv})", ({},)), self._root_uri)
        return True

    def redo_file_operation(self) -> bool:
        self._call_nautilus_file_operation("Redo", GLib.Variant("(a{sv})", ({},)), self._root_uri)
        return True

    def adjust_zoom(self, direction: int) -> bool:
        preview_zoom = getattr(self.preview_column, "_change_image_zoom", None)
        preview_reset = getattr(self.preview_column, "_set_image_zoom", None)
        stack = getattr(self.preview_column, "_preview_stack", None)
        if (
            stack is not None
            and stack.get_visible_child_name() == "image"
            and callable(preview_reset)
        ):
            if direction == 0:
                preview_reset(100)
            elif callable(preview_zoom):
                preview_zoom(25 * direction)
            return True
        levels = ["small", "medium", "large"]
        settings = self._ext._nautilus_prefs._list_view
        current = settings.get_string("default-zoom-level")
        try:
            index = levels.index(current)
        except ValueError:
            index = 1
        target = (
            "medium" if direction == 0 else levels[max(0, min(len(levels) - 1, index + direction))]
        )
        if target != current:
            settings.set_string("default-zoom-level", target)
        return True

    def _on_real_row_activated(self, column: Gtk.Widget, row: Gtk.Widget) -> None:
        self._navigation_generation += 1
        # Single-click navigation. Activating a row always replaces the entire
        # deeper chain to the right of its column: cancel and drop every column
        # past the activated one, each carrying its own .width away with it.
        # The trailing preview width is derived from PREVIEW_WIDTH plus any
        # available viewport slack, so it is not tracked per-instance.
        index = self.columns.index(column)
        # Read before any mutation below (self.focused_index still holds the
        # previously-focused column, see col_nav_direction).
        direction = col_nav_direction(self.focused_index, index)
        _log(f"_on_real_row_activated: direction={direction}")
        # The click landed in this column -- it becomes the focused one for
        # subsequent arrow-key nav ("focus follows last selection").
        self.focused_index = index
        stale = self.columns[index + 1 :]
        # Clicking a folder row that's already open one column over (its
        # child column exists and shows exactly this folder) is a no-op as
        # far as content goes -- e.g. re-clicking the already-selected row,
        # or clicking back onto a folder while browsing deeper inside it.
        # Tearing that column down and rebuilding it from scratch would
        # flash it empty and re-run its async enumeration for content
        # that's already sitting there correctly. Reuse it, but anything
        # beyond it is still a stale, no-longer-relevant selection and gets
        # collapsed exactly like the non-reuse case below.
        already_open = (
            row.is_dir
            and stale
            and Gio.File.new_for_uri(stale[0].folder_uri).equal(Gio.File.new_for_uri(row.uri))
        )

        selected_rows = column.selected_rows() if hasattr(column, "selected_rows") else []
        if len(selected_rows) > 1:
            for stale_column in stale:
                stale_column.destroy_enumeration()
            del self.columns[index + 1 :]
            self._set_preview([r.uri for r in selected_rows])
            self._sync_slot_location(column.folder_uri)
            self._sync_column_selections()
            self._apply_focused_column_style()
            self._rebuild_chain()
            return

        new_column: Gtk.Widget | None = None
        preview_replaced = False
        preview_added = False
        if already_open:
            fits = self._new_content_fits(self.columns[index + 1].width)
            for stale_column in stale[1:]:
                stale_column.destroy_enumeration()
            del self.columns[index + 2 :]
            self._sync_slot_location(row.uri)
        else:
            # The slot right after the clicked column is getting new content
            # either way (a fresh folder's rows replace whatever was there)
            # -- from the user's point of view that's the same column being
            # refreshed, not a new one appearing, even when clicking further
            # back in the stack collapses several deeper columns at once.
            # So its dragged width carries over instead of resetting to the
            # default. Columns beyond that slot are genuinely gone, not
            # replaced.
            reused_width = stale[0].width if stale else None
            for stale_column in stale:
                stale_column.destroy_enumeration()
            del self.columns[index + 1 :]

            if row.is_dir:
                new_width = reused_width if reused_width is not None else COLUMN_WIDTH
                # Checked BEFORE the append below: self.columns still reflects
                # only the columns that are staying (see _new_content_fits).
                # Just the new column's own width -- the trailing preview
                # isn't part of this check (see _align_to_viewport_end).
                fits = self._new_content_fits(new_width)
                # Folder -> drill down: append a fresh column for its
                # contents and clear the preview (nothing is selected in the
                # new column yet).
                new_column = self._make_real_column(row.uri)
                new_column.width = new_width
                self.columns.append(new_column)
                preview_replaced = self._set_preview(None)
                # Push the real Nautilus location to match (title/pathbar/
                # back-forward follow the Miller chain). Also covers
                # navigating back UP the chain (clicking a folder row in an
                # earlier column) -- same is_dir branch, same call.
                # File-preview clicks below don't navigate -- selecting a
                # file for preview isn't "browsing" it.
                self._sync_slot_location(row.uri)
            else:
                # File -> update the preview only, no new column. Same
                # already-visible check, against the preview's own width.
                fits = self._new_content_fits(PREVIEW_WIDTH)
                # Only a genuinely new preview fades in and gets scrolled to;
                # re-selecting the file already shown must leave it, and any
                # text selected in it, exactly as it was.
                preview_replaced = self._set_preview(row.uri)
                preview_added = preview_replaced

        self._sync_column_selections()
        self._apply_focused_column_style()
        if direction == NAV_UP:
            # Collapsing back to an earlier column can drop a lot of width
            # at once (many open columns down to one) -- reset the scroll
            # position before _rebuild_chain()'s _sync_root_width call so
            # it doesn't inflate the new, narrower canvas to match the
            # stale, still-far-scrolled value left over from the columns
            # that just went away (see _reset_viewport_width).
            self._reset_viewport_width()
        self._rebuild_chain()
        if new_column is not None:
            self._fade_in(new_column)
        if preview_replaced:
            self._fade_in(self.preview_column, duration=PREVIEW_FADE_DURATION_MS)
        if preview_added:
            # Selecting a file always surfaces its preview, regardless of
            # nav direction or whether it already fits -- unlike a folder
            # drill-down, there's no "already visible, leave it" case: the
            # preview pane is the whole point of the click. The preview is
            # always the last thing in the canvas, so jump straight to the
            # true scroll max rather than computing an edge (see
            # _scroll_to_viewport_end) -- but not immediately: see
            # _arm_preview_scroll for why this waits out the double-click
            # window instead of scrolling on the spot.
            self._arm_preview_scroll()
        elif direction in (NAV_DOWN, NAV_SELF):
            # New content just appeared at the tail -- pull it into view
            # (right-aligned) only if it doesn't already fit.
            if not fits:
                self._align_to_viewport_end(
                    new_column if new_column is not None else self.preview_column
                )
        else:
            # Backing out to an earlier column -- bring *that* column fully
            # into view (left-aligned), regardless of whatever ends up
            # appended after it.
            if not self._column_fully_visible(index):
                self._align_to_viewport_start(column)

    def _sync_slot_location(self, uri: str) -> None:
        """Push the Miller chain's new deepest folder to Nautilus's real slot
        location (see _on_slot_location_changed below for the reverse
        direction, sync_to_uri below). The "slot." prefix is required or the
        action fails silently (see CLAUDE.md's fragility table / main.py's
        existing tab-open callers).

        This navigates Nautilus's live-underneath native slot, which
        begins to re-enumerate the folder. Measured (2026-07-11): the call
        itself is sub-millisecond and fully async (0.3-0.6ms, never blocks
        the main thread), and real drills land 0.7-9s apart, not in
        coalescable bursts. A debounce was rejected on that data because it
        would buy nothing and risk the sync-loop echo guard. Once Nautilus
        commits the new location and chrome, _on_slot_location_changed
        activates slot.stop to cancel the hidden native view's remaining
        metadata, model, monitor, and thumbnail work. Ctrl+1/Ctrl+2 reloads
        that model before exposing the native view again.

        The pushed URI is remembered so the notify::location it eventually
        produces can be recognized as this chain's own echo rather than a
        user navigation (see sync_to_uri). That echo is asynchronous, so it
        can arrive after further clicks have already moved the chain on --
        without the record, a late echo for an abandoned folder reads as
        somebody navigating there and re-roots the view onto it."""
        self._pending_slot_uris.append((uri, GLib.get_monotonic_time()))
        del self._pending_slot_uris[:-_MAX_PENDING_SLOT_URIS]
        try:
            self._win.activate_action("slot.open-location", GLib.Variant("s", uri))
        except Exception as e:
            if self._pending_slot_uris and self._pending_slot_uris[-1][0] == uri:
                self._pending_slot_uris.pop()
            _log(f"_sync_slot_location failed for {uri!r}: {e}")

    def _detach_root(self) -> None:
        old_root = getattr(self, "root", None)
        if old_root is not None:
            self._detach_paned_children(old_root)
            self.aligner.set_content(None)

    def sync_to_uri(self, new_uri: str) -> None:
        """Reconcile the Miller chain with Nautilus's real location -- called
        when it changes while Column View is already showing (address bar,
        pathbar, back/forward, a bookmark, the sidebar; see
        _on_slot_location_changed below).

        Growing the chain is the exclusive privilege of activating a row in
        the view itself (_on_real_row_activated). Navigation that arrives
        from anywhere else lands as a single fresh column via reset(), even
        when the target happens to sit directly under a folder already on
        screen. That last part is deliberate and was the whole point of the
        rule: a sidebar place used to render one column from a cold start but
        two (its parent, then itself) whenever that parent happened to be
        open, so the same click produced a different view depending on
        history.
        An ancestor column is only ever shown because the user walked
        through it.

        Two cases still preserve what is open. Our own drill-down echo (the
        notify::location produced by _sync_slot_location) changes nothing at
        all -- the chain already moved before the push, and the echo can
        arrive late, after further clicks, so it is matched against the
        pending-push record rather than against the chain's current shape.
        And a location that is already open as an ancestor column -- a
        pathbar chip, or Alt+Left back into the branch -- truncates to it
        and keeps everything to its left untouched: no re-enumeration, no
        flicker, no lost widths. Backing up inside a chain the user built
        by hand is not the same as being sent somewhere new.

        Deliberately does NOT walk new_uri's filesystem ancestry to rebuild
        a multi-level chain: that logic used to treat every location as "a
        descendant of wherever the chain happens to be rooted," which
        degenerates once the root is the filesystem root itself (everything
        is a descendant of "/") and silently exploded any external
        navigation into a full path chain.

        Matches via Gio.File.equal() rather than string comparison --
        trailing slashes, percent-encoding, and other representational
        differences between the same location's two URI strings (e.g. one
        came from Nautilus's real slot, the other from our own enumeration)
        are exactly what GVfs's own equality already normalizes for; a bare
        rstrip("/") string compare is not guaranteed to agree with it in
        every case (the raw filesystem root "file:///" is a corner case:
        rstrip strips all three slashes down to "file:", not one)."""
        target = Gio.File.new_for_uri(new_uri)

        cutoff = GLib.get_monotonic_time() - _PENDING_SLOT_URI_TTL_US
        self._pending_slot_uris[:] = [
            pending for pending in self._pending_slot_uris if pending[1] >= cutoff
        ]

        for position, (pending_uri, _created_at) in enumerate(self._pending_slot_uris):
            if Gio.File.new_for_uri(pending_uri).equal(target):
                # This chain's own push coming back. Everything queued before
                # it was superseded without ever echoing, so drop those too.
                del self._pending_slot_uris[: position + 1]
                return

        existing = [Gio.File.new_for_uri(c.folder_uri) for c in self.columns]
        idx = next((i for i, f in enumerate(existing) if f.equal(target)), None)
        if idx is not None:
            if idx == len(existing) - 1:
                return
            self._navigation_generation += 1
            _log("sync_to_uri: truncating to already-open ancestor")
            self._cancel_row_commit()
            self._cancel_preview_scroll()
            for stale in self.columns[idx + 1 :]:
                stale.destroy_enumeration()
            del self.columns[idx + 1 :]
            self.focused_index = idx
            self._set_preview(None)
            self._sync_column_selections()
            self._apply_focused_column_style()
            self._reset_viewport_width()
            self._rebuild_chain()
            return

        self.reset(new_uri)

        self._set_preview(None)
        if _COLUMN_KEYBOARD_NAV:
            self.focused_index = len(self.columns) - 1
        else:
            # See the equivalent branch in _on_real_row_activated: the
            # column whose row selection leads into the deepest/current
            # column is "current" here.
            self.focused_index = max(0, len(self.columns) - 2)
        self._sync_column_selections()
        self._apply_focused_column_style()
        # Truncating to an ancestor is the same kind of collapse as NAV_UP
        # above -- reset the scroll position before the rebuild so the
        # stale, pre-truncation value doesn't get baked into the new,
        # narrower canvas (see _reset_viewport_width).
        self._reset_viewport_width()
        self._rebuild_chain()
        # Not `idx`: that name still holds the ancestor-lookup result from
        # above, which is None in this fallback branch -- that's exactly why
        # execution reached reset() instead of returning early. focused_index
        # was just (re)computed for this freshly reset chain above and is
        # the column actually meant here.
        if not self._column_fully_visible(self.focused_index):
            self._align_to_viewport_start(self.columns[self.focused_index])
        # Deliberately no _arm_focus_retry call here: focused_index/the accent
        # highlight track the new location just above, but selecting a
        # column (click or the echo of one) no longer grabs GTK keyboard
        # focus onto it.

    def _sync_column_selections(self) -> None:
        """Each column's own row selection is derived from the URI chain
        that is actually open right now, not tracked as click history: a
        column highlights the row whose URI equals the next column's
        folder_uri, and the last column highlights the previewed file (or
        nothing, if the last column is itself a fresh empty drill-down).

        A multi-selection is preserved, but only in the deepest column: every
        multi-select path collapses whatever was open past the column it
        happened in (see _on_real_row_activated), so the deepest column is
        the only one where several selected rows can be current. Preserving
        it anywhere else stranded a stale block of highlighted rows (Ctrl+A
        in an earlier column) that nothing ever cleared, with that column's
        real path row left unhighlighted."""
        last_index = len(self.columns) - 1
        for i, col in enumerate(self.columns):
            col.clear_active_row()
            if col.has_pending_selection_restore():
                continue
            if i == last_index and len(col.selected_rows()) > 1:
                continue
            if i + 1 < len(self.columns):
                col.select_child_for_uri(self.columns[i + 1].folder_uri)
            elif self.preview_column.file_uri:
                col.select_child_for_uri(self.preview_column.file_uri)
            elif not self.preview_column.file_uris:
                col.clear_selection()

    def _apply_focused_column_style(self) -> None:
        """Mark exactly self.columns[self.focused_index] as *the* column
        whose selection reads as accent (see MyComputerColumn.set_current_column
        / the .mc-current-column CSS rule in main.py) -- called after every
        place focused_index is set (a click, sync_to_uri's echo, and the
        keyboard-nav methods below) so the single highlighted row always
        tracks the last-clicked/-focused column, never the whole committed
        path at once."""
        for i, col in enumerate(self.columns):
            col.set_current_column(i == self.focused_index)

    def _clear_typeahead(self) -> bool:
        if self._typeahead_clear_id:
            GLib.source_remove(self._typeahead_clear_id)
            self._typeahead_clear_id = 0
        self._typeahead_query = ""
        return GLib.SOURCE_REMOVE

    def _expire_typeahead(self) -> bool:
        self._typeahead_clear_id = 0
        self._typeahead_query = ""
        return GLib.SOURCE_REMOVE

    def _arm_typeahead_clear(self) -> None:
        if self._typeahead_clear_id:
            GLib.source_remove(self._typeahead_clear_id)
        self._typeahead_clear_id = GLib.timeout_add(_TYPEAHEAD_RESET_MS, self._expire_typeahead)

    def _typeahead_select(self, column: Gtk.Widget, text: str) -> bool:
        rows = column.rows()
        if not rows:
            # The key still belongs to Miller while an empty/slow folder is
            # focused. Propagating it would start typeahead in the covered
            # native view against an unrelated model.
            return True
        query = (self._typeahead_query + text).casefold()
        matches = [row for row in rows if row.display_name.casefold().startswith(query)]
        if not matches and self._typeahead_query:
            query = text.casefold()
            matches = [row for row in rows if row.display_name.casefold().startswith(query)]
        if not matches:
            self._clear_typeahead()
            return True
        current = getattr(column, "_cursor_row", None)
        match = matches[0]
        if current in matches and len(matches) > 1 and query == text.casefold():
            match = matches[(matches.index(current) + 1) % len(matches)]
        self._typeahead_query = query
        self._arm_typeahead_clear()
        column.list_box.unselect_all()
        column.list_box.select_row(match)
        column._anchor_row = match
        column._cursor_row = match
        self._arm_row_commit(column, match)
        match.grab_focus()
        return True

    def _on_key_pressed(self, _ctrl, keyval, _keycode, gtk_state) -> bool:
        """Handle keyboard navigation and multi-selection shortcuts in Column View."""
        if keyval in (Gdk.KEY_asciitilde, Gdk.KEY_slash):
            return False

        # This controller is on the outer Miller scroller in the CAPTURE
        # phase, so it also sees keys aimed at the preview column inside it.
        # A widget with its own text selection (the EPUB reader, the
        # extracted-text view) must keep them: otherwise Ctrl+A selected
        # every row in the column instead of the text being read, and the
        # arrow keys moved the selection instead of scrolling the page.
        focus = self._win.get_focus()
        if _focus_owns_text_selection(focus):
            return False
        # Preview controls (PDF paging/zoom, EPUB/WebKit, media controls,
        # links and copy buttons) own their keyboard events even when they do
        # not expose an editable text selection. The outer capture controller
        # must not reinterpret their Return/arrows/Ctrl+A as file-list input.
        ancestor = focus
        while ancestor is not None:
            if ancestor is self.preview_column:
                return False
            ancestor = ancestor.get_parent()

        column = self._focused_column()
        if column is None:
            return False

        # Keyboard input begins a new selection transaction. It must not be
        # constrained by the temporary pin left by an earlier claimed mouse
        # gesture that produced no row-activated echo.
        column.clear_pinned_selection()

        ctrl = bool(gtk_state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(gtk_state & Gdk.ModifierType.SHIFT_MASK)
        alt = bool(gtk_state & Gdk.ModifierType.ALT_MASK)
        super_pressed = bool(gtk_state & Gdk.ModifierType.SUPER_MASK)

        if ctrl and keyval in (Gdk.KEY_a, Gdk.KEY_A):
            column.list_box.select_all()
            # Routed through the same activation path a shift+click takes, so
            # Select All collapses the deeper chain and surfaces the
            # multi-item preview instead of leaving a fully highlighted
            # column sitting behind an unrelated open chain.
            selected = column.selected_rows()
            if selected:
                self._activate_selection(column, selected[0])
            return True

        if ctrl and keyval in (Gdk.KEY_space, Gdk.KEY_KP_Space):
            rows = column.rows()
            cursor = getattr(column, "_cursor_row", None)
            if cursor not in rows:
                cursor = column.selected_row() or (rows[0] if rows else None)
            if cursor is None:
                return True
            if cursor in column.selected_rows():
                column.list_box.unselect_row(cursor)
            else:
                column.list_box.select_row(cursor)
            column._anchor_row = cursor
            if column.selected_rows():
                self._activate_selection(column, cursor)
            else:
                self._collapse_below(column)
            return True

        if keyval == Gdk.KEY_Escape and self._typeahead_query:
            self._clear_typeahead()
            return True

        if (
            keyval == Gdk.KEY_BackSpace
            and self._typeahead_query
            and not (ctrl or alt or super_pressed)
        ):
            self._typeahead_query = self._typeahead_query[:-1]
            if self._typeahead_query:
                old_query, self._typeahead_query = self._typeahead_query, ""
                self._typeahead_select(column, old_query)
            else:
                self._clear_typeahead()
            return True

        if not (ctrl or alt or super_pressed):
            character = Gdk.keyval_to_unicode(keyval)
            if character >= 0x20 and not chr(character).isspace():
                return self._typeahead_select(column, chr(character))

        rows = column.rows() if hasattr(column, "rows") else []
        if not rows:
            selection_keys = (
                Gdk.KEY_Return,
                Gdk.KEY_KP_Enter,
                Gdk.KEY_ISO_Enter,
                Gdk.KEY_Up,
                Gdk.KEY_KP_Up,
                Gdk.KEY_Down,
                Gdk.KEY_KP_Down,
                Gdk.KEY_Left,
                Gdk.KEY_KP_Left,
                Gdk.KEY_Right,
                Gdk.KEY_KP_Right,
                Gdk.KEY_Home,
                Gdk.KEY_KP_Home,
                Gdk.KEY_End,
                Gdk.KEY_KP_End,
            )
            return keyval in selection_keys

        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_ISO_Enter):
            self._open_selection(column)
            return True

        if keyval in (Gdk.KEY_Up, Gdk.KEY_KP_Up):
            cursor = getattr(column, "_cursor_row", None)
            selected = column.selected_rows() if hasattr(column, "selected_rows") else []
            if cursor in rows:
                current_idx = rows.index(cursor)
            elif selected:
                current_idx = rows.index(selected[0])
            else:
                current_idx = 0

            target_idx = max(0, current_idx - 1)
            target_row = rows[target_idx]
            column._cursor_row = target_row
            if ctrl:
                # Ctrl moves the cursor alone -- it neither changes the
                # selection nor commits, so the user can walk to a row and
                # add it to a multi-selection without dragging the open
                # chain (and Nautilus's real location) along the way.
                target_row.grab_focus()
                return True
            if shift:
                self._select_range(column, target_row)
            else:
                column.list_box.unselect_all()
                column.list_box.select_row(target_row)
                column._anchor_row = target_row
            self._arm_row_commit(column, target_row)
            target_row.grab_focus()
            return True

        if keyval in (Gdk.KEY_Down, Gdk.KEY_KP_Down):
            cursor = getattr(column, "_cursor_row", None)
            selected = column.selected_rows() if hasattr(column, "selected_rows") else []
            if cursor in rows:
                current_idx = rows.index(cursor)
            elif selected:
                current_idx = rows.index(selected[-1])
            else:
                current_idx = 0

            target_idx = min(len(rows) - 1, current_idx + 1)
            target_row = rows[target_idx]
            column._cursor_row = target_row
            if ctrl:
                # Cursor-only, same as Ctrl+Up above.
                target_row.grab_focus()
                return True
            if shift:
                self._select_range(column, target_row)
            else:
                column.list_box.unselect_all()
                column.list_box.select_row(target_row)
                column._anchor_row = target_row
            self._arm_row_commit(column, target_row)
            target_row.grab_focus()
            return True

        rtl = self.scroller.get_direction() == Gtk.TextDirection.RTL
        back_keys = (Gdk.KEY_Right, Gdk.KEY_KP_Right) if rtl else (Gdk.KEY_Left, Gdk.KEY_KP_Left)
        forward_keys = (Gdk.KEY_Left, Gdk.KEY_KP_Left) if rtl else (Gdk.KEY_Right, Gdk.KEY_KP_Right)

        if keyval in back_keys:
            if self.focused_index > 0:
                self.focused_index -= 1
                self._apply_focused_column_style()
                new_col = self._focused_column()
                if new_col is not None:
                    new_col.grab_list_focus()
                return True
            return True

        if keyval in forward_keys:
            row = getattr(column, "_cursor_row", None) or column.selected_row()
            if row is None or not row.is_dir:
                return True
            next_index = self.focused_index + 1
            if next_index < len(self.columns) and Gio.File.new_for_uri(
                self.columns[next_index].folder_uri
            ).equal(Gio.File.new_for_uri(row.uri)):
                self.focused_index += 1
                self._apply_focused_column_style()
                new_col = self._focused_column()
                if new_col is not None:
                    new_col.grab_list_focus()
                return True
            if row not in column.selected_rows():
                column.list_box.unselect_all()
                column.list_box.select_row(row)
                column._anchor_row = row
                column._cursor_row = row
            self._on_real_row_activated(column, row)
            return True

        if keyval in (Gdk.KEY_Home, Gdk.KEY_KP_Home, Gdk.KEY_End, Gdk.KEY_KP_End):
            target_row = rows[0] if keyval in (Gdk.KEY_Home, Gdk.KEY_KP_Home) else rows[-1]
            # Also moves the cursor: leaving it behind meant a following
            # Up/Down resumed from wherever the cursor last was rather than
            # from the row Home/End just jumped to.
            column._cursor_row = target_row
            if shift:
                self._select_range(column, target_row)
            else:
                column.list_box.unselect_all()
                column.list_box.select_row(target_row)
                column._anchor_row = target_row
            # Committed for both, unlike before: a shift+Home/End range left
            # the preview showing whatever the previous selection was.
            self._arm_row_commit(column, target_row)
            target_row.grab_focus()
            return True

        return False

    def _focused_column(self) -> Gtk.Widget | None:
        if 0 <= self.focused_index < len(self.columns):
            return self.columns[self.focused_index]
        return None

    def _arm_focus_retry(self, col) -> None:
        """Re-grab keyboard focus onto `col` for a bounded run of frames.

        A drill-down commit calls _sync_slot_location, which asks Nautilus to
        navigate its real (hidden-behind-our-overlay) slot. That navigation
        finishes asynchronously and, once it does, Nautilus's own GtkGridView
        for the new location grabs focus for itself -- stealing it from our
        list_box a frame or more after our own grab_focus() already
        succeeded. One-shot GLib.idle_add loses this race. Same class of
        problem as _queue_stale_generation_release's multi-frame wait
        elsewhere in this file: re-assert our grab every frame for a bounded
        window so ours is the one still standing once Nautilus's async
        update actually settles."""
        self._cancel_focus_retry()
        state = {"ticks_left": _FOCUS_RETRY_FRAMES}

        def _retry_on_tick(_widget, _frame_clock) -> bool:
            if self._destroyed or col is not self._focused_column():
                self._focus_retry_id = 0
                return GLib.SOURCE_REMOVE
            col.grab_list_focus()
            state["ticks_left"] -= 1
            if state["ticks_left"] > 0:
                return GLib.SOURCE_CONTINUE
            self._focus_retry_id = 0
            return GLib.SOURCE_REMOVE

        self._focus_retry_id = self.scroller.add_tick_callback(_retry_on_tick)

    def _cancel_focus_retry(self) -> None:
        if self._focus_retry_id:
            self.scroller.remove_tick_callback(self._focus_retry_id)
            self._focus_retry_id = 0

    def _make_preview_column(self) -> Gtk.Widget:
        # Starts empty (nothing selected yet); a fresh preview is built each
        # time a file is clicked (see _set_preview).
        return MyComputerPreviewColumn(self._ext, None, self._show_open_error)

    def _set_preview(self, file_uri: str | list[str] | None, *, force: bool = False) -> bool:
        """Point the preview at file_uri, rebuilding it only if that is not
        already what it is showing. True if a new preview was built.

        The early return is what makes re-selecting the current file a no-op
        rather than a reload: the preview is otherwise rebuilt from scratch on
        every activation, which threw away everything the existing one had --
        a text selection, the scroll position, a PDF's rendered pages -- and
        started the whole extraction again for a file already on screen.
        Re-selecting happens constantly (clicking a row that is already
        selected, an echo of our own navigation, a reload of the column).

        Reusing the widget across a rebuild is safe: _detach_paned_children
        only unparents, so the next _rebuild_chain re-adds this same instance
        with its state intact."""
        requested = file_uri if isinstance(file_uri, list) else ([file_uri] if file_uri else [])
        old = self.preview_column
        if not force and old is not None and getattr(old, "file_uris", None) == requested:
            return False
        if old is not None:
            old.destroy_enumeration()
        self.preview_column = MyComputerPreviewColumn(self._ext, file_uri, self._show_open_error)
        return True

    def _rebuild_chain(self) -> None:
        old_root = getattr(self, "root", None)
        if old_root is not None:
            self._detach_paned_children(old_root)

        self.paneds = []
        self.root = self._make_paned_chain([*self.columns, self.preview_column])
        self.root.set_vexpand(True)
        self.root.set_valign(Gtk.Align.FILL)
        self.aligner.set_content(self.root)
        self._sync_root_width()

    def _detach_paned_children(self, widget: Gtk.Widget) -> None:
        if not isinstance(widget, Gtk.Paned):
            return
        start_child = widget.get_start_child()
        end_child = widget.get_end_child()
        widget.set_start_child(None)
        widget.set_end_child(None)
        if isinstance(start_child, Gtk.Paned):
            self._detach_paned_children(start_child)
        if isinstance(end_child, Gtk.Paned):
            self._detach_paned_children(end_child)

    def _make_paned_chain(self, columns: list[Gtk.Widget]) -> Gtk.Widget:
        tail = columns[-1]
        for index, column in reversed(list(enumerate(columns[:-1]))):
            paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
            paned.set_vexpand(True)
            paned.set_valign(Gtk.Align.FILL)
            paned.set_start_child(column)
            paned.set_end_child(tail)
            # The start child is always a fixed-width folder column -- it
            # must never auto-grow when the paned itself is reallocated
            # bigger. All slack cascades rightward toward the trailing
            # preview column, which is the only thing meant to absorb it
            # (see _sync_root_width). Making index 0 a special case here
            # (resize_start_child=True) created a feedback loop: growing
            # the container to fit a wider column 0 made GTK auto-grow
            # column 0 further to fill that same new space, runaway growth
            # with nothing to stop it.
            paned.set_resize_start_child(False)
            paned.set_resize_end_child(True)
            paned.set_shrink_start_child(False)
            paned.set_shrink_end_child(False)
            paned.set_wide_handle(False)
            paned.set_position(COLUMN_WIDTH if not _COLUMN_RESIZE_ENABLED else column.width)
            if _COLUMN_RESIZE_ENABLED:
                paned.connect("notify::position", self._on_paned_position_changed, index)
                # Watches for a genuine press on this handle (see
                # _on_paned_handle_pressed) so _on_paned_position_changed can
                # tell an actual user drag apart from GTK repositioning the
                # handle on its own. Must be CAPTURE phase: the handle itself
                # is a private child widget with its own internal drag
                # gesture, which claims the press before a default BUBBLE
                # probe on the paned would ever see it (confirmed -- with
                # BUBBLE, "pressed" never fired for a real handle grab, so
                # _active_drag_index stayed unset and every real drag got
                # reverted, blocking manual resize entirely). CAPTURE runs on
                # the way down, before that claim happens. Fires for any
                # descendant click too, so the pressed handler re-checks the
                # x coordinate against the handle's own position rather than
                # trusting that it fired.
                handle_probe = Gtk.GestureClick(button=0)
                handle_probe.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
                handle_probe.connect("pressed", self._on_paned_handle_pressed, index)
                handle_probe.connect("released", self._on_paned_handle_released, index)
                paned.add_controller(handle_probe)
            else:
                paned.connect("notify::position", self._on_paned_position_fixed, index)
            self.paneds.append(paned)
            tail = paned
        return tail

    def _on_paned_handle_pressed(
        self, gesture: Gtk.GestureClick, _n_press: int, x: float, _y: float, index: int
    ) -> None:
        paned = gesture.get_widget()
        handle_x = paned.get_position()
        if paned.get_direction() == Gtk.TextDirection.RTL:
            handle_x = paned.get_width() - handle_x
        if abs(x - handle_x) <= HANDLE_HIT_SLOP:
            self._active_drag_index = index

    def _on_paned_handle_released(
        self, _gesture: Gtk.GestureClick, _n_press: int, _x: float, _y: float, index: int
    ) -> None:
        if self._active_drag_index == index:
            self._active_drag_index = None

    def _on_paned_position_fixed(self, paned: Gtk.Paned, _pspec, index: int) -> None:
        """_COLUMN_RESIZE_ENABLED is off: revert any drag on this handle back to
        COLUMN_WIDTH immediately, so it never actually resizes a column. Keeps
        _on_paned_position_changed's clamp/squish logic completely unplugged
        rather than deleted."""
        if self._clamping:
            return
        if paned.get_position() == COLUMN_WIDTH:
            return
        self._clamping = True
        paned.set_position(COLUMN_WIDTH)
        self._clamping = False

    def _on_paned_position_changed(self, paned: Gtk.Paned, _pspec, index: int) -> None:
        """The floor (_COLUMN_MIN_WIDTH) needs no manual clamp: MyComputerColumn
        already carries a size_request(_COLUMN_MIN_WIDTH) floor (widgets.py),
        so Gtk.Paned itself refuses to drag the handle any narrower -- vanilla
        GTK resize handles that better than reasserting a position from here
        ever did. There's no equivalent native ceiling though (size_request
        only ever sets a minimum), so _COLUMN_MAX_WIDTH still needs this
        manual clamp.

        "position" also changes when GTK itself repositions the handle
        during layout (e.g. the preview column's own minimum width grows
        and, for a moment, there isn't room for every column at its current
        width) -- not just from a user drag, and shrink_start_child(False)/
        shrink_end_child(False) above only enforce each child's *measured*
        minimum, they don't stop GTK from moving the handle to satisfy the
        other side. Without this check that transient, GTK-driven squish
        got written into the column's .width as if it were a deliberate resize and
        stayed squished afterward. Only actually commit the new width while
        the user is holding this handle (see _on_paned_handle_pressed);
        otherwise snap back to the last width they chose."""
        if self._clamping:
            return
        if index >= len(self.columns):
            return

        width = paned.get_position()
        col = self.columns[index]

        if self._active_drag_index != index:
            if width != col.width:
                self._clamping = True
                paned.set_position(col.width)
                self._clamping = False
            return

        if width > _COLUMN_MAX_WIDTH:
            self._clamping = True
            paned.set_position(_COLUMN_MAX_WIDTH)
            self._clamping = False
            width = _COLUMN_MAX_WIDTH

        col.width = width
        self._sync_root_width()

    def _reset_viewport_width(self) -> None:
        """Cancel any pending scroll animation/debounce and snap the
        hadjustment straight back to 0, synchronously, no easing.

        Must run before a rebuild that drastically shrinks the open chain
        -- a hard re-root (reset(), e.g. clicking a bookmark while many
        columns are open), backing out to an early column (NAV_UP in
        _on_real_row_activated), or truncating to an already-open ancestor
        (sync_to_uri) -- because _sync_root_width's `visible_right_edge`
        term (viewport_width + adj.get_value()) inflates canvas_width to
        match whatever the adjustment's *current* value is, so an in-flight
        scroll-to animation never has its target canvas yanked narrower
        mid-flight. Right after one of those collapses, that current value
        is still the old, far-scrolled position left over from the chain
        that just went away -- so without resetting it first, the guard
        keeps the new, much narrower canvas artificially stretched to
        match the stale value, and nothing ever corrects it afterward
        (_poll_viewport_size only re-runs _sync_root_width on an actual
        viewport resize, not on scroll).

        Not routed through the debounced align/scroll machinery: callers
        still separately schedule an *animated* move to wherever the final
        resting position should be (e.g. reset()'s own
        _align_to_viewport_end, or _align_to_viewport_start for NAV_UP) --
        this only guarantees the value _sync_root_width reads on the very
        next call, inside the rebuild these callers are about to trigger,
        is already sane."""
        if self._scroll_settle_debounce_id != 0:
            GLib.source_remove(self._scroll_settle_debounce_id)
            self._scroll_settle_debounce_id = 0
        self._pending_scroll_intent = None
        if self._scroll_animation is not None:
            self._scroll_animation.skip()
            self._scroll_animation = None
        self.scroller.get_hadjustment().set_value(0)

    def _sync_root_width(self) -> None:
        adj = self.scroller.get_hadjustment()
        viewport_width = self.scroller.get_width()
        viewport_height = self.scroller.get_height()
        visible_right_edge = viewport_width + adj.get_value()
        # The preview absorbs all slack: when the fixed folder columns don't
        # fill the viewport it stretches to the right edge (hexpand=True,
        # halign=FILL on the preview widget itself); once they overflow it
        # sits at its own PREVIEW_WIDTH floor and the scroller scrolls.
        fixed_width = self._col_position(len(self.columns))
        preview_default_width = PREVIEW_WIDTH

        if viewport_width <= 0:
            preview_width = preview_default_width
        else:
            available_for_preview = viewport_width - fixed_width
            preview_width = max(preview_default_width, available_for_preview)

        total_width = fixed_width + preview_width
        canvas_width = max(total_width, viewport_width, visible_right_edge)
        height_request = viewport_height if viewport_height > 0 else -1
        self.root.set_size_request(canvas_width, height_request)
        # _MillerCanvas is a Gtk.Scrollable: GTK never recomputes or clamps
        # this adjustment on its own (confirmed -- nothing wraps it in an
        # internal Viewport, and Gtk.Adjustment.set_upper() alone never
        # touches value), so setting bounds here can never itself move the
        # scroll position out from under an in-flight animation the way the
        # old size-request-driven approach did. lower/page-size/value are
        # otherwise untouched here; only upper (canvas_width) changes as
        # navigation adds/removes columns.
        adj.set_lower(0)
        adj.set_upper(canvas_width)
        adj.set_page_size(viewport_width)
        adj.set_page_increment(viewport_width)
        self.root.queue_allocate()
        self.aligner.queue_allocate()

    def _col_position(self, index: int) -> float:
        """Canvas x-position where self.columns[index] starts (or, for
        index == len(self.columns), where the trailing preview starts):
        total width (plus handles) of every real column before it. Pure
        arithmetic through each column's own .width, not
        widget.translate_coordinates like the actual scroll target
        (_widget_canvas_x): translate_coordinates needs the column's real,
        settled allocation, which right after _rebuild_chain() reparents it
        into a freshly built Gtk.Paned tree isn't there yet -- GTK hasn't
        relaid it out, so it reads a stale/zeroed position. This has no such
        settling delay, so it's safe to call any time relative to
        _rebuild_chain()."""
        handle_width = 12 if HANDLE_WIDTH_ESTIMATE is None else HANDLE_WIDTH_ESTIMATE
        return (
            sum((getattr(c, "width", None) or 260.0) for c in self.columns[:index])
            + handle_width * index
        )

    def _column_fully_visible(self, index: int) -> bool:
        """True if self.columns[index] is entirely within the currently
        visible viewport (both edges), so NAV_UP navigation landing on it
        can leave the scroll position alone rather than moving it."""
        viewport_width = self.scroller.get_width()
        if viewport_width <= 0:
            return False  # not laid out yet -- nothing to measure
        left = self._widget_canvas_x(self.columns[index])
        if left is None:
            return False
        right = left + self.columns[index].width
        visible_left = self.scroller.get_hadjustment().get_value()
        visible_right = visible_left + viewport_width
        return left >= visible_left and right <= visible_right

    def _widget_canvas_x(self, widget: Gtk.Widget) -> float | None:
        """True on-canvas x position of widget's left edge, read directly
        from GTK's own layout rather than arithmetic through each column's
        .width/HANDLE_WIDTH_ESTIMATE. Relative to self.root, which doesn't itself
        get shifted by scrolling (only its parent aligner does, via
        _MillerCanvas._reposition's move()), so this is already canvas-space
        with no scroll offset to subtract back out. Used for the actual
        scroll landing position (see _apply_pending_scroll) since that's
        where a HANDLE_WIDTH_ESTIMATE mismatch is most visible -- it's only
        ever a guess at a Gtk.Paned handle's real rendered width, and the
        error compounds with every handle counted. Returns None if widget
        isn't part of self.root's hierarchy or hasn't been allocated yet.

        This binding's translate_coordinates returns a plain (x, y) pair
        (or None on failure) rather than the (ok, x, y) triple exposed by
        some GTK/PyGObject versions."""
        result = widget.translate_coordinates(self.root, 0, 0)
        if result is None:
            return None
        x, _y = result
        return x

    def _new_content_fits(self, added_width: float) -> bool:
        """True if the empty space already visible between the end of the
        current (pre-mutation) columns and the right edge of the window is
        big enough to hold added_width without moving the scroll position.
        When true, the caller leaves the scroll position alone; only a real
        space shortage earns a scroll (see _align_to_viewport_end)."""
        viewport_width = self.scroller.get_width()
        if viewport_width <= 0:
            return False  # not laid out yet (e.g. initial load) -- nothing to measure
        adjustment = self.scroller.get_hadjustment()
        visible_left = adjustment.get_value()
        visible_right = visible_left + viewport_width
        preview_left = self._widget_canvas_x(self.preview_column)
        if preview_left is None:
            return False
        if self.scroller.get_direction() == Gtk.TextDirection.RTL:
            available = preview_left + self.preview_column.get_width() - visible_left
        else:
            available = visible_right - preview_left
        return available >= added_width

    def _align_to_viewport_end(self, widget: Gtk.Widget) -> None:
        """Scroll (once things settle) just far enough that widget's
        reading-end edge lands flush against that edge of the viewport --
        right in LTR, left in RTL (named after the logical, direction-aware
        edge rather than the physical one, same convention GTK itself uses
        for Gtk.Align.START/END). GtkPaned mirrors the logical start/end
        children; _apply_pending_scroll mirrors the physical edge math.

        Not a blanket "scroll to the true end of the chain": the trailing
        preview column is deliberately not part of this (unless widget *is*
        the preview, e.g. the initial load), so if it doesn't fit in what's
        left it simply stays off-screen until the user scrolls further,
        rather than every drill-down yanking the view all the way to show it.

        widget's real position/width is read at apply time via
        _widget_canvas_x (see _apply_pending_scroll), not computed here --
        this just records which widget to measure once settled.

        Arms its own settle timer immediately (see _arm_scroll_settle_timer)
        rather than waiting for a subsequent Gtk.Adjustment "changed" event
        to do it -- _MillerCanvas being a Gtk.Scrollable means nothing else
        ever touches this adjustment behind our backs anymore (see its
        docstring), so unlike before, no further "changed" is guaranteed to
        fire after _rebuild_chain()'s own bounds update. _on_hadjustment_changed
        still separately re-arms the same timer for the burst-of-several-
        rebuilds case (e.g. paned positions settling right as a column is
        added), so a later, truly-final layout still wins over this one."""
        self._pending_scroll_intent = ("align_end", widget, None)
        self._arm_scroll_settle_timer()

    def _align_to_viewport_start(self, widget: Gtk.Widget) -> None:
        """Scroll (once things settle) so widget's reading-start edge lands
        flush against that edge of the viewport -- left in LTR, right in
        RTL (see _align_to_viewport_end's docstring for the naming
        rationale). Used for NAV_UP: backing out to an earlier column
        anchors *that* column from the start rather than aligning whatever's
        newest to the end, since the point is to bring the column the user
        just acted on back into full view, not to chase whatever (if
        anything) got appended after it. See _align_to_viewport_end for the
        mirror-image NAV_DOWN case and why this arms its own settle timer
        immediately instead of waiting on a "changed" event."""
        self._pending_scroll_intent = ("align_start", widget, None)
        self._arm_scroll_settle_timer()

    def _align_to_viewport_pos(self, widget: Gtk.Widget, position: float) -> None:
        """Scroll (once things settle) so widget's reading-start edge lands
        `position` pixels in from that edge of the viewport, instead of
        flush against it (position=0 is equivalent to
        _align_to_viewport_start). For a column narrower than the viewport
        that still needs some run-up space visible before it, e.g. leaving
        the previous column partially in view rather than snapping it
        fully off-screen."""
        self._pending_scroll_intent = ("align_pos", widget, position)
        self._arm_scroll_settle_timer()

    def _scroll_to_viewport_end(self) -> None:
        """Scroll straight to the true end of the scrollable range, no widget
        measurement involved. Only valid when the thing that should end up
        flush against the viewport's end edge is guaranteed to be the very
        last thing in the canvas -- the preview column when a file is
        selected (it's always appended last and is what expands the
        scrollable range in the first place), unlike _align_to_viewport_end
        which measures a widget's own edge because that widget isn't always
        the last one still visible (e.g. a folder drill-down where a
        narrower trailing preview may sit past it off-screen)."""
        self._pending_scroll_intent = ("scroll_end", None, None)
        self._arm_scroll_settle_timer()

    def _scroll_to_viewport_start(self) -> None:
        """Scroll straight to the true start of the scrollable range (0), no
        widget measurement involved -- the mirror-image of
        _scroll_to_viewport_end for whenever the leftmost column is
        guaranteed to be what should be flush against the viewport start."""
        self._pending_scroll_intent = ("scroll_start", None, None)
        self._arm_scroll_settle_timer()

    def _scroll_to_viewport_pos(self, position: float) -> None:
        """Scroll straight to an absolute hadjustment value, no widget
        measurement involved -- for callers that already know the raw
        scroll offset they want (e.g. restoring a previously recorded
        position) rather than aligning a widget's edge to one."""
        self._pending_scroll_intent = ("scroll_pos", None, position)
        self._arm_scroll_settle_timer()

    def _on_capture_scroll(self, controller, dx: float, dy: float) -> bool:
        """Pan the Miller chain horizontally for a horizontal-intent scroll,
        claiming the event before any column can consume it (see the
        CAPTURE-phase controller wired in __init__). Horizontal intent is
        either a real horizontal delta (dx, e.g. a tilt-wheel or trackpad) or
        Shift+vertical-wheel (dy with the Shift modifier -- the Linux
        convention; GTK reports it as dy rather than a pre-swapped dx). A
        plain vertical scroll with no Shift is left alone
        (EVENT_PROPAGATE) so each column keeps scrolling its own list."""
        if dx != 0:
            pan = dx
        else:
            event = controller.get_current_event()
            state = event.get_modifier_state() if event is not None else 0
            if not (state & Gdk.ModifierType.SHIFT_MASK):
                return Gdk.EVENT_PROPAGATE
            pan = dy
        if pan == 0:
            return Gdk.EVENT_PROPAGATE
        # Interactive previews (currently spreadsheets) have their own
        # horizontal viewport. Forward the GTK delta explicitly: WebKitGTK
        # does not reliably translate smooth touchpad events into DOM scrolls.
        # This is checked only after establishing horizontal intent, so normal
        # vertical scrolling remains untouched and incurs no picking work.
        preview_owns_scroll = self.preview_column.has_css_class(
            _HORIZONTAL_SCROLL_OWNER_CLASS
        ) and _scroll_event_is_over_widget(controller, self.preview_column)
        if preview_owns_scroll or _scroll_event_targets_css_class(
            controller, _HORIZONTAL_SCROLL_OWNER_CLASS
        ):
            if self.preview_column.scroll_horizontal_preview(pan):
                return Gdk.EVENT_STOP
            return Gdk.EVENT_PROPAGATE
        adj = self.scroller.get_hadjustment()
        step = adj.get_step_increment()
        if step <= 1.0:
            step = 32.0
        target = adj.get_value() + pan * step
        adj.set_value(max(adj.get_lower(), min(adj.get_upper() - adj.get_page_size(), target)))
        return Gdk.EVENT_STOP

    def _on_hadjustment_changed(self, _adj: Gtk.Adjustment) -> None:
        if self._pending_scroll_intent is None:
            return
        self._arm_scroll_settle_timer()

    def _arm_scroll_settle_timer(self) -> None:
        # A burst of several rebuilds/relayouts landing close together (a
        # resize settling right as an add/trim fires, or several adds in a
        # row) can each want to (re-)arm this. Reacting to the first one
        # grabs an intermediate, not-yet-final upper/page-size -- confirmed
        # via repeated test runs: acting immediately intermittently applied
        # a stale end-of-scroll value computed from a layout state that a
        # *later* call (for the same pending action) then superseded, since
        # the pending flag was already cleared by the time that later,
        # truly-final call arrived. Debounce instead: every call restarts a
        # short timer, and only the actual firing of that timer (i.e. calls
        # went quiet for SCROLL_SETTLE_DEBOUNCE_MS) applies the action,
        # always reading the adjustment's live values at that point rather
        # than whatever they were when this was armed.
        if self._scroll_settle_debounce_id != 0:
            GLib.source_remove(self._scroll_settle_debounce_id)
        self._scroll_settle_debounce_id = GLib.timeout_add(
            SCROLL_SETTLE_DEBOUNCE_MS, self._apply_pending_scroll
        )

    def _apply_pending_scroll(self) -> bool:
        self._scroll_settle_debounce_id = 0
        if self._pending_scroll_intent is not None:
            kind, widget, position = self._pending_scroll_intent
            self._pending_scroll_intent = None
            adj = self.scroller.get_hadjustment()
            rtl = self.scroller.get_direction() == Gtk.TextDirection.RTL
            # The "scroll_*" kinds carry no widget -- they jump straight to
            # a known adjustment value, no measurement needed.
            if kind == "scroll_end":
                self._animate_scroll_to(
                    adj.get_lower() if rtl else adj.get_upper() - adj.get_page_size()
                )
                return GLib.SOURCE_REMOVE
            if kind == "scroll_start":
                self._animate_scroll_to(
                    adj.get_upper() - adj.get_page_size() if rtl else adj.get_lower()
                )
                return GLib.SOURCE_REMOVE
            if kind == "scroll_pos":
                self._animate_scroll_to(
                    max(0.0, min(adj.get_upper() - adj.get_page_size(), position))
                )
                return GLib.SOURCE_REMOVE
            # The "align_*" kinds carry a widget whose real position/width
            # is measured fresh right here (see _widget_canvas_x) rather
            # than using whatever was computed when this was scheduled (see
            # _arm_scroll_settle_timer's docstring on why that matters).
            left = self._widget_canvas_x(widget)
            if left is not None:
                # kind decides which edge lands where: "align_end" pulls
                # the view forward just far enough that widget's end edge
                # (right in LTR, left in RTL) meets that edge of the
                # viewport; "align_start" snaps the view so widget's start
                # edge (left) meets the viewport's start edge instead;
                # "align_pos" snaps the view so widget's start edge lands
                # `position` pixels in from the viewport's start.
                if kind == "align_end":
                    target = left if rtl else left + widget.get_width() - adj.get_page_size()
                elif kind == "align_pos":
                    target = (
                        left + widget.get_width() - adj.get_page_size() + position
                        if rtl
                        else left - position
                    )
                else:
                    target = left + widget.get_width() - adj.get_page_size() if rtl else left
                target = max(0.0, min(adj.get_upper() - adj.get_page_size(), target))
                self._animate_scroll_to(target)
        return GLib.SOURCE_REMOVE

    def _animate_scroll_to(self, target_value: float) -> None:
        """Ease the hadjustment to target_value instead of jumping straight
        there, matching the animated feel Gtk.ScrolledWindow gives
        adjustment changes it drives itself elsewhere (e.g. the view sliding
        when a column closes) -- this is the same kind of adjustment-value
        change, just triggered by us instead of GTK's own layout, so it
        should look the same rather than snapping. Safe to animate freely
        now that _MillerCanvas owns this adjustment as a Gtk.Scrollable: no
        internal Viewport is ever going to reclamp its value mid-flight out
        from under us (see _MillerCanvas's docstring).

        A later call while one is still playing (e.g. rapid clicks through
        several folders) skips the old animation straight to its own end
        value first -- two Adw.TimedAnimations independently driving the
        same adjustment would otherwise fight each other frame to frame."""
        if self._scroll_animation is not None:
            self._scroll_animation.skip()
        adj = self.scroller.get_hadjustment()
        current = adj.get_value()
        if current == target_value:
            return
        target = Adw.CallbackAnimationTarget.new(adj.set_value)
        animation = Adw.TimedAnimation.new(
            self.scroller, current, target_value, SCROLL_ANIMATION_DURATION_MS, target
        )
        animation.set_easing(Adw.Easing.EASE_OUT_CUBIC)
        self._scroll_animation = animation
        animation.play()

    def _fade_in(self, widget: Gtk.Widget, duration: int = SCROLL_ANIMATION_DURATION_MS) -> None:
        """Reveal a freshly appended column (or a rebuilt preview column,
        see _set_preview) with a quick fade rather than having it just snap
        into existence -- called right after _rebuild_chain() has parented
        it into the visible widget tree.

        Being parented isn't being mapped though: GTK only actually maps a
        newly added child on the next frame-clock cycle, not synchronously
        inside put()/set_content(). An Adw.TimedAnimation started against an
        unmapped widget has no frame clock to run against and just jumps
        straight to its end value with no visible transition at all
        (confirmed) -- so opacity is set to 0 right away (avoids an initial
        full-opacity flash before mapping), but the animation itself waits
        for the widget's "map" signal before it actually starts."""
        widget.set_opacity(0.0)
        if widget.get_mapped():
            self._start_fade_animation(widget, duration)
            return

        def _on_map(w: Gtk.Widget) -> None:
            w.disconnect(handler_id)
            self._start_fade_animation(w, duration)

        handler_id = widget.connect("map", _on_map)

    def _start_fade_animation(
        self, widget: Gtk.Widget, duration: int = SCROLL_ANIMATION_DURATION_MS
    ) -> None:
        target = Adw.CallbackAnimationTarget.new(widget.set_opacity)
        animation = Adw.TimedAnimation.new(widget, 0.0, 1.0, duration, target)
        animation.set_easing(Adw.Easing.EASE_OUT_CUBIC)
        # Kept alive on the widget itself so it isn't GC'd mid-flight (same
        # concern as _scroll_animation above, but this one is per-column
        # rather than singleton, so it can't live on self).
        widget._mc_fade_animation = animation
        animation.play()


def slot_is_showing_column(slot: Gtk.Widget | None) -> bool:
    """Whether `slot`'s own GtkStack currently shows Column View -- the
    single source of truth for "is this slot in Column View", read straight
    from the tree rather than tracked in any dict (issue #118)."""
    view = getattr(slot, "_mc_column_view", None) if slot is not None else None
    if view is None:
        return False
    stack = view.get_parent()
    return isinstance(stack, Gtk.Stack) and stack.get_visible_child() is view


def is_active_slot_showing_column(ext, win: Gtk.Window) -> bool:
    return slot_is_showing_column(ext._active_slot_widget(win))


def _host_for_window(ext, win: Gtk.Window):
    """The active slot's Miller host, or None -- keyboard-shortcut dispatch
    (rename/trash/copy/paste/new-folder) always targets whatever the user is
    actually looking at."""
    slot = ext._active_slot_widget(win)
    view = getattr(slot, "_mc_column_view", None) if slot is not None else None
    return getattr(view, "_mc_column_host", None) if view is not None else None


def rename_focused_folder(ext, win: Gtk.Window) -> bool:
    """Dispatch the window-level F2 shortcut to the active slot's Miller host."""
    host = _host_for_window(ext, win)
    return host.rename_focused_folder() if host is not None else False


def trash_focused_folder(ext, win: Gtk.Window) -> bool:
    """Dispatch the window-level Delete shortcut to the active slot's Miller host."""
    host = _host_for_window(ext, win)
    return host.trash_focused_folder() if host is not None else False


def delete_permanently_focused_folder(ext, win: Gtk.Window) -> bool:
    """Dispatch the window-level Shift+Delete shortcut to the active slot's
    Miller host."""
    host = _host_for_window(ext, win)
    return host.delete_permanently_focused_folder() if host is not None else False


def copy_focused_folder_to_clipboard(ext, win: Gtk.Window, *, cut: bool) -> bool:
    """Dispatch Ctrl+X/Ctrl+C to the active slot's Miller host."""
    host = _host_for_window(ext, win)
    return host.copy_focused_folder_to_clipboard(cut=cut) if host is not None else False


def copy_preview_selection(ext, win: Gtk.Window) -> bool:
    """Copy text selected in the preview, if any. False means there was none
    and Ctrl+C should fall through to copying the selected file instead.

    The PDF and OCR image previews draw bitmaps with their own selection layer
    on top, so unlike a text widget they cannot be recognised by focus alone
    -- the active preview has to be asked."""
    host = _host_for_window(ext, win)
    preview = getattr(host, "preview_column", None) if host is not None else None
    copier = getattr(preview, "copy_text_selection", None)
    return bool(copier()) if callable(copier) else False


def paste_into_focused_folder(ext, win: Gtk.Window) -> bool:
    """Dispatch Ctrl+V to the active slot's Miller host."""
    host = _host_for_window(ext, win)
    return host.paste_into_focused_folder() if host is not None else False


def create_folder_in_focused_column(ext, win: Gtk.Window) -> bool:
    """Dispatch Shift+Ctrl+N to the active slot's Miller host."""
    host = _host_for_window(ext, win)
    return host.create_folder_in_focused_column() if host is not None else False


def open_focused_selection(ext, win: Gtk.Window) -> bool:
    host = _host_for_window(ext, win)
    return host.open_focused_selection() if host is not None else False


def open_focused_selection_in_tab(ext, win: Gtk.Window) -> bool:
    host = _host_for_window(ext, win)
    return host.open_focused_selection("tab") if host is not None else False


def open_focused_selection_in_window(ext, win: Gtk.Window) -> bool:
    host = _host_for_window(ext, win)
    return host.open_focused_selection("window") if host is not None else False


def show_focused_properties(ext, win: Gtk.Window) -> bool:
    host = _host_for_window(ext, win)
    return host.show_focused_properties() if host is not None else False


def reload_focused_view(ext, win: Gtk.Window) -> bool:
    host = _host_for_window(ext, win)
    return host.reload_focused_view() if host is not None else False


def invert_focused_selection(ext, win: Gtk.Window) -> bool:
    host = _host_for_window(ext, win)
    return host.invert_focused_selection() if host is not None else False


def select_matching_items(ext, win: Gtk.Window) -> bool:
    host = _host_for_window(ext, win)
    return host.select_matching_items() if host is not None else False


def paste_links_in_focused_folder(ext, win: Gtk.Window) -> bool:
    host = _host_for_window(ext, win)
    return host.paste_links_in_focused_folder() if host is not None else False


def create_links_for_focused_selection(ext, win: Gtk.Window) -> bool:
    host = _host_for_window(ext, win)
    return host.create_links_for_focused_selection() if host is not None else False


def undo_file_operation(ext, win: Gtk.Window) -> bool:
    host = _host_for_window(ext, win)
    return host.undo_file_operation() if host is not None else False


def redo_file_operation(ext, win: Gtk.Window) -> bool:
    host = _host_for_window(ext, win)
    return host.redo_file_operation() if host is not None else False


def show_focused_context_menu(ext, win: Gtk.Window) -> bool:
    host = _host_for_window(ext, win)
    return host.show_focused_context_menu() if host is not None else False


def zoom_in(ext, win: Gtk.Window) -> bool:
    host = _host_for_window(ext, win)
    return host.adjust_zoom(1) if host is not None else False


def zoom_out(ext, win: Gtk.Window) -> bool:
    host = _host_for_window(ext, win)
    return host.adjust_zoom(-1) if host is not None else False


def reset_zoom(ext, win: Gtk.Window) -> bool:
    host = _host_for_window(ext, win)
    return host.adjust_zoom(0) if host is not None else False


def build_column_view(ext, win: Gtk.Window, slot: Gtk.Widget) -> Gtk.Widget:
    # Runs once per slot at injection time, generally before navigation has
    # settled -- this initial root is a throwaway placeholder, never seen by
    # the user in practice: enter_column_view() always reseeds from the
    # slot's real current location on the next Ctrl+3 press.
    loc = slot.get_property("location")
    root_uri = loc.get_uri() if loc is not None else default_root_uri()
    host = _ColumnViewHost(ext, win, win.get_display(), root_uri)

    # Keep the early-access status attached to the view itself rather than
    # Nautilus's toolbar: the toolbar remains native, while the badge stays
    # visible whenever this experimental view is open.
    view = Gtk.Overlay()
    view.set_child(host.scroller)

    beta_badge = Gtk.Label(label=_("Beta"))
    beta_badge.add_css_class("mc-beta-badge")
    beta_badge.set_halign(Gtk.Align.END)
    beta_badge.set_valign(Gtk.Align.START)
    beta_badge.set_margin_top(12)
    beta_badge.set_margin_end(12)
    # It is a status indicator, not a control: pointer input continues to
    # reach the Column View content underneath it.
    beta_badge.set_can_target(False)
    beta_badge.set_tooltip_text(_("Early access feature. It may contain bugs or inconsistencies."))
    view.add_overlay(beta_badge)

    # Public helpers retrieve the host from this widget, stored on the owning
    # slot as slot._mc_column_view (see _do_inject_into_slot below).
    view._mc_column_host = host
    return view


def enter_column_view(ext, win: Gtk.Window, root_uri: str) -> None:
    """Show Column View for the active slot, reconciled to root_uri --
    called from the Ctrl+3 shortcut / column-segment click (see main.py's
    _show_column_view / _on_view_segment_activated). Drill-downs commit
    slot.open-location (see _sync_slot_location), so root_uri is normally the
    deepest column already open here: reusing host.sync_to_uri() (rather than
    host.reset()) means round-tripping through Ctrl+1/Ctrl+2 and back keeps
    the whole chain instead of collapsing it to a single column at the
    deepest folder. An ancestor location truncates to it; anything else
    re-roots fresh, same as a brand new Column View entry."""
    slot = ext._active_slot_widget(win)
    if slot is None:
        _log("enter_column_view: no active slot")
        return
    view = getattr(slot, "_mc_column_view", None)
    if view is None:
        _log("enter_column_view: active slot not yet injected")
        return
    host = view._mc_column_host
    _log(f"enter_column_view: root_uri={root_uri!r}")
    # Release the Computer panel's own state first if it currently owns this
    # slot's stack -- otherwise its notify::visible-child reassert handler
    # still trusts its own (now stale) elected flag and fights the
    # set_visible_child below (issue #137's per-slot view-election arbiter).
    ext._leave_computer_panel_for_slot(win, slot)
    host.resume()
    host.sync_to_uri(root_uri)
    stack = view.get_parent()
    current_child = stack.get_visible_child()
    if current_child is not view:
        slot._mc_column_previous_child = current_child
    slot._mc_column_elected = True
    common.set_slot_view_owner(slot, "column")
    stack.set_visible_child(view)
    host.set_native_cut_observer_active(True)
    populate_column_view(ext, win)


def leave_column_view(slot: Gtk.Widget) -> None:
    """Reveal whichever child was showing on `slot`'s own stack before
    Column View. Called from Ctrl+1/Ctrl+2 (main.py's
    _leave_column_view_for_native_mode) and from the per-slot location
    watcher when navigation lands somewhere Column View doesn't support
    (see _on_slot_location_changed)."""
    view = getattr(slot, "_mc_column_view", None)
    if view is None:
        return
    stack = view.get_parent()
    slot._mc_column_elected = False
    common.release_slot_view_owner(slot, "column")
    host = getattr(view, "_mc_column_host", None)
    if host is not None:
        host.suspend()
    previous = getattr(slot, "_mc_column_previous_child", None)
    if previous is None or previous.get_parent() is not stack:
        # Nautilus always adds its own vbox first (nautilus-window-slot.c),
        # so it's the stack's first child; fall back to it if we never
        # captured what was showing before Column View.
        previous = stack.get_first_child()
    if previous is not None and stack.get_visible_child() is not previous:
        stack.set_visible_child(previous)


def _refresh_slot_sort(ext, slot: Gtk.Widget) -> None:
    view = getattr(slot, "_mc_column_view", None)
    host = getattr(view, "_mc_column_host", None) if view is not None else None
    if host is None:
        return
    # Re-resolve from the slot's real current location rather than trusting
    # host._root_uri: drill-downs commit slot.open-location (see
    # _sync_slot_location), so while Column View is showing, the slot is
    # normally at the deepest open column, not host._root_uri.
    loc = slot.get_property("location")
    root_uri = loc.get_uri() if loc is not None else host._root_uri
    host._sort = ext._nautilus_prefs.resolve_column_sort(root_uri)
    for column in host.columns:
        if host._suspended:
            column._sort = host._sort
        else:
            column.set_sort(host._sort)


def refresh_column_view(ext, win: Gtk.Window) -> None:
    """Re-enumerate the active slot's open columns in place, e.g. right
    before Column View becomes visible again for that slot."""
    slot = ext._active_slot_widget(win)
    if slot is not None:
        _refresh_slot_sort(ext, slot)


def refresh_all_column_views(ext, win: Gtk.Window) -> None:
    """Re-enumerate every open column in every tab of `win`, e.g. after a
    Nautilus setting (show-hidden-files, sort order) changes. Each tab's
    Column View is now an independent per-slot instance (issue #118), so a
    global setting change must reach all of them, not just whichever tab
    happens to be active."""
    for slot in _iter_injected_slots(win):
        _refresh_slot_sort(ext, slot)


def populate_column_view(ext, win: Gtk.Window) -> None:
    """Called every time Column View becomes visible for a slot. The chain
    is already built (build_column_view, at slot injection time) -- this
    just re-syncs the open columns to the live prefs (hidden-files, etc.),
    since a setting may have changed while Column View was off-screen for
    this tab."""
    refresh_column_view(ext, win)

    slot = ext._active_slot_widget(win)
    view = getattr(slot, "_mc_column_view", None) if slot is not None else None
    if view is None:
        return

    if not _COLUMN_KEYBOARD_NAV:
        # Focus the view itself so its local capture controller owns regular
        # Column View keys. This does not intercept keyboard input elsewhere
        # in the window once the user focuses a toolbar or location widget.
        view.grab_focus()
        return

    # Arm arrow-key nav without requiring a preliminary click: focus the
    # column at host.focused_index (0 on a fresh reset()) as soon as the view
    # is actually visible/mapped.
    host = getattr(view, "_mc_column_host", None)
    if host is not None:
        col = host._focused_column()
        if col is not None:
            col.grab_list_focus()


def _iter_injected_slots(win: Gtk.Window):
    tab_view = common._find_tab_view(win)
    if tab_view is None:
        return
    for i in range(tab_view.get_n_pages()):
        page = tab_view.get_nth_page(i)
        slot = page.get_child() if page is not None else None
        if slot is not None and getattr(slot, "_mc_column_view", None) is not None:
            yield slot


def watch_tab_view(ext, win: Gtk.Window) -> None:
    """Inject Column View into every current and future tab of `win`.

    Nautilus creates one NautilusWindowSlot per tab, each owning its own
    GtkStack that already holds two sibling children of its own (vbox and
    global_search_page, nautilus-window-slot.c:869-892). We add Column View
    as a third sibling of that same stack instead of the single window-wide
    overlay used before, so tab switching needs no resync: each tab's Column
    View state (chain, scroll, selection) lives on its own slot and is
    untouched by switching away from it. See issue #118."""
    common.watch_slots(win, lambda w, slot, ext=ext: _schedule_slot_init(ext, w, slot))
    tab_view = common._find_tab_view(win)
    if tab_view is not None:
        tab_view.connect("page-detached", _on_column_page_detached)


def _on_column_page_detached(_tab_view, page: Adw.TabPage, _position: int) -> None:
    """Release a closed or moved tab; clipboard signals otherwise retain its host."""
    slot = page.get_child()
    view = getattr(slot, "_mc_column_view", None) if slot is not None else None
    host = getattr(view, "_mc_column_host", None) if view is not None else None
    if host is not None:
        host.destroy()
    if view is not None:
        view._mc_column_host = None
    if slot is not None:
        slot._mc_column_view = None


def _schedule_slot_init(ext, win: Gtk.Window, slot: Gtk.Widget | None) -> None:
    if slot is None:
        return
    common.schedule_slot_init(
        slot,
        "_mc_column_view",
        functools.partial(_do_inject_into_slot, ext, win),
        retry_ms=_SLOT_INIT_RETRY_MS,
        max_attempts=_SLOT_INIT_MAX_ATTEMPTS,
    )


def _do_inject_into_slot(ext, win: Gtk.Window, slot: Gtk.Widget) -> bool:
    if getattr(slot, "_mc_column_view", None) is not None:
        return GLib.SOURCE_REMOVE
    stack = common._find_slot_stack(slot)
    if stack is None:
        _log("_do_inject_into_slot: no GtkStack found on slot")
        return GLib.SOURCE_REMOVE
    view = build_column_view(ext, win, slot)
    stack.add_named(view, _SLOT_STACK_CHILD_NAME)
    slot._mc_column_view = view
    slot._mc_column_elected = False
    slot._mc_column_native_stopped = False
    slot._mc_column_previous_child = None
    slot._mc_reasserting = False
    stack.connect("notify::visible-child", _on_slot_stack_child_changed, slot)
    slot.connect("notify::location", _on_slot_location_changed, ext, win)
    _maybe_auto_elect_column_view(ext, win, slot)
    if not slot_is_showing_column(slot):
        view._mc_column_host.suspend()
    return GLib.SOURCE_REMOVE


def _on_slot_stack_child_changed(stack, _pspec, slot: Gtk.Widget) -> None:
    """Nautilus reasserts its own stack child on its own initiative (e.g.
    leaving global search, nautilus-window-slot.c:1045/1091). Reassert
    Column View if the user elected it for this slot and it hasn't been
    explicitly left (see enter_column_view/leave_column_view).

    Also requires slot_view_owner(slot) == "column": the Computer panel
    (my_computer_view.py) shares this same stack and has its own reassert
    handler with its own local elected flag, which can be stale relative to
    this one (e.g. mid-navigation, before its own notify::location handler
    has run). Without the shared owner token both handlers could reassert
    against each other with no termination condition (issue #137)."""
    if getattr(slot, "_mc_reasserting", False):
        return
    if not getattr(slot, "_mc_column_elected", False) or common.slot_view_owner(slot) != "column":
        return
    view = slot._mc_column_view
    if stack.get_visible_child() is view:
        return
    slot._mc_reasserting = True
    stack.set_visible_child(view)
    slot._mc_reasserting = False


def _maybe_auto_elect_column_view(ext, win: Gtk.Window, slot: Gtk.Widget) -> None:
    """Open `slot` in Column View when it is the persisted default-view
    (issue #102). Cannot fight the user: every explicit grid/list pick
    writes 'native' to the key (see main.py
    _leave_column_view_for_native_mode), and a slot already showing Column
    View short-circuits here, so this only ever acts once per slot."""
    if slot_is_showing_column(slot):
        return
    if ext._auto_elect_view_for_slot(win) != VIEW_COLUMN:
        return
    loc = slot.get_property("location")
    if loc is None or not ext._column_view_available_at(loc):
        return
    if ext._active_slot_widget(win) is slot:
        enter_column_view(ext, win, loc.get_uri())
        refresh_column_view_chrome(ext, win)
    else:
        # Background tab: elect the stack child directly, without
        # populate_column_view()/enter_column_view() -- both resolve the
        # *active* slot internally and would act on (and focus) the wrong tab.
        ext._leave_computer_panel_for_slot(win, slot)
        view = slot._mc_column_view
        stack = view.get_parent()
        if stack.get_visible_child() is not view:
            slot._mc_column_previous_child = stack.get_visible_child()
        slot._mc_column_elected = True
        common.set_slot_view_owner(slot, "column")
        stack.set_visible_child(view)
        view._mc_column_host.set_native_cut_observer_active(True)
    if ext._stop_hidden_native_slot(win, slot):
        slot._mc_column_native_stopped = True


def _on_slot_location_changed(slot, _pspec, ext, win: Gtk.Window) -> None:
    """Keep this slot's own Column View in sync with real Nautilus
    navigation on it (address bar, pathbar, back/forward, a bookmark, our
    own drill-down echo) -- scoped to exactly the slot that navigated,
    unlike the window-wide resync this replaces (issue #118).

    Also the main trigger for the persisted default-view (issue #102): a
    slot's very first location at injection time is very often
    computer:/// (Miller-unavailable, since start-on-disks defaults to
    true), so _maybe_auto_elect_column_view's call from
    _do_inject_into_slot alone would never fire for most users. This is
    where it gets its real chance, on the first navigation that lands
    somewhere Column View actually supports."""
    if not slot_is_showing_column(slot):
        _maybe_auto_elect_column_view(ext, win, slot)
        return
    loc = slot.get_property("location")
    if loc is None:
        return
    if not ext._column_view_available_at(loc):
        _log(f"_on_slot_location_changed: {loc.get_uri()!r} unavailable, leaving column view")
        leave_column_view(slot)
        slot._mc_column_native_stopped = False
        return
    host = slot._mc_column_view._mc_column_host
    host.sync_to_uri(loc.get_uri())
    if ext._stop_hidden_native_slot(win, slot):
        slot._mc_column_native_stopped = True


_SEGMENTS = (
    # Grid/List always resolve through _native() (see _build_view_switcher), so
    # their labels need no N_() marker -- only Column View reads our own catalog.
    ("grid", _ICON_TARGET_GRID, "Grid View"),
    ("list", _ICON_TARGET_LIST, "List View"),
    ("column", _COLUMN_ICON_NAME, N_("Column View")),
)


def inject_column_view_entry(ext, win: Gtk.Window) -> None:
    """Replace the visible content of NautilusViewControls (an Adw.Bin, see
    nautilus-view-controls.blp) with a segmented Grid/List/Column switcher.

    Nautilus creates one NautilusViewControls per window and never rewrites
    it afterwards. The native Adw.SplitButton is kept alive (hidden, not
    removed/rebound) inside our own Box: its icon-name binding to the window
    slot still tells us which native side (Grid/List) is showing, and it
    stays the target we activate for native Grid<->List transitions. If the
    expected widget contract is not present, fail closed and leave Nautilus
    alone.
    """
    state = ext._windows.get(win)
    if state is None:
        return

    menu_btn = ext._nautilus_prefs.find_sort_button(win)
    if menu_btn is None:
        _log("inject_column_view_entry: view-options button not found")
        return

    split_button = _ancestor_split_button(menu_btn)
    if split_button is None:
        split_button = next(
            (
                widget
                for widget in _all_widgets(win)
                if isinstance(widget, Adw.SplitButton)
                and widget.get_action_name() == _NATIVE_TOGGLE_ACTION
            ),
            None,
        )
    if split_button is None or getattr(split_button, "_mc_column_attached", False):
        return

    view_controls = split_button.get_parent()
    if not isinstance(view_controls, Adw.Bin):
        _log("inject_column_view_entry: unexpected parent, leaving Nautilus control untouched")
        return

    popover = split_button.get_popover()
    split_button.set_popover(None)
    split_button.set_visible(False)
    split_button._mc_column_attached = True

    options_btn = Gtk.MenuButton(
        icon_name="view-more-symbolic", tooltip_text=_native("View Options")
    )
    if popover is not None:
        options_btn.set_popover(popover)

    switcher = _build_view_switcher(ext, win)

    box = Gtk.Box(spacing=6)
    box.append(switcher)
    box.append(options_btn)
    # Unparent from the Adw.Bin first: appending a still-parented child is a
    # no-op that trips a GTK assertion and leaves the native button orphaned.
    view_controls.set_child(None)
    box.append(split_button)
    view_controls.set_child(box)

    state["native_split_button"] = split_button
    state["view_switcher"] = switcher
    state["view_options_menu_button"] = options_btn

    # The hidden split button's icon-name is still bound to the window slot,
    # so this is how a native Grid<->List change (e.g. Ctrl+1/2) is detected.
    split_button.connect("notify::icon-name", lambda *_a: _sync_view_switcher(ext, win))
    _sync_view_switcher(ext, win)
    _log("inject_column_view_entry: hid native split button behind three-way switcher")


def _ancestor_split_button(widget: Gtk.Widget) -> Adw.SplitButton | None:
    current = widget
    while current is not None:
        if isinstance(current, Adw.SplitButton):
            return current
        current = current.get_parent()
    return None


def _resolve_column_icon() -> str | Gio.Icon:
    """The active theme's own view-column-symbolic when it has one, else our
    bundled copy of the same name (see common._bundled_gicon). Returns an
    icon name or a Gio.Icon -- MyComputerToggleButton accepts either."""
    if _icon_name_renders(_COLUMN_ICON_NAME):
        return _COLUMN_ICON_NAME
    return _bundled_gicon(_COLUMN_ICON_NAME) or _COLUMN_ICON_NAME


def _refresh_column_icon_all_windows(ext) -> bool:
    """Icon theme changed live (GNOME Settings). The Grid/List/Column
    switcher's column segment is resolved once at construction time
    (_build_view_switcher), so without this watcher it would keep showing
    whatever it resolved to at last build -- the old theme's own icon, or
    our bundled fallback -- even after the user switches packs. The
    switcher itself is always present in the toolbar regardless of which
    of Grid/List/Column is currently active, so every window is updated
    here, not just ones currently showing Column View."""
    icon = _resolve_column_icon()
    # list() copy: this runs from an idle callback, and _on_window_destroyed
    # pops from _windows in the same main loop.
    for _win, state in list(ext._windows.items()):
        switcher = state.get("view_switcher")
        if switcher is not None:
            switcher.set_segment_icon("column", icon)
    return GLib.SOURCE_REMOVE


def init_icon_watcher(ext) -> None:
    """Called once from MyComputerExtension.__init__. See
    _refresh_column_icon_all_windows for why this is needed."""
    icon_theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
    icon_theme.connect(
        "changed", lambda *_a, ext=ext: GLib.idle_add(_refresh_column_icon_all_windows, ext)
    )


def _build_view_switcher(ext, win: Gtk.Window) -> Gtk.Widget:
    """Build the Grid/List/Column segmented control (see
    widgets.MyComputerToggleButton)."""
    segments = [
        (name, icon, tooltip) if name != "column" else (name, _resolve_column_icon(), tooltip)
        for name, icon, tooltip in _SEGMENTS
    ]
    switcher = MyComputerToggleButton(
        (name, icon, _(tooltip) if name == "column" else _native(tooltip))
        for name, icon, tooltip in segments
    )
    switcher.connect("changed", lambda _w, name: _on_view_segment_activated(ext, win, name))
    return switcher


def _set_active_segment(switcher: MyComputerToggleButton, name: str) -> None:
    switcher.set_active_name(name)


def _set_segment_enabled(switcher: MyComputerToggleButton, name: str, enabled: bool) -> None:
    switcher.set_segment_enabled(name, enabled)


def _on_view_segment_activated(ext, win: Gtk.Window, name: str | None) -> None:
    """Direct Grid/List/Column selection -- one click, one target view.
    MyComputerToggleButton never emits "changed" for a programmatic
    set_active_name (see its own _syncing guard), so no re-entrance guard
    is needed here for calls coming from _sync_view_switcher below."""
    state = ext._windows.get(win)
    if name is None or state is None:
        return

    split_button = state.get("native_split_button")
    native_on_list = split_button is not None and split_button.get_icon_name() == _ICON_TARGET_GRID

    if name == "column":
        ext._show_column_view(win)
    elif name == "grid":
        if is_active_slot_showing_column(ext, win):
            ext._leave_column_view_for_native_mode(win)
        if native_on_list:
            win.activate_action(_NATIVE_TOGGLE_ACTION, None)
    elif name == "list":
        if is_active_slot_showing_column(ext, win):
            ext._leave_column_view_for_native_mode(win)
        if not native_on_list:
            win.activate_action(_NATIVE_TOGGLE_ACTION, None)
    _sync_view_switcher(ext, win)


def _sync_view_switcher(ext, win: Gtk.Window) -> None:
    """Reflect the active slot's Column View state / the native split
    button's frozen icon back onto the segmented control.
    MyComputerToggleButton.set_active_name() does not emit "changed", so
    this never re-triggers _on_view_segment_activated."""
    state = ext._windows.get(win)
    switcher = state.get("view_switcher") if state is not None else None
    if switcher is None:
        return

    column_available = ext._column_view_available_for_window(win)
    _set_segment_enabled(switcher, "column", column_available)

    if column_available and is_active_slot_showing_column(ext, win):
        active = "column"
    else:
        split_button = state.get("native_split_button")
        native_icon = split_button.get_icon_name() if split_button is not None else None
        active = "list" if native_icon == _ICON_TARGET_GRID else "grid"

    _set_active_segment(switcher, active)


def refresh_column_view_chrome(ext, win: Gtk.Window) -> None:
    """Refresh the persistent view switcher after slot navigation."""
    _sync_view_switcher(ext, win)


def detach_column_view_entry(ext, win: Gtk.Window, state: dict | None = None) -> None:
    """Release per-slot Miller resources before the Nautilus window disappears."""
    for slot in list(_iter_injected_slots(win)):
        view = getattr(slot, "_mc_column_view", None)
        host = getattr(view, "_mc_column_host", None) if view is not None else None
        if host is not None:
            host.destroy()
        if view is not None:
            view._mc_column_host = None
        slot._mc_column_view = None
