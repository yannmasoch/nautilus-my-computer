"""Reusable context-menu models and native GTK popover construction."""

from __future__ import annotations

import dataclasses

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk

from nautilus_my_computer.common import _, _native


@dataclasses.dataclass
class ContextMenuItem:
    """One entry in a contextual menu.

    The action is a plain callable run on activation, so callers do not have
    to register Gio actions or manage action-name strings.
    """

    label: str
    action: object = None
    shortcut: str = ""
    enabled: bool = True
    visible: bool = True
    submenu: ContextMenu | None = None


@dataclasses.dataclass
class BuiltContextMenu:
    """A rendered menu model and the actions referenced by that model."""

    model: Gio.Menu
    action_group: Gio.SimpleActionGroup


@dataclasses.dataclass
class ContextMenuSection:
    """One separator-delimited group of context-menu items.

    A section can be rendered independently for insertion into an existing
    native menu, or composed with other sections in a ContextMenu.
    """

    items: list[ContextMenuItem] = dataclasses.field(default_factory=list)

    def build(self, prefix: str) -> BuiltContextMenu:
        builder = _ContextMenuBuilder(prefix)
        model = Gio.Menu()
        builder.append_items(model, self.items)
        return BuiltContextMenu(model, builder.action_group)


@dataclasses.dataclass
class ContextMenu:
    """An ordered list of context-menu items grouped into sections.

    Build the popover at show time so its items can reflect live target state.
    """

    sections: list[ContextMenuSection] = dataclasses.field(default_factory=list)

    def build(self, prefix: str) -> BuiltContextMenu:
        builder = _ContextMenuBuilder(prefix)
        model = Gio.Menu()
        builder.append_sections(model, self.sections)
        return BuiltContextMenu(model, builder.action_group)

    def build_popover(self, parent: Gtk.Widget, prefix: str) -> Gtk.PopoverMenu:
        built = self.build(prefix)
        popover = Gtk.PopoverMenu.new_from_model(built.model)
        popover.set_has_arrow(False)
        popover.set_parent(parent)
        popover.insert_action_group(prefix, built.action_group)
        return popover


def open_section(
    open_action,
    *,
    open_label: str | None = None,
    open_tab_action=None,
    open_window_action=None,
    open_with_action=None,
    open_enabled: bool = True,
    submenu: bool = True,
    shortcuts: bool = True,
) -> ContextMenuSection:
    """Build the standard Open section used by folders, disks, and places.

    Folder-like targets use the native-style Open submenu. Sidebar places can
    request the same actions as flat top-level items without accelerator labels.
    """
    primary_items = [
        ContextMenuItem(
            open_label or _native("Open"),
            action=open_action,
            shortcut="Return" if shortcuts else "",
            enabled=open_enabled,
        )
    ]
    if open_tab_action is not None:
        primary_items.append(
            ContextMenuItem(
                _("Open in New Tab"),
                action=open_tab_action,
                shortcut="<Control>Return" if shortcuts else "",
            )
        )
    if open_window_action is not None:
        primary_items.append(
            ContextMenuItem(
                _("Open in New Window"),
                action=open_window_action,
                shortcut="<Shift>Return" if shortcuts else "",
            )
        )

    if not submenu:
        if open_with_action is not None:
            primary_items.append(ContextMenuItem(_("Open With…"), action=open_with_action))
        return ContextMenuSection(primary_items)

    submenu_sections = [ContextMenuSection(primary_items)]
    if open_with_action is not None:
        submenu_sections.append(
            ContextMenuSection([ContextMenuItem(_("Open With…"), action=open_with_action)])
        )
    return ContextMenuSection(
        [ContextMenuItem(_native("Open"), submenu=ContextMenu(submenu_sections))]
    )


def my_computer_additions_section(
    *,
    bookmarked: bool | None = None,
    preferred: bool | None = None,
    toggle_bookmark_action=None,
    toggle_preferred_action=None,
) -> ContextMenuSection:
    """Build bookmark and Preferred Folder actions for a folder target.

    A state of None omits that feature, allowing the same prefab to represent
    both native folder selections and an already-pinned Preferred Folder card.
    """
    items = []
    if bookmarked is not None:
        items.append(
            ContextMenuItem(
                _("Remove from Bookmarks") if bookmarked else _("Add to Bookmarks"),
                action=toggle_bookmark_action,
                enabled=callable(toggle_bookmark_action),
            )
        )
    if preferred is not None:
        items.append(
            ContextMenuItem(
                _("Unpin from My Computer") if preferred else _("Pin to My Computer"),
                action=toggle_preferred_action,
                enabled=callable(toggle_preferred_action),
            )
        )
    return ContextMenuSection(items)


