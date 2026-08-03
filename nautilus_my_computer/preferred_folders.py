"""Preferred Folders target: model, GSettings storage, the folder card's
right-click menu, and the native pathbar "Current Folder Menu" injection.

Data helpers take gsettings/uri explicitly; UI helpers take `ext` (the
MyComputerExtension instance) for window/state access. No app state of its
own -- this module can be imported from anywhere without import cycles.
"""

import dataclasses

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk

from nautilus_my_computer.common import (
    _,
    _all_widgets,
    _find_widget,
    _log,
    _menu_item_index,
    _menu_section_with_action,
    _native,
    _uri_is_hidden,
)
from nautilus_my_computer.context_menu import (
    ContextMenu,
    my_computer_additions_section,
    open_section,
    properties_section,
)


def folder_context_menu(ext, win, pf) -> ContextMenu:
    """Preferred-folder card menu: open actions, remove from group, properties."""
    uri = pf.nav_uri
    return ContextMenu(
        [
            open_section(
                lambda: ext._do_open(uri, win),
                open_tab_action=lambda: ext._do_open_tab(uri, win, make_active=False),
                open_window_action=lambda: ext._do_open_window(uri),
                open_with_action=(
                    (lambda: ext._do_open_with(uri, win))
                    if not pf.is_special_place and uri.startswith("file://")
                    else None
                ),
            ),
            my_computer_additions_section(
                preferred=True,
                toggle_preferred_action=lambda: ext._do_remove_preferred_folder(pf, win),
            ),
            properties_section(lambda: ext._do_properties(uri, win)),
        ]
    )


@dataclasses.dataclass
class PreferredFolder:
    """Typed representation of one Preferred Folders card. Parallel to MountInfo,
    but with no mount/usage state -- folders are always navigable."""

    key: str  # logical token or exact URI entry persisted in GSettings
    display_name: str
    nav_uri: str
    icon_name: str = "folder"
    gio_icon: object | None = None
    is_special_place: bool = False  # True for recent:///, starred:///, x-network-view:///
    is_hidden: bool = False  # standard::is-hidden on the folder itself
    # Position in the gsettings "preferred-folders" array (its only ordering
    # signal -- the key is not unique enough alone to splice during drag-reorder).
    # Set by load_preferred_folders(); not persisted as a separate gsettings value.
    index: int = 0

    # Right-click menu factory menu(ext, win, pf) -> ContextMenu (built at show-time).
    menu: object = folder_context_menu


# Maps a logical token to its translatable label, symbolic icon, and a way to
# resolve the URI: either a zero-arg callable (fixed locations) or a
# GLib.UserDirectory enum value (resolved via GLib.get_user_special_dir).
PREFERRED_TOKENS: dict[str, dict] = {
    "home": {
        "label": _native("Home"),
        "uri": lambda: GLib.filename_to_uri(GLib.get_home_dir(), None),
    },
    "recent": {
        "label": _native("Recent"),
        "icon": "folder-recent",
        "uri": lambda: "recent:///",
    },
    "starred": {
        "label": _native("Starred"),
        "icon": "folder-favorites",
        "uri": lambda: "starred:///",
    },
    "network": {
        "label": _native("Network"),
        "icon": "folder-network",
        "uri": lambda: "x-network-view:///",
    },
    # The one token whose icon is not folder-shaped. Nautilus shows a bin in its
    # own sidebar, so a folder icon here would both diverge from native and hide
    # which card is the Trash ("folder-trash" exists in some third-party themes
    # but not in Adwaita, so it cannot be a default). Kept last in
    # DEFAULT_PREFERRED_FOLDERS so the odd silhouette sits at the grid boundary.
    # "gio_icon" defers the icon to GIO like a real folder token: trash:/// has a
    # real standard::icon that GIO already flips between user-trash and
    # user-trash-full, so the empty/full state needs no watching of our own.
    # "icon" is only the first-frame placeholder until that query resolves.
    "trash": {
        "label": _native("Trash"),
        "icon": "user-trash",
        "uri": lambda: "trash:///",
        "gio_icon": True,
    },
    # Not wrapped in _(): the real xdg-user-dirs name always overwrites this
    # within one async GIO query (see _refresh_folder_icon_async), so it's
    # only ever visible for a single frame -- translating it would be
    # translator effort spent on text no user ever actually reads (#64).
    "documents": {
        "label": "Documents",
        "special_dir": GLib.UserDirectory.DIRECTORY_DOCUMENTS,
    },
    "downloads": {
        "label": "Downloads",
        "special_dir": GLib.UserDirectory.DIRECTORY_DOWNLOAD,
    },
    "music": {
        "label": "Music",
        "special_dir": GLib.UserDirectory.DIRECTORY_MUSIC,
    },
    "videos": {
        "label": "Videos",
        "special_dir": GLib.UserDirectory.DIRECTORY_VIDEOS,
    },
    "pictures": {
        "label": "Pictures",
        "special_dir": GLib.UserDirectory.DIRECTORY_PICTURES,
    },
}

