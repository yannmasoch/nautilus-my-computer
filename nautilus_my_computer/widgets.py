"""Reusable, self-contained card/section widgets for the Computer panel.

Each card renders itself from a single model object (a MountInfo or
PreferredFolder, accessed by duck-typing -- this module never imports those
classes) and adapts its layout to the current view mode ("icon-view" grid vs
"list-view" row). Cards never import the entry file; behaviour that needs the
extension (right-click menus, file-op D-Bus calls, navigation) is reached
through the injected `ext` instance.
"""

import dataclasses
import math
import threading

import cairo
import gi

gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Gtk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, GObject, Graphene, Gsk, Gtk, Pango

# GNOME's own thumbnail engine: it drives the system thumbnailers installed in
# /usr/share/thumbnailers (Evince/Papers for PDF, glycin for images, gsf-office
# for documents, ...), the same cheap, already-present mechanism Nautilus uses.
# Guarded: on a system without the gnome-desktop typelib the preview simply
# falls back to the file's icon rather than breaking the whole extension.
try:
    gi.require_version("GnomeDesktop", "4.0")
    from gi.repository import GnomeDesktop

    _thumb_factory = GnomeDesktop.DesktopThumbnailFactory.new(
        GnomeDesktop.DesktopThumbnailSize.LARGE
    )
except (ValueError, ImportError):
    GnomeDesktop = None
    _thumb_factory = None

# Bounds concurrent thumbnailer subprocess spawns for Column View row
# thumbnails (see MyComputerColumnRow._row_thumbnail_worker) -- a folder with
# hundreds of un-cached files would otherwise launch that many subprocesses
# at once.
_ROW_THUMBNAIL_SEMAPHORE = threading.Semaphore(4)

# Named pages of MyComputerPreviewColumn's stable preview surface.  The
# contents of a page may evolve (for example, video can later become a real
# player), but callers only select a semantic slot and never rebuild the UI.
PREVIEW_SLOT_LOADING = "loading"
PREVIEW_SLOT_ICON = "icon"
PREVIEW_SLOT_IMAGE = "image"
PREVIEW_SLOT_VIDEO = "video"
PREVIEW_SLOT_DOCUMENT = "document"


# Video dimensions for the preview column's Dimensions row. Guarded like
# GnomeDesktop above: a system without GStreamer's pbutils typelib just never
# shows a Dimensions row for videos (images still get one, via GdkPixbuf).
try:
    gi.require_version("Gst", "1.0")
    gi.require_version("GstPbutils", "1.0")
    from gi.repository import Gst, GstPbutils

    Gst.init(None)
except (ValueError, ImportError):
    Gst = None
    GstPbutils = None

from nautilus_my_computer.common import (
    _CARD_WIDTH,
    _COLUMN_MIN_WIDTH,
    _COLUMN_PREVIEW_IMAGE_MAX_WIDTH,
    _COLUMN_PREVIEW_IMAGE_SIZE,
    _COLUMN_PREVIEW_WIDTH,
    _COLUMN_ROW_ICON_SIZE,
    _COLUMN_ROW_SPACING,
    _COLUMN_WIDTH,
    _DISK_CARD_ICON_SPACING,
    _DISK_CARD_MARGIN_BOTTOM,
    _DISK_CARD_MARGIN_END,
    _DISK_CARD_MARGIN_START,
    _DISK_CARD_MARGIN_TOP,
    _GROUP_ICON,
    _INTERNAL_FSTYPES,
    _LIST_BAR_MAX_WIDTH,
    N_,
    _,
    _disk_icon_size,
    _disk_list_icon_size,
    _folder_card_width,
    _format_size,
    _gicon_renders,
    _icon_name_renders,
    _is_activating_click,
    _log,
    _native,
    _nautilus_icon_size,
    _nautilus_list_icon_size,
    _resolve_custom_gicon,
    _set_regular_icon,
)
from nautilus_my_computer.components import set_row_active, set_row_selected


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

        # One gesture on all buttons, dispatched from "pressed", mirroring
        # nautilus-list-base.c:880-886 (on_item_click_pressed / button=0).
        # Primary is left unclaimed -- activation stays on FlowBox's own
        # child-activated binding (_on_card_activated).
        click = Gtk.GestureClick()
        click.set_button(0)
        click.connect("pressed", self._ext._on_card_pressed, self._win, self)
        self.add_controller(click)

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
        icon.set_pixel_size(_disk_icon_size())
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
        icon.set_pixel_size(_disk_list_icon_size())
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


