"""Bookmarks target: GTK bookmarks file (read/write/toggle), the
custom-bookmark-icon GSettings map, and the native sidebar-row menu
injection (Change Icon item + icon picker + icon pinning).

Data helpers take gsettings/uri explicitly; UI helpers take `ext` (the
MyComputerExtension instance) for window/state access. No app state of its
own -- this module can be imported from anywhere without import cycles.
"""

import os

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk

from nautilus_my_computer.common import (
    _,
    _all_widgets,
    _find_row_start_image,
    _log,
    _menu_section_with_action,
    _native,
    _pin_icon,
)

# Bookmark icon-picker grid geometry.
_ICON_PICKER_COLS = 6  # visible columns
_ICON_PICKER_ROWS = 5  # visible rows before scrolling
_ICON_PICKER_CELL_SIZE = 36  # px, square cell (FlowBoxChild) holding one icon
_ICON_PICKER_SPACING = 4  # px, row/column spacing between cells


def _bookmarks_path() -> str:
    return os.path.join(GLib.get_user_config_dir(), "gtk-3.0", "bookmarks")


def bookmark_uris() -> set:
    """The user's bookmark URIs from ~/.config/gtk-3.0/bookmarks (trailing
    slash stripped), used to tell real bookmarks apart from mounted volumes
    and built-in places (which also carry file:// URIs)."""
    uris = set()
    try:
        with open(_bookmarks_path()) as f:
            for line in f:
                line = line.strip()
                if line:
                    uris.add(line.split(maxsplit=1)[0].rstrip("/"))
    except OSError:
        pass
    return uris


def is_bookmarked(uri: str) -> bool:
    return uri.rstrip("/") in bookmark_uris()


def add_bookmark(uri: str, label: str | None = None) -> None:
    """Append uri to the GTK bookmarks file, no-op if already present.

    Nautilus monitors this file and refreshes the sidebar live, so no extra
    signaling is needed after the write.
    """
    norm = uri.rstrip("/")
    if norm in bookmark_uris():
        return
    path = _bookmarks_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = f"{norm} {label}\n" if label else f"{norm}\n"
    with open(path, "a") as f:
        f.write(line)


def remove_bookmark(uri: str) -> None:
    """Rewrite the GTK bookmarks file, dropping the line matching uri."""
    norm = uri.rstrip("/")
    path = _bookmarks_path()
    try:
        with open(path) as f:
            lines = f.readlines()
    except OSError:
        return
    kept = [line for line in lines if line.strip().split(maxsplit=1)[:1] != [norm]]
    if len(kept) != len(lines):
        with open(path, "w") as f:
            f.writelines(kept)


def toggle_bookmark(uri: str, label: str | None = None) -> bool:
    """Add or remove uri's bookmark depending on current state. Returns the
    new state (True if now bookmarked)."""
    if is_bookmarked(uri):
        remove_bookmark(uri)
        return False
    add_bookmark(uri, label)
    return True


def get_bookmark_icons(gsettings) -> dict:
    """uri (trailing slash stripped) -> custom symbolic icon name. Only
    bookmarks the user has explicitly customized are present."""
    if not gsettings:
        return {}
    return gsettings.get_value("custom-bookmark-icons").unpack()


def set_bookmark_icon(gsettings, uri: str, icon_name: str) -> None:
    if not gsettings:
        return
    icons = get_bookmark_icons(gsettings)
    icons[uri.rstrip("/")] = icon_name
    gsettings.set_value("custom-bookmark-icons", GLib.Variant("a{ss}", icons))


def clear_bookmark_icon(gsettings, uri: str) -> None:
    if not gsettings:
        return
    icons = get_bookmark_icons(gsettings)
    if icons.pop(uri.rstrip("/"), None) is not None:
        gsettings.set_value("custom-bookmark-icons", GLib.Variant("a{ss}", icons))


# ── Native sidebar-row menu injection (issue #23) ───────────────────────────
# Adds a "Change Icon" item to the NATIVE bookmark context menu instead of
# replacing it. Nautilus rebuilds the row popover fresh on each right-click
# (nautilus-sidebar.c: show_row_popover -> create_row_popover, a
# GtkPopoverMenu parented to the NautilusSidebar). We fire a capture-phase
# gesture (before native), then on idle - once the native popover exists -
# append our item to its live GMenu model.


