"""Reusable, self-contained card/section widgets for the Computer panel.

Each card renders itself from a single model object (a MountInfo or
PreferredFolder, accessed by duck-typing -- this module never imports those
classes) and adapts its layout to the current view mode ("icon-view" grid vs
"list-view" row). Cards never import the entry file; behaviour that needs the
extension (right-click menus, file-op D-Bus calls, navigation) is reached
through the injected `ext` instance.
"""

import dataclasses

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango

from nautilus_my_computer.common import (
    _CARD_WIDTH,
    _DISK_CARD_ICON_SPACING,
    _DISK_CARD_MARGIN_BOTTOM,
    _DISK_CARD_MARGIN_END,
    _DISK_CARD_MARGIN_START,
    _DISK_CARD_MARGIN_TOP,
    _DISK_ICON_SIZE,
    _FOLDER_CARD_MARGIN_BOTTOM,
    _FOLDER_CARD_MARGIN_END,
    _FOLDER_CARD_MARGIN_START,
    _FOLDER_CARD_MARGIN_TOP,
    _GROUP_ICON,
    _INTERNAL_FSTYPES,
    _LIST_BAR_MAX_WIDTH,
    _,
    _folder_card_width,
    _format_size,
    _gicon_renders,
    _icon_name_renders,
    _log,
    _nautilus_icon_size,
)


