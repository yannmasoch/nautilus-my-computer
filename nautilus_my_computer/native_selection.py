"""Bridge between Column View and Nautilus's native List/Grid view (#178, #198).

Nautilus stays the authority for slot and history selection. Its NautilusFilesView
owns one NautilusViewModel (a Gtk.SelectionModel) that outlives the List/Grid
widget showing it: a List/Grid switch destroys only the inner view, by dropping
the overlay's reference (nautilus_files_view_change). This module only reads that
model, and writes Column View's selection into it by URI (Nautilus.FileInfo.get_uri()
matched against model positions, never by display name).

Ownership rule: nothing of Nautilus's is ever kept alive from here. No widget is
stored at all, and the slot and the model are held through GObject weak references.
A strongly held view survives its own destruction unparented but still bound to
the shared model, and GTK crashes on that model's next items-changed (#198).

The only other state is `_pending`: a temporary set of URIs waiting for the model
of the matching folder to load. It is never applied to a model showing another
folder and is dropped on the next location change or the next selection change.
"""

import os

from gi.repository import Gio, GLib, Gtk, Nautilus

from . import selection
from .common import _log, _native

# Stack pages the extension adds to the slot (Column View, Computer panel).
_OWN_PAGE_PREFIX = "mc-"


def _norm(uri: str) -> str:
    return Gio.File.new_for_uri(uri).get_uri()


def _item_uri(obj) -> str | None:
    if isinstance(obj, Gtk.TreeListRow):
        obj = obj.get_item()
    info = obj.get_property("file") if obj is not None else None
    return info.get_uri() if isinstance(info, Nautilus.FileInfo) else None


def find_native_model(slot):
    """The NautilusViewModel shown by `slot`'s own List/Grid view, or None.
    Only the model is returned; the view met on the way is dropped here."""
    for w in _walk(slot):
        if isinstance(w, (Gtk.ListView, Gtk.GridView)):
            model = w.get_model()
            if model is not None and type(model).__name__ == "NautilusViewModel":
                return model
    return None


class NativeModelBinding:
    """One signal handler on a native model, held through a weak reference.
    When Nautilus finalizes the model, the handler goes with it, get() returns
    None and `on_lost` runs from an idle (never inside finalization)."""

    def __init__(self, signal: str, callback, on_lost=None) -> None:
        self._signal = signal
        self._callback = callback
        self._on_lost = on_lost
        self._ref = None
        self._handler = 0

    def get(self):
        return self._ref() if self._ref is not None else None

    def bind(self, model) -> bool:
        """Point at `model` (None unbinds). True when the target changed."""
        if model == self.get():
            return False
        self.unbind()
        if model is not None:
            self._ref = model.weak_ref(self._on_finalized)
            self._handler = model.connect(self._signal, self._callback)
        return True

    def unbind(self) -> None:
        model = self.get()
        if model is not None and self._handler:
            model.disconnect(self._handler)
        if self._ref is not None:
            self._ref.unref()
        self._ref = None
        self._handler = 0

    def _on_finalized(self) -> None:
        self._ref = None
        self._handler = 0
        if self._on_lost is not None:
            GLib.idle_add(self._lost_idle)

    def _lost_idle(self) -> bool:
        self._on_lost()
        return GLib.SOURCE_REMOVE


def _menu_entries(menu: Gio.MenuModel):
    """(label, action) of every item in `menu`, descending into sections."""
    for i in range(menu.get_n_items()):
        label = menu.get_item_attribute_value(i, "label", None)
        action = menu.get_item_attribute_value(i, "action", None)
        if label is not None and action is not None:
            yield label.get_string(), action.get_string()
        section = menu.get_item_link(i, Gio.MENU_LINK_SECTION)
        if section is not None:
            yield from _menu_entries(section)


# Terminal actions Nautilus itself ships (upstream, and Ubuntu's xdg-terminal-exec patch),
# in priority order, with the label Grid/List shows for them.
_NATIVE_MENU_RESOURCES = (
    "/org/gnome/nautilus/menu/nautilus-files-view-context-menus.ui",
    "/org/gnome/nautilus/ui/nautilus-files-view-context-menus.ui",
)
_NATIVE_TERMINAL_ACTIONS = (
    ("current-directory-xdg-terminal", "Open in T_erminal"),
    ("current-directory-console", "Open in Consol_e"),
)