def apply_icon_to_row(row, icon_name: str) -> None:
    """Apply `icon_name` to a bookmark row, both the displayed widget and
    the row's "start-icon" property.

    Nautilus' sidebar drag ghost is a fresh NautilusSidebarRow built from
    properties (nautilus-sidebar-row.c: nautilus_sidebar_row_clone uses
    self->start_icon, not the live widget), so the property must carry the
    custom icon too, or dragging shows the original icon under the cursor.
    We stash the pre-existing native GIcon once so reset_icon_on_row can
    restore it exactly. _pin_icon then keeps the inner Gtk.Image locked
    against Nautilus' one-way bookmark.symbolic-icon -> row.start-icon
    binding (nautilus-sidebar.c: g_object_bind_property in update_places)."""
    img = _find_row_start_image(row)
    if img is None:
        _log("apply_icon_to_row: no start image found on row")
        return
    theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
    if not theme.has_icon(icon_name):
        # Store the intent regardless (already done by the caller); render
        # best-effort by leaving the native icon in place.
        _log(f"apply_icon_to_row: {icon_name} missing in current theme, skipping pin")
        return
    if not hasattr(row, "_mc_native_start_icon"):
        row._mc_native_start_icon = row.get_property("start-icon")
    row.set_property("start-icon", Gio.ThemedIcon.new(icon_name))
    _pin_icon(img, icon_name)
    _log(f"apply_icon_to_row: pinned {icon_name}")


def reset_icon_on_row(row) -> None:
    """Stop pinning and restore the native icon on both the widget and the
    "start-icon" property, using the GIcon stashed by apply_icon_to_row
    before it was first overridden."""
    img = _find_row_start_image(row)
    if img is not None:
        img._diskinfo_pin_name = None  # disarm _pin_icon's notify handler first
    native_gicon = getattr(row, "_mc_native_start_icon", None)
    if native_gicon is None:
        native_gicon = row.get_property("start-icon")
    row.set_property("start-icon", native_gicon)
    if hasattr(row, "_mc_native_start_icon"):
        del row._mc_native_start_icon


def apply_bookmark_icons(ext, native_listbox) -> None:
    """Re-pin every customized bookmark's icon. Called on initial sidebar
    injection and on every native list rebuild, since rebuilt rows get fresh
    widgets that need re-pinning."""
    icons = get_bookmark_icons(ext._gsettings)
    if not icons:
        return
    idx = 0
    while (row := native_listbox.get_row_at_index(idx)) is not None:
        idx += 1
        try:
            uri = row.get_property("uri")
        except Exception:
            uri = None
        if not uri:
            continue
        icon_name = icons.get(uri.rstrip("/"))
        if icon_name:
            apply_icon_to_row(row, icon_name)


def reapply_bookmark_icons_all_windows(ext) -> bool:
    """Re-apply custom bookmark icons in every window after a settings
    change (e.g. another window customized a bookmark)."""
    for _win, state in list(ext._windows.items()):
        native_listbox = state.get("sidebar_listbox")
        if native_listbox is not None:
            apply_bookmark_icons(ext, native_listbox)
    return GLib.SOURCE_REMOVE


def attach_bookmark_context_menus(ext, win, native_listbox) -> None:
    """Attach a button-3 menu to each native bookmark row. Idempotent: a
    per-row flag prevents double-attaching, and rows Nautilus rebuilds get a
    fresh controller on the next pass (re-armed by the caller's native-list
    change watcher)."""
    uris = bookmark_uris()
    if not uris:
        return
    idx = 0
    while (row := native_listbox.get_row_at_index(idx)) is not None:
        idx += 1
        if getattr(row, "_mc_bookmark_menu", False):
            continue
        try:
            uri = row.get_property("uri")
        except Exception:
            uri = None
        if not uri or uri.rstrip("/") not in uris:
            continue
        try:
            label = row.get_property("label") or uri
        except Exception:
            label = uri
        gesture = Gtk.GestureClick()
        gesture.set_button(3)
        # Nautilus builds the row popover on RELEASE (nautilus-sidebar.c:
        # on_row_released -> show_row_popover), not press. Capture phase so we
        # fire alongside native; idle then lands right after the popover exists.
        gesture.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        gesture.connect("released", _on_bookmark_menu_augment, ext, uri, label, win, row)
        row.add_controller(gesture)
        row._mc_bookmark_menu = True
        _log(f"attach_bookmark_context_menus: armed bookmark row uri={uri}")