class MyComputerFolderCard(Gtk.Widget):
    """Self-contained card: renders one PreferredFolder as a grid card
    (native grid layout, mirroring NautilusGridCell) or a list row."""

    __gtype_name__ = "MyComputerFolderCard"

    def __init__(
        self,
        ext,
        win: Gtk.Window,
        view_mode: str,
        model,
        interactive: bool = True,
        reorderable: bool = True,
    ) -> None:
        super().__init__()
        self._ext = ext
        self._win = win
        self.view_mode = view_mode
        self.model = model
        self.icon: Gtk.Image | None = None
        self.name_label: Gtk.Label | None = None
        # Up to 3 caption lines below the name (Nautilus "Captions", grid
        # mode only -- list mode never creates them, so set_captions() is a
        # safe no-op there).
        self.caption_first: Gtk.Label | None = None
        self.caption_second: Gtk.Label | None = None
        self.caption_third: Gtk.Label | None = None
        # Only populated in list mode -- see _build_list/do_measure etc.
        self._list_box: Gtk.Box | None = None

        self.get_style_context().add_class("nautilus-view-cell")
        self._build()
        self._apply_hidden_state(model.is_hidden)

        # interactive=False skips right-click/drag-source wiring, for clones
        # that aren't draggable or right-clickable themselves (drag ghost,
        # reorder placeholder). reorderable=False additionally skips the
        # drop/motion controllers -- only the drag ghost needs that, as it
        # floats outside the FlowBox and is never a drop target. See
        # _build_drag_ghost / _build_reorder_placeholder.
        if interactive:
            # One gesture on all buttons, dispatched from "pressed", mirroring
            # nautilus-list-base.c:880-886 (on_item_click_pressed / button=0).
            # Primary is left unclaimed -- activation stays on FlowBox's own
            # child-activated binding (_on_card_activated).
            click = Gtk.GestureClick()
            click.set_button(0)
            click.connect("pressed", self._ext._on_card_pressed, self._win, self)
            self.add_controller(click)

            self._wire_drag()

        if reorderable:
            self._wire_reorder_preview()

    @property
    def is_list(self) -> bool:
        return self.view_mode == "list-view"

    @property
    def nav_uri(self) -> str:
        return self.model.nav_uri

    def _wire_reorder_preview(self) -> None:
        """Persist a FlowBox reorder only after a successful MOVE drop."""
        motion = Gtk.DropControllerMotion()
        motion.connect("enter", self._on_reorder_enter)
        self.add_controller(motion)

        drop = Gtk.DropTarget.new(MyComputerFolderCard, Gdk.DragAction.MOVE)
        drop.connect("drop", self._on_reorder_drop)
        self.add_controller(drop)

    def _on_reorder_drop(self, _target, value, _x, _y) -> bool:
        placeholder = getattr(self._ext, "_reorder_placeholder", None)
        # Without a placeholder the drag never set up (see _on_drag_begin);
        # committing here would skip `value` and drop it from the order.
        if placeholder is None:
            return False
        dst_child = self.get_parent()
        flow = dst_child.get_parent() if dst_child is not None else None
        if not isinstance(flow, Gtk.FlowBox):
            return False
        keys = []
        child = flow.get_first_child()
        while child is not None:
            card = child.get_child()
            if card is placeholder:
                keys.append(value.model.key)
            elif isinstance(card, MyComputerFolderCard) and card is not value:
                keys.append(card.model.key)
            child = child.get_next_sibling()
        _log(
            f"preferred folders dragging dropped: {value.model.display_name}/ "
            f"position {value.model.index}"
        )
        GLib.idle_add(self._ext._commit_preferred_order, keys)
        return True

    def _on_reorder_enter(self, _ctrl, _x, _y) -> None:
        """Move only the dimmed placeholder into the landing slot -- the real,
        stateful dragged card is never reparented mid-drag (see
        _on_drag_begin), so this can never lose/flicker its highlight the way
        reparenting the real FlowBoxChild wrapper on every crossing did."""
        if getattr(self._ext, "_folder_reordering", False):
            return
        placeholder = getattr(self._ext, "_reorder_placeholder", None)
        if placeholder is None or placeholder is self:
            return
        placeholder_child = placeholder.get_parent()
        dst_child = self.get_parent()
        if not isinstance(placeholder_child, Gtk.FlowBoxChild) or not isinstance(
            dst_child, Gtk.FlowBoxChild
        ):
            return
        flow = dst_child.get_parent()
        if not isinstance(flow, Gtk.FlowBox):
            return
        dst_index = dst_child.get_index()
        if placeholder_child.get_index() == dst_index:
            return

        self._ext._folder_reordering = True
        try:
            placeholder_child.set_child(None)
            flow.remove(placeholder_child)
            flow.insert(placeholder, dst_index)
            new_child = placeholder.get_parent()
            if isinstance(new_child, Gtk.FlowBoxChild):
                new_child.add_css_class("mc-selected")
        finally:
            self._ext._folder_reordering = False

    def _wire_drag(self) -> None:
        drag = Gtk.DragSource()
        drag.set_actions(Gdk.DragAction.MOVE)
        # CAPTURE, matching nautilus-list-base.c:1373 -- runs ahead of the
        # BUBBLE-phase click gesture above so a drag on primary press isn't
        # shadowed by it.
        drag.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        drag.connect("prepare", self._on_drag_prepare)
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-end", self._on_drag_end)
        drag.connect("drag-cancel", self._on_drag_cancel)
        self.add_controller(drag)

    def _on_drag_prepare(self, _source, _x, _y):
        return Gdk.ContentProvider.new_for_value(self)

    def _on_drag_begin(self, _source, drag) -> None:
        Gtk.DragIcon.get_for_drag(drag).set_child(self._build_drag_ghost())

        # Until the drop is confirmed, the drag is only a visual cue: hide
        # the real card's slot (it stays alive, untouched, off-layout) and
        # show a dimmed placeholder in its place. The placeholder -- not the
        # real card -- is what _on_reorder_enter moves between slots.
        src_wrapper = self.get_parent()
        placeholder = None
        if isinstance(src_wrapper, Gtk.FlowBoxChild):
            flow = src_wrapper.get_parent()
            if isinstance(flow, Gtk.FlowBox):
                src_index = src_wrapper.get_index()
                src_wrapper.set_visible(False)
                placeholder = self._build_reorder_placeholder()
                flow.insert(placeholder, src_index)
                new_child = placeholder.get_parent()
                if isinstance(new_child, Gtk.FlowBoxChild):
                    new_child.add_css_class("mc-selected")

        _log(
            f"preferred folders dragging started: {self.model.display_name}/ "
            f"position {self.model.index}"
        )
        self._ext._reorder_placeholder = placeholder
        self._ext._reorder_source_wrapper = src_wrapper

    def _on_drag_end(self, _source, _drag, _delete_data) -> None:
        placeholder = getattr(self._ext, "_reorder_placeholder", None)
        if placeholder is not None:
            placeholder_child = placeholder.get_parent()
            if isinstance(placeholder_child, Gtk.FlowBoxChild):
                flow = placeholder_child.get_parent()
                if isinstance(flow, Gtk.FlowBox):
                    placeholder_child.set_child(None)
                    flow.remove(placeholder_child)

        # Reveal the real card again. It was only hidden (never dimmed), so
        # nothing else needs restoring; a committed drop repopulates anyway.
        src_wrapper = getattr(self._ext, "_reorder_source_wrapper", None)
        if src_wrapper is not None:
            src_wrapper.set_visible(True)
            src_wrapper.remove_css_class("mc-selected")

        self._ext._reorder_placeholder = None
        self._ext._reorder_source_wrapper = None

    def _on_drag_cancel(self, _source, _drag, _reason) -> bool:
        self._on_drag_end(_source, _drag, False)
        return False

    def _build_clone(self, reorderable: bool, dim: bool) -> "MyComputerFolderCard":
        """Full-size, non-interactive clone of this card in the same view
        mode. interactive=False drops the right-click/drag-source wiring;
        `reorderable` and `dim` tailor it for its role (see the two callers)."""
        clone = MyComputerFolderCard(
            self._ext,
            self._win,
            self.view_mode,
            self.model,
            interactive=False,
            reorderable=reorderable,
        )
        clone.set_focusable(False)
        clone.set_captions([None, None, None])
        if dim:
            clone._set_content_opacity(0.55)
        return clone

    def _build_drag_ghost(self) -> "MyComputerFolderCard":
        """Clone that floats under the cursor via Gtk.DragIcon. It never sits
        in the FlowBox, so it needs no reorder controllers."""
        return self._build_clone(reorderable=False, dim=False)

    def _build_reorder_placeholder(self) -> "MyComputerFolderCard":
        """Dimmed stand-in shown in the FlowBox at the landing slot. It keeps
        the reorder drop/motion controllers so a drop landing directly on its
        own slot still fires _on_reorder_drop, but carries no drag source of
        its own -- only the real card is ever the drag source."""
        return self._build_clone(reorderable=True, dim=True)

    def do_measure(self, orientation, for_size):
        """Mirror NautilusGridCell's fixed-width, height-for-width measure
        (grid mode), or delegate straight through to the list row's box."""
        if self.is_list:
            return self._list_box.measure(orientation, for_size)

        icon_size = _nautilus_icon_size()
        width = _folder_card_width()
        if orientation == Gtk.Orientation.HORIZONTAL:
            labels_min, _, _, _ = self._labels_box.measure(orientation, -1)
            if labels_min > width:
                width = labels_min
            icon_min, _, _, _ = self.icon.measure(orientation, -1)
            if icon_min > icon_size:
                width += icon_min - icon_size
            emblems_min, _, _, _ = self._emblems_box.measure(orientation, -1)
            if emblems_min > 18:
                width += 2 * (emblems_min - 18)
            return (width, width, -1, -1)

        labels_min, labels_natural, min_baseline, nat_baseline = self._labels_box.measure(
            Gtk.Orientation.VERTICAL, width
        )
        if min_baseline != -1:
            min_baseline += icon_size + 6
        if nat_baseline != -1:
            nat_baseline += icon_size + 6
        return (
            icon_size + 6 + labels_min,
            icon_size + 6 + labels_natural,
            min_baseline,
            nat_baseline,
        )

    @staticmethod
    def _allocation_transform(x: int, y: int):
        return Gsk.Transform.new().translate(Graphene.Point().init(x, y))

    def do_size_allocate(self, width: int, height: int, baseline: int) -> None:
        """Allocate the icon, emblem gutter, and labels exactly as Nautilus
        does (grid mode), or the list row's box across the full cell."""
        if self.is_list:
            self._list_box.allocate(width, height, baseline, None)
            return

        icon_size = _nautilus_icon_size()
        self.icon.allocate(
            width - 36,
            icon_size,
            -1,
            self._allocation_transform(18, 0),
        )
        emblem_x = width - 18 if self.get_direction() == Gtk.TextDirection.LTR else 0
        self._emblems_box.allocate(
            18,
            icon_size,
            -1,
            self._allocation_transform(emblem_x, 0),
        )
        labels_baseline = baseline - icon_size - 6 if baseline != -1 else -1
        self._labels_box.allocate(
            width,
            max(0, height - icon_size - 6),
            labels_baseline,
            self._allocation_transform(0, icon_size + 6),
        )

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        if self.is_list:
            self.snapshot_child(self._list_box, snapshot)
            return
        for child in (self.icon, self._emblems_box, self._labels_box):
            self.snapshot_child(child, snapshot)

    def do_dispose(self) -> None:
        if self._list_box is not None:
            self._list_box.unparent()
        for child in (self.icon, self._emblems_box, self._labels_box):
            if child is not None:
                child.unparent()
        super().do_dispose()

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
            self._build_list()
        else:
            self._build_grid()

    def _build_list(self) -> None:
        """List-view compact cell: keep Preferred Folders multi-column (the
        section's FlowBox stays in grid layout -- see always_grid on its
        MyComputerCardSection) while rendering each card as a compact
        horizontal icon+name row instead of the full icon-grid cell."""
        pf = self.model
        self.set_valign(Gtk.Align.FILL)

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.set_margin_start(6)
        box.set_margin_end(6)
        box.set_margin_top(3)
        box.set_margin_bottom(3)
        box.set_halign(Gtk.Align.START)
        box.set_valign(Gtk.Align.CENTER)
        box.set_parent(self)

        icon = Gtk.Image()
        icon.set_pixel_size(_nautilus_list_icon_size())
        icon.set_valign(Gtk.Align.CENTER)
        self._set_icon(icon)
        box.append(icon)

        labels_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        labels_box.set_valign(Gtk.Align.CENTER)
        name_lbl = Gtk.Label(label=pf.display_name)
        name_lbl.set_xalign(0.0)
        name_lbl.set_valign(Gtk.Align.CENTER)
        name_lbl.set_max_width_chars(14)
        name_lbl.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        labels_box.append(name_lbl)
        box.append(labels_box)

        self._list_box = box
        self.icon = icon
        self.name_label = name_lbl

    def _build_grid(self) -> None:
        pf = self.model
        self.set_valign(Gtk.Align.START)

        icon = Gtk.Image()
        icon.set_pixel_size(_nautilus_icon_size())
        icon.set_halign(Gtk.Align.CENTER)
        icon.set_valign(Gtk.Align.CENTER)
        self._set_icon(icon)
        icon.set_parent(self)

        emblems_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        emblems_box.set_halign(Gtk.Align.END)
        emblems_box.set_margin_start(2)
        emblems_box.add_css_class("dim-label")
        emblems_box.set_parent(self)

        labels_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        labels_box.add_css_class("icon-ui-labels-box")
        labels_box.set_parent(self)

        name_lbl = Gtk.Label(label=pf.display_name)
        name_lbl.set_justify(Gtk.Justification.CENTER)
        name_lbl.set_halign(Gtk.Align.CENTER)
        name_lbl.set_wrap(True)
        name_lbl.set_wrap_mode(Pango.WrapMode.WORD)
        name_lbl.set_lines(3)
        name_lbl.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        attributes = Pango.AttrList()
        attributes.insert(Pango.attr_insert_hyphens_new(False))
        name_lbl.set_attributes(attributes)
        labels_box.append(name_lbl)

        # Up to 3 caption lines (Nautilus "Captions" feature, icon-view only).
        # Built empty/hidden -- set_captions() fills them in once resolved.
        # Style/wrap matches nautilus-grid-cell.c's caption_widget_new() exactly.
        caption_labels = []
        for _i in range(3):
            cap_lbl = Gtk.Label(label="")
            cap_lbl.get_style_context().add_class("caption")
            cap_lbl.get_style_context().add_class("dim-label")
            cap_lbl.set_justify(Gtk.Justification.CENTER)
            cap_lbl.set_halign(Gtk.Align.CENTER)
            cap_lbl.set_valign(Gtk.Align.START)
            cap_lbl.set_wrap(True)
            cap_lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            cap_lbl.set_lines(2)
            cap_lbl.set_ellipsize(Pango.EllipsizeMode.END)
            cap_lbl.set_visible(False)
            labels_box.append(cap_lbl)
            caption_labels.append(cap_lbl)

        self.icon = icon
        self._emblems_box = emblems_box
        self._labels_box = labels_box
        self.name_label = name_lbl
        self.caption_first, self.caption_second, self.caption_third = caption_labels

    def update_metadata(self, pf) -> None:
        """Patch the icon + name label in place; called once async metadata resolves."""
        self.model = pf
        # Full precedence (gicon -> icon_name -> "folder"), not just set_from_gicon:
        # a special place (recent/starred/network) whose custom icon was removed
        # comes back with gio_icon=None and must revert to its token icon_name
        # default, not keep the stale custom gicon (issue #83).
        if self.icon is not None:
            self._set_icon(self.icon)
        if self.name_label is not None:
            self.name_label.set_label(pf.display_name)
        self._apply_hidden_state(pf.is_hidden)

    def set_captions(self, lines: list) -> None:
        """Update the up-to-3 caption lines below the name (Nautilus
        "Captions"). lines[i] is None/empty to hide that line."""
        for label, text in zip(
            (self.caption_first, self.caption_second, self.caption_third), lines
        ):
            if label is None:
                continue
            if text:
                label.set_label(text)
                label.set_visible(True)
            else:
                label.set_visible(False)

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