class MyComputerDiskCard(Gtk.Box):
    """Self-contained card: renders one MountInfo as a grid card or a list row."""

    __gtype_name__ = "MyComputerDiskCard"

    def __init__(self, ext, win: Gtk.Window, view_mode: str, model, group_key: str) -> None:
        super().__init__()
        self._ext = ext
        self._win = win
        self.view_mode = view_mode
        self.model = model
        self.group_key = group_key
        self.usage_bar: Gtk.LevelBar | None = None
        self.sub_label: Gtk.Label | None = None

        self.get_style_context().add_class("nautilus-view-cell")
        self.set_focusable(True)
        self.set_focus_on_click(True)
        self._build()

        right_click = Gtk.GestureClick()
        right_click.set_button(3)
        right_click.connect("pressed", self._ext._on_card_right_clicked, self._win, self)
        self.add_controller(right_click)

    @property
    def is_list(self) -> bool:
        return self.view_mode == "list-view"

    @property
    def nav_uri(self) -> str:
        m = self.model
        return m.nav_uri or (Gio.File.new_for_path(m.mountpoint).get_uri() if m.mountpoint else "")

    def _info(self) -> tuple:
        """(has_size, sub_text, bar_value) for the current model state."""
        m = self.model
        has_size = m.total > 0
        if not m.is_mounted:
            return has_size, _("Not mounted"), 0.0
        if has_size:
            sub_text = _("{free} free of {total}").format(
                free=_format_size(m.free), total=_format_size(m.total)
            )
            return has_size, sub_text, min(m.percent / 100.0, 1.0)
        return has_size, self.nav_uri, 0.0

    def _icon_name(self) -> str:
        return _GROUP_ICON.get(self.group_key, "drive-harddisk")

    def _build(self) -> None:
        m = self.model
        has_size, sub_text, bar_value = self._info()

        if not m.is_mounted:
            self.get_style_context().add_class("unmounted")
        if m.is_hidden:
            self.get_style_context().add_class("hidden-file")
        if m.mountpoint:
            fstype_part = f" ({m.fstype})" if m.fstype and m.fstype not in _INTERNAL_FSTYPES else ""
            self.set_tooltip_text(f"{m.mountpoint}{fstype_part}")

        if self.is_list:
            self._build_list(has_size, sub_text, bar_value)
        else:
            self._build_grid(has_size, sub_text, bar_value)

    def _build_grid(self, has_size: bool, sub_text: str, bar_value: float) -> None:
        m = self.model
        self.set_orientation(Gtk.Orientation.HORIZONTAL)
        self.set_spacing(_DISK_CARD_ICON_SPACING)
        self.set_margin_start(_DISK_CARD_MARGIN_START)
        self.set_margin_end(_DISK_CARD_MARGIN_END)
        self.set_margin_top(_DISK_CARD_MARGIN_TOP)
        self.set_margin_bottom(_DISK_CARD_MARGIN_BOTTOM)
        self.set_size_request(_CARD_WIDTH, -1)

        icon = Gtk.Image()
        icon.set_pixel_size(_DISK_ICON_SIZE)
        icon.set_valign(Gtk.Align.CENTER)
        # icon.set_margin_end(12)
        if _gicon_renders(m.gio_icon):
            icon.set_from_gicon(m.gio_icon)
        else:
            icon.set_from_icon_name(self._icon_name())
        self.append(icon)

        details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        details.set_hexpand(True)
        details.set_valign(Gtk.Align.CENTER)

        display_name = m.display_name or self.nav_uri.rsplit("/", 1)[-1] or "/"
        name_lbl = Gtk.Label(label=display_name)
        name_lbl.set_xalign(0.0)
        name_lbl.set_ellipsize(3)
        name_lbl.set_max_width_chars(0)
        details.append(name_lbl)

        bar = Gtk.LevelBar()
        bar.set_mode(Gtk.LevelBarMode.CONTINUOUS)
        bar.set_min_value(0.0)
        bar.set_max_value(1.0)
        bar.set_hexpand(True)
        bar.set_value(bar_value)
        bar.set_visible(has_size)
        bar.get_style_context().add_class("diskinfo-bar")
        details.append(bar)

        sub_lbl = Gtk.Label(label=sub_text)
        sub_lbl.set_xalign(0.0)
        sub_lbl.set_ellipsize(3)
        sub_lbl.set_max_width_chars(0)
        sub_lbl.get_style_context().add_class("diskinfo-subtext")
        sub_lbl.get_style_context().add_class("caption")
        details.append(sub_lbl)

        self.append(details)
        self.usage_bar = bar if has_size else None
        self.sub_label = sub_lbl if has_size else None

    def _build_list(self, has_size: bool, sub_text: str, bar_value: float) -> None:
        m = self.model
        self.set_orientation(Gtk.Orientation.HORIZONTAL)
        self.set_spacing(12)
        self.set_margin_start(6)
        self.set_margin_end(6)
        self.set_margin_top(6)
        self.set_margin_bottom(6)
        self.set_hexpand(True)

        icon = Gtk.Image()
        icon.set_pixel_size(36)
        icon.set_valign(Gtk.Align.CENTER)
        if _gicon_renders(m.gio_icon):
            icon.set_from_gicon(m.gio_icon)
        else:
            icon.set_from_icon_name(self._icon_name())
        self.append(icon)

        display_name = m.display_name or self.nav_uri.rsplit("/", 1)[-1] or "/"
        name_lbl = Gtk.Label(label=display_name)
        name_lbl.set_xalign(0.0)
        name_lbl.set_ellipsize(3)
        name_lbl.set_valign(Gtk.Align.CENTER)
        self.append(name_lbl)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        self.append(spacer)

        sub_lbl = Gtk.Label(label=sub_text)
        sub_lbl.set_xalign(1.0)
        sub_lbl.set_ellipsize(3)
        sub_lbl.set_valign(Gtk.Align.CENTER)
        sub_lbl.get_style_context().add_class("diskinfo-subtext")
        sub_lbl.get_style_context().add_class("caption")
        self.append(sub_lbl)

        bar = Gtk.LevelBar()
        bar.set_mode(Gtk.LevelBarMode.CONTINUOUS)
        bar.set_min_value(0.0)
        bar.set_max_value(1.0)
        bar.set_size_request(_LIST_BAR_MAX_WIDTH, -1)
        bar.set_valign(Gtk.Align.CENTER)
        bar.set_value(bar_value)
        bar.set_visible(has_size)
        bar.get_style_context().add_class("diskinfo-bar")
        self.append(bar)

        self.usage_bar = bar if has_size else None
        self.sub_label = sub_lbl if has_size else None

    def update_usage(self, m) -> None:
        """Patch the LevelBar + sub-label in place; called by poll workers."""
        self.model = m
        total, free = m.total, m.free
        if self.usage_bar is not None and total > 0:
            self.usage_bar.set_value(min(1.0, (total - free) / total))
        if self.sub_label is not None and total > 0:
            self.sub_label.set_label(
                _("{free} free of {total}").format(
                    free=_format_size(free), total=_format_size(total)
                )
            )