def _on_bookmark_menu_augment(_gesture, _n, _x, _y, ext, uri, label, win, row) -> None:
    # Do NOT claim the event: Nautilus must still build its native menu. We
    # only piggyback an extra item once that menu exists (next idle tick).
    GLib.idle_add(_inject_change_icon_item, ext, win, uri, label, row)


def _inject_change_icon_item(ext, win, uri: str, label: str, row) -> bool:
    state = ext._windows.get(win)
    sidebar = state.get("sidebar_native_widget") if state else None
    if sidebar is None:
        return GLib.SOURCE_REMOVE
    popover = None
    for w in _all_widgets(sidebar):
        if isinstance(w, Gtk.PopoverMenu) and w.get_mapped():
            popover = w  # sidebar->popover, the row menu just built natively
    if popover is None:
        _log("inject_change_icon_item: native popover not found")
        return GLib.SOURCE_REMOVE
    if getattr(popover, "_mc_injected", False):
        return GLib.SOURCE_REMOVE
    model = popover.get_menu_model()
    if not isinstance(model, Gio.Menu):
        _log(f"inject_change_icon_item: model is {type(model).__name__}, not Gio.Menu")
        return GLib.SOURCE_REMOVE

    # Append to the native Remove/Rename section so the item sits in that group
    # (after Rename), not in a separate block at the bottom. Find it by the
    # row.rename action rather than a hard-coded index. Appending fires
    # items-changed, so the already-mapped GtkPopoverMenu rebuilds and shows it.
    section = _menu_section_with_action(model, "row.rename")
    if not isinstance(section, Gio.Menu):
        _log("inject_change_icon_item: Remove/Rename section not found, using own section")
        section = Gio.Menu()
        model.append_section(None, section)
    section.append(_("Change Icon"), "mcbookmark.change-icon")

    ag = Gio.SimpleActionGroup()
    act = Gio.SimpleAction.new("change-icon", None)
    act.connect("activate", lambda *_a: open_bookmark_icon_picker(ext, uri, label, row))
    ag.add_action(act)
    popover.insert_action_group("mcbookmark", ag)
    popover._mc_injected = True
    _log(f"inject_change_icon_item: added Change Icon to native menu uri={uri}")
    return GLib.SOURCE_REMOVE


