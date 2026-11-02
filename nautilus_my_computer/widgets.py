"""Reusable, self-contained card/section widgets for the Computer panel.

Each card renders itself from a single model object (a MountInfo or
PreferredFolder, accessed by duck-typing -- this module never imports those
classes) and adapts its layout to the current view mode ("icon-view" grid vs
"list-view" row). Cards never import the entry file; behaviour that needs the
extension (right-click menus, file-op D-Bus calls, navigation) is reached
through the injected `ext` instance.
"""

import collections
import dataclasses
import math
import os
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
# Matches Nautilus's own HOVER_TIMEOUT: how long a drag must rest over a folder
# row before that folder opens as the next column.
DND_HOVER_OPEN_MS = 500

_ROW_THUMBNAIL_SEMAPHORE = threading.Semaphore(4)

# Decoded Gdk.Texture objects kept in process, one independent lane per kind.
# The kind names the use and fixes the texture size, so a texture can never be
# served to a use it was not made for, even if two sizes are later made equal.
# Entries are keyed by (uri, mtime), so an edited file never serves a stale one.
#
# Reparenting a column's ListView can make GTK unbind and immediately bind all
# of its recycled rows again. GNOME's disk thumbnail cache avoids re-running a
# thumbnailer in that case, but decoding every thumbnail again still makes the
# row visibly fall back to its generic icon, and revisiting a previewed file,
# or extending a selection by one row (the other stack items stay), would
# decode again. Each lane has its own storage, bound, eviction and lock, so one
# flood (hundreds of rows) can never evict the few large previews. Row
# thumbnails are read from worker threads, hence the locks.
TEXTURE_ROW = "row"  # 24 px column row thumbnail
TEXTURE_SINGLE = "single"  # _COLUMN_PREVIEW_IMAGE_SIZE single-file preview
TEXTURE_STACK = "stack"  # _PREVIEW_ICON_SIZE multi-selection stack item
_TEXTURE_CACHE_LIMITS = {TEXTURE_ROW: 512, TEXTURE_SINGLE: 16, TEXTURE_STACK: 16}


class _TextureLane:
    """One bounded LRU of (texture, translucent) entries. The translucent flag
    says the picture really uses its alpha channel (see _pixbuf_alpha_kind);
    only the stack reads it."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._entries: collections.OrderedDict[tuple[str, int], tuple[Gdk.Texture, bool]] = (
            collections.OrderedDict()
        )
        self._lock = threading.Lock()

    def get(self, uri: str, mtime: int) -> tuple[Gdk.Texture, bool] | None:
        key = (uri, mtime)
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                self._entries.move_to_end(key)
            return entry

    def put(self, uri: str, mtime: int, texture: Gdk.Texture, translucent: bool = False) -> None:
        key = (uri, mtime)
        with self._lock:
            self._entries[key] = (texture, translucent)
            self._entries.move_to_end(key)
            while len(self._entries) > self._limit:
                self._entries.popitem(last=False)


class _TextureCache:
    """The three lanes behind one object: get/put pick the lane by kind."""

    def __init__(self) -> None:
        self._lanes = {kind: _TextureLane(limit) for kind, limit in _TEXTURE_CACHE_LIMITS.items()}

    def get(self, kind: str, uri: str, mtime: int) -> tuple[Gdk.Texture, bool] | None:
        return self._lanes[kind].get(uri, mtime)

    def put(
        self, kind: str, uri: str, mtime: int, texture: Gdk.Texture, translucent: bool = False
    ) -> None:
        self._lanes[kind].put(uri, mtime, texture, translucent)


_texture_cache = _TextureCache()


def _pixbuf_alpha_kind(pixbuf: GdkPixbuf.Pixbuf) -> str:
    """Classify a decoded picture as "opaque", "translucent" or "blank".

    has-alpha is only the cheap first gate: many files carry an alpha channel they
    never use, so a flagged picture still has its alpha bytes checked. An 8-bit
    RGBA pixbuf has no row padding (rowstride == 4 * width), so the whole alpha
    plane is one strided slice and two C-speed counts, no Python loop. "blank"
    means fully transparent: nothing to show. Worker-thread safe."""
    if not pixbuf.get_has_alpha():
        return "opaque"
    alpha = pixbuf.get_pixels()[3::4]
    if alpha.count(b"\xff") == len(alpha):
        return "opaque"
    if alpha.count(b"\x00") == len(alpha):
        return "blank"
    return "translucent"


def _decode_preview_pixbuf(path: str, size: int) -> GdkPixbuf.Pixbuf | None:
    """Decode a source image to fit `size` x `size`. Worker-thread safe.
    None when it cannot be decoded."""
    try:
        return GdkPixbuf.Pixbuf.new_from_file_at_scale(path, size, size, True)
    except GLib.Error:
        return None


# Frames MyComputerColumn.with_selected_row waits for a scrolled-to row to
# actually bind before giving up. Generous: the realize lands within a frame
# or two of the scroll_to in practice, this is only the ceiling for a stall.
_SELECTED_ROW_REALIZE_FRAMES = 30

# Named pages of MyComputerPreviewColumn's stable preview surface.  The
# contents of a page may evolve (for example, video can later become a real
# player), but callers only select a semantic slot and never rebuild the UI.
PREVIEW_SLOT_LOADING = "loading"
PREVIEW_SLOT_ICON = "icon"
PREVIEW_SLOT_IMAGE = "image"
PREVIEW_SLOT_VIDEO = "video"
PREVIEW_SLOT_DOCUMENT = "document"
PREVIEW_SLOT_STACK = "stack"


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

from nautilus_my_computer import selection
from nautilus_my_computer import uris as uri_utils
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
    _PREVIEW_ICON_SIZE,
    N_,
    _,
    _disk_icon_size,
    _disk_list_icon_size,
    _folder_card_width,
    _format_item_count,
    _format_size,
    _gicon_renders,
    _icon_name_renders,
    _log,
    _n,
    _native,
    _nautilus_icon_size,
    _nautilus_list_icon_size,
    _resolve_custom_gicon,
    _set_regular_icon,
    admin_backend_available,
)
from nautilus_my_computer.components import set_row_drop_active


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
        # Deliberately not set_focusable/set_focus_on_click (issue #161): a focusable inner
        # widget lets gtk_flow_box_child_focus's backward-entry branch grab focus on this Box
        # directly instead of the FlowBoxChild wrapper, so Shift+Tab landing fresh on this card
        # would skip the wrapper's own selection/focus-visible state entirely
        # (gtk_flow_box_child_set_focus never runs). Arrow-key nav is unaffected -- it goes
        # through GtkFlowBox's own move-cursor handler, which always focuses the wrapper.
        self._build()

        # One gesture on all buttons, dispatched from "pressed"/"released",
        # mirroring nautilus-list-base.c:880-886 (on_item_click_pressed /
        # button=0). Primary is claimed and driven end to end by
        # _on_card_pressed/_on_card_released (#161) rather than left to
        # FlowBox's own competing click gesture.
        click = Gtk.GestureClick()
        click.set_button(0)
        click.connect("pressed", self._ext._on_card_pressed, self._win, self)
        click.connect("released", self._ext._on_card_released, self._win, self)
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
            # One gesture on all buttons, dispatched from "pressed"/"released",
            # mirroring nautilus-list-base.c:880-886 (on_item_click_pressed /
            # button=0). Primary is claimed and driven end to end by
            # _on_card_pressed/_on_card_released (#161) rather than left to
            # FlowBox's own competing click gesture.
            click = Gtk.GestureClick()
            click.set_button(0)
            click.connect("pressed", self._ext._on_card_pressed, self._win, self)
            click.connect("released", self._ext._on_card_released, self._win, self)
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
        MyComputerCardGroup) while rendering each card as a compact
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


class MyComputerCardGroup(Gtk.Box):
    """A heading + FlowBox of cards. Dedups the section setup shared by the
    Preferred Folders block and each disk group in _populate()."""

    __gtype_name__ = "MyComputerCardGroup"

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
        self.flow = Gtk.FlowBox()
        self.flow.set_homogeneous(homogeneous)
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


class _ColumnRowItem(GObject.Object):
    """One Gio.ListStore entry backing a Gtk.ListView row in a Miller column.

    Plain mutable data, no GObject.Property machinery -- nothing binds to
    these declaratively, MyComputerColumnRow.bind() just reads the attributes
    directly on each recycle. ``is_cut`` is the one field mutated in place
    after construction (see MyComputerColumn.apply_cut_uris): cut state must
    survive scrolling an item out of and back into view, so it lives on the
    model item, not on the transient row widget.
    """

    __gtype_name__ = "MyComputerColumnRowItem"

    def __init__(
        self,
        uri: str,
        display_name: str,
        is_dir: bool,
        gio_icon=None,
        is_hidden: bool = False,
        content_type: str | None = None,
        mtime: int = 0,
        can_read: bool = True,
        can_execute: bool = True,
    ) -> None:
        super().__init__()
        self.uri = uri
        self.display_name = display_name
        self.is_dir = is_dir
        self.icon = gio_icon
        self.is_hidden = is_hidden
        self.content_type = content_type
        self.mtime = mtime
        self.can_read = can_read
        self.can_execute = can_execute
        self.is_cut = False

    @property
    def open_uri(self) -> str:
        """Where a deliberate open of this row navigates: admin:// for a local
        folder the user can't read or enter (issue #187), uri otherwise."""
        if not self.is_dir or not admin_backend_available():
            return self.uri
        return uri_utils.admin_uri_if_needed(self.uri, self.can_read, self.can_execute)