class MyComputerToggleButton(Gtk.Box):
    """Dynamic N-way segmented toggle: flat Gtk.ToggleButtons in a light
    pill, with a 1px separator between adjacent buttons that hides whenever
    either neighbor is selected or hovered -- that neighbor's own highlight
    already reads as the boundary, so the divider would be redundant.

    Built from Gtk.Box/Gtk.ToggleButton/Gtk.Separator instead of
    Adw.ToggleGroup (libadwaita 1.7+ only), so it renders identically on
    GNOME 47 and 48+. See .mc-toggle-group/.mc-toggle-btn in
    my_computer_view._CSS for the pill/button styling.
    """

    __gtype_name__ = "MyComputerToggleButton"
    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_FIRST, None, (str,))}

    def __init__(self, segments, height: int = -1) -> None:
        """segments: iterable of (name, icon, tooltip_text), where icon is
        either an icon name (str) or a Gio.Icon (e.g. a bundled fallback SVG,
        see column_view._resolve_column_icon). height defaults to -1 (natural
        size, stretched to fill via valign=FILL below so it matches the
        containing toolbar's height); pass an explicit value to pin it
        instead."""
        # 1px spacing matches the separator's own stroke width, so the gap
        # between buttons stays visually constant whether the separator is
        # shown or hidden (opacity toggle in _update_separators).
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=1)
        self.add_css_class("mc-toggle-group")
        self.set_size_request(-1, height)
        self.set_valign(Gtk.Align.FILL)

        self._buttons: dict[str, Gtk.ToggleButton] = {}
        self._hovered: dict[str, bool] = {}
        self._separators: list[Gtk.Separator] = []  # gap i sits between order[i]/order[i+1]
        self._order: list[str] = []
        self._active_name: str | None = None
        self._syncing = False

        first_btn = None
        for i, (name, icon, tooltip) in enumerate(segments):
            if i > 0:
                sep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
                sep.add_css_class("mc-toggle-sep")
                sep.set_margin_top(6)
                sep.set_margin_bottom(6)
                self.append(sep)
                self._separators.append(sep)

            btn = Gtk.ToggleButton(child=self._make_segment_image(icon), tooltip_text=tooltip)
            btn.add_css_class("flat")
            btn.add_css_class("mc-toggle-btn")
            if first_btn is None:
                first_btn = btn
            else:
                btn.set_group(first_btn)
            btn.connect("toggled", self._on_button_toggled, name)

            motion = Gtk.EventControllerMotion()
            motion.connect("enter", self._on_button_enter, name)
            motion.connect("leave", self._on_button_leave, name)
            btn.add_controller(motion)

            self.append(btn)
            self._buttons[name] = btn
            self._hovered[name] = False
            self._order.append(name)

        self._update_separators()

    @staticmethod
    def _make_segment_image(icon) -> Gtk.Image:
        """icon: an icon name (str) or a Gio.Icon (e.g. a bundled fallback
        SVG, see column_view._resolve_column_icon)."""
        image = (
            Gtk.Image.new_from_gicon(icon)
            if isinstance(icon, Gio.Icon)
            else Gtk.Image.new_from_icon_name(icon)
        )
        image.set_pixel_size(-1)
        image.set_margin_start(8)
        image.set_margin_end(8)
        image.set_valign(Gtk.Align.CENTER)
        image.set_halign(Gtk.Align.CENTER)
        return image

    def _on_button_enter(self, _ctrl, _x, _y, name: str) -> None:
        self._hovered[name] = True
        self._update_separators()

    def _on_button_leave(self, _ctrl, name: str) -> None:
        self._hovered[name] = False
        self._update_separators()

    def _on_button_toggled(self, btn: Gtk.ToggleButton, name: str) -> None:
        if self._syncing or not btn.get_active():
            return
        self._active_name = name
        self._update_separators()
        self.emit("changed", name)

    def _update_separators(self) -> None:
        # Toggling a CSS class (not Widget.set_opacity()) is what lets the
        # 200ms transition on .mc-toggle-sep actually animate.
        for i, sep in enumerate(self._separators):
            left, right = self._order[i], self._order[i + 1]
            hidden = (
                self._active_name in (left, right)
                or self._hovered.get(left)
                or self._hovered.get(right)
            )
            if hidden:
                sep.add_css_class("mc-toggle-sep-hidden")
            else:
                sep.remove_css_class("mc-toggle-sep-hidden")

    def get_active_name(self) -> str | None:
        return self._active_name

    def set_active_name(self, name: str) -> None:
        btn = self._buttons.get(name)
        if btn is None or btn.get_active():
            return
        self._syncing = True
        try:
            btn.set_active(True)
        finally:
            self._syncing = False
        self._active_name = name
        self._update_separators()

    def set_segment_enabled(self, name: str, enabled: bool) -> None:
        btn = self._buttons.get(name)
        if btn is not None:
            btn.set_sensitive(enabled)

    def set_segment_icon(self, name: str, icon) -> None:
        """Re-resolve a segment's icon after construction (icon: name or
        Gio.Icon, same contract as __init__'s segments) -- e.g. when the
        active icon theme changes live, see
        column_view._refresh_column_icon_all_windows."""
        btn = self._buttons.get(name)
        if btn is not None:
            btn.set_child(self._make_segment_image(icon))


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
        minimum, natural, _min_bl, _nat_bl = Gtk.FlowBox.do_measure(self, orientation, for_size)
        # A FlowBox has no baseline. Chaining up through PyGObject hands the
        # baseline out-params back as 0 instead of leaving them at -1, and GTK
        # warns on any non -1 horizontal baseline.
        return minimum, natural, -1, -1

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
        if is_capped_grid:
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
        # GTK 4.16 (used by Nautilus 47) does not allocate spare horizontal space to
        # an expanding FlowBox whose halign is START, leaving the justified folder
        # flow one card wide while its height is measured for a full-width row, so
        # following sections overlap it. FILL gives the flow the width its
        # spacing/allocation code expects on 47 and newer alike (verified on this
        # machine's GTK 4.22 too, no regression from the previous START behavior).
        self.flow.set_halign(Gtk.Align.FILL)
        self.flow.set_valign(Gtk.Align.START)

        self.flow.connect("child-activated", ext._on_card_activated, win)
        self.flow.connect("selected-children-changed", ext._on_flow_selection_changed, win)
        ext._attach_flow_shortcuts(self.flow, win)

        self._query = ""
        self.flow.set_filter_func(self._filter_child)

        self.append(self.flow)

    def add_card(self, card: Gtk.Widget) -> None:
        self._size_group.add_widget(card)
        self.flow.append(card)

    def _filter_child(self, child: Gtk.FlowBoxChild) -> bool:
        inner = child.get_child()
        model = getattr(inner, "model", None)
        if model is None:
            # Not a card (e.g. the drag-reorder placeholder) -- always show it.
            return True
        # Issue #115: a disk whose mount root is hidden (dot-prefixed mountpoint,
        # .hidden entry) follows Nautilus' Show Hidden Files. Folder cards are
        # exempt: the user pinned those deliberately, as with sidebar bookmarks.
        if isinstance(inner, MyComputerDiskCard) and model.is_hidden:
            if not self._ext._nautilus_prefs.hidden_files():
                return False
        if not self._query:
            return True
        display_name = getattr(model, "display_name", None)
        return display_name is not None and self._query in display_name.lower()

    def set_query(self, query: str) -> None:
        """Filter this section's cards by `query` (case-insensitive substring
        of the card's display name). Self-hides the whole section (heading
        included) when nothing matches, so an empty group never lingers."""
        self._query = query.strip().lower()
        self.refresh_filter()

    def refresh_filter(self) -> None:
        """Re-evaluate the filter without changing the query (e.g. after cards
        are (re)built, or the Show Hidden Files preference changes). Self-hides
        the whole section (heading included) when nothing matches."""
        self.flow.invalidate_filter()
        child = self.flow.get_first_child()
        any_match = False
        while child is not None:
            if self._filter_child(child):
                any_match = True
                break
            child = child.get_next_sibling()
        self.set_visible(any_match)


