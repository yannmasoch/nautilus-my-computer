"""Shared dialogs, popovers, panels, and composite UI components.

This module owns presentation and generic GTK/GIO interaction. Feature
modules keep ownership of their state and pass a small completion callback,
so a dialog can be reused without depending on My Computer or Miller internals.
"""

from __future__ import annotations

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk

from nautilus_my_computer.common import _, _log, _native


def set_row_selected(row: Gtk.ListBoxRow, selected: bool) -> None:
    """Select or unselect one row through its owning ``Gtk.ListBox``."""
    list_box = row.get_parent()
    if not isinstance(list_box, Gtk.ListBox):
        return
    if selected:
        list_box.select_row(row)
    else:
        list_box.unselect_row(row)


def set_row_active(row: Gtk.Widget, active: bool) -> None:
    """Apply or clear GTK's pressed-state styling on a row-like widget."""
    if active:
        row.set_state_flags(Gtk.StateFlags.ACTIVE, False)
    else:
        row.unset_state_flags(Gtk.StateFlags.ACTIVE)


def show_rename_popover(parent: Gtk.Widget, uri: str, on_renamed, *, item_kind: str) -> None:
    """Show the Nautilus-style single-item rename editor.

    ``on_renamed(old_uri, new_uri)`` runs only after GIO has completed the
    rename. Callers own any model, navigation, or cache updates that follow.
    """
    gfile = Gio.File.new_for_uri(uri)
    old_name = gfile.get_basename() or uri

    popover = Gtk.Popover()
    popover.set_parent(parent)
    popover.set_position(Gtk.PositionType.BOTTOM)
    popover.set_autohide(True)
    popover.connect("unmap", lambda *_args: set_row_active(parent, False))

    def keep_anchor_active() -> bool:
        # The right-click's button release clears ACTIVE after the popover
        # first maps. Run one main-loop turn later so the anchor retains the
        # pressed appearance for the editor's full lifetime.
        if popover.get_mapped():
            set_row_active(parent, True)
        return GLib.SOURCE_REMOVE

    # Matches Nautilus's nautilus-rename-file-popover template.
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    box.set_margin_top(18)
    box.set_margin_bottom(18)
    box.set_margin_start(18)
    box.set_margin_end(18)
    popover.set_child(box)

    title = Gtk.Label(
        label=_native("Rename Folder") if item_kind == "folder" else _native("Rename File")
    )
    title.set_margin_bottom(12)
    title.add_css_class("title-2")
    box.append(title)

    name_entry = Gtk.Entry()
    name_entry.set_text(old_name)
    name_entry.set_activates_default(True)
    name_entry.set_width_chars(min(max(len(old_name), 30), 50))
    name_entry.set_margin_bottom(12)
    box.append(name_entry)

    rename = Gtk.Button(label=_("Rename"))
    rename.set_halign(Gtk.Align.END)
    rename.add_css_class("suggested-action")
    box.append(rename)

    def name_is_valid() -> bool:
        new_name = name_entry.get_text().strip()
        if not new_name:
            return False
        return "/" not in new_name and new_name not in (".", "..")

    def refresh_validation(*_args) -> None:
        rename.set_sensitive(name_is_valid())

    def on_rename_finished(source, result, _data) -> None:
        try:
            new_uri = source.set_display_name_finish(result).get_uri()
        except GLib.Error as error:
            _log(f"{item_kind.capitalize()} rename failed for {uri!r}: {error.message}")
            rename.set_sensitive(True)
            return
        popover.popdown()
        on_renamed(uri, new_uri)

    def submit(*_args) -> None:
        new_name = name_entry.get_text().strip()
        if not name_is_valid():
            return
        if new_name == old_name:
            popover.popdown()
            return
        rename.set_sensitive(False)
        gfile.set_display_name_async(
            new_name,
            GLib.PRIORITY_DEFAULT,
            None,
            on_rename_finished,
            None,
        )

    rename.connect("clicked", submit)
    name_entry.connect("activate", submit)
    name_entry.connect("changed", refresh_validation)
    refresh_validation()
    popover.popup()
    GLib.idle_add(keep_anchor_active)
    name_entry.grab_focus()
    name_entry.select_region(0, -1)