# Mirrors Nautilus' own sidebar: its built-in places in source order
# (Home, Recent, Starred, Network -- see nautilus-sidebar.c), then the XDG user
# directories in /etc/xdg/user-dirs.defaults order. Those five are not built-in
# sidebar places at all: Nautilus reads them from the user's GTK bookmarks, so
# there is no native sidebar order to copy and the xdg-user-dirs order is the
# closest canonical one. Trash is the exception to the mirror -- native keeps it
# with the built-in places, but it is the only non-folder icon, so it goes last
# where the odd silhouette breaks the grid least. Users can reorder by drag and
# drop and unpin anything, so this is only a starting point.
DEFAULT_PREFERRED_FOLDERS: list[str] = [
    "home",
    "recent",
    "starred",
    "network",
    "downloads",
    "documents",
    "music",
    "pictures",
    "videos",
    "trash",
]


def resolve_preferred_uri(entry: str) -> str:
    """Resolve portable URI forms used in the preferred-folders setting.

    ``file://~/…`` is intentionally supported as a distro-friendly way to
    name a folder below the current user's home directory without knowing the
    account name when a GSettings default is written.  Keep the remainder as
    URI text so already-escaped path components are not escaped a second time.
    """
    if entry == "file://~":
        return GLib.filename_to_uri(GLib.get_home_dir(), None)
    if entry.startswith("file://~/"):
        home_uri = GLib.filename_to_uri(GLib.get_home_dir(), None)
        relative_uri = entry.removeprefix("file://~/")
        if not relative_uri:
            return home_uri
        separator = "" if home_uri.endswith("/") else "/"
        return f"{home_uri}{separator}{relative_uri}"
    return entry


def load_preferred_folders(gsettings) -> list[PreferredFolder]:
    """Resolve the ordered preferred-folders list into PreferredFolder objects.

    Entries are either logical tokens (resolved via PREFERRED_TOKENS) or URI
    entries (including portable file://~/… paths) resolved via Gio.File.
    """
    if gsettings is not None:
        entries = list(gsettings.get_value("preferred-folders").unpack())
    else:
        entries = list(DEFAULT_PREFERRED_FOLDERS)

    folders: list[PreferredFolder] = []
    for entry in entries:
        token = PREFERRED_TOKENS.get(entry)
        if token is not None:
            special_dir = token.get("special_dir")
            if special_dir is not None:
                path = GLib.get_user_special_dir(special_dir)
                if not path:
                    continue
                uri = GLib.filename_to_uri(path, None)
            else:
                uri = token["uri"]()
            is_special_place = not uri.startswith("file://")
            folders.append(
                PreferredFolder(
                    key=entry,
                    display_name=token["label"],
                    nav_uri=uri,
                    # Real folders (home/documents/downloads/...) start with the plain
                    # gettext label as a placeholder and get their live name and icon
                    # (native special-folder icon or a user-set custom icon) from an
                    # async GIO query -- see my_computer_view._refresh_folder_icon_async.
                    # The real xdg-user-dirs name always wins over our label once that
                    # query resolves (issue #64). Only the 3 virtual tokens
                    # (recent/starred/network) keep the fixed label, since they aren't
                    # real directories GIO can query.
                    icon_name=token.get("icon", "folder"),
                    is_special_place=is_special_place,
                    is_hidden=False if is_special_place else _uri_is_hidden(uri),
                    index=len(folders),
                )
            )
            continue

        # URI added by the user or supplied as a default. Display-name/icon are
        # resolved with a free, zero-I/O fallback here; the real metadata is fetched
        # asynchronously by the caller (see _refresh_folder_metadata_async) so a slow
        # or unreachable URI never blocks panel population or menu opening.
        uri = resolve_preferred_uri(entry)
        gfile = Gio.File.new_for_uri(uri)
        folders.append(
            PreferredFolder(
                key=entry,
                display_name=gfile.get_basename() or uri,
                nav_uri=uri,
                icon_name="folder",
                index=len(folders),
            )
        )
    return folders