def _droppable_uris(uris: list[str], destination_uri: str, *, is_move: bool) -> list[str]:
    """Drop the sources a drop into destination_uri cannot sensibly act on:
    the destination itself, a destination inside a source, and (moves only) a
    source already sitting directly in the destination. Compared through
    Gio.File so URI encoding and trailing slashes normalize like GVfs does."""
    destination = Gio.File.new_for_uri(destination_uri)
    kept = []
    for uri in uris:
        source = Gio.File.new_for_uri(uri)
        if source.equal(destination) or destination.has_prefix(source):
            continue
        if is_move:
            parent = source.get_parent()
            if parent is not None and parent.equal(destination):
                continue
        kept.append(uri)
    return kept


def _preferred_move(uris: list[str], destination_uri: str) -> bool:
    """Mirror nautilus_dnd_get_preferred_action: trash sources and sources on
    the destination's filesystem move, everything else copies. Only local
    file:// paths are stat'd (cheap); any other scheme copies."""
    dest = Gio.File.new_for_uri(destination_uri).get_path()
    if dest is None:
        return False
    try:
        dest_dev = os.stat(dest).st_dev
    except OSError:
        return False
    for uri in uris:
        if uri.startswith("trash:"):
            continue
        path = Gio.File.new_for_uri(uri).get_path()
        if path is None:
            return False
        try:
            if os.stat(path).st_dev != dest_dev or not os.access(os.path.dirname(path), os.W_OK):
                return False
        except OSError:
            return False
    return True


class MyComputerColumnRow(Gtk.Box):
    """One recyclable Gtk.ListView row widget for a Column View column: icon,
    name, and a trailing chevron for folders (visual affordance that
    activating the row opens a child column).

    Unlike the old Gtk.ListBoxRow version, one instance is reused across many
    different _ColumnRowItem entries as the list scrolls (GTK's documented
    ListView/GtkSignalListItemFactory recycling lifecycle) -- the static
    widget tree is built once in __init__, and bind()/unbind() apply and tear
    down per-item state (label text, icon, thumbnail request, cut styling) on
    every recycle. ``self.item`` always reflects whichever entry this widget
    instance currently represents, so external code reading row.uri/row.is_dir
    at click time (column_view.py's press/release dispatch) sees live data
    without needing to be re-wired on every bind.
    """

    __gtype_name__ = "MyComputerColumnRow"

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=_COLUMN_ROW_SPACING)
        self.item: _ColumnRowItem | None = None
        self._is_cut = False
        self._thumb_cancellable: Gio.Cancellable | None = None

        # Inset and height come from the .mc-column-row-content CSS rule, so the
        # box itself carries no margin.
        self.add_css_class("mc-column-row-content")
        self.set_hexpand(True)
        self.set_vexpand(True)

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
        self._icon = icon

        # Normal state has its own stable pages too: a themed icon is shown
        # immediately, then a completed thumbnail simply becomes the other
        # page. The async thumbnail path consequently never mutates the row's
        # widget tree, only which stack page is visible.
        thumbnail = Gtk.Picture()
        thumbnail.set_hexpand(True)
        thumbnail.set_halign(Gtk.Align.CENTER)
        thumbnail.set_valign(Gtk.Align.CENTER)
        thumbnail.set_content_fit(Gtk.ContentFit.SCALE_DOWN)
        thumbnail.set_overflow(Gtk.Overflow.HIDDEN)
        thumbnail.add_css_class("mc-row-thumbnail")
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
        self.append(icon_stack)
        self._icon_stack = icon_stack

        name_lbl = Gtk.Label()
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
        self.append(name_lbl)
        self._name_lbl = name_lbl

        chevron = Gtk.Image.new_from_icon_name("go-next-symbolic")
        chevron.set_pixel_size(12)
        chevron.get_style_context().add_class("dim-label")
        chevron.set_visible(False)
        self.append(chevron)
        self._chevron = chevron

    @property
    def uri(self) -> str | None:
        return self.item.uri if self.item is not None else None

    @property
    def is_dir(self) -> bool:
        return bool(self.item is not None and self.item.is_dir)

    @property
    def open_uri(self) -> str | None:
        return self.item.open_uri if self.item is not None else None

    @property
    def display_name(self) -> str | None:
        return self.item.display_name if self.item is not None else None

    @property
    def content_type(self) -> str | None:
        return self.item.content_type if self.item is not None else None

    def bind(self, item: _ColumnRowItem) -> None:
        """Apply one model entry's data to this recycled widget instance.

        A fresh per-bind Gio.Cancellable backs any thumbnail lookup this
        bind starts, so a lookup/generation begun for a since-recycled item
        can never land on the widget after it has been rebound to a
        different one (see unbind()).
        """
        self.item = item
        self._name_lbl.set_label(item.display_name)
        self._chevron.set_visible(item.is_dir)

        # gio_icon is already the fully-resolved icon by the time it gets
        # here (custom icon if the caller found one, else GIO's own real icon
        # for the path -- see MyComputerColumn._populate_rows) --
        # _set_regular_icon (not a plain set_from_icon_name + set_pixel_size)
        # forces the full-color variant: at this small 24px size GTK would
        # otherwise auto-select a monochrome/symbolic-looking fixed-size
        # theme variant on some themes. See common._set_regular_icon.
        if _gicon_renders(item.icon):
            _set_regular_icon(self._icon, _COLUMN_ROW_ICON_SIZE, gicon=item.icon)
        else:
            _set_regular_icon(
                self._icon,
                _COLUMN_ROW_ICON_SIZE,
                icon_name=("folder" if item.is_dir else "text-x-generic"),
            )
        # Same class/opacity Nautilus's own grid/list cells use to dim hidden
        # entries (nautilus-grid-cell.c, nautilus-name-cell.c), applied only
        # to the icon/thumbnail -- not the label or the whole row -- to match
        # native exactly.
        self._icon.set_visible(True)
        if item.is_hidden:
            self._icon.add_css_class("hidden-file")
            self._thumbnail.add_css_class("hidden-file")
        else:
            self._icon.remove_css_class("hidden-file")
            self._thumbnail.remove_css_class("hidden-file")
        self._thumbnail.set_paintable(None)
        self._regular_stack.set_visible_child_name("icon")

        self.set_cut(item.is_cut)

        # macOS Finder-style: files show a real image/document thumbnail
        # instead of the generic icon when one is available. Same
        # GnomeDesktop.DesktopThumbnailFactory engine as the preview column
        # (see MyComputerPreviewColumn) -- a cached thumbnail is read inline
        # (cheap, already-scaled file read), a miss is generated on a daemon
        # thread and swapped in once ready, never on the main loop. Folders
        # never go through this: their icon is already the real folder icon.
        if not item.is_dir and item.content_type and _thumb_factory is not None:
            cached = _texture_cache.get(TEXTURE_ROW, item.uri, item.mtime)
            cached_texture = cached[0] if cached is not None else None
            if cached_texture is not None:
                self.set_thumbnail(cached_texture)
                return
            self._thumb_cancellable = Gio.Cancellable()
            self._load_row_thumbnail(
                item.uri, item.content_type, item.mtime, self._thumb_cancellable
            )

    def unbind(self) -> None:
        """Release this widget's binding to its current item before GTK
        recycles it for a different one. Cancelling the per-bind thumbnail
        cancellable is what stops a stale async lookup/generation for the
        outgoing item from ever calling back into this (now differently
        bound) widget -- see bind()'s cancellable layering."""
        if self._thumb_cancellable is not None:
            self._thumb_cancellable.cancel()
            self._thumb_cancellable = None
        self.item = None

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        """Draw the complete cut treatment in the padded row's bounds."""
        if not self._is_cut:
            Gtk.Box.do_snapshot(self, snapshot)
            return

        width = float(self.get_width())
        height = float(self.get_height())
        # A 2px stroke centered 1px from the allocation edge lands flush
        # with the row surface instead of floating inside it.
        inset = 1.0
        if width <= inset * 2 or height <= inset * 2:
            Gtk.Box.do_snapshot(self, snapshot)
            return

        bounds = Graphene.Rect()
        bounds.init(0.0, 0.0, width, height)
        color = self.get_color()

        # The background is below GTK's normal row snapshot so native hover
        # and selection layers retain their expected stacking order.
        cr = snapshot.append_cairo(bounds)
        cr.set_source_rgba(color.red, color.green, color.blue, color.alpha * 0.07)
        # Our .mc-column-list > row rule supplies the visible row radius
        # (9px, as in Nautilus' list view). Keep this snapshot path in sync with
        # it because GTK does not expose a computed CSS radius to Python.
        self._append_rounded_rectangle(cr, 0.0, 0.0, width, height, 9.0)
        cr.fill()
        del cr

        Gtk.Box.do_snapshot(self, snapshot)

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

    def row_node(self) -> Gtk.Widget | None:
        """The GTK-owned GtkListItemWidget wrapping this row -- the actual
        CSS node named "row" that carries :selected/:hover/:active, since
        under Gtk.ListView this Gtk.Box is only its content, not the row
        itself (unlike the old Gtk.ListBoxRow subclass). Callers that need to
        apply a state flag or CSS class the sidebar/:selected rules expect on
        "row" (e.g. set_row_active for the right-click anchor) must target
        this, not self."""
        return self.get_parent()

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

    def set_path(self, on: bool) -> None:
        """Mark this row as the one the open Miller path runs through."""
        if on:
            self.add_css_class("mc-row-path")
        else:
            self.remove_css_class("mc-row-path")

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
        self, uri: str, content_type: str, mtime: int, cancellable: Gio.Cancellable
    ) -> None:
        # Nautilus-style icon-then-thumbnail: the row already shows its plain
        # icon (set just above, in bind()) the instant it's bound. Both the
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
            args=(uri, content_type, mtime, cancellable),
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
            _texture_cache.put(TEXTURE_ROW, uri, mtime, texture)
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
        _texture_cache.put(TEXTURE_ROW, uri, mtime, texture)
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
    can_read: bool
    can_execute: bool