class MyComputerColumnRow(Gtk.ListBoxRow):
    """One entry in a Column View column: icon, name, and a trailing chevron
    for folders (visual affordance that activating the row opens a child
    column). Holds the plain file attributes the column/orchestration layer
    needs -- no MountInfo/PreferredFolder model, this is native filesystem
    browsing, not a disk/folder-shortcut card."""

    __gtype_name__ = "MyComputerColumnRow"

    def __init__(
        self,
        uri: str,
        display_name: str,
        is_dir: bool,
        gio_icon=None,
        is_hidden: bool = False,
        content_type: str | None = None,
        mtime: int = 0,
        cancellable: Gio.Cancellable | None = None,
    ) -> None:
        super().__init__()
        self.uri = uri
        self.display_name = display_name
        self.is_dir = is_dir
        self.content_type = content_type
        self._is_cut = False

        # No manual margin here -- .navigation-sidebar > row already carries
        # its own native inset (padding: 0 9px, margin-top: 3px between rows;
        # see ~/Downloads/nautilus/src/resources/style.css and gtk.css). A box
        # margin on top of that native row padding double-inset the content.
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=_COLUMN_ROW_SPACING)
        # box.get_style_context().add_class("nautilus-view-cell")
        box.add_css_class("mc-column-row-content")
        box.set_hexpand(True)
        box.set_vexpand(True)
        self.add_css_class("mc-column-row")

        self._is_hidden = is_hidden

        # Fixed 24x24 slot: the icon column's stable footprint, so every row's
        # label starts at the same x regardless of what's drawn inside (themed
        # icon or an aspect-preserving thumbnail -- see
        # _set_row_thumbnail_texture). Icon/thumbnail center inside it both
        # ways; slot itself is not visible (no background/border of its own).
        icon_slot = Gtk.Box()
        icon_slot.set_size_request(_COLUMN_ROW_ICON_SIZE, _COLUMN_ROW_ICON_SIZE)
        icon_slot.set_valign(Gtk.Align.CENTER)
        # A container that never sets hexpand explicitly inherits expand=True
        # from any child that has it -- the thumbnail Picture needs hexpand on
        # itself to center within this slot (see _set_row_thumbnail_texture),
        # which would otherwise propagate up and make this whole slot compete
        # with the name label for the row's leftover width. Pinning it False
        # here blocks that propagation at the slot's boundary.
        icon_slot.set_hexpand(False)

        icon = Gtk.Image()
        icon.set_halign(Gtk.Align.CENTER)
        icon.set_valign(Gtk.Align.CENTER)

        # gio_icon is already the fully-resolved icon by the time it gets here (custom
        # icon if the caller found one, else GIO's own real icon for the path -- see
        # _populate_rows) -- _set_regular_icon (not a plain set_from_icon_name +
        # set_pixel_size) forces the full-color variant: at this small 24px size GTK
        # would otherwise auto-select a monochrome/symbolic-looking fixed-size theme
        # variant on some themes. See common._set_regular_icon.
        if _gicon_renders(gio_icon):
            _set_regular_icon(icon, _COLUMN_ROW_ICON_SIZE, gicon=gio_icon)
        else:
            _set_regular_icon(
                icon, _COLUMN_ROW_ICON_SIZE, icon_name=("folder" if is_dir else "text-x-generic")
            )
        # Same class/opacity Nautilus's own grid/list cells use to dim hidden
        # entries (nautilus-grid-cell.c, nautilus-name-cell.c), applied only to
        # the icon -- not the label or the whole row -- to match native exactly.
        if is_hidden:
            icon.add_css_class("hidden-file")
        self._icon = icon

        # Normal state has its own stable pages too: a themed icon is shown
        # immediately, then a completed thumbnail simply becomes the other
        # page. The async thumbnail path consequently never mutates the row's
        # widget tree after construction.
        thumbnail = Gtk.Picture()
        thumbnail.set_hexpand(True)
        thumbnail.set_halign(Gtk.Align.CENTER)
        thumbnail.set_valign(Gtk.Align.CENTER)
        thumbnail.set_content_fit(Gtk.ContentFit.SCALE_DOWN)
        thumbnail.set_overflow(Gtk.Overflow.HIDDEN)
        thumbnail.add_css_class("mc-row-thumbnail")
        if is_hidden:
            thumbnail.add_css_class("hidden-file")
        self._thumbnail = thumbnail

        regular_stack = Gtk.Stack()
        regular_stack.set_transition_type(Gtk.StackTransitionType.NONE)
        regular_stack.set_size_request(_COLUMN_ROW_ICON_SIZE, _COLUMN_ROW_ICON_SIZE)
        regular_stack.add_named(icon, "icon")
        regular_stack.add_named(thumbnail, "thumbnail")
        regular_stack.set_visible_child_name("icon")
        icon_slot.append(regular_stack)
        self._regular_stack = regular_stack

        # Match NautilusNameCell's two-state visual ownership: the regular
        # visual (itself icon-or-thumbnail) and the cut glyph occupy the same
        # fixed-size bounds. set_cut() only switches the outer page.
        cut_slot = Gtk.Box()
        cut_slot.set_size_request(_COLUMN_ROW_ICON_SIZE, _COLUMN_ROW_ICON_SIZE)
        cut_slot.set_halign(Gtk.Align.CENTER)
        cut_slot.set_valign(Gtk.Align.CENTER)
        cut_slot.set_homogeneous(True)
        cut_icon = Gtk.Image()
        cut_icon.set_from_resource(
            "/org/gnome/nautilus/icons/scalable/actions/cut-large-symbolic.svg"
        )
        cut_icon.set_pixel_size(16)
        cut_icon.set_size_request(16, 16)
        cut_icon.set_halign(Gtk.Align.CENTER)
        cut_icon.set_valign(Gtk.Align.CENTER)
        cut_icon.set_opacity(0.7)
        cut_slot.append(cut_icon)
        self._cut_icon = cut_icon

        icon_stack = Gtk.Stack()
        icon_stack.set_transition_type(Gtk.StackTransitionType.NONE)
        icon_stack.set_size_request(_COLUMN_ROW_ICON_SIZE, _COLUMN_ROW_ICON_SIZE)
        icon_stack.set_halign(Gtk.Align.CENTER)
        icon_stack.set_valign(Gtk.Align.CENTER)
        icon_stack.add_named(icon_slot, "regular")
        icon_stack.add_named(cut_slot, "cut")
        icon_stack.set_visible_child_name("regular")
        box.append(icon_stack)
        self._icon_stack = icon_stack

        # macOS Finder-style: files show a real image/document thumbnail
        # instead of the generic icon when one is available. Same
        # GnomeDesktop.DesktopThumbnailFactory engine as the preview column
        # (see MyComputerPreviewColumn) -- a cached thumbnail is read inline
        # (cheap, already-scaled file read), a miss is generated on a daemon
        # thread and swapped in once ready, never on the main loop. Folders
        # never go through this: their icon is already the real folder icon.
        if not is_dir and content_type and _thumb_factory is not None and cancellable is not None:
            self._load_row_thumbnail(content_type, mtime, cancellable)

        name_lbl = Gtk.Label(label=display_name)
        name_lbl.set_xalign(0.0)
        name_lbl.set_hexpand(True)
        name_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        # Without a max-width-chars cap, an ellipsized label's NATURAL width is
        # still the full unellipsized text extent -- a single long filename
        # then dictates this row's (and thus the whole column's, and the
        # whole Paned chain's) natural width, silently overriding the
        # Paned's shrink_start_child(False) floor and _COLUMN_MIN/MAX_WIDTH
        # clamp (same class of bug as the recipe's wrapped-button-label
        # gotcha, just in a label instead of a button). max_width_chars(1)
        # + hexpand=True is the standard idiom: natural request shrinks to
        # near-zero, but it still fills the row via hexpand at allocation time.
        name_lbl.set_max_width_chars(1)
        box.append(name_lbl)

        if is_dir:
            chevron = Gtk.Image.new_from_icon_name("go-next-symbolic")
            chevron.set_pixel_size(12)
            chevron.get_style_context().add_class("dim-label")
            box.append(chevron)

        self.set_child(box)

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        """Draw the complete cut treatment in the padded row's bounds."""
        if not self._is_cut:
            Gtk.ListBoxRow.do_snapshot(self, snapshot)
            return

        width = float(self.get_width())
        height = float(self.get_height())
        # A 2px stroke centered 1px from the allocation edge lands flush
        # with the row surface instead of floating inside it.
        inset = 1.0
        if width <= inset * 2 or height <= inset * 2:
            Gtk.ListBoxRow.do_snapshot(self, snapshot)
            return

        bounds = Graphene.Rect()
        bounds.init(0.0, 0.0, width, height)
        color = self.get_color()

        # The background is below GTK's normal row snapshot so native hover
        # and selection layers retain their expected stacking order.
        cr = snapshot.append_cairo(bounds)
        cr.set_source_rgba(color.red, color.green, color.blue, color.alpha * 0.07)
        # Libadwaita's .navigation-sidebar > row rule supplies the visible
        # row radius (9px). Keep this snapshot path in sync with that native
        # rule because GTK does not expose a computed CSS radius to Python.
        self._append_rounded_rectangle(cr, 0.0, 0.0, width, height, 9.0)
        cr.fill()
        del cr

        Gtk.ListBoxRow.do_snapshot(self, snapshot)

        cr = snapshot.append_cairo(bounds)
        opacity = 0.5 if Adw.StyleManager.get_default().get_high_contrast() else 0.15
        cr.set_source_rgba(color.red, color.green, color.blue, color.alpha * opacity)
        cr.set_line_width(2.0)
        cr.set_dash([4.0, 3.0])
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        self._append_rounded_rectangle(
            cr, inset, inset, width - inset * 2.0, height - inset * 2.0, 9.0
        )
        cr.stroke()
        del cr

    @staticmethod
    def _append_rounded_rectangle(
        cr: cairo.Context, x: float, y: float, width: float, height: float, radius: float
    ) -> None:
        """Append a rounded rectangle path to a Cairo context."""
        left = x
        top = y
        right = x + width
        bottom = y + height
        radius = min(radius, width / 2.0, height / 2.0)
        cr.move_to(left + radius, top)
        cr.line_to(right - radius, top)
        cr.arc(right - radius, top + radius, radius, -math.pi / 2.0, 0.0)
        cr.line_to(right, bottom - radius)
        cr.arc(right - radius, bottom - radius, radius, 0.0, math.pi / 2.0)
        cr.line_to(left + radius, bottom)
        cr.arc(left + radius, bottom - radius, radius, math.pi / 2.0, math.pi)
        cr.line_to(left, top + radius)
        cr.arc(left + radius, top + radius, radius, math.pi, math.pi * 1.5)
        cr.close_path()

    def set_cut(self, cut: bool) -> None:
        """Switch between the row-owned regular and cut visual pages."""
        if self._is_cut == cut:
            return
        self._is_cut = cut
        if cut:
            self.add_css_class("mc-row-cut")
        else:
            self.remove_css_class("mc-row-cut")
        self._icon_stack.set_visible_child_name("cut" if cut else "regular")
        self.queue_draw()

    def set_thumbnail(self, texture: Gdk.Texture) -> None:
        """Replace the regular icon page with a finished thumbnail."""
        self._thumbnail.set_paintable(texture)
        self._regular_stack.set_visible_child_name("thumbnail")

    @staticmethod
    def _load_thumbnail_texture(path: str) -> Gdk.Texture | None:
        """Decode one cached thumbnail at this row's fixed visual size."""
        try:
            # Scale during decode: gdk-pixbuf preserves the aspect ratio and
            # never enlarges a smaller source, so the row receives an already
            # fitted paintable.
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                path, _COLUMN_ROW_ICON_SIZE, _COLUMN_ROW_ICON_SIZE, True
            )
        except GLib.Error:
            return None
        return Gdk.Texture.new_for_pixbuf(pixbuf)

    def _load_row_thumbnail(
        self, content_type: str, mtime: int, cancellable: Gio.Cancellable
    ) -> None:
        # Nautilus-style icon-then-thumbnail: the row already shows its plain
        # icon (set just above, in __init__) the instant it's built. Both the
        # cache lookup AND the fallback generation run entirely on a daemon
        # thread -- previously the lookup ran inline on the main thread during
        # row construction, and with many files in a folder that serial chain
        # of stat/hash checks delayed the whole column's first paint (it
        # looked like "blank, then all rows appear at once"). Now every row
        # renders immediately and its thumbnail (if any) pops in on its own,
        # independent thread as soon as that one file's lookup/generation
        # finishes -- one by one, never blocking the others.
        threading.Thread(
            target=self._row_thumbnail_worker,
            args=(self.uri, content_type, mtime, cancellable),
            daemon=True,
        ).start()

    def _row_thumbnail_worker(
        self, uri: str, content_type: str, mtime: int, cancellable: Gio.Cancellable
    ) -> None:
        if cancellable.is_cancelled():
            return
        cached = _thumb_factory.lookup(uri, mtime)
        if cached:
            texture = self._load_thumbnail_texture(cached)
            if texture is None:
                return
            if cancellable.is_cancelled():
                return
            GLib.idle_add(self._set_row_thumbnail_texture, texture, cancellable)
            return
        if _thumb_factory.has_valid_failed_thumbnail(uri, mtime):
            return
        if not _thumb_factory.can_thumbnail(uri, content_type, mtime):
            return
        # Generation shells out to a system thumbnailer subprocess per file --
        # bounded so a folder with hundreds of un-cached images doesn't spawn
        # hundreds of them at once. Cache lookups above are unthrottled (cheap
        # stat/read), so repeat folder visits stay instant regardless.
        with _ROW_THUMBNAIL_SEMAPHORE:
            if cancellable.is_cancelled():
                return
            try:
                pixbuf = _thumb_factory.generate_thumbnail(uri, content_type, cancellable)
            except GLib.Error:
                pixbuf = None
        if cancellable.is_cancelled():
            return
        if pixbuf is None:
            try:
                _thumb_factory.create_failed_thumbnail(uri, mtime, cancellable)
            except GLib.Error:
                pass
            return
        try:
            _thumb_factory.save_thumbnail(pixbuf, uri, mtime, cancellable)
        except GLib.Error:
            return
        cached = _thumb_factory.lookup(uri, mtime)
        if not cached or cancellable.is_cancelled():
            return
        texture = self._load_thumbnail_texture(cached)
        if texture is None:
            return
        GLib.idle_add(self._set_row_thumbnail_texture, texture, cancellable)

    def _set_row_thumbnail_texture(self, texture: Gdk.Texture, cancellable: Gio.Cancellable) -> int:
        if cancellable.is_cancelled():
            return GLib.SOURCE_REMOVE
        # The thumbnail page was fully constructed with the row. Swapping its
        # paintable through set_thumbnail() preserves a fixed, predictable
        # widget tree while retaining the same SCALE_DOWN centering behavior.
        self.set_thumbnail(texture)
        return GLib.SOURCE_REMOVE