def _native_terminal_item() -> list[tuple[str, str]]:
    """The terminal item Nautilus's own folder menu defines, read from the menu resource it
    was built with, so an action is only offered when this Nautilus really has it."""
    text = ""
    for path in _NATIVE_MENU_RESOURCES:
        try:
            data = Gio.resources_lookup_data(path, Gio.ResourceLookupFlags.NONE)
        except GLib.Error:
            continue
        text = data.get_data().decode("utf-8", "replace")
        break
    else:
        _log("native_selection: native menu resource unavailable")
        return []
    for name, label in _NATIVE_TERMINAL_ACTIONS:
        if f"view.{name}<" not in text:
            continue
        if name == "current-directory-console" and not _console_service_installed():
            continue
        return [(_native(label), f"view.{name}")]
    return []


# D-Bus names Nautilus's console action launches: upstream's, and Fedora's Ptyxis patch.
_CONSOLE_SERVICES = ("org.gnome.Console", "org.gnome.Ptyxis")


def _console_service_installed() -> bool:
    """Nautilus enables its console action only when the terminal's D-Bus service can be
    activated; it hides the item otherwise, so do the same."""
    for data_dir in GLib.get_system_data_dirs() + [GLib.get_user_data_dir()]:
        for service in _CONSOLE_SERVICES:
            if os.path.exists(os.path.join(data_dir, "dbus-1", "services", f"{service}.service")):
                return True
    return False