def open_bookmark_icon_picker(ext, uri: str, label: str, row) -> None:
    """Searchable symbolic-icon grid for a bookmark. Matches native Rename's
    presentation (nautilus-sidebar.c: create_rename_popover /
    show_rename_popover): a Gtk.Popover parented to the row, arrow pointing
    at it, GTK_POS_RIGHT - not a centered modal dialog. A plain Gtk.Popover
    (not GtkPopoverMenu) so icons can be clicked repeatedly without the
    popover auto-closing."""
    _log(f"open_bookmark_icon_picker: uri={uri} label={label}")
    current_icon = get_bookmark_icons(ext._gsettings).get(uri.rstrip("/"))

    # Only the visible-row count needs a pixel height; the width is dynamic.
    # A ScrolledWindow with horizontal policy NEVER requests its child's full
    # natural width, so the grid's actual rendered width (cell + padding +
    # borders + its own start/end margins) drives the popover - no brittle
    # width arithmetic. The header/button stretch to match via hexpand.
    grid_height = (
        _ICON_PICKER_ROWS * _ICON_PICKER_CELL_SIZE + (_ICON_PICKER_ROWS - 1) * _ICON_PICKER_SPACING
    )

    popover = Gtk.Popover()
    popover.set_parent(row)
    popover.set_position(Gtk.PositionType.RIGHT)

    # No horizontal margin on the outer box: each child manages its own side
    # margins so the grid can reach further right than the header/button.
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    box.set_margin_top(10)
    box.set_margin_bottom(10)

    # Header: label + search bar grouped, 10px side margins, hexpand so it
    # stretches to the grid's dynamic width (the grid drives the box width).
    header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    header.set_hexpand(True)
    header.set_margin_start(10)
    header.set_margin_end(10)
    box.append(header)

    # Bold markup label, matching native Rename's "<b>Name</b>" field label
    # style (nautilus-sidebar.c: create_rename_popover), not a .heading.
    title = Gtk.Label(label="<b>%s</b>" % _native("Icon"))
    title.set_use_markup(True)
    title.set_xalign(0.0)
    header.append(title)

    search_entry = Gtk.SearchEntry()
    search_entry.set_placeholder_text(_("Search icons…"))
    # Gtk.Popover closes on Escape natively (same as the native rename
    # popover), but GtkSearchEntry intercepts Escape first to clear/stop
    # its own search and never lets it bubble up. Forward it explicitly so
    # Escape closes this popover too, matching native behaviour.
    search_entry.connect("stop-search", lambda _e: popover.popdown())
    header.append(search_entry)

    flow = Gtk.FlowBox()
    # SINGLE selection mode gives each cell the same native hover/selected
    # background as the disk cards (.diskinfo-panel flowbox), instead of a
    # nested Gtk.Button - a button's own hover background stacked on top of
    # the FlowBoxChild's was the "double grey" look. The "mc-icon-grid"
    # class scopes the same --accent-bg-color grey override used there.
    flow.add_css_class("mc-icon-grid")
    flow.set_selection_mode(Gtk.SelectionMode.SINGLE)
    flow.set_activate_on_single_click(True)
    flow.set_max_children_per_line(_ICON_PICKER_COLS)
    flow.set_min_children_per_line(_ICON_PICKER_COLS)
    flow.set_homogeneous(True)
    flow.set_row_spacing(_ICON_PICKER_SPACING)
    flow.set_column_spacing(_ICON_PICKER_SPACING)
    # Anchor to the start corner (top-left, or top-right under RTL) instead
    # of the FlowBox's default fill/center, so a filtered-down result sits
    # at the corner rather than floating in the middle of the popover.
    flow.set_valign(Gtk.Align.START)
    flow.set_halign(Gtk.Align.START)
    flow.set_margin_start(10)
    flow.set_margin_end(10)
    reset_button = Gtk.Button(label=_("Reset"))
    reset_button.set_sensitive(current_icon is not None)

    def _on_icon_activated(_flow, child: Gtk.FlowBoxChild) -> None:
        icon_name = child._mc_icon_name
        set_bookmark_icon(ext._gsettings, uri, icon_name)
        apply_icon_to_row(row, icon_name)
        reset_button.set_sensitive(True)
        _log(f"open_bookmark_icon_picker: set {icon_name} for uri={uri}")

    def _on_reset_clicked(_button) -> None:
        clear_bookmark_icon(ext._gsettings, uri)
        reset_icon_on_row(row)
        flow.unselect_all()
        reset_button.set_sensitive(False)
        _log(f"open_bookmark_icon_picker: cleared custom icon for uri={uri}")

    flow.connect("child-activated", _on_icon_activated)

    theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
    all_symbolic = sorted(n for n in theme.get_icon_names() if n.endswith("-symbolic"))
    current_child = None
    for icon_name in all_symbolic:
        img = Gtk.Image.new_from_icon_name(icon_name)
        img.set_tooltip_text(icon_name)
        child = Gtk.FlowBoxChild()
        child.set_child(img)
        child.set_size_request(_ICON_PICKER_CELL_SIZE, _ICON_PICKER_CELL_SIZE)
        child._mc_icon_name = icon_name
        flow.append(child)
        if icon_name == current_icon:
            current_child = child

    if current_child is not None:
        flow.select_child(current_child)

    def _filter_icons(child) -> bool:
        query = search_entry.get_text().strip().lower()
        return not query or query in child._mc_icon_name.lower()

    flow.set_filter_func(_filter_icons)
    search_entry.connect("search-changed", lambda _e: flow.invalidate_filter())

    # Horizontal policy NEVER: the scrolled window requests the grid's full
    # natural width (icons + the FlowBox's own start/end margins above), so
    # the popover sizes itself to the grid - no width math. Height is fixed
    # here on the wrapper - setting it on the GtkScrollable flow itself did
    # not constrain the viewport, so the ScrolledWindow owns it instead.
    scrolled = Gtk.ScrolledWindow()
    scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scrolled.set_size_request(-1, grid_height)
    scrolled.set_child(flow)
    box.append(scrolled)

    reset_button.set_margin_start(10)
    reset_button.set_margin_end(10)
    reset_button.connect("clicked", _on_reset_clicked)
    box.append(reset_button)

    popover.set_child(box)
    popover.connect("closed", lambda p: p.unparent())
    popover.popup()