@dataclasses.dataclass(frozen=True, slots=True)
class _ColumnEntry:
    """One row's sort-relevant attributes, built once per enumerate in
    MyComputerColumn._populate_rows. `size` holds byte size for files, item
    count for directories (see MyComputerColumn._maybe_count_dirs_then_populate) --
    matches native Nautilus's own compare_by_size, which never compares a
    folder's on-disk byte size."""

    is_dir: bool
    sort_key: str
    sort_last: bool
    name: str
    display_name: str
    icon: Gio.Icon | None
    content_type: str | None
    is_hidden: bool
    size: int
    mtime: int
    btime: int
    atime: int


def _name_tiebreak(e: _ColumnEntry) -> tuple:
    """Nautilus's compare_by_full_path is the tiebreak for every criterion
    (and *is* the entire comparison for name sort), and resolves through
    compare_by_display_name: names starting with "." or "#" sort last
    (SORT_LAST_CHAR1/2, nautilus-file.c), otherwise by filename collation
    key -- g_utf8_collate_key_for_filename, natural-numeric and locale
    aware, not a plain lowercase compare (img2.png vs img10.png sorted
    "wrong" under str.lower(), right here)."""
    return (e.sort_last, e.sort_key)


# Transcribed from nautilus-file.c's mime_type_map -- the coarse basic-type
# string Nautilus's Type sort groups files by (Program, Audio, Image, ...).
_BASIC_TYPE_BY_GENERIC_ICON = {
    "application-x-executable": N_("Program"),
    "audio-x-generic": N_("Audio"),
    "font-x-generic": N_("Font"),
    "image-x-generic": N_("Image"),
    "package-x-generic": N_("Archive"),
    "text-html": N_("Markup"),
    "text-x-generic": N_("Text"),
    "text-x-generic-template": N_("Text"),
    "text-x-script": N_("Program"),
    "video-x-generic": N_("Video"),
    "x-office-address-book": N_("Contacts"),
    "x-office-calendar": N_("Calendar"),
    "x-office-document": N_("Document"),
    "x-office-presentation": N_("Presentation"),
    "x-office-spreadsheet": N_("Spreadsheet"),
}


def _basic_type_string(content_type: str | None) -> str:
    # get_basic_type_for_mime_type: map the content type to its generic
    # themed-icon name, then to Nautilus's coarse type label. These strings
    # are Nautilus's own and already in its gettext catalog (14/14 locales),
    # so _native() rather than _() per the #120 convention -- and native
    # collates the *translated* string, so comparing translated strings here
    # is what reproduces native's actual on-screen order.
    if content_type is None:
        return _native("Other")
    generic_icon = Gio.content_type_get_generic_icon_name(content_type)
    key = _BASIC_TYPE_BY_GENERIC_ICON.get(generic_icon or "")
    return _native(key) if key else _native("Other")


def _type_key(e: _ColumnEntry) -> tuple:
    # compare_by_type: directories always first and tied with each other (a
    # folder carries no type string) -- unconditional on the dirs-first pref,
    # same as size (see _size_key in the next commit). Among files: coarse
    # basic-type string, collated, then the raw mime type, then the same
    # tiebreak as every other criterion. No hidden bucket here either -- see
    # _name_tiebreak; the old is-hidden-based 4-bucket model was never what
    # nautilus-file.c's compare_by_type actually does.
    if e.is_dir:
        return (0, "", "", *_name_tiebreak(e))
    return (1, _basic_type_string(e.content_type), e.content_type or "", *_name_tiebreak(e))


def _size_key(e: _ColumnEntry) -> tuple:
    # compare_by_size: directories always first (compared by item count),
    # files after (compared by byte size) -- baked into the criterion
    # itself, unconditional on the "Sort Folders Before Files" pref, same as
    # type. Because this bucketing is part of the criterion's own result
    # (not a separately-pinned pass), reverse= DOES flip it, same as native:
    # "if (reversed) result = -result;" negates the whole per-criterion
    # result, dir/file split included -- there is no fixed-bucket exception
    # here despite what an earlier version of this code assumed.
    return (0 if e.is_dir else 1, e.size, *_name_tiebreak(e))


# Each criterion's key, derived directly from nautilus_file_compare_for_sort
# in nautilus-file.c. A plain `entries.sort(key=..., reverse=)` reproduces
# native's "if (reversed) result = -result" exactly, because that negates
# the *whole* per-criterion result, tiebreak and any criterion-local
# bucketing (size/type's dir-first split) included -- which is also why the
# separate "Sort Folders Before Files" pref, once wired in, has to be its
# own post-pass in _populate_rows rather than living inside one of these
# keys: unlike these, that pinned bucket is applied *before* reversed is
# ever considered (nautilus_file_compare_for_sort_internal's
# directories_first check returns its -1/+1 directly, never through the
# reversed branch).
_SORT_KEY_BUILDERS = {
    # compare_by_display_name IS the whole comparison for name sort: no
    # is-hidden bucket, only the sort-last (. or #) rule inside the tiebreak.
    "name": _name_tiebreak,
    # Flat pool, no buckets -- folders/files/hidden all mixed, sorted purely
    # by timestamp, tiebreak for equal timestamps (matches native's
    # compare_by_time -> compare_by_full_path chain; btime especially needs
    # this, since time::created is 0/equal for many files).
    "mtime": lambda e: (e.mtime, *_name_tiebreak(e)),
    "btime": lambda e: (e.btime, *_name_tiebreak(e)),
    "atime": lambda e: (e.atime, *_name_tiebreak(e)),
    "size": _size_key,
    "type": _type_key,
}