def _access_allowed(info: Gio.FileInfo, attribute: str) -> bool:
    """An access:: flag, True when the backend doesn't report it: only a flag
    that is really false may send an open to admin://."""
    return not info.has_attribute(attribute) or info.get_attribute_boolean(attribute)


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


class _DisplaySelection(GObject.Object, Gio.ListModel, Gtk.SelectionModel):
    """Selection model that only displays. GTK's list items select the pressed
    row on press (before a release decides it was a click), so the columns'
    selection must be unwritable by GTK: every select/unselect vfunc keeps its
    default and returns False. Only `show()` changes what is selected, and our
    own input paths are the only callers (#189)."""

    def __init__(self, store: Gio.ListStore) -> None:
        super().__init__()
        self._store = store
        self._shown: frozenset[str] = frozenset()
        store.connect("items-changed", lambda _s, pos, rem, add: self.items_changed(pos, rem, add))

    def do_get_item_type(self):
        return self._store.get_item_type()

    def do_get_n_items(self):
        return self._store.get_n_items()

    def do_get_item(self, position):
        return self._store.get_item(position)

    def do_is_selected(self, position):
        item = self._store.get_item(position)
        return item is not None and item.uri in self._shown

    def do_get_selection_in_range(self, position, n_items):
        bits = Gtk.Bitset.new_empty()
        for i in range(position, position + n_items):
            if self.do_is_selected(i):
                bits.add(i)
        return bits

    @property
    def shown(self) -> frozenset[str]:
        return self._shown

    def show(self, uris: frozenset[str]) -> None:
        """Replace the displayed selection and announce it once."""
        if uris == self._shown:
            return
        self._shown = uris
        n = self._store.get_n_items()
        if n:
            self.selection_changed(0, n)


