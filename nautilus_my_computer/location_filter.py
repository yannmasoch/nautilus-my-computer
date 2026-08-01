"""Location-bar target: redirects plain keystrokes typed while the My
Computer panel is showing into Nautilus's own address-bar entry (the same
NautilusToolbar surface "/" and "~" already reveal, see _LOCATION_ENTRY_KEYVALS
in main.py) and drives the per-group card filters in widgets.py from that
entry's live text. We own no search UI of our own -- the address bar doubles
as a live filter box, exactly like typing after "/" or "~" already does.
"""

import functools

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

from nautilus_my_computer.common import _find_widget, _log
from nautilus_my_computer.my_computer_view import VIEW_DISKINFO


def find_toolbar(nautilus_win: Gtk.Window) -> Gtk.Widget | None:
    """Find the NautilusToolbar instance that owns the address-bar actions
    ("toolbar.edit-location" and friends, see nautilus-toolbar.c)."""
    return _find_widget(nautilus_win, class_name="NautilusToolbar", site="find_toolbar")


def find_location_entry(nautilus_win: Gtk.Window) -> Gtk.Editable | None:
    """Find Nautilus's own NautilusLocationEntry. It subclasses GtkEntry, so
    the widget itself is a Gtk.Editable -- no inner GtkText to dig for."""
    return _find_widget(
        nautilus_win, class_name="NautilusLocationEntry", site="find_location_entry"
    )


def attach_location_filter_watch(ext, nautilus_win: Gtk.Window) -> None:
    """Watch the address-bar entry's live text and forward it to the
    per-group card filters. Idempotent -- the entry is a persistent template
    child of the toolbar, so this only needs to succeed once per window."""
    state = ext._windows.get(nautilus_win)
    if not state or state.get("location_filter_watch_attached"):
        return
    entry = find_location_entry(nautilus_win)
    if entry is None:
        _log("location entry not found in toolbar")
        return
    entry.connect("changed", functools.partial(_on_location_text_changed, ext), nautilus_win)
    entry.connect("cancel", functools.partial(_on_location_cancel, ext), nautilus_win)
    state["location_filter_watch_attached"] = True
    _log(f"location filter watch attached ({type(entry).__name__})")


def _has_focus_within(nautilus_win: Gtk.Window, entry: Gtk.Widget) -> bool:
    """GtkEntry (and its NautilusLocationEntry subclass) is a composite
    widget: keyboard focus actually lands on its internal GtkText delegate,
    not on the entry object itself, so identity comparison against
    get_focus() always fails. Walk the focus widget's ancestors instead."""
    focus = nautilus_win.get_focus()
    while focus is not None:
        if focus is entry:
            return True
        focus = focus.get_parent()
    return False


def _on_location_text_changed(ext, entry: Gtk.Editable, nautilus_win: Gtk.Window) -> None:
    state = ext._active_panel_state(nautilus_win)
    if not state or state.get("visible_view") != VIEW_DISKINFO:
        return
    # "/", "~" and Ctrl+L open this same entry for real navigation, not card
    # filtering -- _on_window_key_capture disowns the entry (see
    # location_filter_owned) whenever one of those opens it, so typing a
    # path there navigates instead of filtering the panel to nothing.
    if not state.get("location_filter_owned"):
        return
    # Nautilus keeps this entry's text primed with the current location even
    # while it's hidden/unfocused (so it's ready next time "/" or "~" opens
    # it) -- only treat a "changed" as a live filter edit while the user is
    # actually typing into it, or every such resync would filter the panel
    # down to nothing.
    if not _has_focus_within(nautilus_win, entry):
        return
    query = entry.get_text()
    _log(f"location filter text changed -> {query!r}")
    ext._apply_card_filter(nautilus_win, query)


def _on_location_cancel(ext, _entry: Gtk.Editable, nautilus_win: Gtk.Window) -> None:
    """Escape: unconditionally reset to the default (unfiltered) view. Does
    not go through the focus-gated _on_location_text_changed, since the
    focus-within check can otherwise race with the entry's own close/blur
    handling and leave a stale filter applied."""
    state = ext._active_panel_state(nautilus_win)
    if not state:
        return
    _log("location filter cancelled -> reset to default view")
    state["location_filter_owned"] = False
    ext._apply_card_filter(nautilus_win, "")


def reveal_and_seed(ext, nautilus_win: Gtk.Window, char: str) -> bool:
    """Open the address bar (the same toolbar.edit-location action Ctrl+L
    uses) and seed it with the first typed character, so a plain keystroke
    acts like Ctrl+L followed by typing, but in one step. Returns False if
    the toolbar/entry couldn't be found, so the caller can fall back to
    swallowing the key as before."""
    toolbar = find_toolbar(nautilus_win)
    if toolbar is None:
        _log("reveal_and_seed: toolbar not found")
        return False
    if not toolbar.activate_action("toolbar.edit-location", None):
        _log("reveal_and_seed: toolbar.edit-location action failed")
        return False
    entry = find_location_entry(nautilus_win)
    if entry is None:
        _log("reveal_and_seed: location entry not found after reveal")
        return False
    entry.set_text(char)
    entry.set_position(-1)
    return True