def clipboard_actions_section(
    *,
    cut_action=None,
    copy_action=None,
    paste_action=None,
    move_to_action=None,
    copy_to_action=None,
) -> ContextMenuSection:
    """Build the standard Cut/Copy/Move to/Copy to action group."""
    return ContextMenuSection(
        [
            ContextMenuItem(
                _native("Cut"),
                action=cut_action,
                shortcut="<Control>x",
                enabled=callable(cut_action),
            ),
            ContextMenuItem(
                _native("Copy"),
                action=copy_action,
                shortcut="<Control>c",
                enabled=callable(copy_action),
            ),
            ContextMenuItem(
                _native("Paste"),
                action=paste_action,
                shortcut="<Control>v",
                enabled=callable(paste_action),
            ),
            ContextMenuItem(
                _native("Move to…"), action=move_to_action, enabled=callable(move_to_action)
            ),
            ContextMenuItem(
                _native("Copy to…"), action=copy_to_action, enabled=callable(copy_to_action)
            ),
        ]
    )


def background_creation_section(
    *, new_folder_action=None, new_document_items=None, open_with_action=None
) -> ContextMenuSection:
    """Build the creation/opening section for a folder background."""
    items = [
        ContextMenuItem(
            _("New Folder…"),
            action=new_folder_action,
            shortcut="<Shift><Control>n",
            enabled=callable(new_folder_action),
        )
    ]
    if new_document_items:
        items.append(
            ContextMenuItem(
                _("New Document"),
                submenu=ContextMenu([ContextMenuSection(new_document_items)]),
            )
        )
    if open_with_action is not None:
        items.append(ContextMenuItem(_("Open With…"), action=open_with_action))
    return ContextMenuSection(items)


def background_clipboard_section(*, paste_action=None) -> ContextMenuSection:
    """Build the folder-background clipboard section."""
    return ContextMenuSection(
        [
            ContextMenuItem(
                _native("Paste"),
                action=paste_action,
                shortcut="<Control>v",
                enabled=callable(paste_action),
            )
        ]
    )


def background_terminal_section(*, open_terminal_action=None) -> ContextMenuSection:
    """Build the optional terminal action for a local folder background."""
    return ContextMenuSection(
        [
            ContextMenuItem(
                _("Open in Terminal"),
                action=open_terminal_action,
                enabled=callable(open_terminal_action),
            )
        ]
    )


def file_actions_section(
    *,
    rename_action=None,
    rename_enabled: bool = True,
    move_to_trash_action=None,
    show_compress: bool = False,
    show_email: bool = False,
) -> ContextMenuSection:
    """Build the currently implemented subset of Nautilus's File Actions section.

    In-progress actions can be displayed disabled to communicate planned
    capability without exposing a nonfunctional activation.
    """
    items = []
    if rename_action is not None:
        items.append(
            ContextMenuItem(
                _("Rename…"),
                action=rename_action,
                shortcut="F2",
                enabled=rename_enabled,
            )
        )
    if show_compress:
        items.append(ContextMenuItem(_("Compress…"), enabled=False))
    if show_email:
        items.append(ContextMenuItem(_native("Email…"), enabled=False))
    if move_to_trash_action is not None:
        items.append(
            ContextMenuItem(
                _("Move to Trash"),
                action=move_to_trash_action,
                shortcut="Delete",
            )
        )
    return ContextMenuSection(items)


def properties_section(action, *, enabled: bool = True) -> ContextMenuSection:
    """Build the standard trailing Properties section."""
    return ContextMenuSection(
        [
            ContextMenuItem(
                _native("Properties"),
                action=action,
                shortcut="<Alt>Return",
                enabled=enabled,
            )
        ]
    )


class _ContextMenuBuilder:
    """Internal recursive renderer shared by complete menus and sections."""

    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        self.action_group = Gio.SimpleActionGroup()
        self._counter = 0

    def append_sections(self, target_menu: Gio.Menu, sections: list[ContextMenuSection]) -> None:
        for section in sections:
            section_model = Gio.Menu()
            self.append_items(section_model, section.items)
            if section_model.get_n_items() > 0:
                target_menu.append_section(None, section_model)

    def append_items(self, target_menu: Gio.Menu, items: list[ContextMenuItem]) -> None:
        for item in items:
            if not item.visible:
                continue

            if item.submenu is not None:
                submenu_model = Gio.Menu()
                self.append_sections(submenu_model, item.submenu.sections)
                if submenu_model.get_n_items() > 0:
                    target_menu.append_item(Gio.MenuItem.new_submenu(item.label, submenu_model))
                continue

            action_name = f"item{self._counter}"
            self._counter += 1
            menu_item = Gio.MenuItem.new(item.label, f"{self.prefix}.{action_name}")
            if item.shortcut:
                menu_item.set_attribute_value("accel", GLib.Variant("s", item.shortcut))
            target_menu.append_item(menu_item)

            action = Gio.SimpleAction.new(action_name, None)
            action.set_enabled(item.enabled)
            if callable(item.action):
                action.connect("activate", lambda *_args, callback=item.action: callback())
            self.action_group.add_action(action)
