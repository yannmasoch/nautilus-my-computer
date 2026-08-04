"""Column View: Miller (macOS Finder-style) columns injected into Nautilus."""

import functools
import os

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
    _icon_name_renders,
    _log,
    _native,
)
from nautilus_my_computer.context_menu import (
    ContextMenu,
    ContextMenuItem,
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

VIEW_COLUMN = "column"

# Name Column View is added under on each slot's own GtkStack (see
# watch_tab_view/_do_inject_into_slot below). Nautilus's own two stack
# children (vbox, global_search_page) are added via gtk_stack_add_child
# with no name, so this name can never collide with anything of theirs.
_SLOT_STACK_CHILD_NAME = "mc-column"
_SLOT_INIT_RETRY_MS = 20  # retry interval while waiting for a new slot to settle
_SLOT_INIT_MAX_ATTEMPTS = 100  # ~2s budget, mirrors main.py's _WIN_INIT_MAX_ATTEMPTS

# Whether a navigation event moves into a subfolder of where browsing
# currently is (NAV_DOWN), back toward a parent (NAV_UP), or re-selects
# within the same column that was already focused (NAV_SELF). Detected from
# the shape of the change rather than comparing URIs directly: NAV_DOWN is
# the row activated living in the currently deepest open column (so the new
# folder is a child of the current one), or, for sync_to_uri, the new chain
# being a strict extension of the existing one. NAV_SELF is a click landing
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
# (theme-dependent, confirmed narrower than expected in prior measurement).
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
# How many frame ticks to keep re-asserting keyboard focus onto a freshly
# drilled-into column after a commit (see _arm_focus_retry) -- long enough to
# outlast Nautilus's own async re-focus of its real, hidden GtkGridView for
# the newly navigated slot.
_FOCUS_RETRY_FRAMES = 30


def default_root_uri() -> str:
    """Fallback root for the very first (pre-navigation) widget build, before
    any real location is known -- never seen by the user in practice, since
    entering Column View (Ctrl+3) always re-seeds from the real current
    location via enter_column_view()."""
    return Gio.File.new_for_path(GLib.get_home_dir()).get_uri()


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
        self._clipboard.connect("changed", self._on_clipboard_changed)
        self._operation_monitors: list[Gio.FileMonitor] = []
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
        scroller.add_tick_callback(self._poll_viewport_size)

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
        self._detach_root()

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
            sort=self._sort,
        )
        right_click = Gtk.GestureClick(button=3)
        right_click.connect("pressed", self._on_column_background_right_clicked, column)
        column.add_controller(right_click)
        return column

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

        # Rows are recreated for every enumeration, so install their
        # context-menu controllers only after this batch has populated.
        # The menu itself is still built on demand below, keeping bookmark
        # and Preferred Folder state current at the instant it opens.
        for row in _column.rows():
            click = Gtk.GestureClick(button=0)
            click.connect("pressed", self._on_row_pressed, _column, row)
            row.add_controller(click)
        self._set_cut_rows()

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
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
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
                else None
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
                    action=lambda template_uri=template_uri: self._paste_uris_into_folder(
                        [template_uri], destination_uri, cut=False
                    ),
                )
            )
        return items

    def _terminal_action(self, uri: str):
        """Return a launcher for an installed GNOME terminal, if one is available."""
        if not uri.startswith("file://"):
            return None
        terminal_ids = {
            "org.gnome.Console.desktop",
            "org.gnome.Terminal.desktop",
            "org.gnome.Ptyxis.desktop",
        }
        terminal = next(
            (app for app in Gio.AppInfo.get_all() if app.get_id() in terminal_ids),
            None,
        )
        if terminal is None:
            return None

        def open_terminal() -> None:
            try:
                terminal.launch_uris([uri], None)
            except GLib.Error as error:
                _log(f"Could not open terminal for {uri!r}: {error.message}")

        return open_terminal

    def _create_folder(self, column: Gtk.Widget) -> None:
        """Create a new folder in one column, then refresh its listing."""
        parent = Gio.File.new_for_uri(column.folder_uri)
        base_name = _native("New Folder")

        def create_named(name: str, suffix: int) -> None:
            candidate = parent.get_child(name)

            def on_folder_created(source, result, _data) -> None:
                try:
                    source.make_directory_finish(result)
                except GLib.Error as error:
                    if error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.EXISTS):
                        create_named(f"{base_name} {suffix}", suffix + 1)
                        return
                    _log(f"Could not create folder in {column.folder_uri!r}: {error.message}")
                    return
                if column in self.columns:
                    column.reload()

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

        # Every other button, primary above all, belongs to Gtk.ListBox's own
        # gesture (row-activated drives Miller navigation). Deny rather than
        # just return: a GtkGestureSingle with button=0 tracks the first
        # button pressed and ignores the rest until that sequence ends, and
        # primary activation rebuilds the paned chain (_rebuild_chain), which
        # can swallow the matching release. An undenied sequence would then
        # linger and make the gesture drop the next secondary/middle press --
        # the menu-needs-two-clicks symptom. DENIED resets it at once and
        # leaves the event free to propagate. Native has no equivalent branch
        # because its cell gesture handles primary itself
        # (nautilus-list-base.c on_item_click_released).
        gesture.set_state(Gtk.EventSequenceState.DENIED)

    def _on_row_right_clicked(
        self,
        gesture: Gtk.GestureClick,
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
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        # Keep the context-menu target in GTK's native dark-grey :active
        # state; it must not become the Miller column's blue :selected row.
        components.set_row_active(row, True)
        uri = row.uri
        content_type = row.content_type or "application/octet-stream"
        default_app = Gio.AppInfo.get_default_for_type(content_type, False)
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
                open_tab_action=lambda: self._ext._do_open_tab(uri, self._win, make_active=False),
                open_window_action=lambda: self._ext._do_open_window(uri),
                open_with_action=(
                    (lambda: self._ext._do_open_with(uri, self._win, content_type=content_type))
                    if uri.startswith("file://")
                    else None
                ),
            )
            if row.is_dir
            else open_section(
                lambda: self._open_file(uri),
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
                if self._clipboard_has_pasteable_files()
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
                move_to_trash_action=(
                    (lambda: self._move_to_trash(column, uri))
                    if uri.startswith("file://")
                    else None
                ),
                show_compress=True,
                show_email=True,
            ),
        ]
        if uri.startswith("file://"):
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

    def _on_item_renamed(self, source_column: Gtk.Widget, old_uri: str, new_uri: str) -> None:
        """Apply a completed shared rename operation to the Miller chain."""
        old_prefix = old_uri.rstrip("/")
        new_prefix = new_uri.rstrip("/")
        for open_column in self.columns:
            if open_column.folder_uri.rstrip("/") == old_prefix:
                open_column.folder_uri = new_uri
            elif open_column.folder_uri.startswith(f"{old_prefix}/"):
                open_column.folder_uri = f"{new_prefix}{open_column.folder_uri[len(old_prefix) :]}"

        # Re-enumerate the parent column so it displays the new row name.
        # Descendant columns keep their contents but their location URIs
        # above are updated before Nautilus is synchronized to the renamed
        # deepest folder.
        if source_column in self.columns:
            source_column.reload()
        if self.preview_column.file_uri == old_uri:
            self._set_preview(new_uri)
            self._rebuild_chain()
            self._fade_in(self.preview_column, duration=PREVIEW_FADE_DURATION_MS)
        self._sync_slot_location(self.columns[-1].folder_uri)

    def _show_rename_popover(self, column: Gtk.Widget, row: Gtk.Widget) -> None:
        components.show_rename_popover(
            row,
            row.uri,
            lambda old_uri, new_uri: self._on_item_renamed(column, old_uri, new_uri),
            item_kind="folder" if row.is_dir else "file",
        )

    def _move_to_trash(self, source_column: Gtk.Widget, uri: str) -> None:
        """Run Nautilus's own trash operation, including its undo manager."""
        parent = Gio.File.new_for_uri(uri).get_parent()
        if parent is not None:
            self._watch_operation_directories([parent.get_uri()])
        self._call_nautilus_file_operation("TrashURIs", GLib.Variant("(asa{sv})", ([uri], {})), uri)

    def _call_nautilus_file_operation(
        self, method: str, parameters: GLib.Variant, uri: str, *, on_started=None
    ) -> None:
        """Start a native Nautilus file operation through its session D-Bus API."""
        try:
            operations = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SESSION,
                Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES,
                None,
                "org.gnome.Nautilus",
                "/org/gnome/Nautilus/FileOperations2",
                "org.gnome.Nautilus.FileOperations2",
                None,
            )
        except GLib.Error as error:
            _log(f"Could not start Nautilus trash operation for {uri!r}: {error.message}")
            return

        def on_operation_started(proxy, result, _data) -> None:
            try:
                proxy.call_finish(result)
            except GLib.Error as error:
                _log(f"Nautilus {method} failed for {uri!r}: {error.message}")
                return
            if callable(on_started):
                on_started()

        operations.call(
            method,
            parameters,
            Gio.DBusCallFlags.NONE,
            -1,
            None,
            on_operation_started,
            None,
        )

    def _copy_to_clipboard(self, uri: str, *, cut: bool) -> None:
        """Publish one Miller item as standard and Nautilus clipboard data."""
        file_list = Gdk.FileList.new_from_list([Gio.File.new_for_uri(uri)])
        value = GObject.Value()
        value.init(Gdk.FileList)
        value.set_boxed(file_list)
        file_provider = Gdk.ContentProvider.new_for_value(value)
        nautilus_data = f"{'cut' if cut else 'copy'}\n{uri}".encode()
        nautilus_provider = Gdk.ContentProvider.new_for_bytes(
            "x-special/gnome-copied-files", GLib.Bytes.new(nautilus_data)
        )
        provider = Gdk.ContentProvider.new_union([file_provider, nautilus_provider])
        self._clipboard.set_content(provider)
        self._set_miller_clipboard_state([uri], cut=cut)

    def _open_file(self, uri: str) -> None:
        """Launch a file with its default application."""
        try:
            Gio.AppInfo.launch_default_for_uri(uri, None)
        except GLib.Error as error:
            _log(f"Could not open {uri!r}: {error.message}")

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
        return formats.contain_gtype(Gdk.FileList.__gtype__)

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
                components.set_row_active(row, False)

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

        self._clipboard.read_value_async(
            Gdk.FileList.__gtype__,
            GLib.PRIORITY_DEFAULT,
            None,
            self._on_external_file_list_read,
            destination_uri,
        )

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
            on_started=self._clear_clipboard_after_paste,
        )

    def _clear_clipboard_after_paste(self) -> None:
        """Drop copied-file ownership once Nautilus has accepted a paste."""
        self._clipboard.set_content(None)

    def _watch_operation_directories(self, directory_uris: list[str]) -> None:
        """Reload open source/destination columns after a native operation changes them."""
        monitors = []
        watched = {uri for uri in directory_uris}
        watched_files = [Gio.File.new_for_uri(uri) for uri in watched]
        refresh_id = 0

        def finish_refresh() -> bool:
            nonlocal refresh_id
            refresh_id = 0
            for column in self.columns:
                column_file = Gio.File.new_for_uri(column.folder_uri)
                if any(column_file.equal(watched_file) for watched_file in watched_files):
                    column.reload()
            for monitor in monitors:
                monitor.cancel()
                if monitor in self._operation_monitors:
                    self._operation_monitors.remove(monitor)
            return GLib.SOURCE_REMOVE

        def on_changed(*_args) -> None:
            nonlocal refresh_id
            if refresh_id:
                GLib.source_remove(refresh_id)
            refresh_id = GLib.timeout_add(150, finish_refresh)

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

    def _show_destination_picker(self, uri: str, *, move: bool) -> None:
        """Choose a destination folder in Nautilus's modal native file dialog."""
        dialog = Gtk.FileDialog()
        dialog.set_title(
            _native("Select Move Destination") if move else _native("Select Copy Destination")
        )
        dialog.set_accept_label(_("Select"))
        dialog.set_initial_folder(Gio.File.new_for_uri(uri).get_parent())

        def on_destination_selected(source, result, _data) -> None:
            try:
                destination = source.select_folder_finish(result)
            except GLib.Error as error:
                if not error.matches(Gtk.DialogError, Gtk.DialogError.DISMISSED):
                    _log(f"Could not select destination for {uri!r}: {error.message}")
                return
            method = "MoveURIs" if move else "CopyURIs"
            parameters = GLib.Variant("(assa{sv})", ([uri], destination.get_uri(), {}))
            source_parent = Gio.File.new_for_uri(uri).get_parent()
            watch_uris = [destination.get_uri()]
            if source_parent is not None:
                watch_uris.append(source_parent.get_uri())
            self._watch_operation_directories(watch_uris)
            self._call_nautilus_file_operation(method, parameters, uri)

        dialog.select_folder(self._win, None, on_destination_selected, None)

    def trash_focused_folder(self) -> bool:
        """Move the focused local Miller item to trash (the Delete target)."""
        column = self._focused_column()
        row = column.selected_row() if column is not None else None
        if row is None or not row.uri.startswith("file://"):
            return False
        self._move_to_trash(column, row.uri)
        return True

    def copy_focused_folder_to_clipboard(self, *, cut: bool) -> bool:
        """Copy or cut the focused Miller item (the Ctrl+X/Ctrl+C targets)."""
        column = self._focused_column()
        row = column.selected_row() if column is not None else None
        if row is None:
            return False
        self._copy_to_clipboard(row.uri, cut=cut)
        return True

    def paste_into_focused_folder(self) -> bool:
        """Paste into the focused Miller folder (the Ctrl+V target)."""
        column = self._focused_column()
        row = column.selected_row() if column is not None else None
        if row is None or not row.is_dir or not self._clipboard_has_pasteable_files():
            return False
        self._paste_into_folder(row.uri)
        return True

    def rename_focused_folder(self) -> bool:
        """Open Rename for the focused local Miller item (the F2 target)."""
        column = self._focused_column()
        row = column.selected_row() if column is not None else None
        if row is None or not row.uri.startswith("file://"):
            return False
        self._show_rename_popover(column, row)
        return True

    def _on_real_row_activated(self, column: Gtk.Widget, row: Gtk.Widget) -> None:
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
            row.is_dir and stale and stale[0].folder_uri.rstrip("/") == row.uri.rstrip("/")
        )

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
                self._set_preview(None)
                preview_replaced = True
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
                self._set_preview(row.uri)
                preview_replaced = True
                preview_added = True

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
            # _scroll_to_viewport_end).
            self._scroll_to_viewport_end()
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
        that model before exposing the native view again."""
        try:
            self._win.activate_action("slot.open-location", GLib.Variant("s", uri))
        except Exception as e:
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

        Only ever preserves columns that are already open: an exact match
        against one of them is either our own drill-down's
        _sync_slot_location echo (already the deepest column -- no-op) or a
        pathbar chip for an already-open ancestor (truncate what's deeper,
        keep the rest untouched -- no re-enumeration, no flicker, no lost
        widths). A location that is the immediate child of an already-open
        column (e.g. the native view opened a folder while sitting inside a
        folder the Miller chain already has open) extends the chain by one
        column instead, via _drill_into_open_chain -- same "still browsing
        the same branch" UX as a click would give. Anything else -- a
        bookmark, a typed address, a sidebar jump, or any location that
        isn't one of the columns on screen or their immediate child --
        re-roots fresh there via reset(), a single column, same as a brand
        new Column View entry (see enter_column_view). Deliberately does NOT
        walk new_uri's full filesystem ancestry to rebuild a multi-level
        chain: that logic used to treat every location as "a descendant of
        wherever the chain happens to be rooted," which degenerates once the
        root is the filesystem root itself (everything is a descendant of
        "/") and silently exploded any external navigation into a full path
        chain instead of a single column. Checking only the immediate parent
        (one hop, against the finite list of columns actually open) doesn't
        have that failure mode: a jump that skips levels, or lands outside
        the open chain entirely, still falls through to reset() below.

        Matches via Gio.File.equal() rather than string comparison --
        trailing slashes, percent-encoding, and other representational
        differences between the same location's two URI strings (e.g. one
        came from Nautilus's real slot, the other from our own enumeration)
        are exactly what GVfs's own equality already normalizes for; a bare
        rstrip("/") string compare is not guaranteed to agree with it in
        every case (the raw filesystem root "file:///" is a corner case:
        rstrip strips all three slashes down to "file:", not one)."""
        target = Gio.File.new_for_uri(new_uri)
        existing = [Gio.File.new_for_uri(c.folder_uri) for c in self.columns]

        idx = next((i for i, f in enumerate(existing) if f.equal(target)), None)
        if idx is None:
            parent = target.get_parent()
            parent_idx = (
                next((i for i, f in enumerate(existing) if f.equal(parent)), None)
                if parent is not None
                else None
            )
            if parent_idx is None:
                self.reset(new_uri)
                return
            self._drill_into_open_chain(parent_idx, new_uri)
            return

        if idx == len(existing) - 1:
            # Already exactly the deepest open column.
            return

        _log("sync_to_uri: truncating to already-open ancestor")
        for stale in self.columns[idx + 1 :]:
            stale.destroy_enumeration()
        del self.columns[idx + 1 :]

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
        if not self._column_fully_visible(idx):
            self._align_to_viewport_start(self.columns[idx])
        # Deliberately no _arm_focus_retry call here: focused_index/the accent
        # highlight track the new location just above, but selecting a
        # column (click or the echo of one) no longer grabs GTK keyboard
        # focus onto it.

    def _drill_into_open_chain(self, parent_idx: int, new_uri: str) -> None:
        """Extend the chain by one column: new_uri is the immediate child of
        an already-open column (see sync_to_uri's parent_idx check), so this
        is "still browsing the same branch," not a new location -- append a
        fresh column exactly like a click on that row would
        (_on_real_row_activated's NAV_DOWN branch), just triggered by an
        external navigation instead of a click."""
        for stale in self.columns[parent_idx + 1 :]:
            stale.destroy_enumeration()
        del self.columns[parent_idx + 1 :]

        # Checked BEFORE the append below: self.columns still reflects only
        # the columns that are staying (see _new_content_fits).
        fits = self._new_content_fits(COLUMN_WIDTH)
        new_column = self._make_real_column(new_uri)
        new_column.width = COLUMN_WIDTH
        self.columns.append(new_column)
        self._set_preview(None)

        if _COLUMN_KEYBOARD_NAV:
            self.focused_index = len(self.columns) - 1
        else:
            self.focused_index = max(0, len(self.columns) - 2)
        self._sync_column_selections()
        self._apply_focused_column_style()
        self._rebuild_chain()
        self._fade_in(new_column)
        self._fade_in(self.preview_column, duration=PREVIEW_FADE_DURATION_MS)
        if not fits:
            self._align_to_viewport_end(new_column)

    def _sync_column_selections(self) -> None:
        """Each column's own row selection is derived from the URI chain
        that is actually open right now, not tracked as click history: a
        column highlights the row whose URI equals the next column's
        folder_uri, and the last column highlights the previewed file (or
        nothing, if the last column is itself a fresh empty drill-down).
        This is what keeps the accent highlight (see _CSS's .mc-column-list
        rule) on exactly the active path -- redriving every column here,
        including ones the click didn't touch, means a stale selection from
        a since-abandoned branch can never linger."""
        for i, col in enumerate(self.columns):
            col.clear_active_row()
            if i + 1 < len(self.columns):
                col.select_child_for_uri(self.columns[i + 1].folder_uri)
            elif self.preview_column.file_uri:
                col.select_child_for_uri(self.preview_column.file_uri)
            else:
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

    def _on_key_pressed(self, _ctrl, keyval, _keycode, _gtk_state) -> bool:
        """Block keys only while the Column View itself owns focus.

        This controller is attached to the view scroller, not the Nautilus
        window, so header-bar controls and location-entry widgets retain
        their normal keyboard handling. ``~`` and ``/`` pass through for
        Nautilus's URI-entry shortcuts.
        """
        return keyval not in (Gdk.KEY_asciitilde, Gdk.KEY_slash)

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
        state = {"ticks_left": _FOCUS_RETRY_FRAMES}

        def _retry_on_tick(_widget, _frame_clock) -> bool:
            if col is not self._focused_column():
                return GLib.SOURCE_REMOVE
            col.grab_list_focus()
            state["ticks_left"] -= 1
            return GLib.SOURCE_CONTINUE if state["ticks_left"] > 0 else GLib.SOURCE_REMOVE

        col.list_box.add_tick_callback(_retry_on_tick)

    def _make_preview_column(self) -> Gtk.Widget:
        # Starts empty (nothing selected yet); a fresh preview is built each
        # time a file is clicked (see _set_preview).
        return MyComputerPreviewColumn(self._ext, None)

    def _set_preview(self, file_uri: str | None) -> None:
        # The preview is rebuilt (never updated in place) on every navigation:
        # cancel the old one's async work and swap in a fresh widget. The old
        # widget is detached from the paned chain by the next _rebuild_chain.
        old = self.preview_column
        if old is not None:
            old.destroy_enumeration()
        self.preview_column = MyComputerPreviewColumn(self._ext, file_uri)

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
        if abs(x - paned.get_position()) <= HANDLE_HIT_SLOP:
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
        return sum(c.width for c in self.columns[:index]) + HANDLE_WIDTH_ESTIMATE * index

    def _column_fully_visible(self, index: int) -> bool:
        """True if self.columns[index] is entirely within the currently
        visible viewport (both edges), so NAV_UP navigation landing on it
        can leave the scroll position alone rather than moving it."""
        viewport_width = self.scroller.get_width()
        if viewport_width <= 0:
            return False  # not laid out yet -- nothing to measure
        left = self._col_position(index)
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
        (or None on failure) rather than the (ok, x, y) triple some other
        GTK/PyGObject versions expose -- confirmed via a live traceback
        (ValueError: not enough values to unpack, expected 3, got 2)."""
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
        visible_right_edge = self.scroller.get_hadjustment().get_value() + viewport_width
        available = visible_right_edge - self._col_position(len(self.columns))
        return available >= added_width

    def _align_to_viewport_end(self, widget: Gtk.Widget) -> None:
        """Scroll (once things settle) just far enough that widget's
        reading-end edge lands flush against that edge of the viewport --
        right in LTR, left in RTL (named after the logical, direction-aware
        edge rather than the physical one, same convention GTK itself uses
        for Gtk.Align.START/END, so a future RTL layout only needs the edge
        math itself made direction-aware, not every caller re-audited). The
        canvas x-coordinate math this and _align_to_viewport_start build on
        is still LTR-only for now -- true RTL also needs the column layout
        itself mirrored (see _make_paned_chain/_MillerCanvas), not just
        which edge gets aligned.

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
        convention; a probe confirmed the event arrives as a plain dy, not a
        pre-swapped dx). A plain vertical scroll with no Shift is left alone
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
        adj = self.scroller.get_hadjustment()
        target = adj.get_value() + pan * adj.get_step_increment()
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
            # The "scroll_*" kinds carry no widget -- they jump straight to
            # a known adjustment value, no measurement needed.
            if kind == "scroll_end":
                self._animate_scroll_to(adj.get_upper() - adj.get_page_size())
                return GLib.SOURCE_REMOVE
            if kind == "scroll_start":
                self._animate_scroll_to(adj.get_lower())
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
                # (right, in this LTR-only canvas math -- see
                # _align_to_viewport_end's docstring) meets that edge of the
                # viewport; "align_start" snaps the view so widget's start
                # edge (left) meets the viewport's start edge instead;
                # "align_pos" snaps the view so widget's start edge lands
                # `position` pixels in from the viewport's start.
                if kind == "align_end":
                    target = left + widget.get_width() - adj.get_page_size()
                elif kind == "align_pos":
                    target = left - position
                else:
                    target = left
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


def copy_focused_folder_to_clipboard(ext, win: Gtk.Window, *, cut: bool) -> bool:
    """Dispatch Ctrl+X/Ctrl+C to the active slot's Miller host."""
    host = _host_for_window(ext, win)
    return host.copy_focused_folder_to_clipboard(cut=cut) if host is not None else False


def paste_into_focused_folder(ext, win: Gtk.Window) -> bool:
    """Dispatch Ctrl+V to the active slot's Miller host."""
    host = _host_for_window(ext, win)
    return host.paste_into_focused_folder() if host is not None else False


def create_folder_in_focused_column(ext, win: Gtk.Window) -> bool:
    """Dispatch Shift+Ctrl+N to the active slot's Miller host."""
    host = _host_for_window(ext, win)
    return host.create_folder_in_focused_column() if host is not None else False


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
        host.set_native_cut_observer_active(False)
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
    """Nothing to restore -- injection only hides/reparents widgets that stay
    alive in the tree, and window teardown drops our Box along with them."""