class ColumnSelection:
    """Selection state of one Miller column. The rules live in selection.py and
    work on URIs; this class holds the current `Selection` and shows it through
    a display-only model over the row store, so GTK can never change it. Every
    selection change in a column goes through this object, so Ctrl/Shift
    multi-selection (#178) and drag and drop (#154) share one set of rules."""

    def __init__(self, store: Gio.ListStore) -> None:
        self._store = store
        self.model = _DisplaySelection(store)
        self._sel = selection.Selection()
        # A reload empties the store before the new listing arrives. That gap
        # is not the user deselecting, so listeners stay silent until
        # end_reload/abort_reload of the current generation.
        self._reload_gen = 0
        self._reloading = False

    @property
    def reloading(self) -> bool:
        return self._reloading

    @property
    def reload_gen(self) -> int:
        return self._reload_gen

    def begin_reload(self) -> int:
        """Start a reload and return its token. A newer begin supersedes every
        older token, so a stale enumeration can never end or abort it."""
        self._reload_gen += 1
        self._reloading = True
        return self._reload_gen

    def end_reload(self, gen: int, ordered: list[str]) -> None:
        """The new listing is in the store: keep the URIs still listed, drop
        the rest, and project the result once."""
        if gen != self._reload_gen:
            return
        self._reloading = False
        self._apply(selection.reconcile(self._sel, ordered))

    def abort_reload(self, gen: int) -> None:
        """The enumeration failed: nothing is listed, so nothing stays selected.
        A stale token is a no-op, the newer reload still owns the lifecycle."""
        if gen != self._reload_gen:
            return
        self._reloading = False
        self._apply(selection.reconcile(self._sel, self._ordered()))

    def _ordered(self) -> list[str]:
        return [self._store.get_item(i).uri for i in range(self._store.get_n_items())]

    def _apply(self, sel: "selection.Selection") -> None:
        """Make `sel` the selection and show it at once, so no transient empty
        or partial state fires the selection callbacks."""
        self._sel = sel
        self.model.show(sel.uris)

    def positions(self) -> list[int]:
        return [
            i
            for i in range(self._store.get_n_items())
            if self._store.get_item(i).uri in self._sel.uris
        ]

    def items(self) -> list["_ColumnRowItem"]:
        return [self._store.get_item(i) for i in self.positions()]

    def uris(self) -> list[str]:
        return [item.uri for item in self.items()]

    def count(self) -> int:
        return len(self.positions())

    def contains(self, item) -> bool:
        return any(item is other for other in self.items())

    def set_uris(self, uris) -> None:
        """Make exactly these URIs the selection (the cross-view bridge, #178).
        While the rows are still loading, end_reload reconciles and applies."""
        uris = frozenset(uris)
        anchor = next(iter(uris)) if len(uris) == 1 else None
        sel = selection.Selection(uris, anchor)
        if self._reloading:
            self._sel = sel
        else:
            self._apply(selection.reconcile(sel, self._ordered()))

    def rebase(self, old: str, new: str) -> None:
        """Carry the selection over to where its URIs moved. Called during a
        reload, so end_reload projects it once the new listing is in."""
        anchor = self._sel.anchor
        self._sel = selection.Selection(
            frozenset(uri_utils.rebase(uri, old, new) for uri in self._sel.uris),
            uri_utils.rebase(anchor, old, new) if anchor is not None else None,
        )

    def clear(self) -> None:
        self._apply(selection.clear(self._sel))

    def select_all(self) -> None:
        self._apply(selection.select_all(self._ordered()))

    def select_only(self, position: int) -> None:
        """Select exactly one row, dropping every other selected row."""
        self._apply(selection.click(self._sel, self._store.get_item(position).uri))

    def toggle(self, position: int) -> None:
        """Ctrl-click: add or remove one row, leaving the rest alone."""
        self._apply(selection.ctrl_click(self._sel, self._store.get_item(position).uri))

    def select_range_to(self, position: int, *, extend: bool) -> None:
        """Shift-click: select from the click anchor to this row. Without
        extend (Ctrl+Shift) every row outside the range is unselected."""
        uri = self._store.get_item(position).uri
        self._apply(selection.shift_click(self._sel, uri, self._ordered(), extend=extend))

    def modifier_click(self, item, *, ctrl: bool, shift: bool) -> None:
        found, position = self._store.find(item)
        if not found:
            return
        if shift:
            self.select_range_to(position, extend=ctrl)
        else:
            self.toggle(position)

    def select_only_item(self, item) -> None:
        found, position = self._store.find(item)
        if found:
            self.select_only(position)

    def primary(self) -> int | None:
        """The anchor row's position (the only one when one row is selected)."""
        positions = self.positions()
        if not positions:
            return None
        found = self._sel.anchor is not None and self._sel.anchor in self.uris()
        if found:
            return positions[self.uris().index(self._sel.anchor)]
        return positions[0]


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
        on_row_pressed=None,
        on_row_released=None,
        on_files_dropped=None,
        on_drag_start=None,
        on_drag_end=None,
    ) -> None:
        super().__init__()
        self._ext = ext
        self.folder_uri = folder_uri
        # on_drag_start(column) makes this the active column before a drag
        # reads its selection, so a drag never uses another column's.
        self._on_drag_start = on_drag_start
        # on_drag_end(column) runs when a drag started from one of its rows
        # ends, dropped or cancelled.
        self._on_drag_end = on_drag_end
        # on_files_dropped(uris, destination_uri, move) runs after a drop on a
        # folder row or this column's background has been filtered.
        self._on_files_dropped = on_files_dropped
        self._hover_timer = 0
        self._hover_row: MyComputerColumnRow | None = None
        self._on_row_activated = on_row_activated
        self._on_loaded = on_loaded
        self._sort = sort
        # Persistent per-row-widget GestureClick installed once, in the
        # factory's "setup" (see _on_factory_setup): unlike the retired
        # Gtk.ListBox version, a recycled ListView row widget is never
        # revisited after load to attach a controller (column_view.py used to
        # loop column.rows() in _on_column_loaded for exactly that -- gone
        # now, since "loaded" is not the same event as "a widget exists").
        # These callbacks receive (gesture, n_press, x, y, column, row), same
        # signature _ColumnViewHost._on_row_pressed/_released already expect.
        self._on_row_pressed = on_row_pressed
        self._on_row_released = on_row_released
        self._cancellable = Gio.Cancellable()
        # Distinguish a genuinely empty folder from one whose asynchronous
        # enumeration has not produced its model yet. Column View's keyboard
        # Right/Enter handling uses this to defer moving focus into a new
        # child until it knows there is a row that can actually own selection.
        self._loaded = False
        # Manual repeat-click detection for opening the already-previewed file row
        # (see activate_row): a raw GestureClick on the row can't be
        # used for this because every activation rebuilds the paned chain
        # (column_view.py's _rebuild_chain), which resets GTK's own press-count
        # tracking on the row before a second click can land.
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

        # GListModel-backed, recycling row list (experimental replacement for
        # the former Gtk.ListBox -- see issue #139: native Nautilus's own
        # "lazy load based on what's visible" is an emergent property of this
        # exact recycling mechanism, not an explicit visibility check). Rows
        # no longer exist 1:1 with folder entries: live MyComputerColumnRow
        # widgets are a BOUNDED, RECYCLED POOL (see _row_widgets, populated/
        # depopulated from the factory's bind/unbind, GTK's documented
        # GtkSignalListItemFactory lifecycle) -- bounded, but well beyond the
        # rows actually on screen, since GTK keeps a substantial overscan.
        # #139 measured ~205 bound rows for a 240-item AND a 1001-item
        # folder, i.e. O(1) in folder size but large in absolute terms, so
        # any folder smaller than that pool has every one of its rows bound
        # at once. Do not read _row_widgets as "what is currently visible";
        # it is not (see page_step, which derives the visible count from the
        # adjustment instead).
        self._store: Gio.ListStore = Gio.ListStore(item_type=_ColumnRowItem)
        # Multi-selection (#178): Ctrl-click toggles one row and Shift-click
        # extends from the last clicked row, both applied by GtkListItemWidget
        # itself, as in native Nautilus. The single-item API below
        # (selected_item and friends) reads the anchor row, so callers that
        # only ever deal with one file keep working unchanged.
        self._selection_state = ColumnSelection(self._store)
        self._selection = self._selection_state.model
        # Set only by ColumnView._repaint through paint(): whether this
        # column's rows read as accent. At most one column has it (#189).
        self._accent = False
        # URI of the row that leads to the next column or preview. Shown as a
        # path highlight, never written into the selection (#178).
        self.path_uri: str | None = None
        self._selection_callbacks: list = []
        self._selection.connect("selection-changed", lambda *_args: self._refresh_rows())
        self._row_widgets: dict[int, MyComputerColumnRow] = {}

        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._on_factory_setup)
        factory.connect("bind", self._on_factory_bind)
        factory.connect("unbind", self._on_factory_unbind)
        factory.connect("teardown", self._on_factory_teardown)

        self.list_view = Gtk.ListView(model=self._selection, factory=factory)
        # Not .navigation-sidebar: column rows follow Nautilus' list view, see the
        # .mc-column-list rules in my_computer_view.py's _CSS.
        self.list_view.add_css_class("mc-column-list")
        # Deliberately NOT set_single_click_activate(True): on Gtk.ListView
        # (unlike Gtk.ListBox's activate-on-single-click) that flag also makes
        # GtkListItemWidget select whatever row the pointer is hovering, which
        # fights the user's own selection -- the selected row would jump to
        # the mouse and vanish on mouse-out.
        # Pointer clicks don't need it anyway: they are dispatched manually by
        # the per-row GestureClick installed in _on_factory_setup, same #161
        # press/release pattern the old Gtk.ListBox version used, ported onto
        # the persistent recycled widget instead of a per-row post-load loop.
        # Gtk.ListView still emits "activate" for keyboard (Enter/Space) with
        # the flag off, so that path is unaffected.
        self.list_view.connect("activate", self._on_view_activated)

        # Matches Nautilus's own empty-folder state (nautilus-files-view.c
        # update_empty_view -- AdwStatusPage, "folder-symbolic" icon, "Folder
        # is Empty" title, no description). .compact keeps it readable at
        # column width. Gtk.ListView has no built-in placeholder concept (that
        # was GtkListBox-specific), so this simply *replaces* the scrolled
        # window's child when the folder turns out to be empty (see
        # _show_empty_page) -- only once loading has actually finished, so the
        # whole async-enumerate window doesn't flash "Folder is Empty" first.
        #
        # Deliberately NOT a Gtk.Stack holding both: Gtk.Stack does not
        # implement Gtk.Scrollable, so a Stack in this position makes
        # Gtk.ScrolledWindow wrap it in an internal Gtk.Viewport and hand the
        # ListView its full natural height instead of the viewport height.
        # GtkListView virtualizes against its own allocation, so that
        # allocation being the whole list's height means it realizes rows for
        # (nearly) the entire folder -- measured at ~205 bound rows for both a
        # 240-item and a 1001-item folder, i.e. no virtualization at all. The
        # ListView has to be the ScrolledWindow's direct child for its
        # adjustments to be driven by real scrolling.
        self._empty_page = Adw.StatusPage()
        self._empty_page.set_icon_name("folder-symbolic")
        self._empty_page.set_title(_native("Folder is Empty"))
        self._empty_page.add_css_class("compact")

        self.set_child(self.list_view)

        background_drop = self._new_drop_target()
        background_drop.connect("motion", self._on_background_drop_motion)
        background_drop.connect("drop", self._on_background_drop)
        self.add_controller(background_drop)

        # Outline the column for any drag over it, rows included. A motion
        # controller in CAPTURE never claims the event, so it sees drags that
        # a row's own target accepts or rejects alike, and the column being
        # dragged from.
        drag_over = Gtk.DropControllerMotion()
        drag_over.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        # A style class, not :drop(active): the column's own Gtk.DropTarget
        # clears that state flag whenever it rejects a drag.
        drag_over.connect("enter", lambda *_a: self.add_css_class("mc-drop-over"))
        drag_over.connect("leave", lambda *_a: self.remove_css_class("mc-drop-over"))
        self.add_controller(drag_over)

        self._load(self._selection_state.begin_reload())

    def _show_empty_page(self, empty: bool) -> None:
        """Swap the scrolled window's child between the row list and the
        empty-folder status page. See the __init__ comment above for why this
        is a child swap rather than a Gtk.Stack."""
        wanted = self._empty_page if empty else self.list_view
        if self.get_child() is not wanted:
            self.set_child(wanted)

    def _on_factory_setup(self, _factory, list_item: Gtk.ListItem) -> None:
        row = MyComputerColumnRow()
        list_item.set_child(row)
        # GtkListView's internal per-item container runs its own
        # Gtk.GestureClick that grabs keyboard focus unconditionally on press
        # when focusable (same mechanism as GtkGridView's GtkListFactoryWidget
        # -- see project memory project_gtk_state_flag_mechanics.md), which
        # paints a premature focus-visible outline before our own release-time
        # dispatch below ever runs. set_focusable(False) here is the
        # documented fix, proxying straight through to that container.
        list_item.set_focusable(False)

        click = Gtk.GestureClick(button=0)
        click.connect("pressed", self._on_row_widget_pressed, row)
        click.connect("released", self._on_row_widget_released, row)
        row.add_controller(click)

        # CAPTURE like nautilus-list-base.c, so a drag is not shadowed by the
        # click gesture. The press stays unclaimed (#161), so a plain click
        # still reaches _on_row_widget_released.
        drag = Gtk.DragSource()
        drag.set_actions(Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        drag.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        drag.connect("prepare", self._on_row_drag_prepare, row)
        drag.connect("drag-begin", self._on_row_drag_begin, row)
        drag.connect("drag-end", self._on_row_drag_end)
        row.add_controller(drag)

        row_drop = self._new_drop_target()
        row_drop.connect("accept", lambda _t, _d: row.is_dir)
        row_drop.connect("motion", self._on_row_drop_motion, row)
        row_drop.connect("leave", self._on_row_drop_leave, row)
        row_drop.connect("drop", self._on_row_drop, row)
        row.add_controller(row_drop)

    @staticmethod
    def _new_drop_target() -> Gtk.DropTarget:
        target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        target.set_preload(True)
        return target

    @staticmethod
    def _dropped_uris(value) -> list[str]:
        try:
            return [f.get_uri() for f in value.get_files()]
        except (AttributeError, TypeError):
            return []

    def _drop_action(self, target: Gtk.DropTarget, destination_uri: str) -> Gdk.DragAction:
        """Action the pointer currently offers, 0 when nothing can be dropped.
        Ctrl/Shift reach us as a drag restricted to COPY/MOVE; otherwise the
        native preferred action decides."""
        value = target.get_value()
        uris = self._dropped_uris(value) if value is not None else []
        drop = target.get_current_drop()
        if not uris or drop is None:
            return 0
        offered = drop.get_actions() & (Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        if offered == Gdk.DragAction.COPY or offered == Gdk.DragAction.MOVE:
            action = offered
        else:
            move = _preferred_move(uris, destination_uri)
            action = Gdk.DragAction.MOVE if move else Gdk.DragAction.COPY
        if not _droppable_uris(uris, destination_uri, is_move=action == Gdk.DragAction.MOVE):
            return 0
        path = Gio.File.new_for_uri(destination_uri).get_path()
        if path is not None and not os.access(path, os.W_OK):
            return 0
        return action

    def _finish_drop(self, target: Gtk.DropTarget, value, destination_uri: str) -> bool:
        action = self._drop_action(target, destination_uri)
        if not action or self._on_files_dropped is None:
            return False
        move = action == Gdk.DragAction.MOVE
        uris = _droppable_uris(self._dropped_uris(value), destination_uri, is_move=move)
        if not uris:
            return False
        self._on_files_dropped(uris, destination_uri, move)
        return True

    def _on_row_drop_motion(self, target, _x, _y, row: "MyComputerColumnRow"):
        uri = row.uri
        action = self._drop_action(target, uri) if uri else 0
        row_node = row.row_node()
        if row_node is not None:
            set_row_drop_active(row_node, bool(action))
        if action and self._ext._nautilus_prefs.open_folder_on_dnd_hover():
            self._arm_hover_open(row)
        else:
            self._cancel_hover_open()
        return action

    def _on_row_drop_leave(self, _target, row: "MyComputerColumnRow") -> None:
        row_node = row.row_node()
        if row_node is not None:
            set_row_drop_active(row_node, False)
        self._cancel_hover_open()

    def _on_row_drop(self, target, value, _x, _y, row: "MyComputerColumnRow") -> bool:
        self._on_row_drop_leave(target, row)
        return bool(row.uri) and self._finish_drop(target, value, row.uri)

    def _on_background_drop_motion(self, target, _x, _y):
        return self._drop_action(target, self.folder_uri)

    def _on_background_drop(self, target, value, _x, _y) -> bool:
        return self._finish_drop(target, value, self.folder_uri)

    def _arm_hover_open(self, row: "MyComputerColumnRow") -> None:
        # Exception to the no-timers rule, approved for this constant only:
        # native Nautilus uses the same one-shot HOVER_TIMEOUT.
        if self._hover_row is row and self._hover_timer:
            return
        self._cancel_hover_open()
        self._hover_row = row
        self._hover_timer = GLib.timeout_add(DND_HOVER_OPEN_MS, self._on_hover_open)

    def _cancel_hover_open(self) -> None:
        if self._hover_timer:
            GLib.source_remove(self._hover_timer)
        self._hover_timer = 0
        self._hover_row = None

    def _on_hover_open(self) -> bool:
        row = self._hover_row
        self._hover_timer = 0
        self._hover_row = None
        if row is not None and row.is_dir:
            self._on_row_activated(self, row, dnd_hover=True)
        return GLib.SOURCE_REMOVE

    def _on_row_drag_prepare(self, _source, _x, _y, row: "MyComputerColumnRow"):
        # Read the item live: the recycled row may have been rebound since setup.
        item = row.item
        if item is None:
            return None
        if self._on_drag_start is not None:
            self._on_drag_start(self)
        if not self.is_item_selected(item):
            self.select_only_item(item)
        uris = self.selected_uris()
        if not uris:
            return None
        file_list = Gdk.FileList.new_from_list([Gio.File.new_for_uri(u) for u in uris])
        value = GObject.Value()
        value.init(Gdk.FileList)
        value.set_boxed(file_list)
        text = "".join(f"{u}\r\n" for u in uris)
        return Gdk.ContentProvider.new_union(
            [
                Gdk.ContentProvider.new_for_value(value),
                Gdk.ContentProvider.new_for_bytes("text/uri-list", GLib.Bytes.new(text.encode())),
            ]
        )

    def _on_row_drag_begin(self, source, _drag, row: "MyComputerColumnRow") -> None:
        source.set_icon(Gtk.WidgetPaintable.new(row._icon_stack), 0, 0)

    def _on_row_drag_end(self, _source, _drag, _delete_data) -> None:
        if self._on_drag_end is not None:
            self._on_drag_end(self)

    def _on_row_widget_pressed(
        self,
        gesture: Gtk.GestureClick,
        n_press: int,
        x: float,
        y: float,
        row: "MyComputerColumnRow",
    ) -> None:
        if self._on_row_pressed is not None and row.item is not None:
            self._on_row_pressed(gesture, n_press, x, y, self, row)

    def _on_row_widget_released(
        self,
        gesture: Gtk.GestureClick,
        n_press: int,
        x: float,
        y: float,
        row: "MyComputerColumnRow",
    ) -> None:
        if self._on_row_released is not None and row.item is not None:
            self._on_row_released(gesture, n_press, x, y, self, row)

    def _on_factory_bind(self, _factory, list_item: Gtk.ListItem) -> None:
        item: _ColumnRowItem = list_item.get_item()
        row: MyComputerColumnRow = list_item.get_child()
        row.bind(item)
        self._row_widgets[id(item)] = row
        row.set_path(self._is_path_item(item))
        self._apply_row_join(row)

    def _is_path_item(self, item) -> bool:
        """The path mark shows only while the column has no selection: a
        selection always wins, so a drag, a Ctrl/Shift click or an arrow key
        moving the selection can never leave the old path row blue next to it
        (#189). Read at bind time and on every selection change."""
        return (
            self.path_uri is not None
            and not self._selection.shown
            and uri_utils.same_uri(item.uri, self.path_uri)
        )

    def paint(self, path_uri: str | None, accent: bool) -> None:
        """The column's only paint input, given by ColumnView._repaint: the
        row the Miller path runs through, and whether this column is the one
        blue column. Rows re-derive their marks from it at bind time too, so a
        recycled row widget never keeps a stale mark."""
        if accent != self._accent:
            self._accent = accent
            # Toggled on the ListView, not the row: the selected row's widget
            # can be recycled away, the ListView stays (see the
            # .mc-column-view-highlighted-row rules in my_computer_view.py).
            if accent:
                self.list_view.add_css_class("mc-column-view-highlighted-row")
            else:
                self.list_view.remove_css_class("mc-column-view-highlighted-row")
        if path_uri != self.path_uri:
            self.path_uri = path_uri
            self._refresh_path_marks()

    def _refresh_path_marks(self) -> None:
        for row in self._row_widgets.values():
            if row.item is not None:
                row.set_path(self._is_path_item(row.item))

    def _on_factory_unbind(self, _factory, list_item: Gtk.ListItem) -> None:
        row: MyComputerColumnRow = list_item.get_child()
        if row.item is not None:
            self._row_widgets.pop(id(row.item), None)
        # Never touch the parent here: unbind also runs while the column is
        # being unparented (paned rebuild), and a half-disposed GtkListItemWidget
        # segfaults. Bind recomputes both join classes, so stale ones self-heal.
        row.unbind()

    def _on_factory_teardown(self, _factory, list_item: Gtk.ListItem) -> None:
        list_item.set_child(None)

    def _on_view_activated(self, _list_view: Gtk.ListView, position: int) -> None:
        """Keyboard (Enter/Space) activation path. Pointer single-clicks are
        claimed and dispatched by the manual press/release controller in
        _on_factory_setup instead (see that controller's docstring), so this
        only ever fires from real keyboard activation on a focused row --
        which by definition has a live, bound widget."""
        item = self._store.get_item(position)
        if item is None:
            return
        row = self._row_widgets.get(id(item))
        if row is not None:
            self.activate_row(row)

    def set_sort(self, sort: tuple[str, bool, bool]) -> None:
        """Update this column's sort and reload. Always reloads regardless of
        whether sort actually changed: this is only ever called from
        refresh_column_view, which also needs it to pick up other prefs
        changes (hidden-files) that reload() alone would apply on its own --
        an early-return here when sort is unchanged would skip that too."""
        self._sort = sort
        self.reload()

    def move_root(self, old_root: str, new_root: str) -> None:
        """Show the same folder under another scheme root (file:/// and
        admin:///, issue #187): rebase the location and the selection, then
        list it again."""
        self.folder_uri = uri_utils.rebase(self.folder_uri, old_root, new_root)
        self.reload()
        self._selection_state.rebase(old_root, new_root)

    def reload(self) -> None:
        """Re-enumerate this column's own folder in place (e.g. after the
        hidden-files setting changes), without touching sibling columns or
        collapsing the Miller chain."""
        self._cancellable.cancel()
        self._cancellable = Gio.Cancellable()
        self._loaded = False
        self._show_empty_page(False)
        gen = self._selection_state.begin_reload()
        self._store.remove_all()
        self._load(gen)

    def _load(self, gen: int) -> None:
        gfile = Gio.File.new_for_uri(self.folder_uri)
        gfile.enumerate_children_async(
            "standard::name,standard::display-name,standard::icon,"
            "standard::is-hidden,standard::is-backup,standard::type,standard::content-type,"
            "standard::size,time::modified,time::created,time::access,"
            "metadata::custom-icon,metadata::custom-icon-name,"
            "access::can-read,access::can-execute",
            Gio.FileQueryInfoFlags.NONE,
            GLib.PRIORITY_DEFAULT,
            self._cancellable,
            self._on_enumerator_ready,
            gen,
        )

    def _on_enumerator_ready(self, gfile: Gio.File, result: Gio.AsyncResult, gen: int) -> None:
        try:
            enumerator = gfile.enumerate_children_finish(result)
        except GLib.Error:
            self._selection_state.abort_reload(gen)
            return
        enumerator.next_files_async(
            200, GLib.PRIORITY_DEFAULT, self._cancellable, self._on_next_files_ready, [], gen
        )

    def _on_next_files_ready(
        self, enumerator: Gio.FileEnumerator, result: Gio.AsyncResult, collected: list, gen: int
    ) -> None:
        try:
            infos = enumerator.next_files_finish(result)
        except GLib.Error:
            self._selection_state.abort_reload(gen)
            return
        if infos:
            collected.extend(infos)
            enumerator.next_files_async(
                200,
                GLib.PRIORITY_DEFAULT,
                self._cancellable,
                self._on_next_files_ready,
                collected,
                gen,
            )
            return
        self._maybe_count_dirs_then_populate(collected, gen)

    def _maybe_count_dirs_then_populate(self, infos: list, gen: int) -> None:
        # Directory item counts are needed for sorting by size (native
        # Nautilus: a folder's "size" is its item count, never its on-disk
        # byte size -- see compare_by_size in nautilus-file.c). Always count
        # regardless of the active sort mode, since sort can change later.
        dir_names = [
            info.get_name() for info in infos if info.get_file_type() == Gio.FileType.DIRECTORY
        ]
        if not dir_names:
            self._populate_rows(infos, {}, gen)
            return
        dir_counts: dict[str, int] = {}
        pending = len(dir_names)

        def on_one_done() -> None:
            nonlocal pending
            pending -= 1
            if pending == 0:
                self._populate_rows(infos, dir_counts, gen)

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

    def _populate_rows(self, infos: list, dir_counts: dict, gen: int) -> None:
        if gen != self._selection_state.reload_gen:
            # A newer reload superseded this enumeration (its dir counts may
            # have been cancelled): never splice its stale rows in.
            return
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
                    # GVfs backends can omit standard::content-type even when
                    # requested; get_content_type() would log a GLib critical.
                    content_type=(
                        info.get_content_type()
                        if info.has_attribute("standard::content-type")
                        else None
                    ),
                    is_hidden=is_hidden,
                    size=size,
                    mtime=info.get_attribute_uint64("time::modified"),
                    btime=info.get_attribute_uint64("time::created"),
                    atime=info.get_attribute_uint64("time::access"),
                    can_read=_access_allowed(info, "access::can-read"),
                    can_execute=_access_allowed(info, "access::can-execute"),
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

        _log(
            "view.sort column populated "
            f"uri={self.folder_uri!r} sort={self._sort!r} "
            f"first_10={[entry.display_name for entry in entries[:10]]!r}"
        )

        base = Gio.File.new_for_uri(self.folder_uri)
        items = [
            _ColumnRowItem(
                base.get_child(entry.name).get_uri(),
                entry.display_name,
                entry.is_dir,
                entry.icon,
                entry.is_hidden,
                content_type=entry.content_type,
                mtime=entry.mtime,
                can_read=entry.can_read,
                can_execute=entry.can_execute,
            )
            for entry in entries
        ]
        self._show_empty_page(not items)
        if items:
            self._store.splice(0, 0, items)
        self._selection_state.end_reload(gen, [item.uri for item in items])
        self._notify_selection_changed()

        self._loaded = True
        if callable(self._on_loaded):
            self._on_loaded(self)

    def _index_for_uri(self, uri: str) -> int | None:
        for i in range(self._store.get_n_items()):
            if uri_utils.same_uri(self._store.get_item(i).uri, uri):
                return i
        return None

    def select_child_for_uri(self, uri: str) -> None:
        """Pre-select (highlight, without activating) the row whose child URI
        matches uri -- used to show which entry leads to the next column when
        seeding the view from the current location's ancestor chain."""
        index = self._index_for_uri(uri)
        if index is not None:
            self._selection_state.select_only(index)

    def selected_positions(self) -> list[int]:
        """Every selected position, ascending."""
        return self._selection_state.positions()

    def selected_items(self) -> list["_ColumnRowItem"]:
        return self._selection_state.items()

    def selected_uris(self) -> list[str]:
        return self._selection_state.uris()

    def selection_count(self) -> int:
        return self._selection_state.count()

    def is_item_selected(self, item) -> bool:
        return self._selection_state.contains(item)

    def select_all(self) -> None:
        self._selection_state.select_all()

    def modifier_click(self, item, *, ctrl: bool, shift: bool) -> None:
        """Apply a Ctrl (toggle) or Shift (range from last click) click."""
        self._selection_state.modifier_click(item, ctrl=ctrl, shift=shift)

    def select_only_item(self, item) -> None:
        """Drop any multi-selection and keep just this row selected."""
        self._selection_state.select_only_item(item)

    def connect_selection_changed(self, callback) -> None:
        """Call callback() after any change of this column's row selection."""
        if not self._selection_callbacks:
            self._selection.connect(
                "selection-changed", lambda *_args: self._notify_selection_changed()
            )
        self._selection_callbacks.append(callback)

    def _notify_selection_changed(self) -> None:
        """Tell listeners the selection changed. Silent during a reload, whose
        transient empty store is not a user change; end_reload notifies once."""
        if self._selection_state.reloading:
            return
        for callback in self._selection_callbacks:
            callback()

    def set_selected_uris(self, uris) -> None:
        """Select exactly these URIs, also before the rows have loaded."""
        self._selection_state.set_uris(uris)

    def clear_selection(self) -> None:
        """Drop this column's own row selection -- used by column_view.py's
        _on_real_row_activated so only the row that led to the current
        column/preview stays highlighted, never an earlier column too."""
        self._selection_state.clear()

    def select_index(self, index: int) -> "_ColumnRowItem | None":
        """Select this column's entry at index (clamped into range) and
        return its model item, or None if the folder is empty (or hasn't
        finished enumerating yet).

        Drives every keyboard-originated selection change (see
        column_view.py's _focus_child_column and _move_column_selection).
        The selection-changed listeners fire as for any other change; the
        Miller commit is not one of them, so the caller arms it explicitly
        off the returned item."""
        n = self._store.get_n_items()
        if n == 0:
            return None
        index = max(0, min(index, n - 1))
        self._selection_state.select_only(index)
        return self._store.get_item(index)

    def select_first(self) -> "_ColumnRowItem | None":
        """Select this column's first entry -- used when keyboard navigation
        moves into a column that has no selection of its own yet (see
        column_view.py's _focus_child_column)."""
        return self.select_index(0)

    def has_selection(self) -> bool:
        return self.selection_count() > 0

    def shows_selection(self) -> bool:
        """Whether a selection is on screen, without walking the rows (read on
        every repaint, see ColumnView._repaint)."""
        return bool(self._selection.shown)

    def selected_index(self) -> int | None:
        """The anchor row's position (the only one when one row is selected)."""
        return self._selection_state.primary()

    def item_count(self) -> int:
        return self._store.get_n_items()

    def is_loaded(self) -> bool:
        return self._loaded

    def contains_item(self, item) -> bool:
        """Whether item is still one of this column's live model entries -- a
        reload replaces every item, so anything holding one across an async
        gap (the debounced keyboard commit) has to re-check before acting."""
        found, _position = self._store.find(item)
        return found

    def page_step(self) -> int | None:
        """Rows to jump for Page Up/Page Down (see column_view.py's
        _move_column_selection): the number of rows currently visible in
        the viewport, minus one -- the same "leave one row of overlap for
        context" convention most list/text widgets use for paging, rather
        than jumping a full page with nothing carried over.

        GtkListView exposes no live "how many rows are visible" query of
        its own (checked: nothing on the public Gtk.ListView API surface
        reports rendered/visible item count), so this is derived from two
        values that are: the real viewport height (Gtk.Adjustment's own
        get_page_size(), the same value GTK's own native page-up/down
        reads internally) divided by a row's real SIZE (get_allocated_height(),
        sampled off any currently realized row). Deliberately never a row's
        on-screen POSITION: reading each realized row's rectangle via
        compute_bounds() was tried and reverted during #91, because rows
        GTK has realized but not yet positioned against the current scroll
        offset report bounds near y=0 regardless of where they really are,
        making every realized row look "visible" and turning Page Up/Down
        into Home/End. Recomputed fresh on every call rather than cached,
        so a window resize is automatically reflected on the very next
        keypress with no separate resize handler needed.

        None if nothing is realized yet to measure a row height from -- the
        caller then jumps straight to the near edge (row 0 / the last row)
        instead of guessing a made-up row count. A column with fewer rows
        than fit in one page lands on that same edge too, but via the
        ordinary min/max clamp on a real step in the caller -- this None
        case is only for when no step can be computed at all."""
        page_size = self.get_vadjustment().get_page_size()
        if page_size <= 0:
            return None
        # Scan until a row reports a real height rather than trusting only
        # the first entry in _row_widgets: that dict is insertion-ordered,
        # not viewport-ordered, and whichever row happens to be first can
        # transiently read 0 (mid-relayout, e.g. right after a fast
        # scroll_to) even while plenty of others in the same dict are
        # already settled -- bailing out on that single candidate meant a
        # rapid run of Page Down presses could suddenly see page_step()
        # return None and jump to the true last row instead of stepping.
        for row in self._row_widgets.values():
            row_height = row.get_allocated_height()
            if row_height > 0:
                visible_rows = int(page_size // row_height)
                return max(1, visible_rows - 1)
        return None

    def _refresh_rows(self) -> None:
        for row in self._row_widgets.values():
            if row.item is not None:
                row.set_path(self._is_path_item(row.item))
            self._apply_row_join(row)

    def _apply_row_join(self, row: "MyComputerColumnRow") -> None:
        """Finder-style blocks: a selected row whose neighbour is also selected
        loses the corner radius on that side, so a run of selected rows reads
        as one block rounded only at its first and last row. The classes go on
        our own row content, which paints the selection (see the
        .mc-column-list rules in my_computer_view.py); Nautilus's
        GtkListItemWidget is never touched, mutating it crashed on rebuilds."""
        if row.item is None:
            return
        found, pos = self._store.find(row.item)
        if not found:
            return
        sel = self._selection
        selected = sel.is_selected(pos)
        above = selected and pos > 0 and sel.is_selected(pos - 1)
        below = selected and sel.is_selected(pos + 1)
        for name, on in (("mc-join-above", above), ("mc-join-below", below)):
            if on:
                row.add_css_class(name)
            else:
                row.remove_css_class(name)

    def apply_cut_uris(self, cut_uris: set[str]) -> None:
        """Apply cut styling to every item whose uri is in cut_uris, and clear
        it from every other item. Cut state lives on the model item (see
        _ColumnRowItem.is_cut), not just the transient widget, so scrolling an
        item back into view after it was cut off-screen still shows it cut;
        any currently realized widget is also patched immediately via
        _row_widgets so an on-screen row updates without waiting for a rebind."""
        for i in range(self._store.get_n_items()):
            item = self._store.get_item(i)
            item.is_cut = item.uri in cut_uris
            widget = self._row_widgets.get(id(item))
            if widget is not None:
                widget.set_cut(item.is_cut)

    def realized_rows(self) -> list["MyComputerColumnRow"]:
        """Every row widget currently bound to a model item -- i.e. every
        entry actually scrolled into view right now. Used only for
        transient, purely-visual per-widget state (clearing a stray :active
        flag left by a closed context menu, see column_view.py's
        _clear_cut_rows) -- persistent state belongs on the model item
        instead (see apply_cut_uris), since an off-screen entry has no live
        widget at all under GtkListView's recycling."""
        return list(self._row_widgets.values())

    def selected_item(self) -> "_ColumnRowItem | None":
        """The selected entry's model item, realized on screen or not.

        This -- not selected_row() -- is what anything acting on the selected
        *file* should read (trash, cut/copy, paste target): under
        GtkListView's recycling an item scrolled out of view has no widget at
        all, so a selection-driven shortcut keyed off the widget would
        silently do nothing purely because the user had scrolled away."""
        index = self.selected_index()
        return None if index is None else self._store.get_item(index)

    def selected_row(self) -> "MyComputerColumnRow | None":
        """The currently selected row's widget, if it is realized (scrolled
        into view) right now -- a selected item scrolled out of view has no
        live widget, per GtkListView's recycling model. Prefer
        selected_item() unless a real widget is genuinely required (only
        Rename is, as a popover anchor -- see with_selected_row)."""
        item = self.selected_item()
        if item is None:
            return None
        return self._row_widgets.get(id(item))

    def with_selected_row(self, on_row_ready) -> bool:
        """Hand the selected entry's row widget to on_row_ready, scrolling it
        back into view first if recycling has left it unrealized.

        Only Rename needs this: its popover has to anchor on a real widget.
        Everything else selection-driven reads selected_item() instead. The
        wait is a bounded tick callback rather than one GLib.idle_add because
        the bind happens during the frame-clock layout that scroll_to
        schedules, which a single default-priority idle can still beat."""
        position = self.selected_index()
        if position is None:
            return False
        item = self._store.get_item(position)
        if item is None:
            return False

        row = self._row_widgets.get(id(item))
        if row is not None:
            on_row_ready(row)
            return True

        self.list_view.scroll_to(position, Gtk.ListScrollFlags.NONE, None)
        state = {"ticks_left": _SELECTED_ROW_REALIZE_FRAMES}

        def _deliver_on_tick(_widget, _frame_clock) -> bool:
            widget = self._row_widgets.get(id(item))
            if widget is not None:
                on_row_ready(widget)
                return GLib.SOURCE_REMOVE
            state["ticks_left"] -= 1
            return GLib.SOURCE_CONTINUE if state["ticks_left"] > 0 else GLib.SOURCE_REMOVE

        self.list_view.add_tick_callback(_deliver_on_tick)
        return True

    def scroll_position(self) -> float:
        """This column's own vertical scroll offset, read live off the
        native Gtk.ScrolledWindow adjustment -- same pattern as
        selected_row(), not a value tracked separately on the object."""
        return self.get_vadjustment().get_value()

    def grab_list_focus(self) -> bool:
        return self.list_view.grab_focus()

    def activate_row(self, row: "MyComputerColumnRow") -> None:
        # Miller browsing itself is always single-click, for every policy: one
        # click drills into a folder or previews a file, exactly like the
        # folder-column drill-down (set_activate_on_single_click above). Once a
        # file is already the active preview (the "last" column), a further
        # click on that same row is an *open* action rather than navigation,
        # and Nautilus' click-policy governs it there -- same contract as the
        # preview pane (_on_preview_area_pressed/_released), just re-derived by
        # timing instead of n_press since the chain rebuild below destroys
        # GTK's own press-count tracking on the row before a second click can
        # land (see the field comment above).
        now = GLib.get_monotonic_time()
        double_click_us = Gtk.Settings.get_default().get_property("gtk-double-click-time") * 1000
        is_same_file = not row.is_dir and row.uri == self._last_activated_uri
        is_repeat_click = is_same_file and (now - self._last_activated_time) <= double_click_us
        self._last_activated_uri = row.uri
        self._last_activated_time = now

        single_click = self._ext._nautilus_prefs.click_policy == "single"
        if is_same_file and (is_repeat_click or single_click):
            # Single policy: every further click on the already-active file
            # opens it, no timing needed -- it already selected/previewed on
            # the click before this one. Double policy: only a genuine repeat
            # click within the double-click window opens it.
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
    click handlers and file rows in the folder columns, so both surfaces open
    a file the same way."""
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


_STACK_MAX_ITEMS = 3
# Finder-style pose: the front item is straight at its natural size, every item
# under it is tilted and slightly larger so its edges peek out around the front.
_STACK_UNDER_ANGLE = 8.0
_STACK_UNDER_SCALE = 1.1


def _stack_pose(rank: int) -> tuple[float, float]:
    """(rotation in degrees, scale) of the stacked item `rank` places from the
    front. Single place to change the look (e.g. a random angle method)."""
    if rank == 0:
        return 0.0, 1.0
    angle = _STACK_UNDER_ANGLE if rank % 2 else -_STACK_UNDER_ANGLE
    return angle, _STACK_UNDER_SCALE


class MyComputerStackPreview(Gtk.Fixed):
    """Finder-style preview of a multi-selection: the last few selected items
    as a stack of icons (or decoded pictures for images), the last one in
    front. Also totals the whole selection (documents, folders, size) and
    reports it through `on_summary(text)` once every item has been queried."""

    __gtype_name__ = "MyComputerStackPreview"

    def __init__(self, uris: list[str], on_summary) -> None:
        super().__init__()
        self._uris = list(uris)
        self._on_summary = on_summary
        self._cancellable = Gio.Cancellable()
        self._items: dict[str, Gtk.Box] = {}
        self._pending = len(self._uris)
        self._files = 0
        self._folders = 0
        self._bytes = 0

        # Sized by its children and never clipped: tilted corners and shadows
        # extend past the front item.
        self.set_size_request(-1, -1)
        self.set_overflow(Gtk.Overflow.VISIBLE)
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.CENTER)

        # Last items of the list sit in front; add back to front so the front
        # one draws on top.
        shown = self._uris[-_STACK_MAX_ITEMS:][::-1]
        center = _PREVIEW_ICON_SIZE / 2
        for rank in range(len(shown) - 1, -1, -1):
            # The wrapper is what gets transformed; its content (icon or
            # picture) is swapped in place once an image has been decoded.
            item = Gtk.Box()
            item.set_size_request(_PREVIEW_ICON_SIZE, _PREVIEW_ICON_SIZE)
            icon = Gtk.Image()
            icon.set_pixel_size(_PREVIEW_ICON_SIZE)
            icon.set_from_icon_name("text-x-generic")
            # Hidden until the item's type is known: showing a placeholder
            # first would flash before the real icon or the picture arrives.
            icon.set_visible(False)
            icon.add_css_class("mc-stack-icon")
            item.append(icon)
            self.put(item, 0, 0)
            angle, scale = _stack_pose(rank)
            transform = (
                Gsk.Transform()
                .translate(Graphene.Point().init(center, center))
                .rotate(angle)
                .scale(scale, scale)
                .translate(Graphene.Point().init(-center, -center))
            )
            self.set_child_transform(item, transform)
            self._items[shown[rank]] = item

        for uri in self._uris:
            Gio.File.new_for_uri(uri).query_info_async(
                "standard::icon,standard::type,standard::content-type,standard::size,time::modified",
                Gio.FileQueryInfoFlags.NONE,
                GLib.PRIORITY_DEFAULT,
                self._cancellable,
                self._on_info_ready,
                uri,
            )

    def cancel(self) -> None:
        self._cancellable.cancel()

    def _on_info_ready(self, gfile: Gio.File, result: Gio.AsyncResult, uri: str) -> None:
        if self._cancellable.is_cancelled():
            return
        self._pending -= 1
        try:
            info = gfile.query_info_finish(result)
        except GLib.Error:
            info = None
        if info is None:
            self._files += 1
            item = self._items.get(uri)
            if item is not None:
                item.get_first_child().set_visible(True)
        else:
            if info.get_file_type() == Gio.FileType.DIRECTORY:
                self._folders += 1
            else:
                self._files += 1
                self._bytes += info.get_size()
            item = self._items.get(uri)
            if item is not None:
                gio_icon = info.get_icon()
                if _gicon_renders(gio_icon):
                    item.get_first_child().set_from_gicon(gio_icon)
                if not self._load_picture(item, uri, info):
                    item.get_first_child().set_visible(True)
        if self._pending == 0:
            self._on_summary(self._summary())

    def _load_picture(self, item: Gtk.Box, uri: str, info: Gio.FileInfo) -> bool:
        """Images show their real picture; every other type keeps its file
        icon (no document thumbnails). Returns True when a decode was started,
        in which case the icon stays hidden until the picture (or a failure)
        arrives."""
        content_type = info.get_content_type()
        if not content_type or not content_type.startswith("image/"):
            return False
        path = Gio.File.new_for_uri(uri).get_path()
        if path is None:
            return False
        mtime = info.get_attribute_uint64("time::modified")
        cached = _texture_cache.get(TEXTURE_STACK, uri, mtime)
        if cached is not None:
            self._apply_picture(item, *cached)
            return True
        threading.Thread(
            target=self._picture_worker, args=(item, path, uri, mtime), daemon=True
        ).start()
        return True

    def _picture_worker(self, item: Gtk.Box, path: str, uri: str, mtime: int) -> None:
        # GNOME's own 256 px thumbnail (kept current by mtime, shared with
        # Nautilus) is far cheaper to read than the original; never generated
        # here, so no thumbnailer subprocess. Without one, decode the original.
        thumb_path = _thumb_factory.lookup(uri, mtime) if _thumb_factory is not None else None
        pixbuf = _decode_preview_pixbuf(thumb_path or path, _PREVIEW_ICON_SIZE)
        if pixbuf is None and thumb_path:
            pixbuf = _decode_preview_pixbuf(path, _PREVIEW_ICON_SIZE)
        kind = _pixbuf_alpha_kind(pixbuf) if pixbuf is not None else "blank"
        if kind == "blank":
            GLib.idle_add(self._apply_picture, item, None)
            return
        if self._cancellable.is_cancelled():
            return
        texture = Gdk.Texture.new_for_pixbuf(pixbuf)
        GLib.idle_add(self._store_picture, item, uri, mtime, texture, kind == "translucent")

    def _store_picture(
        self, item: Gtk.Box, uri: str, mtime: int, texture: Gdk.Texture, translucent: bool
    ) -> int:
        _texture_cache.put(TEXTURE_STACK, uri, mtime, texture, translucent)
        return self._apply_picture(item, texture, translucent)

    def _apply_picture(
        self, item: Gtk.Box, texture: Gdk.Texture | None, translucent: bool = False
    ) -> int:
        if texture is None:
            # Decode failed or came out blank: fall back to the file icon.
            item.get_first_child().set_visible(True)
        elif not self._cancellable.is_cancelled():
            # Sized to the texture itself so the rounded clip and the shadow
            # follow the real image edges, not the square slot. Cut-out images
            # (real transparency) use a Gtk.Image instead: its icon shadow is
            # cast from the alpha shape, where a box-shadow would outline the
            # bounding box.
            if translucent:
                picture = Gtk.Image.new_from_paintable(texture)
                # GtkImage ignores a paintable's own size and falls back to the
                # icon size (16 px): pin it to the texture's longest side.
                picture.set_pixel_size(max(texture.get_width(), texture.get_height()))
                picture.add_css_class("mc-stack-icon")
            else:
                picture = Gtk.Picture.new_for_paintable(texture)
                picture.set_overflow(Gtk.Overflow.HIDDEN)
                picture.add_css_class("mc-stack-image")
            picture.set_halign(Gtk.Align.CENTER)
            picture.set_valign(Gtk.Align.CENTER)
            picture.set_hexpand(True)
            item.remove(item.get_first_child())
            item.append(picture)
        return GLib.SOURCE_REMOVE

    def _summary(self) -> str:
        parts = []
        if self._files:
            parts.append(_n("{n} document", "{n} documents", self._files).format(n=self._files))
        if self._folders:
            parts.append(_n("{n} folder", "{n} folders", self._folders).format(n=self._folders))
        counts = (
            _("{first} and {second}").format(first=parts[0], second=parts[1])
            if len(parts) == 2
            else parts[0]
        )
        if self._files:
            return f"{counts}, {_format_size(self._bytes)}"
        return counts


class MyComputerPreviewColumn(Gtk.Box):
    """A preview column split between a responsive image area and bottom-pinned
    file details. Always the permanent rightmost column in the chain (see
    column_view.py's populate_column_view / _on_row_activated, which always
    (re)append one after any truncate). file_uri is None for its empty state.
    Real file preview (text, ...) is a later iteration."""

    __gtype_name__ = "MyComputerPreviewColumn"

    def __init__(self, ext, file_uri: str | None, uris: list[str] | None = None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._ext = ext
        # More than one selected item switches the whole widget to the
        # Finder-style stack summary; file_uri then stays None so callers that
        # read it (column_view.py chain sync) see "no single previewed file".
        self._uris = list(uris) if uris and len(uris) > 1 else []
        self.is_multi = bool(self._uris)
        self.file_uri = None if self.is_multi else file_uri
        self._stack = None
        self._cancellable = Gio.Cancellable()
        self._discoverer = None
        # Deferred single-click-policy activation, set on "pressed" and consumed on
        # "released" -- see _on_preview_area_pressed/_released/_stopped. Initialized above
        # the file_uri is None early-return below so it exists on empty-state instances too.
        self._activate_on_release = False

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

        if self.file_uri is None and not self.is_multi:
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
        # Open the file with its default app on click, honoring Nautilus' own
        # single-click/double-click setting -- unlike the folder columns to its
        # left, which are always single-click (Miller drill-down, see
        # MyComputerColumn). Mirrors the native item-cell press/release state
        # machine (nautilus-list-base.c on_item_click_pressed/released/stopped):
        # double-click policy activates on the second press; single-click policy
        # defers to release so a press that turns into a drag doesn't activate.
        # The gesture is left with GTK's default button (1, primary-only) --
        # middle/secondary never reach these handlers.
        click = Gtk.GestureClick()
        click.connect("pressed", self._on_preview_area_pressed)
        click.connect("released", self._on_preview_area_released)
        click.connect("stopped", self._on_preview_area_stopped)
        preview_area.add_controller(click)
        self.append(preview_area)

        self._icon = Gtk.Image()
        self._icon.set_pixel_size(_PREVIEW_ICON_SIZE)
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

        if self.is_multi:
            name_text = _format_item_count(len(self._uris))
        else:
            gfile = Gio.File.new_for_uri(self.file_uri)
            name_text = gfile.get_basename() or self.file_uri
        self._name_lbl = Gtk.Label(label=name_text)
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

        if self.is_multi:
            self._stack = MyComputerStackPreview(self._uris, self._detail_lbl.set_label)
            self._preview_stack.set_overflow(Gtk.Overflow.VISIBLE)
            self._thumb_revealer.set_overflow(Gtk.Overflow.VISIBLE)
            self._preview_stack.add_named(self._stack, PREVIEW_SLOT_STACK)
            self.set_preview_slot(PREVIEW_SLOT_STACK)
            return

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

    def _on_preview_area_pressed(
        self, gesture: Gtk.GestureClick, n_press: int, _x: float, _y: float
    ) -> None:
        modifiers = gesture.get_current_event_state() & Gtk.accelerator_get_default_mod_mask()
        selection_mode = bool(
            modifiers & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK)
        )
        if self.file_uri is None:
            return
        single_click = self._ext._nautilus_prefs.click_policy == "single"
        self._activate_on_release = single_click and n_press == 1 and not selection_mode
        if not single_click and n_press == 2 and not selection_mode:
            _open_file_with_default_app(self.file_uri, self._cancellable)
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)

    def _on_preview_area_released(
        self, _gesture: Gtk.GestureClick, _n_press: int, _x: float, _y: float
    ) -> None:
        if self._activate_on_release:
            _open_file_with_default_app(self.file_uri, self._cancellable)
        self._activate_on_release = False

    def _on_preview_area_stopped(self, _gesture: Gtk.GestureClick) -> None:
        self._activate_on_release = False

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
            self._load_preview_image(mtime)
        else:
            self.set_preview_slot(PREVIEW_SLOT_ICON)
            self._maybe_load_thumbnail(content_type, mtime)
        self._maybe_load_dimensions(content_type)

    def _load_preview_image(self, mtime: int) -> None:
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
        cached = _texture_cache.get(TEXTURE_SINGLE, self.file_uri, mtime)
        if cached is not None:
            self._apply_thumbnail(cached[0])
            return
        threading.Thread(
            target=self._preview_image_worker, args=(path, self.file_uri, mtime), daemon=True
        ).start()

    def _preview_image_worker(self, path: str, uri: str, mtime: int) -> None:
        pixbuf = _decode_preview_pixbuf(path, _COLUMN_PREVIEW_IMAGE_SIZE)
        if pixbuf is None:
            # Decode failures fall back from loading to the file icon.
            GLib.idle_add(self._show_icon)
            return
        if self._cancellable.is_cancelled():
            return
        texture = Gdk.Texture.new_for_pixbuf(pixbuf)
        GLib.idle_add(self._store_preview_image, uri, mtime, texture)

    def _store_preview_image(self, uri: str, mtime: int, texture: Gdk.Texture) -> int:
        _texture_cache.put(TEXTURE_SINGLE, uri, mtime, texture)
        return self._apply_thumbnail(texture)

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
            PREVIEW_SLOT_STACK,
        }:
            raise ValueError(f"unknown preview slot: {slot}")
        self._preview_stack.set_visible_child_name(slot)

    def destroy_enumeration(self) -> None:
        self._cancellable.cancel()
        if self._stack is not None:
            self._stack.cancel()
        if self._discoverer is not None:
            self._discoverer.stop()