class MyComputerFolderCard(Gtk.Box):
    """Self-contained card: renders one PreferredFolder as a grid card or a list row."""

    __gtype_name__ = "MyComputerFolderCard"

    def __init__(self, ext, win: Gtk.Window, view_mode: str, model) -> None:
        super().__init__()
        self._ext = ext
        self._win = win
        self.view_mode = view_mode
        self.model = model
        self.icon: Gtk.Image | None = None
        self.name_label: Gtk.Label | None = None

        self.get_style_context().add_class("nautilus-view-cell")
        self.get_style_context().add_class("mc-folder-card")
        self.set_focusable(True)
        self.set_focus_on_click(True)
        self._build()
        self._apply_hidden_state(model.is_hidden)

        right_click = Gtk.GestureClick()
        right_click.set_button(3)
        right_click.connect("pressed", self._ext._on_card_right_clicked, self._win, self)
        self.add_controller(right_click)

        self._wire_drag()
        self._wire_reorder_preview()

    @property
    def is_list(self) -> bool:
        return self.view_mode == "list-view"

    @property
    def nav_uri(self) -> str:
        return self.model.nav_uri

    def do_measure(self, orientation, for_size):
        if not self.is_list and orientation == Gtk.Orientation.HORIZONTAL:
            width = _folder_card_width()
            return (width, width, -1, -1)
        return Gtk.Box.do_measure(self, orientation, for_size)

    def _wire_reorder_preview(self) -> None:
        """Drop side: when a folder-card drag enters this card (the target),
        move the dragged card to this card's position in the FlowBox for a
        live preview, and reindex every card to match the new order.
        DropControllerMotion's "enter" fires once per card the cursor crosses
        onto; it never claims the drop, so the separate DropTarget below owns
        committing the result. The DropTarget accepts the dragged card's own
        GType so its "drop" fires (and the drag ends as a successful MOVE, no
        snap-back), then persists the FlowBox's final order to gsettings."""
        motion = Gtk.DropControllerMotion()
        motion.connect("enter", self._on_reorder_enter)
        self.add_controller(motion)

        drop = Gtk.DropTarget.new(MyComputerFolderCard, Gdk.DragAction.MOVE)
        drop.connect("drop", self._on_reorder_drop)
        self.add_controller(drop)

    def _on_reorder_drop(self, _target, value, _x, _y) -> bool:
        dst_child = self.get_parent()
        flow = dst_child.get_parent() if dst_child is not None else None
        if flow is None:
            return False
        keys = []
        child = flow.get_first_child()
        while child is not None:
            card = child.get_child()
            if isinstance(card, MyComputerFolderCard):
                keys.append(card.model.key)
            child = child.get_next_sibling()
        _log(
            f"preferred folders dragging dropped: {value.model.display_name}/ "
            f"position {value.model.index}"
        )
        # Defer the gsettings write (which repopulates the panel, destroying
        # these cards) until the drag has fully finished -- doing it inside the
        # drop callback would tear down the very widget running this handler.
        GLib.idle_add(self._ext._commit_preferred_order, keys)
        return True

    def _on_reorder_enter(self, _ctrl, _x, _y) -> None:
        # Re-entrancy guard: moving widgets under the pointer makes GTK synthesize
        # crossing events that re-fire "enter" synchronously mid-move. Without this
        # the nested call removes the dragged card while the outer insert is still
        # on the stack, corrupting the FlowBox (card goes blank, then vanishes on a
        # fast drag). Same pattern as _diskinfo_restoring for icon pinning.
        if getattr(self._ext, "_folder_reordering", False):
            return
        src = getattr(self._ext, "_dragging_folder_card", None)
        if src is None or src is self:
            return
        src_child = src.get_parent()  # FlowBoxChild wrapping the dragged card
        dst_child = self.get_parent()  # FlowBoxChild wrapping this target card
        if src_child is None or dst_child is None:
            return
        flow = dst_child.get_parent()  # the section's Gtk.FlowBox
        if flow is None:
            return

        # Step 1: take the target card's index. Skip if the dragged card is
        # already there -- avoids needless remove/insert churn on fast moves.
        dst_index = dst_child.get_index()
        if src_child.get_index() == dst_index:
            return

        # Step 2: move the dragged card to the target's position. Detach the card
        # from its FlowBoxChild wrapper *first* (set_child(None) unparents it
        # synchronously), then drop the now-empty wrapper and re-wrap the card at
        # the new index. Removing by the inner card instead would leave it briefly
        # parented to the dying wrapper, so the re-insert hits "already has parent"
        # and the card is orphaned (blank, then gone on drop).
        self._ext._folder_reordering = True
        try:
            src_child.set_child(None)
            flow.remove(src_child)
            flow.insert(src, dst_index)
            # flow.insert() wraps src in a brand-new FlowBoxChild, so the "mc-selected"
            # highlight applied to the old wrapper at drag-begin was destroyed along
            # with it. Reapply it here or the dragged card's gutter highlight vanishes
            # after its first move.
            new_child = src.get_parent()
            if new_child is not None:
                new_child.add_css_class("mc-selected")

            # Step 3: rewrite every card's .index to match the new FlowBox order.
            child = flow.get_first_child()
            while child is not None:
                card = child.get_child()
                if isinstance(card, MyComputerFolderCard):
                    card.model.index = child.get_index()
                child = child.get_next_sibling()
        finally:
            self._ext._folder_reordering = False

    def _wire_drag(self) -> None:
        """Drag source ("select the folder"): the card itself is the drag
        payload -- its model carries the index/position, nav_uri, and
        display_name the drop side needs. A ghost copy of the card (icon +
        label) is shown under the cursor for the whole drag, and the card
        left behind in the list is ghosted (dimmed) via a CSS class -- no
        extra state needed, just toggle the class on begin/end/cancel."""
        drag = Gtk.DragSource()
        drag.set_actions(Gdk.DragAction.MOVE)
        drag.connect("prepare", self._on_drag_prepare)
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-end", self._on_drag_end)
        drag.connect("drag-cancel", self._on_drag_cancel)
        self.add_controller(drag)

    def _on_drag_prepare(self, _source, _x, _y):
        return Gdk.ContentProvider.new_for_value(self)

    def _on_drag_begin(self, _source, drag) -> None:
        Gtk.DragIcon.get_for_drag(drag).set_child(self._build_drag_ghost())
        self._set_content_opacity(0.55)
        # Painted on the FlowBoxChild, not self: self's own margins (the card's
        # gutter) sit outside its CSS box, so a highlight on self would be drawn
        # smaller than the native :hover highlight, which GTK paints on the
        # FlowBoxChild and therefore spans the full cell including that gutter.
        parent = self.get_parent()
        if parent is not None:
            parent.add_css_class("mc-selected")
        _log(
            f"preferred folders dragging started: {self.model.display_name}/ "
            f"position {self.model.index}"
        )
        # The drop side can't read the payload mid-hover (Gtk.DropTarget.get_value()
        # is None during motion for in-process GObject drags), so stash the dragged
        # card here for the target's reorder-preview handler to read.
        self._ext._dragging_folder_card = self

    def _on_drag_end(self, _source, _drag, _delete_data) -> None:
        # Only clear if the model itself isn't hidden -- _apply_hidden_state owns
        # this opacity for genuinely hidden folders.
        if not self.model.is_hidden:
            self._set_content_opacity(1.0)
        parent = self.get_parent()
        if parent is not None:
            parent.remove_css_class("mc-selected")
        self._ext._dragging_folder_card = None

    def _on_drag_cancel(self, _source, _drag, _reason) -> bool:
        if not self.model.is_hidden:
            self._set_content_opacity(1.0)
        parent = self.get_parent()
        if parent is not None:
            parent.remove_css_class("mc-selected")
        self._ext._dragging_folder_card = None
        return False

    def _build_drag_ghost(self) -> Gtk.Widget:
        """Copy of the card shown under the cursor while dragging.

        Match the active card layout so list-view drags show the compact
        horizontal cell instead of the grid-style icon-over-label ghost.
        """
        if self.is_list:
            ghost = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            ghost.set_halign(Gtk.Align.START)

            icon = Gtk.Image()
            icon.set_pixel_size(42)
            icon.set_valign(Gtk.Align.CENTER)
            self._set_icon(icon)
            ghost.append(icon)

            labels_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            labels_box.set_valign(Gtk.Align.CENTER)
            name_lbl = Gtk.Label(label=self.model.display_name)
            name_lbl.set_xalign(0.0)
            name_lbl.set_valign(Gtk.Align.CENTER)
            name_lbl.set_max_width_chars(14)
            name_lbl.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            labels_box.append(name_lbl)
            ghost.append(labels_box)
        else:
            ghost = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            ghost.set_halign(Gtk.Align.CENTER)

            icon = Gtk.Image()
            icon.set_pixel_size(_nautilus_icon_size())
            icon.set_halign(Gtk.Align.CENTER)
            self._set_icon(icon)
            ghost.append(icon)

            name_lbl = Gtk.Label(label=self.model.display_name)
            name_lbl.set_justify(Gtk.Justification.CENTER)
            name_lbl.set_halign(Gtk.Align.CENTER)
            label_chars = max(6, _nautilus_icon_size() // 11)
            name_lbl.set_width_chars(label_chars)
            name_lbl.set_max_width_chars(label_chars)
            name_lbl.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            ghost.append(name_lbl)

        ghost.get_style_context().add_class("nautilus-view-cell")
        return ghost

    def _set_icon(self, icon: Gtk.Image) -> None:
        pf = self.model
        if _gicon_renders(pf.gio_icon):
            icon.set_from_gicon(pf.gio_icon)
        elif _icon_name_renders(pf.icon_name):
            icon.set_from_icon_name(pf.icon_name)
        else:
            icon.set_from_icon_name("folder")

    def _build(self) -> None:
        if self.is_list:
            self._build_compact_grid()
        else:
            self._build_grid()
        self.set_tooltip_text(Gio.File.new_for_uri(self.model.nav_uri).get_parse_name())

    def _build_grid(self) -> None:
        pf = self.model
        self.set_orientation(Gtk.Orientation.VERTICAL)
        self.set_spacing(0)
        self.set_margin_start(_FOLDER_CARD_MARGIN_START)
        self.set_margin_end(_FOLDER_CARD_MARGIN_END)
        self.set_margin_top(_FOLDER_CARD_MARGIN_TOP)
        self.set_margin_bottom(_FOLDER_CARD_MARGIN_BOTTOM)
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.CENTER)

        content = MyComputerFixedWidthBox(_nautilus_icon_size(), spacing=2)
        content.set_halign(Gtk.Align.CENTER)
        self.append(content)

        icon = Gtk.Image()
        icon.set_pixel_size(_nautilus_icon_size())
        icon.set_halign(Gtk.Align.CENTER)
        self._set_icon(icon)
        content.append(icon)

        name_lbl = Gtk.Label(label=pf.display_name)
        name_lbl.set_justify(Gtk.Justification.CENTER)
        name_lbl.set_halign(Gtk.Align.CENTER)
        name_lbl.set_wrap(True)
        name_lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        name_lbl.set_lines(1)
        label_chars = max(6, _nautilus_icon_size() // 11)
        name_lbl.set_width_chars(label_chars)
        # Cap the wrap width to the icon's own width (scaled with zoom level), like
        # native Nautilus grid cells -- otherwise a long name stretches the whole
        # FlowBox column since the label has no natural width limit.
        name_lbl.set_max_width_chars(label_chars)
        name_lbl.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        content.append(name_lbl)

        self.icon = icon
        self.name_label = name_lbl

    def _build_compact_grid(self) -> None:
        """List-view compact cell: keep Preferred Folders multi-column while
        keeping the content aligned like the pre-wrapper layout."""
        pf = self.model
        self.set_orientation(Gtk.Orientation.HORIZONTAL)
        self.set_spacing(0)
        self.set_margin_start(0)
        self.set_margin_end(0)
        self.set_margin_top(0)
        self.set_margin_bottom(0)
        self.set_vexpand(True)
        self.set_valign(Gtk.Align.FILL)

        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        content.set_margin_start(6)
        content.set_margin_end(6)
        content.set_margin_top(3)
        content.set_margin_bottom(3)
        content.set_halign(Gtk.Align.START)
        content.set_valign(Gtk.Align.CENTER)

        icon = Gtk.Image()
        icon.set_pixel_size(42)
        icon.set_valign(Gtk.Align.CENTER)
        self._set_icon(icon)
        content.append(icon)

        labels_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        labels_box.set_valign(Gtk.Align.CENTER)
        name_lbl = Gtk.Label(label=pf.display_name)
        name_lbl.set_xalign(0.0)
        name_lbl.set_valign(Gtk.Align.CENTER)
        name_lbl.set_max_width_chars(14)
        name_lbl.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        labels_box.append(name_lbl)
        content.append(labels_box)

        self.append(content)

        self.icon = icon
        self.name_label = name_lbl

    def update_metadata(self, pf) -> None:
        """Patch the icon + name label in place; called once async metadata resolves."""
        self.model = pf
        if self.icon is not None and _gicon_renders(pf.gio_icon):
            self.icon.set_from_gicon(pf.gio_icon)
        if self.name_label is not None:
            self.name_label.set_label(pf.display_name)
        self._apply_hidden_state(pf.is_hidden)

    def _apply_hidden_state(self, is_hidden: bool) -> None:
        self._set_content_opacity(0.55 if is_hidden else 1.0)

    def _set_content_opacity(self, opacity: float) -> None:
        # Dim only the icon + label, not self -- self carries the "nautilus-view-cell"
        # class that draws Nautilus's native selection/focus/hover background and
        # border. GtkWidget.opacity dims a widget's own render as one group, so
        # setting it on self would dim that border/background too.
        if self.icon is not None:
            self.icon.set_opacity(opacity)
        if self.name_label is not None:
            self.name_label.set_opacity(opacity)


class MyComputerFixedWidthBox(Gtk.Box):
    """Box whose horizontal size is hard-capped to a single fixed width."""

    __gtype_name__ = "MyComputerFixedWidthBox"

    def __init__(self, width: int, *, spacing: int = 0) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=spacing)
        self._width = width

    def do_measure(self, orientation, for_size):
        if orientation == Gtk.Orientation.HORIZONTAL:
            return (self._width, self._width, -1, -1)
        return Gtk.Box.do_measure(self, orientation, for_size)