class NativeBridge:
    """Per-slot access to the native view's selection."""

    def __init__(self, slot, on_native_selected) -> None:
        self._slot_ref = slot.weak_ref()
        # Called with the native URIs (possibly none) when the native selection
        # changes by itself or a load finishes.
        self._on_native_selected = on_native_selected
        self._binding = NativeModelBinding("selection-changed", self._on_selection_changed)
        self._writing = False
        self._pending = selection.PendingSelection()
        slot.connect("notify::location", self._on_location_changed)
        slot.connect("notify::loading", self._on_loading_changed)

    # model access

    def model(self):
        """The slot's native model. Searched again only once the one held is gone."""
        model = self._binding.get()
        return model if model is not None else self._rebind()

    def _rebind(self):
        slot = self._slot_ref()
        model = find_native_model(slot) if slot is not None else None
        if self._binding.bind(model) and slot is not None:
            loc = slot.get_property("location")
            _log(
                f"native_selection: bind slot={id(slot) & 0xFFFF:04x} "
                f"folder={loc.get_uri() if loc else None!r} found={model is not None}"
            )
        return model

    def extension_background_items(self) -> list[tuple[str, str]]:
        """(label, action) of the items extensions add to the native folder background
        menu, such as gnome-terminal's "Open in Terminal". The view is only looked up
        for the read and never kept."""
        slot = self._slot_ref()
        if slot is None:
            return []
        for w in _walk(slot):
            if type(w).__name__ == "NautilusFilesView":
                menu = w.get_property("extensions-background-menu")
                items = list(_menu_entries(menu)) if menu is not None else []
                _log(f"native_selection: extension background items={items!r}")
                return items
        _log("native_selection: no NautilusFilesView found for extension items")
        return []

    def native_terminal_items(self) -> list[tuple[str, str]]:
        """(label, action) of the terminal item Nautilus's own folder menu offers."""
        return _native_terminal_item()

    def shows(self, folder_uri: str) -> bool:
        slot = self._slot_ref()
        loc = slot.get_property("location") if slot is not None else None
        return loc is not None and loc.equal(Gio.File.new_for_uri(folder_uri))

    def _loading(self) -> bool:
        slot = self._slot_ref()
        return slot is not None and bool(slot.get_property("loading"))

    def read(self) -> list[str]:
        model = self.model()
        if model is None:
            return []
        bits = model.get_selection()
        uris = (_item_uri(model.get_item(bits.get_nth(i))) for i in range(bits.get_size()))
        return [u for u in uris if u]

    def _available(self, model) -> frozenset[str]:
        uris = (_item_uri(model.get_item(i)) for i in range(model.get_n_items()))
        return frozenset(_norm(u) for u in uris if u)

    def _write(self, model, uris: frozenset[str]) -> None:
        """Select exactly `uris`, all known to be in the model."""
        n = model.get_n_items()
        if n == 0:
            return
        selected = Gtk.Bitset.new_empty()
        for i in range(n):
            uri = _item_uri(model.get_item(i))
            if uri is not None and _norm(uri) in uris:
                selected.add(i)
        self._writing = True
        try:
            model.set_selection(selected, Gtk.Bitset.new_range(0, n))
        finally:
            self._writing = False
        _log(f"native_selection: wrote {len(uris)} of {n} rows")

    # Column View -> native

    def push(self, folder_uri: str, uris: list[str]) -> None:
        """Column selection changed in the column showing `folder_uri`. Written
        only when the native model shows that same folder; an ancestor column
        leaves it alone (see carry)."""
        if not self.shows(folder_uri):
            return
        _log(f"native_selection: push {len(uris)} for {folder_uri!r}")
        self._set_pending(folder_uri, uris)

    def carry(self, folder_uri: str, uris: list[str]) -> None:
        """Leaving Column View with an ancestor column selected: navigate the
        slot to that column's folder, then apply once it has loaded."""
        self._set_pending(folder_uri, uris)
        slot = self._slot_ref()
        if slot is not None and not self.shows(folder_uri):
            _log(f"native_selection: carry {len(uris)} via open-location {folder_uri!r}")
            slot.activate_action("slot.open-location", GLib.Variant("s", folder_uri))

    def expect(self, folder_uri: str, uris: list[str]) -> None:
        """Column View is about to navigate the slot to `folder_uri` and wants
        `uris` selected there. Applied after the load, on top of Nautilus's own
        default (it selects the folder it came from when going up)."""
        self._set_pending(folder_uri, uris)

    def _set_pending(self, folder_uri: str, uris: list[str]) -> None:
        self._pending.set(folder_uri, (_norm(u) for u in uris))
        self._try_apply(final=False)

    def _try_apply(self, *, final: bool) -> None:
        folder = self._pending.folder
        if folder is None or not self.shows(folder):
            return
        if self._loading() and not final:
            return
        model = self.model()
        if model is None:
            return
        ready = self._pending.resolve(self._available(model), final=final)
        if ready is not None:
            self._write(model, ready)

    # slot and model signals

    def _on_loading_changed(self, _slot, _pspec) -> None:
        if self._loading():
            return
        # A load can follow a view rebuild (older Nautilus replaces the whole
        # files view, and with it the model), so check what is shown now.
        self._rebind()
        # Nautilus has applied its own restored selection by now (end_loading
        # runs before this notify), so ours goes on top exactly once.
        self._try_apply(final=True)
        self._pending.drop()
        self._report()

    def _on_selection_changed(self, _model, _pos, _n) -> None:
        if self._writing or self._loading():
            return
        # A selection made natively wins over anything still pending.
        self._pending.drop()
        self._report()

    def _report(self) -> None:
        if selection.adopt_native(
            loading=self._loading(), writing=self._writing, pending=self._pending.active
        ):
            self._on_native_selected(self.read())

    def _on_location_changed(self, _slot, _pspec) -> None:
        folder = self._pending.folder
        if folder is not None and not self.shows(folder):
            self._pending.drop()


def _walk(widget):
    """Depth-first walk of `widget`, skipping the stack pages the extension
    added (their own list views are never the native one)."""
    yield widget
    stack = widget if isinstance(widget, Gtk.Stack) else None
    child = widget.get_first_child()
    while child is not None:
        page = stack.get_page(child) if stack is not None else None
        name = page.get_name() if page is not None else None
        if not (name or "").startswith(_OWN_PAGE_PREFIX):
            yield from _walk(child)
        child = child.get_next_sibling()