def get_preferred_entries(gsettings) -> list:
    """Raw ordered list of tokens/URIs, straight from gsettings."""
    if not gsettings:
        return list(DEFAULT_PREFERRED_FOLDERS)
    return list(gsettings.get_value("preferred-folders").unpack())


def add_preferred(gsettings, uri: str) -> None:
    if not gsettings:
        return
    entries = get_preferred_entries(gsettings)
    uri = uri.rstrip("/")
    # A URI that matches a logical token's fixed location (home, recent,
    # starred, network) must store as that token, not the raw URI, so it
    # keeps the translated label and token icon instead of GVfs' generic
    # resolution for that URI (e.g. "yann" instead of "Home", or the wrong
    # icon for "recent:" -- issue #79). special_dir tokens (documents,
    # downloads, ...) resolve to real filesystem paths shared with other
    # users' home dirs, so they are intentionally excluded here.
    for token, meta in PREFERRED_TOKENS.items():
        token_uri = meta.get("uri")
        if token_uri is not None and uri == token_uri().rstrip("/"):
            uri = token
            break
    if uri not in entries:
        entries.append(uri)
        gsettings.set_value("preferred-folders", GLib.Variant("as", entries))


def remove_preferred(gsettings, key: str) -> None:
    if not gsettings:
        return
    entries = get_preferred_entries(gsettings)
    if key in entries:
        entries.remove(key)
        gsettings.set_value("preferred-folders", GLib.Variant("as", entries))


def save_order(gsettings, keys: list[str]) -> None:
    """Persist the exact Preferred Folders order produced by drag-reorder.
    Unlike add_preferred(), this does not force "home" to lead -- the user
    placed each card explicitly, so the order is stored verbatim."""
    if not gsettings:
        return
    gsettings.set_value("preferred-folders", GLib.Variant("as", keys))


def preferred_for_uri(gsettings, uri: str) -> "PreferredFolder | None":
    """Look up the PreferredFolder whose nav_uri matches uri, if any."""
    norm = uri.rstrip("/")
    for folder in load_preferred_folders(gsettings):
        if folder.nav_uri.rstrip("/") == norm:
            return folder
    return None


def is_preferred(gsettings, uri: str) -> bool:
    return preferred_for_uri(gsettings, uri) is not None


def toggle_preferred(gsettings, uri: str) -> bool:
    """Add or remove uri from Preferred depending on current state. Returns
    the new state (True if now preferred)."""
    pf = preferred_for_uri(gsettings, uri)
    if pf is not None:
        remove_preferred(gsettings, pf.key)
        return False
    add_preferred(gsettings, uri)
    return True


# ── Native pathbar menu injection (issue #30) ───────────────────────────────


def find_pathbar_menu_button(win):
    """Find the NautilusPathBar's "Current Folder Menu" button -- the sole
    Gtk.MenuButton inside NautilusPathBar (icon "view-more-symbolic"). Its
    popover is bound to the same GMenu shared with the background context
    menu, which contains "Add to _Bookmarks" (slot.bookmark-current-directory)."""
    pathbar = _find_widget(win, class_name="NautilusPathBar", site="find_pathbar_menu_button")
    if pathbar is None:
        return None
    for w in _all_widgets(pathbar):
        if isinstance(w, Gtk.MenuButton):
            return w
    return None