class MyComputerJustifiedFlowBox(Gtk.FlowBox):
    """FlowBox of constant-width cards whose column spacing stretches to fill
    each row's full width on resize, instead of a fixed gutter that leaves
    empty space at the end of the row. Cards stay halign=START and a fixed
    width; only the gaps between them grow or shrink."""

    __gtype_name__ = "MyComputerJustifiedFlowBox"

    def __init__(self, card_width: int, min_spacing: int) -> None:
        super().__init__()
        self._card_width = card_width
        self._min_spacing = min_spacing

    def _child_count(self) -> int:
        n = 0
        child = self.get_first_child()
        while child is not None:
            n += 1
            child = child.get_next_sibling()
        return n

    def _apply_spacing_for_width(self, width: int) -> None:
        step = self._card_width + self._min_spacing
        width_cols = max(1, (width + self._min_spacing) // step) if step > 0 else 1
        # If the width could fit more columns than we actually have children for,
        # every card already fits on a single row: there's no second row to save
        # by stretching the gaps, so stay at min_spacing and leave the remainder
        # as trailing blank space, same as native Nautilus grid view. This is
        # recomputed from scratch every time (not "frozen" from a prior wider
        # layout), so it stays correct across DnD reordering and re-populating
        # the panel after switching away and back.
        if width_cols >= self._child_count():
            spacing = self._min_spacing
        else:
            cols = min(width_cols, self.get_max_children_per_line())
            spacing = self._min_spacing
            if cols > 1:
                spacing = max(self._min_spacing, (width - cols * self._card_width) // (cols - 1))
        if spacing != self.get_column_spacing():
            self.set_column_spacing(spacing)

    def do_measure(self, orientation, for_size):
        # FlowBox's own height-for-width measurement uses whatever column_spacing
        # is currently set, which may be stale from the previous width. If that
        # stale spacing makes FlowBox's internal row-fit calculation disagree with
        # the column count _apply_spacing_for_width will use at allocate time, the
        # reserved height ends up sized for one row more than what's actually laid
        # out, leaving empty space at the bottom of the section. Syncing spacing
        # here, before the measurement, keeps both passes in agreement about how
        # many cards fit per row at this width. PyGObject vfunc override: returns
        # a 4-tuple, does NOT take minimum/natural as out-params (unlike the C
        # signature) -- get this wrong and measurement silently breaks.
        if orientation == Gtk.Orientation.VERTICAL and for_size > 0:
            self._apply_spacing_for_width(for_size)
        return Gtk.FlowBox.do_measure(self, orientation, for_size)

    def do_size_allocate(self, width: int, height: int, baseline: int) -> None:
        self._apply_spacing_for_width(width)
        Gtk.FlowBox.do_size_allocate(self, width, height, baseline)


class MyComputerCappedGridFlowBox(Gtk.FlowBox):
    """Homogeneous FlowBox that raises its column count as the window widens,
    capping each card's stretched width at max_card_width instead of letting a
    single row of cards grow past it -- same trigger native Nautilus grid view
    uses to add a column, applied to our homogeneous card cells."""

    __gtype_name__ = "MyComputerCappedGridFlowBox"

    def __init__(self, max_card_width: int, spacing: int, hard_max_cols: int) -> None:
        super().__init__()
        self._max_card_width = max_card_width
        self._spacing = spacing
        self._hard_max_cols = hard_max_cols
        self.set_max_children_per_line(hard_max_cols)

    def _cols_for_width(self, width: int) -> int:
        step = self._max_card_width + self._spacing
        cols = -(-(width + self._spacing) // step) if step > 0 else 1  # ceil division
        return max(1, min(cols, self._hard_max_cols))

    def do_measure(self, orientation, for_size):
        if orientation == Gtk.Orientation.VERTICAL and for_size > 0:
            self.set_max_children_per_line(self._cols_for_width(for_size))
        return Gtk.FlowBox.do_measure(self, orientation, for_size)

    def do_size_allocate(self, width: int, height: int, baseline: int) -> None:
        self.set_max_children_per_line(self._cols_for_width(width))
        Gtk.FlowBox.do_size_allocate(self, width, height, baseline)


class MyComputerCardSection(Gtk.Box):
    """A heading + FlowBox of cards. Dedups the section setup shared by the
    Preferred Folders block and each disk group in _populate()."""

    __gtype_name__ = "MyComputerCardSection"

    def __init__(
        self,
        ext,
        win: Gtk.Window,
        label: str,
        view_mode: str,
        *,
        max_cols: int,
        col_spacing: int,
        row_spacing: int,
        always_grid: bool = False,
        justify: bool = False,
        card_width: int = 0,
        homogeneous: bool = False,
        max_card_width: int = 0,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self._ext = ext
        self._size_group = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)

        heading = Gtk.Label(label=label)
        heading.set_xalign(0.0)
        heading.get_style_context().add_class("heading")
        heading.set_margin_top(12)
        heading.set_margin_start(6)
        self.append(heading)

        is_list = view_mode == "list-view" and not always_grid
        is_capped_grid = homogeneous and max_card_width > 0 and not is_list
        if justify and not is_list:
            self.flow = MyComputerJustifiedFlowBox(card_width, col_spacing)
        elif is_capped_grid:
            self.flow = MyComputerCappedGridFlowBox(max_card_width, col_spacing, max_cols)
        else:
            self.flow = Gtk.FlowBox()
        self.flow.set_homogeneous(homogeneous)
        if not is_capped_grid:
            self.flow.set_max_children_per_line(1 if is_list else max_cols)
        self.flow.set_column_spacing(col_spacing)
        self.flow.set_row_spacing(row_spacing)
        self.flow.set_margin_bottom(12)
        self.flow.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.flow.set_activate_on_single_click(ext._click_policy == "single")
        self.flow.set_hexpand(True)
        # GTK 4.16 (used by Nautilus 47) does not allocate spare horizontal
        # space to an expanding FlowBox whose halign is START. That leaves the
        # justified folder flow one card wide while its height was measured for
        # a full-width row, so following sections overlap it. FILL gives the
        # flow the width its spacing/allocation code expects on 47 and 48+.
        self.flow.set_halign(Gtk.Align.FILL)
        self.flow.set_valign(Gtk.Align.START)

        self.flow.connect("child-activated", ext._on_card_activated, win)
        self.flow.connect("selected-children-changed", ext._on_flow_selection_changed, win)
        ext._attach_flow_shortcuts(self.flow, win)

        self.append(self.flow)

    def add_card(self, card: Gtk.Widget) -> None:
        self._size_group.add_widget(card)
        self.flow.append(card)


@dataclasses.dataclass
class MyComputerMenuItem:
    """One entry in a contextual (right-click) menu.

    Reusable across sidebar places and disk-group cards. The action is a plain
    callable run on activate, so callers don't have to register Gio actions or
    juggle action-name strings.
    """

    label: str  # display label (translatable)
    action: object = None  # callable() run on activate; None = inert item
    shortcut: str = ""  # accel string, e.g. "Return", "<Control>Return", "<Alt>Return"
    section: int = 0  # consecutive items sharing a number group together; gaps draw separators
    enabled: bool = True  # rendered greyed out when False
    visible: bool = True  # omitted from the menu entirely when False
    submenu: list = None  # list[MyComputerMenuItem]; when set, renders as a native submenu


@dataclasses.dataclass
class MyComputerContextualMenu:
    """A reusable right-click menu: an ordered list of MyComputerMenuItem grouped into
    sections.

    Built fresh at show-time so items can reflect live state (on-page, mounted,
    ejectable, ...). Call build_popover() to turn it into a ready Gtk.PopoverMenu.
    """

    items: list = dataclasses.field(default_factory=list)

    def build_popover(self, parent: Gtk.Widget, prefix: str) -> Gtk.PopoverMenu:
        model = Gio.Menu()
        ag = Gio.SimpleActionGroup()
        counter = 0

        def add_items(items: list, target_menu: Gio.Menu) -> None:
            nonlocal counter
            section_menu = None
            current_section = None

            for it in items:
                if not it.visible:
                    continue
                if section_menu is None or it.section != current_section:
                    section_menu = Gio.Menu()
                    target_menu.append_section(None, section_menu)
                    current_section = it.section

                if it.submenu:
                    sub_model = Gio.Menu()
                    add_items(it.submenu, sub_model)
                    section_menu.append_item(Gio.MenuItem.new_submenu(it.label, sub_model))
                    continue

                action_name = f"item{counter}"
                counter += 1
                menu_item = Gio.MenuItem.new(it.label, f"{prefix}.{action_name}")
                if it.shortcut:
                    menu_item.set_attribute_value("accel", GLib.Variant("s", it.shortcut))
                section_menu.append_item(menu_item)

                act = Gio.SimpleAction.new(action_name, None)
                act.set_enabled(it.enabled)
                if callable(it.action):
                    act.connect("activate", lambda *_a, cb=it.action: cb())
                ag.add_action(act)

        add_items(self.items, model)

        popover = Gtk.PopoverMenu.new_from_model(model)
        popover.set_has_arrow(False)
        popover.set_parent(parent)
        popover.insert_action_group(prefix, ag)
        return popover