class MyComputerColumn(Gtk.ScrolledWindow):
    """One fixed-width Miller column: lists the contents of a single folder.

    Enumeration is always async (never Gio.File.enumerate_children /
    query_info sync variants) per this project's async-only rule for anything
    that can touch the filesystem/network from an event handler.
    """

    __gtype_name__ = "MyComputerColumn"

    def __init__(
        self,
        ext,
        folder_uri: str,
        on_row_activated,
        on_loaded=None,
        sort: tuple[str, bool] = ("name", False),
    ) -> None:
        super().__init__()
        self._ext = ext
        self.folder_uri = folder_uri
        self._on_row_activated = on_row_activated
        self._on_loaded = on_loaded
        self._sort = sort
        self._cancellable = Gio.Cancellable()
        # Keyboard navigation is a cursor, not a change to the committed
        # Gtk.ListBox selection. It is rendered with GTK's :active state so
        # the selected path and the arrow-key target can coexist.
        self._keyboard_active_row: MyComputerColumnRow | None = None
        # Manual double-click detection for opening file rows (see
        # _on_row_activated_internal): a raw GestureClick on the row can't be
        # used for this because every activation rebuilds the paned chain
        # (column_view.py's _rebuild_chain), which resets GTK's own
        # press-count tracking on the row before a second click can land.
        self._last_activated_uri: str | None = None
        self._last_activated_time: int = 0
        # Single source of truth for this column's width (column_view.py's
        # _on_paned_position_changed writes here on drag; _make_paned_chain
        # reads it to set the enclosing Gtk.Paned's initial position). A
        # freshly-created column starts at the default; _on_real_row_activated/
        # sync_to_uri overwrite it before append when a replaced column's
        # dragged width should carry over.
        self.width: float = _COLUMN_WIDTH

        self.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        # size_request is only this column's enforced floor (_COLUMN_MIN_WIDTH).
        # shrink_start_child(False) on the enclosing paned is what actually
        # enforces this floor at drag time.
        self.set_size_request(_COLUMN_MIN_WIDTH, -1)
        self.set_vexpand(True)
        # Clip contents to the column's own bounds so a mid-drag width change
        # can't paint list rows past the column edge into the neighbour.
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self.add_css_class("mc-column")

        self.list_box = Gtk.ListBox()
        # Same style class the native Nautilus sidebar (a Gtk.ListBox) carries:
        # gives rows the sidebar's rounded-corner selection shape and
        # theme-aware hover highlight. Its native :selected fill is a neutral
        # grey (sidebar convention, not accent) -- .mc-column-list below
        # re-tints just the selected state to accent (see _CSS in main.py),
        # keeping the sidebar's shape/hover but with content-view-style
        # selection color, since this is a Miller *browsing* view, not a
        # places sidebar.
        self.list_box.add_css_class("navigation-sidebar")
        self.list_box.add_css_class("mc-column-list")
        self.list_box.set_selection_mode(Gtk.SelectionMode.SINGLE)
        # Column view always activates on single click, regardless of the
        # Nautilus double-click setting (ext._nautilus_prefs.click_policy) that the cards use
        # -- Miller columns read naturally as single-click-to-drill-down. A
        # future Column View settings tab may make this configurable; for now
        # it's fixed.
        self.list_box.set_activate_on_single_click(True)
        self.list_box.connect("row-activated", self._on_row_activated_internal)
        # Matches Nautilus's own empty-folder state (nautilus-files-view.c
        # update_empty_view -- AdwStatusPage, "folder-symbolic" icon, "Folder
        # is Empty" title, no description). .compact keeps it readable at
        # column width. Not installed as the GtkListBox placeholder yet --
        # GtkListBox shows its placeholder natively the instant it has zero
        # rows, which is true of every column for the whole async-enumerate
        # window, so setting it up front flashes "Folder is Empty" before a
        # non-empty folder's rows land. _populate_rows installs it only once
        # loading has actually finished and the folder is confirmed empty.
        self._empty_page = Adw.StatusPage()
        self._empty_page.set_icon_name("folder-symbolic")
        self._empty_page.set_title(_native("Folder is Empty"))
        self._empty_page.add_css_class("compact")
        self.set_child(self.list_box)

        self._load()

    def set_sort(self, sort: tuple[str, bool, bool]) -> None:
        """Update this column's sort and reload. Always reloads regardless of
        whether sort actually changed: this is only ever called from
        refresh_column_view, which also needs it to pick up other prefs
        changes (hidden-files) that reload() alone would apply on its own --
        an early-return here when sort is unchanged would skip that too."""
        self._sort = sort
        self.reload()

    def reload(self) -> None:
        """Re-enumerate this column's own folder in place (e.g. after the
        hidden-files setting changes), without touching sibling columns or
        collapsing the Miller chain."""
        self._cancellable.cancel()
        self._cancellable = Gio.Cancellable()
        self.clear_active_row()
        self.list_box.set_placeholder(None)
        child = self.list_box.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self.list_box.remove(child)
            child = next_child
        self._load()

    def _load(self) -> None:
        gfile = Gio.File.new_for_uri(self.folder_uri)
        gfile.enumerate_children_async(
            "standard::name,standard::display-name,standard::icon,"
            "standard::is-hidden,standard::is-backup,standard::type,standard::content-type,"
            "standard::size,time::modified,time::created,time::access,"
            "metadata::custom-icon,metadata::custom-icon-name",
            Gio.FileQueryInfoFlags.NONE,
            GLib.PRIORITY_DEFAULT,
            self._cancellable,
            self._on_enumerator_ready,
        )

    def _on_enumerator_ready(self, gfile: Gio.File, result: Gio.AsyncResult) -> None:
        try:
            enumerator = gfile.enumerate_children_finish(result)
        except GLib.Error:
            return
        enumerator.next_files_async(
            200, GLib.PRIORITY_DEFAULT, self._cancellable, self._on_next_files_ready, []
        )

    def _on_next_files_ready(
        self, enumerator: Gio.FileEnumerator, result: Gio.AsyncResult, collected: list
    ) -> None:
        try:
            infos = enumerator.next_files_finish(result)
        except GLib.Error:
            return
        if infos:
            collected.extend(infos)
            enumerator.next_files_async(
                200, GLib.PRIORITY_DEFAULT, self._cancellable, self._on_next_files_ready, collected
            )
            return
        self._maybe_count_dirs_then_populate(collected)

    def _maybe_count_dirs_then_populate(self, infos: list) -> None:
        # Directory item counts are needed for sorting by size (native
        # Nautilus: a folder's "size" is its item count, never its on-disk
        # byte size -- see compare_by_size in nautilus-file.c). Always count
        # regardless of the active sort mode, since sort can change later.
        dir_names = [
            info.get_name() for info in infos if info.get_file_type() == Gio.FileType.DIRECTORY
        ]
        if not dir_names:
            self._populate_rows(infos, {})
            return
        dir_counts: dict[str, int] = {}
        pending = len(dir_names)

        def on_one_done() -> None:
            nonlocal pending
            pending -= 1
            if pending == 0:
                self._populate_rows(infos, dir_counts)

        base = Gio.File.new_for_uri(self.folder_uri)
        for name in dir_names:
            self._count_dir_items(base.get_child(name), name, dir_counts, on_one_done)

    def _count_dir_items(self, gfile: Gio.File, name: str, dir_counts: dict, on_done) -> None:
        # Mirrors Nautilus's own "shallow count" job (nautilus-directory-async.c
        # count_children_callback): a minimal enumeration, only the 3
        # attributes needed to filter hidden/backup entries, nothing else.
        gfile.enumerate_children_async(
            "standard::name,standard::is-hidden,standard::is-backup",
            Gio.FileQueryInfoFlags.NONE,
            GLib.PRIORITY_LOW,
            self._cancellable,
            self._on_dir_count_enumerator_ready,
            name,
            dir_counts,
            on_done,
        )

    def _on_dir_count_enumerator_ready(
        self, gfile: Gio.File, result: Gio.AsyncResult, name: str, dir_counts: dict, on_done
    ) -> None:
        try:
            enumerator = gfile.enumerate_children_finish(result)
        except GLib.Error:
            dir_counts[name] = 0
            on_done()
            return
        enumerator.next_files_async(
            200,
            GLib.PRIORITY_LOW,
            self._cancellable,
            self._on_dir_count_next_files,
            name,
            dir_counts,
            on_done,
            0,
        )

    def _on_dir_count_next_files(
        self,
        enumerator: Gio.FileEnumerator,
        result: Gio.AsyncResult,
        name: str,
        dir_counts: dict,
        on_done,
        running_count: int,
    ) -> None:
        try:
            infos = enumerator.next_files_finish(result)
        except GLib.Error:
            dir_counts[name] = running_count
            on_done()
            return
        if infos:
            show_hidden = self._ext._nautilus_prefs.hidden_files()
            for info in infos:
                is_hidden = info.get_attribute_boolean(
                    "standard::is-hidden"
                ) or info.get_attribute_boolean("standard::is-backup")
                if show_hidden or not is_hidden:
                    running_count += 1
            enumerator.next_files_async(
                200,
                GLib.PRIORITY_LOW,
                self._cancellable,
                self._on_dir_count_next_files,
                name,
                dir_counts,
                on_done,
                running_count,
            )
            return
        dir_counts[name] = running_count
        on_done()

    def _populate_rows(self, infos: list, dir_counts: dict) -> None:
        show_hidden = self._ext._nautilus_prefs.hidden_files()
        entries = []
        for info in infos:
            # nautilus_file_should_show / update_info_and_name (nautilus-file.c):
            # a backup file (name~) is treated as hidden even though
            # standard::is-hidden is false for it -- matches
            # MyComputerColumn._on_dir_count_next_files, which already reads
            # both attributes for its item counts.
            is_hidden = info.get_attribute_boolean(
                "standard::is-hidden"
            ) or info.get_attribute_boolean("standard::is-backup")
            if not show_hidden and is_hidden:
                continue
            name = info.get_name()
            is_dir = info.get_file_type() == Gio.FileType.DIRECTORY
            size = (
                dir_counts.get(name, 0) if is_dir else info.get_attribute_uint64("standard::size")
            )
            display_name = info.get_display_name() or name
            entries.append(
                _ColumnEntry(
                    is_dir=is_dir,
                    sort_key=GLib.utf8_collate_key_for_filename(display_name, -1),
                    sort_last=display_name[:1] in (".", "#"),
                    name=name,
                    display_name=display_name,
                    icon=_resolve_custom_gicon(info) or info.get_icon(),
                    content_type=info.get_content_type(),
                    is_hidden=is_hidden,
                    size=size,
                    mtime=info.get_attribute_uint64("time::modified"),
                    btime=info.get_attribute_uint64("time::created"),
                    atime=info.get_attribute_uint64("time::access"),
                )
            )

        col, reverse = self._sort
        key_fn = _SORT_KEY_BUILDERS.get(col, _SORT_KEY_BUILDERS["name"])
        entries.sort(key=key_fn, reverse=reverse)

        if self._ext._nautilus_prefs.sort_directories_first():
            # Outer, pinned grouping applied after the criterion sort --
            # Python's sort is stable, so within-bucket order (reverse
            # included) survives unchanged. Never flipped by reverse: unlike
            # every key above, compare_for_sort_internal's directories_first
            # check returns its -1/+1 directly, before the function ever
            # reaches the "if (reversed) result = -result" branch.
            entries.sort(key=lambda e: not e.is_dir)

        base = Gio.File.new_for_uri(self.folder_uri)
        for entry in entries:
            child_uri = base.get_child(entry.name).get_uri()
            row = MyComputerColumnRow(
                child_uri,
                entry.display_name,
                entry.is_dir,
                entry.icon,
                entry.is_hidden,
                content_type=entry.content_type,
                mtime=entry.mtime,
                cancellable=self._cancellable,
            )
            self.list_box.append(row)

        if not entries:
            self.list_box.set_placeholder(self._empty_page)

        if callable(self._on_loaded):
            self._on_loaded(self)

    def select_child_for_uri(self, uri: str) -> None:
        """Pre-select (highlight, without activating) the row whose child URI
        matches uri -- used to show which entry leads to the next column when
        seeding the view from the current location's ancestor chain."""
        norm = uri.rstrip("/")
        row = self.list_box.get_first_child()
        while row is not None:
            if isinstance(row, MyComputerColumnRow) and row.uri.rstrip("/") == norm:
                set_row_selected(row, True)
                return
            row = row.get_next_sibling()

    def clear_selection(self) -> None:
        """Drop this column's own row selection -- used by column_view.py's
        _on_real_row_activated so only the row that led to the current
        column/preview stays highlighted, never an earlier column too."""
        row = self.selected_row()
        if row is not None:
            set_row_selected(row, False)

    def active_index(self) -> int | None:
        """Return the arrow-key cursor's row index, if this column has one."""
        active = self._keyboard_active_row
        if active is None:
            return None
        for i, row in enumerate(self.rows()):
            if row is active:
                return i
        self._keyboard_active_row = None
        return None

    def active_row(self) -> "MyComputerColumnRow | None":
        """Return the temporary :active row used by arrow-key navigation."""
        return self._keyboard_active_row

    def clear_active_row(self) -> None:
        """Clear this column's temporary arrow-key cursor."""
        if self._keyboard_active_row is not None:
            set_row_active(self._keyboard_active_row, False)
            self._keyboard_active_row = None

    def set_active_index(self, index: int) -> None:
        """Move the temporary :active cursor without changing :selected."""
        rows = self.rows()
        if not rows:
            return
        clamped = max(0, min(index, len(rows) - 1))
        target = rows[clamped]
        if target is self._keyboard_active_row:
            return
        self.clear_active_row()
        set_row_active(target, True)
        self._keyboard_active_row = target

    def set_current_column(self, is_current: bool) -> None:
        """Mark whether this column is the one whose selected row should read
        as *the* accent-highlighted selection (see the .mc-current-column CSS
        rule in main.py). Driven by column_view.py's own tracked
        focused_index -- i.e. whichever column was last clicked (or, when
        arrow-key nav is enabled, last focused) -- rather than by actual GTK
        keyboard focus, so it works independent of any real focus-grabbing."""
        if is_current:
            self.list_box.add_css_class("mc-current-column")
        else:
            self.list_box.remove_css_class("mc-current-column")

    def rows(self) -> list["MyComputerColumnRow"]:
        """This column's rows in display order -- the keyboard-cursor helpers
        below index into this rather than tracking a separate list."""
        result = []
        row = self.list_box.get_first_child()
        while row is not None:
            if isinstance(row, MyComputerColumnRow):
                result.append(row)
            row = row.get_next_sibling()
        return result

    def selected_index(self) -> int | None:
        """Index of the currently highlighted row, or None if none is
        selected -- e.g. a freshly drilled-into column before any cursor
        movement has happened in it."""
        selected = self.list_box.get_selected_row()
        if selected is None:
            return None
        for i, row in enumerate(self.rows()):
            if row is selected:
                return i
        return None

    def selected_row(self) -> "MyComputerColumnRow | None":
        selected = self.list_box.get_selected_row()
        return selected if isinstance(selected, MyComputerColumnRow) else None

    def scroll_position(self) -> float:
        """This column's own vertical scroll offset, read live off the
        native Gtk.ScrolledWindow adjustment -- same pattern as
        selected_row(), not a value tracked separately on the object."""
        return self.get_vadjustment().get_value()

    def grab_list_focus(self) -> bool:
        return self.list_box.grab_focus()

    def _on_row_activated_internal(self, _list_box: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        if not isinstance(row, MyComputerColumnRow):
            return

        now = GLib.get_monotonic_time()
        double_click_us = Gtk.Settings.get_default().get_property("gtk-double-click-time") * 1000
        is_repeat_click = (
            not row.is_dir
            and row.uri == self._last_activated_uri
            and (now - self._last_activated_time) <= double_click_us
        )
        self._last_activated_uri = row.uri
        self._last_activated_time = now

        if is_repeat_click:
            # Second activation of the same file row within the double-click
            # window: open it, unconditionally (regardless of Nautilus'
            # click-policy setting) -- the single click already
            # selected/previewed it (see set_activate_on_single_click above),
            # so this is the symmetric "one more click" action. Same open
            # helper the preview column's click uses.
            _open_file_with_default_app(row.uri, self._cancellable)
            return

        self._on_row_activated(self, row)

    def destroy_enumeration(self) -> None:
        self._cancellable.cancel()


def _is_media_content_type(content_type: str) -> bool:
    return content_type.startswith("image/") or content_type.startswith("video/")


def _format_datetime(unix_time: int) -> str:
    if not unix_time:
        return ""
    return GLib.DateTime.new_from_unix_local(unix_time).format("%x %X")


def _open_file_with_default_app(file_uri: str, cancellable: Gio.Cancellable) -> None:
    """Launch file_uri with its default app. Shared by the preview column's
    click handler and file rows in the folder columns, so both surfaces open
    a file the same way, honoring Nautilus' own single/double-click setting
    via _is_activating_click()."""
    Gio.AppInfo.launch_default_for_uri_async(
        file_uri, None, cancellable, _on_launch_default_app_done
    )


def _on_launch_default_app_done(_source, result: Gio.AsyncResult) -> None:
    try:
        Gio.AppInfo.launch_default_for_uri_finish(result)
    except GLib.Error as e:
        _log(f"Open-with-default failed: {e}")


def _make_kv_row(title: str) -> tuple[Gtk.Box, Gtk.Label]:
    """A full-width label/value row for the preview column's details area
    (e.g. "Modified" on the left, the timestamp on the right)."""
    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
    row.set_halign(Gtk.Align.FILL)
    row.set_hexpand(True)

    title_lbl = Gtk.Label(label=title)
    title_lbl.get_style_context().add_class("dim-label")
    title_lbl.get_style_context().add_class("caption")
    title_lbl.set_halign(Gtk.Align.START)
    title_lbl.set_hexpand(True)
    row.append(title_lbl)

    value_lbl = Gtk.Label(label="")
    value_lbl.get_style_context().add_class("caption")
    value_lbl.set_halign(Gtk.Align.END)
    value_lbl.set_justify(Gtk.Justification.RIGHT)
    row.append(value_lbl)

    return row, value_lbl


class MyComputerPreviewColumn(Gtk.Box):
    """A preview column split between a responsive image area and bottom-pinned
    file details. Always the permanent rightmost column in the chain (see
    column_view.py's populate_column_view / _on_row_activated, which always
    (re)append one after any truncate). file_uri is None for its empty state.
    Real file preview (text, ...) is a later iteration."""

    __gtype_name__ = "MyComputerPreviewColumn"

    def __init__(self, ext, file_uri: str | None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._ext = ext
        self.file_uri = file_uri
        self._cancellable = Gio.Cancellable()
        self._discoverer = None

        self.set_size_request(_COLUMN_PREVIEW_WIDTH, -1)
        self.set_vexpand(True)
        self.set_valign(Gtk.Align.FILL)
        # The preview column is always the rightmost element, so it always
        # absorbs slack Finder-style: when the folder columns don't fill the
        # viewport it stretches to the right edge; once they overflow it sits
        # at its own _COLUMN_PREVIEW_WIDTH floor and the scroller scrolls. Its
        # size_request is the floor, hexpand/halign the slack.
        self.set_hexpand(True)
        self.set_halign(Gtk.Align.FILL)
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self.add_css_class("mc-column")
        # CSS target for the preview column (12px inner padding lives here).
        self.add_css_class("mc-preview-column")

        if file_uri is None:
            return

        # The outer widget is the fixed-height split. The first child is the
        # only vertically expanding section, so it consumes exactly the space
        # left after the bottom details area. This keeps details at the window
        # bottom instead of letting a whole-column scroller move them away.
        preview_area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        preview_area.set_halign(Gtk.Align.FILL)
        preview_area.set_valign(Gtk.Align.FILL)
        preview_area.set_vexpand(True)
        preview_area.set_hexpand(True)
        # Open the file with its default app on click, honoring Nautilus'
        # own single-click/double-click setting via _is_activating_click()
        # rather than hardcoding one -- unlike the folder columns to its left,
        # which are always single-click (Miller drill-down, see MyComputerColumn).
        click = Gtk.GestureClick()
        click.connect("pressed", self._on_preview_area_clicked)
        preview_area.add_controller(click)
        self.append(preview_area)

        self._icon = Gtk.Image()
        self._icon.set_pixel_size(128)
        self._icon.set_from_icon_name("text-x-generic")
        self._icon.set_halign(Gtk.Align.CENTER)
        self._icon.set_valign(Gtk.Align.CENTER)
        self._icon.set_vexpand(True)

        # Shown in place of the icon once a thumbnail is ready. The aspect
        # frame first carves an image-ratio rectangle out of the responsive
        # preview area. The picture then fills that rectangle, so its rounded
        # clip follows the actual rendered image rather than the surrounding
        # CONTAIN letterbox canvas.
        self._thumb_frame = Gtk.AspectFrame.new(0.5, 0.5, 1.0, False)
        self._thumb_frame.set_halign(Gtk.Align.FILL)
        self._thumb_frame.set_valign(Gtk.Align.FILL)
        self._thumb_frame.set_hexpand(True)
        self._thumb_frame.set_vexpand(True)

        self._thumb = Gtk.Picture()
        self._thumb.set_halign(Gtk.Align.FILL)
        self._thumb.set_valign(Gtk.Align.FILL)
        self._thumb.set_hexpand(True)
        self._thumb.set_vexpand(True)
        self._thumb.set_can_shrink(True)
        self._thumb.set_content_fit(Gtk.ContentFit.FILL)
        self._thumb.set_overflow(Gtk.Overflow.HIDDEN)
        self._thumb.add_css_class("mc-preview-image")
        self._thumb_frame.set_child(self._thumb)

        # Cap the rendered image width so it stops growing past a sane size on a
        # wide window, while the preview column itself keeps absorbing slack (its
        # padding/background still fills to the right edge). Adw.Clamp is the
        # native "cap width, center the overflow" widget -- no custom do_measure,
        # no resize signal, just a single layout constraint.
        self._thumb_clamp = Adw.Clamp(maximum_size=_COLUMN_PREVIEW_IMAGE_MAX_WIDTH)
        self._thumb_clamp.set_halign(Gtk.Align.FILL)
        self._thumb_clamp.set_valign(Gtk.Align.FILL)
        self._thumb_clamp.set_hexpand(True)
        self._thumb_clamp.set_vexpand(True)
        self._thumb_clamp.set_child(self._thumb_frame)

        # Keep every present and future preview type in one stable surface.
        # Switching a named Gtk.Stack page avoids rebuilding the preview area
        # as async metadata/decoding work completes.
        self._preview_stack = Gtk.Stack()
        self._preview_stack.set_halign(Gtk.Align.FILL)
        self._preview_stack.set_valign(Gtk.Align.FILL)
        self._preview_stack.set_hexpand(True)
        self._preview_stack.set_vexpand(True)
        self._preview_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._preview_stack.set_transition_duration(100)
        loading = Gtk.Spinner()
        loading.set_spinning(True)
        loading.set_halign(Gtk.Align.CENTER)
        loading.set_valign(Gtk.Align.CENTER)
        loading.set_size_request(32, 32)
        self._preview_stack.add_named(loading, PREVIEW_SLOT_LOADING)
        self._preview_stack.add_named(self._icon, PREVIEW_SLOT_ICON)
        self._preview_stack.add_named(self._thumb_clamp, PREVIEW_SLOT_IMAGE)
        # Reserved semantic pages keep the preview API ready for dedicated
        # video and document renderers without changing its layout contract.
        self._preview_stack.add_named(Gtk.Box(), PREVIEW_SLOT_VIDEO)
        self._preview_stack.add_named(Gtk.Box(), PREVIEW_SLOT_DOCUMENT)
        # Start on the image surface itself. It is blank until a paintable is
        # ready, which avoids both an icon flash and a loading indicator for
        # the common image-preview path. The loading slot remains available
        # for future preview types that need explicit progress feedback.
        self.set_preview_slot(PREVIEW_SLOT_IMAGE)

        # The revealer owns the complete preview surface; Gtk.Stack transitions
        # between semantic pages without changing the surface's allocation or
        # the aspect-framed image layout.
        self._thumb_revealer = Gtk.Revealer()
        self._thumb_revealer.set_halign(Gtk.Align.FILL)
        self._thumb_revealer.set_valign(Gtk.Align.FILL)
        self._thumb_revealer.set_hexpand(True)
        self._thumb_revealer.set_vexpand(True)
        self._thumb_revealer.set_transition_type(Gtk.RevealerTransitionType.CROSSFADE)
        self._thumb_revealer.set_transition_duration(100)
        self._thumb_revealer.set_reveal_child(True)
        self._thumb_revealer.set_child(self._preview_stack)
        preview_area.append(self._thumb_revealer)

        details_area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        details_area.set_halign(Gtk.Align.FILL)
        details_area.set_valign(Gtk.Align.END)
        details_area.set_vexpand(False)
        self.append(details_area)

        gfile = Gio.File.new_for_uri(file_uri)
        self._name_lbl = Gtk.Label(label=gfile.get_basename() or file_uri)
        self._name_lbl.set_justify(Gtk.Justification.CENTER)
        self._name_lbl.set_wrap(True)
        self._name_lbl.set_max_width_chars(20)
        self._name_lbl.set_halign(Gtk.Align.FILL)
        self._name_lbl.set_hexpand(True)
        self._name_lbl.get_style_context().add_class("heading")
        details_area.append(self._name_lbl)

        self._detail_lbl = Gtk.Label(label="")
        self._detail_lbl.set_halign(Gtk.Align.FILL)
        self._detail_lbl.set_hexpand(True)
        self._detail_lbl.get_style_context().add_class("dim-label")
        self._detail_lbl.get_style_context().add_class("caption")
        details_area.append(self._detail_lbl)

        created_row, self._created_val = _make_kv_row(_native("Created"))
        details_area.append(created_row)

        modified_row, self._modified_val = _make_kv_row(_native("Modified"))
        details_area.append(modified_row)

        self._dim_row, self._dim_val = _make_kv_row(_("Dimensions"))
        # Reserve the row's height up front for images/videos (a fast, sync,
        # I/O-free filename guess -- no MIME sniffing) rather than waiting for
        # the confirmed content-type and the actual width/height to come back
        # from their async calls: doing that later popped the row in after the
        # rest of the details were already laid out, jumping the whole column.
        # _on_info_ready reconciles this against the real content-type once it
        # lands, and the width/height calls below only ever touch the label
        # text of an already-visible row, never its visibility.
        guessed_type, _uncertain = Gio.content_type_guess(gfile.get_basename(), None)
        self._dim_row.set_visible(bool(guessed_type) and _is_media_content_type(guessed_type))
        details_area.append(self._dim_row)

        self._load()

    def _on_preview_area_clicked(
        self, _gesture: Gtk.GestureClick, n_press: int, _x: float, _y: float
    ) -> None:
        if _is_activating_click(self._ext, n_press):
            _open_file_with_default_app(self.file_uri, self._cancellable)

    def _load(self) -> None:
        gfile = Gio.File.new_for_uri(self.file_uri)
        gfile.query_info_async(
            "standard::display-name,standard::icon,standard::content-type,standard::size,"
            "time::modified,time::created",
            Gio.FileQueryInfoFlags.NONE,
            GLib.PRIORITY_DEFAULT,
            self._cancellable,
            self._on_info_ready,
        )

    def _on_info_ready(self, gfile: Gio.File, result: Gio.AsyncResult) -> None:
        try:
            info = gfile.query_info_finish(result)
        except GLib.Error:
            self._show_icon()
            return
        display_name = info.get_display_name()
        if display_name:
            self._name_lbl.set_label(display_name)
        content_type = info.get_content_type()
        size = info.get_size()
        parts = []
        if content_type:
            parts.append(Gio.content_type_get_description(content_type) or content_type)
        parts.append(_format_size(size))
        self._detail_lbl.set_label(" · ".join(parts))
        mtime = info.get_attribute_uint64("time::modified")
        self._created_val.set_label(_format_datetime(info.get_attribute_uint64("time::created")))
        self._modified_val.set_label(_format_datetime(mtime))
        gio_icon = info.get_icon()
        if _gicon_renders(gio_icon):
            self._icon.set_from_gicon(gio_icon)
        if content_type and content_type.startswith("image/"):
            # The image page is already visible and fills in as soon as its
            # decoded paintable arrives, with no interim icon or spinner.
            self._load_preview_image()
        else:
            self.set_preview_slot(PREVIEW_SLOT_ICON)
            self._maybe_load_thumbnail(content_type, mtime)
        self._maybe_load_dimensions(content_type)

    def _load_preview_image(self) -> None:
        """Decode a real local image on a worker thread for the large preview.

        The thumbnail factory remains the correct source for list rows and
        non-image previews. A preview can justify decoding the source file,
        but the decode limit avoids allocating a full-resolution texture for
        multi-megapixel photos in a 400px column.
        """
        path = Gio.File.new_for_uri(self.file_uri).get_path()
        if path is None:
            # Non-local image (no path to decode from, e.g. a GVfs/network
            # location) -- use its file icon as the preview fallback.
            self._show_icon()
            return
        threading.Thread(target=self._preview_image_worker, args=(path,), daemon=True).start()

    def _preview_image_worker(self, path: str) -> None:
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                path,
                _COLUMN_PREVIEW_IMAGE_SIZE,
                _COLUMN_PREVIEW_IMAGE_SIZE,
                True,
            )
        except GLib.Error:
            # Decode failures fall back from loading to the file icon.
            GLib.idle_add(self._show_icon)
            return
        if self._cancellable.is_cancelled():
            return
        GLib.idle_add(self._apply_thumbnail, Gdk.Texture.new_for_pixbuf(pixbuf))

    def _show_icon(self) -> int:
        if not self._cancellable.is_cancelled():
            self.set_preview_slot(PREVIEW_SLOT_ICON)
        return GLib.SOURCE_REMOVE

    def _maybe_load_dimensions(self, content_type: str | None) -> None:
        """Populate the Dimensions row for images and videos. Images read the
        header only (GdkPixbuf, needs a local path -- most gvfs mounts expose
        one via the fuse daemon; otherwise the row reverts to hidden). Videos
        go through GstPbutils' Discoverer, which works off the URI directly.

        The row's visibility was already set from a fast filename guess in
        __init__ (to avoid a layout jump -- see that comment); this is the
        reconciliation against the real, confirmed content-type, and the only
        place allowed to hide a row that guess had shown. Everything below
        this point (the async calls and their callbacks) only ever fills in
        the label text of an already-decided-visible row."""
        is_media = bool(content_type) and _is_media_content_type(content_type)
        if not is_media:
            self._dim_row.set_visible(False)
            return
        if content_type.startswith("image/"):
            path = Gio.File.new_for_uri(self.file_uri).get_path()
            if path is None:
                self._dim_row.set_visible(False)
                return
            self._dim_row.set_visible(True)
            GdkPixbuf.Pixbuf.get_file_info_async(path, self._cancellable, self._on_image_info_ready)
        elif GstPbutils is not None:
            self._dim_row.set_visible(True)
            self._discoverer = GstPbutils.Discoverer.new(5 * Gst.SECOND)
            self._discoverer.connect("discovered", self._on_video_discovered)
            self._discoverer.start()
            self._discoverer.discover_uri_async(self.file_uri)
        else:
            self._dim_row.set_visible(False)

    def _on_image_info_ready(self, _source, result: Gio.AsyncResult) -> None:
        try:
            _fmt, width, height = GdkPixbuf.Pixbuf.get_file_info_finish(result)
        except GLib.Error:
            return
        if width and height:
            self._dim_val.set_label(f"{width}x{height}")

    def _on_video_discovered(self, discoverer, info, error) -> None:
        discoverer.stop()
        if error:
            return
        video_streams = info.get_video_streams()
        if not video_streams:
            return
        width, height = video_streams[0].get_width(), video_streams[0].get_height()
        if width and height:
            GLib.idle_add(self._dim_val.set_label, f"{width}x{height}")

    def _maybe_load_thumbnail(self, content_type: str | None, mtime: int) -> None:
        """Show a native thumbnail for the file when GNOME can make one.

        A cached thumbnail is loaded inline (cheap file read). A miss goes to a
        daemon thread because generate_thumbnail() runs the system thumbnailer
        subprocess and can block -- never on Nautilus's main loop. Files GNOME
        can't thumbnail (e.g. video with no thumbnailer lib installed) just keep
        the icon."""
        if _thumb_factory is None or not content_type:
            return
        uri = self.file_uri
        cached = _thumb_factory.lookup(uri, mtime)
        if cached:
            self._show_thumbnail_from_file(cached)
            return
        if _thumb_factory.has_valid_failed_thumbnail(uri, mtime):
            return
        if not _thumb_factory.can_thumbnail(uri, content_type, mtime):
            return
        threading.Thread(
            target=self._thumbnail_worker, args=(uri, content_type, mtime), daemon=True
        ).start()

    def _thumbnail_worker(self, uri: str, content_type: str, mtime: int) -> None:
        try:
            pixbuf = _thumb_factory.generate_thumbnail(uri, content_type, self._cancellable)
        except GLib.Error:
            pixbuf = None
        if self._cancellable.is_cancelled():
            return
        if pixbuf is None:
            try:
                _thumb_factory.create_failed_thumbnail(uri, mtime, self._cancellable)
            except GLib.Error:
                pass
            return
        try:
            _thumb_factory.save_thumbnail(pixbuf, uri, mtime, self._cancellable)
        except GLib.Error:
            pass
        GLib.idle_add(self._apply_thumbnail, Gdk.Texture.new_for_pixbuf(pixbuf))

    def _show_thumbnail_from_file(self, path: str) -> None:
        try:
            texture = Gdk.Texture.new_from_filename(path)
        except GLib.Error:
            return
        self._apply_thumbnail(texture)

    def _apply_thumbnail(self, texture: Gdk.Texture) -> int:
        if self._cancellable.is_cancelled():
            return GLib.SOURCE_REMOVE
        self._thumb_frame.set_ratio(texture.get_width() / texture.get_height())
        self._thumb.set_paintable(texture)
        self.set_preview_slot(PREVIEW_SLOT_IMAGE)
        return GLib.SOURCE_REMOVE

    def set_preview_slot(self, slot: str) -> None:
        """Show a named preview surface from the stable preview stack.

        Keeping this state transition on the widget mirrors
        ``MyComputerColumnRow.set_cut()`` / ``set_thumbnail()``: callers
        request a semantic state without depending on its Gtk.Stack layout.
        """
        if slot not in {
            PREVIEW_SLOT_LOADING,
            PREVIEW_SLOT_ICON,
            PREVIEW_SLOT_IMAGE,
            PREVIEW_SLOT_VIDEO,
            PREVIEW_SLOT_DOCUMENT,
        }:
            raise ValueError(f"unknown preview slot: {slot}")
        self._preview_stack.set_visible_child_name(slot)

    def destroy_enumeration(self) -> None:
        self._cancellable.cancel()
        if self._discoverer is not None:
            self._discoverer.stop()