def attach_pathbar_menu_watch(ext, win) -> None:
    """Watch the pathbar's Current Folder Menu button so we can inject
    "Pin to My Computer" / "Unpin from My Computer" into its native menu
    every time it opens."""
    btn = find_pathbar_menu_button(win)
    if btn is None:
        _log("attach_pathbar_menu_watch: pathbar menu button not found")
        return
    btn.connect("notify::active", _on_pathbar_menu_active, ext, win)
    _log("attach_pathbar_menu_watch: attached")


def _on_pathbar_menu_active(btn, _param, ext, win) -> None:
    if not btn.get_active():
        return
    GLib.idle_add(_inject_preferred_menu_item, ext, btn, win)


def _get_active_slot_uri(win) -> str | None:
    """URI of the currently active Nautilus slot (the folder shown in the
    view), or None if it can't be determined."""
    uri = None
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
                uri = loc.get_uri()
                break
        except TypeError:
            pass
        uri = loc.get_uri()
    return uri


def _inject_preferred_menu_item(ext, btn, win) -> bool:
    """Insert "Pin to My Computer" / "Unpin from My Computer" directly below
    "Add to Bookmarks" in the pathbar's Current Folder Menu, mirroring how
    that native item flips to "Remove from Bookmarks" when the folder is
    already bookmarked. Re-evaluated on every open since the button and
    its popover persist across navigations while the current folder (and
    whether it's preferred) can change between opens."""
    popover = btn.get_popover()
    if popover is None:
        return GLib.SOURCE_REMOVE
    model = popover.get_menu_model()
    if not isinstance(model, Gio.Menu):
        _log(f"_inject_preferred_menu_item: model is {type(model).__name__}, not Gio.Menu")
        return GLib.SOURCE_REMOVE

    # Undo the previous injection (item or, in the fallback case, the
    # whole standalone section) before re-evaluating against the folder
    # now shown, so repeated opens don't accumulate stale items/sections.
    prev_section = getattr(popover, "_mc_pref_section", None)
    prev_index = getattr(popover, "_mc_pref_index", None)
    prev_was_fallback = getattr(popover, "_mc_pref_was_fallback", False)
    if prev_section is not None and prev_index is not None:
        if prev_was_fallback:
            for i in range(model.get_n_items()):
                if model.get_item_link(i, Gio.MENU_LINK_SECTION) is prev_section:
                    model.remove(i)
                    break
        else:
            prev_section.remove(prev_index)

    section = _menu_section_with_action(model, "slot.bookmark-current-directory")
    is_fallback = not isinstance(section, Gio.Menu)
    if is_fallback:
        _log("_inject_preferred_menu_item: Add to Bookmarks section not found, using own section")
        section = Gio.Menu()
        model.append_section(None, section)

    uri = _get_active_slot_uri(win)
    pf = preferred_for_uri(ext._gsettings, uri) if uri else None
    _log(f"_inject_preferred_menu_item: uri={uri!r} pf={pf!r}")

    label = _("Unpin from My Computer") if pf is not None else _("Pin to My Computer")
    bookmark_index = _menu_item_index(section, "slot.bookmark-current-directory")
    insert_at = section.get_n_items() if bookmark_index is None else bookmark_index + 1
    section.insert(insert_at, label, "mcpref.toggle-current")

    ag = Gio.SimpleActionGroup()
    act = Gio.SimpleAction.new("toggle-current", None)
    act.connect("activate", lambda *_a: do_toggle_preferred_current(ext, uri))
    ag.add_action(act)
    popover.insert_action_group("mcpref", ag)

    popover._mc_pref_section = section
    popover._mc_pref_index = insert_at
    popover._mc_pref_was_fallback = is_fallback
    _log(f"_inject_preferred_menu_item: added '{label}' to native menu")
    return GLib.SOURCE_REMOVE


def do_toggle_preferred_current(ext, uri: str | None) -> None:
    if not uri:
        return
    now_preferred = toggle_preferred(ext._gsettings, uri)
    _log(f"do_toggle_preferred_current: {'added' if now_preferred else 'removed'} {uri}")
