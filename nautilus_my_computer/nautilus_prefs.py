"""Adapter for Nautilus's own settings: view mode, click policy, sort order,
icon zoom, hidden files. Consolidates reads that used to be scattered ad hoc
GSettings/GVfs-metadata calls across main.py into one cached object, so a
second view module (Column View) can read the same values without duplicating
the GSettings handles or native View Options action watching.

`NautilusPrefs` takes `ext` as an explicit parameter on its watcher methods,
same as every other target module -- it does not import main.py.
"""

from __future__ import annotations

from gi.repository import Adw, Gio, GLib, Gtk

from nautilus_my_computer.common import (
    _GRID_ZOOM_PX,
    _LIST_ZOOM_PX,
    _all_widgets,
    _find_widget,
    _log,
)

# Per-folder sort override, stored as GVfs metadata rather than GSettings.
METADATA_SORT_BY = "metadata::nautilus-icon-view-sort-by"
METADATA_SORT_REVERSED = "metadata::nautilus-icon-view-sort-reversed"

# The per-folder GVfs metadata (METADATA_SORT_BY) and the global GSettings
# "default-sort-order" enum use two different vocabularies for the same
# concept (confirmed against nautilus-column-utilities.c and a live gsettings
# range query): metadata stores "date_modified"/"date_created"/"date_accessed",
# GSettings stores the enum nicks "mtime"/"atime"/"btime". Column View needs
# one canonical set regardless of which source answered, so metadata tokens
# are normalized to the GSettings nicks here.
_METADATA_SORT_TO_CANONICAL = {
    "date_modified": "mtime",
    "date_created": "btime",
    "date_accessed": "atime",
}


class NautilusPrefs:
    def __init__(self):
        self._prefs = Gio.Settings.new("org.gnome.nautilus.preferences")
        self._icon_view = Gio.Settings.new("org.gnome.nautilus.icon-view")
        self._list_view = Gio.Settings.new("org.gnome.nautilus.list-view")
        # Nautilus migrated hidden-files state to the shared GTK4 file-chooser
        # schema (nautilus-global-preferences.c: "Some settings such as show
        # hidden files are shared between Nautilus and GTK file chooser").
        # org.gnome.nautilus.preferences::show-hidden-files still exists in the
        # schema but the running binary no longer reads/writes it -- Ctrl+H and
        # the view-options toggle both go through this key instead.
        self._filechooser = Gio.Settings.new("org.gtk.gtk4.Settings.FileChooser")

        self.view_mode: str = "icon-view"
        # Nautilus "click-policy": 'single' or 'double'. Read live at construction
        # (main.py instantiates NautilusPrefs before any window/view exists) rather than
        # hardcoding a default -- refresh_click_policy() can't be reused here since it reads
        # self.click_policy to compute its return value before this first assignment exists.
        self.click_policy: str = self._prefs.get_string("click-policy")

    # ── Global GSettings reads ───────────────────────────────────────────────

    def hidden_files(self) -> bool:
        return self._filechooser.get_boolean("show-hidden")

    def sort_directories_first(self) -> bool:
        """Nautilus Preferences > "Sort Folders Before Files". Lives in the
        same shared GTK4 file-chooser schema as show-hidden
        (nautilus-global-preferences.c opens this schema for both). Applied
        as an outer, pinned grouping that reverse never flips
        (nautilus-file.c: nautilus_file_compare_for_sort_internal returns its
        -1/+1 directly, before the function's reversed branch)."""
        return self._filechooser.get_boolean("sort-directories-first")

    def zoom_level(self, view: str = "icon-view") -> str:
        settings = self._list_view if view == "list-view" else self._icon_view
        return settings.get_string("default-zoom-level")

    def zoom_px(self, view: str = "icon-view") -> int:
        if view == "list-view":
            return _LIST_ZOOM_PX.get(self.zoom_level(view), 32)
        return _GRID_ZOOM_PX.get(self.zoom_level(view), 96)

    def captions(self) -> list[str]:
        """The up-to-3 caption tokens from icon-view's "captions" key (e.g.
        ["size", "date_modified", "none"]). "none" means that slot is empty."""
        return list(self._icon_view.get_strv("captions"))

    def default_sort(self) -> tuple[str, bool]:
        return (
            self._prefs.get_string("default-sort-order"),
            self._prefs.get_boolean("default-sort-in-reverse-order"),
        )

    # ── Per-folder GVfs metadata (sort override) ────────────────────────────

    def folder_sort(self, uri: str) -> tuple[str, bool] | None:
        """Read the per-folder sort override for `uri`, or None if unset/unreadable."""
        try:
            f = Gio.File.new_for_uri(uri)
            info = f.query_info(
                f"{METADATA_SORT_BY},{METADATA_SORT_REVERSED}",
                Gio.FileQueryInfoFlags.NONE,
                None,
            )
        except Exception:
            return None
        col = info.get_attribute_string(METADATA_SORT_BY)
        if col is None:
            return None
        rev = (info.get_attribute_string(METADATA_SORT_REVERSED) or "false") == "true"
        return (col, rev)

    def get_effective_folder_sort(self, uri: str) -> tuple[str, bool]:
        """Return one URI's effective sort without subscribing or changing it.

        A saved per-folder GVfs sort override wins. When no override exists,
        return Nautilus's global GSettings default. This is used by the
        Computer panel; Column View has its own global extension settings.
        """
        return self._effective_sort_from_override(self.folder_sort(uri))

    def _effective_sort_from_override(self, folder: tuple[str, bool] | None) -> tuple[str, bool]:
        """Resolve an already-read folder override without another GIO query."""
        if folder is not None:
            col, rev = folder
            col = _METADATA_SORT_TO_CANONICAL.get(col, col)
        else:
            col, rev = self.default_sort()
        return (col, rev)

    # ── View mode / click policy ─────────────────────────────────────────────

    def refresh_view_mode(self) -> bool:
        """Read current view mode from Nautilus preferences. Returns True
        when it changed since last read."""
        mode = self._prefs.get_string("default-folder-viewer")
        changed = mode != self.view_mode
        self.view_mode = mode
        return changed

    def refresh_click_policy(self) -> bool:
        """Read current click policy from Nautilus preferences. Returns True
        when it changed since last read. Kept separate from
        refresh_view_mode(): click-policy only affects the disk-view grid's
        activate-on-single-click flag (widgets.py's
        flow.set_activate_on_single_click) and Column View's Miller file rows
        and preview column, both of which read click_policy live at click
        time (widgets.py's MyComputerColumn._on_row_activated_internal and
        MyComputerPreviewColumn._on_preview_area_pressed/_released) -- folder
        rows stay single-click regardless (drill-down UX, see
        MyComputerColumn). Bundling this with view-mode's full
        _repopulate_visible() would force a needless re-enumeration/resort of
        every open Miller column on a setting that doesn't affect their
        content at all."""
        policy = self._prefs.get_string("click-policy")
        changed = policy != self.click_policy
        self.click_policy = policy
        return changed

    # ── Watcher wiring ───────────────────────────────────────────────────────

    def watch_global(self, ext) -> None:
        """Subscribe to GSettings so global preference changes are instant.
        Call once, not per window."""
        self._prefs.connect("changed::default-folder-viewer", self._on_view_mode_changed, ext)
        self._prefs.connect("changed::click-policy", self._on_click_policy_changed, ext)
        self._filechooser.connect("changed::show-hidden", self._on_hidden_files_changed, ext)
        self._filechooser.connect(
            "changed::sort-directories-first", self._on_dirs_first_changed, ext
        )
        self._icon_view.connect("changed::captions", self._on_captions_changed, ext)
        # Zoom is per-view (grid uses icon-view, list uses list-view); either
        # can change via Ctrl+scroll / +/-. Cards read px from the active view's
        # zoom, so repopulate whatever is visible when the matching key changes.
        self._icon_view.connect("changed::default-zoom-level", self._on_zoom_changed, ext)
        self._list_view.connect("changed::default-zoom-level", self._on_zoom_changed, ext)

    def _on_view_mode_changed(self, _settings: Gio.Settings, _key: str, ext) -> None:
        if self.refresh_view_mode():
            _log(f"view mode changed → mode='{self.view_mode}'")
            ext._repopulate_visible()

    def _on_click_policy_changed(self, _settings: Gio.Settings, _key: str, ext) -> None:
        if self.refresh_click_policy():
            _log(f"click-policy changed → policy='{self.click_policy}'")
            ext._repopulate_disk_view_only()

    def _on_hidden_files_changed(self, _settings: Gio.Settings, _key: str, ext) -> None:
        _log(f"show-hidden changed → {self.hidden_files()}")
        ext._repopulate_visible()

    def _on_dirs_first_changed(self, _settings: Gio.Settings, _key: str, ext) -> None:
        # Only Column View mixes folders and files in one listing -- the disk
        # panel shows mounts, and Preferred Folders shows folder cards only,
        # so neither has anything for this pref to regroup.
        _log(f"sort-directories-first changed → {self.sort_directories_first()}")
        ext._repopulate_column_view_only()

    def _on_captions_changed(self, _settings: Gio.Settings, _key: str, ext) -> None:
        _log(f"captions changed → {self.captions()}")
        ext._reapply_folder_captions()

    def _on_zoom_changed(self, settings: Gio.Settings, _key: str, ext) -> None:
        _log(f"zoom changed → {settings.get_string('default-zoom-level')}")
        ext._repopulate_visible()

    def watch_sort_button(self, ext, nautilus_win: Gtk.Window, *, on_sort_changed) -> None:
        """Sample native folder metadata when View Options closes.

        The menu's action group is private, so its sort signal is not exposed
        to Python extensions. The known MenuButton active state gives a stable
        lifecycle: capture the target URI and baseline on open, then read that
        same URI once after the user's selection closes the popover.
        """
        state = ext._windows.get(nautilus_win)
        if not state or state.get("sort_watch_button"):
            return
        # Once inject_column_view_entry has run, the view-options popover
        # lives on our own MenuButton (the native split button's popover was
        # moved there), not on the native MenuButton find_sort_button locates.
        btn = state.get("view_options_menu_button") or self.find_sort_button(nautilus_win)
        if btn is None:
            _log("sort button not found in toolbar")
            return
        btn.connect(
            "notify::active", self._on_sort_button_active, ext, nautilus_win, on_sort_changed
        )
        state["sort_watch_button"] = btn
        _log(f"sort button focus watch attached ({type(btn).__name__})")

    def find_sort_button(self, nautilus_win: Gtk.Window):
        """Find the GtkMenuButton inside NautilusViewControls (the sort/view popover button)."""
        # NautilusViewControls has no real buildable_id (auto-generated) and no css class.
        # Tier 2 (class name) is the primary match; tier 4 structural is the fallback.
        view_controls = _find_widget(
            nautilus_win,
            class_name="NautilusViewControls",
            site="find_sort_button",
        )
        if view_controls:
            for child in _all_widgets(view_controls):
                if isinstance(child, Gtk.MenuButton):
                    return child

        # Structural fallback: navigate via typed Adwaita getters to the content
        # toolbar and find the first MenuButton that isn't the hamburger.
        split_view = next(
            (w for w in _all_widgets(nautilus_win) if isinstance(w, Adw.OverlaySplitView)), None
        )
        if split_view:
            content = split_view.get_content()
            toolbar_view = (
                next((w for w in _all_widgets(content) if isinstance(w, Adw.ToolbarView)), None)
                if content
                else None
            )
            if toolbar_view:
                for w in _all_widgets(toolbar_view):
                    if isinstance(w, Gtk.MenuButton) and w.get_icon_name() != "open-menu-symbolic":
                        _log("find_sort_button: matched via structural nav (NautilusViewControls)")
                        return w
        return None

    def _on_sort_button_active(
        self, btn: Gtk.MenuButton, _param, ext, nautilus_win: Gtk.Window, on_sort_changed
    ) -> None:
        """Capture a baseline on open and resolve the selected sort on close."""
        state = ext._windows.get(nautilus_win)
        if state is None:
            return
        if btn.get_active():
            target = ext._sort_watch_target(nautilus_win)
            if target is None:
                return
            raw = self.folder_sort(target)
            state["sort_watch_uri"] = target
            state["sort_watch_snapshot"] = (raw, self._effective_sort_from_override(raw))
            _log(f"view.sort watch armed uri={target!r} baseline={state['sort_watch_snapshot']!r}")
            return

        uri = state.pop("sort_watch_uri", None)
        previous = state.pop("sort_watch_snapshot", None)
        if uri is None:
            GLib.idle_add(ext._restore_column_focus_after_sort, nautilus_win, btn)
            return
        _log(f"view.sort popover closed; deferring selected-sort read uri={uri!r}")
        GLib.idle_add(
            self._read_sort_after_popover_close,
            ext,
            nautilus_win,
            btn,
            uri,
            previous,
            on_sort_changed,
        )

    def _read_sort_after_popover_close(
        self,
        ext,
        nautilus_win: Gtk.Window,
        btn: Gtk.MenuButton,
        uri: str,
        previous: tuple[tuple[str, bool] | None, tuple[str, bool]] | None,
        on_sort_changed,
    ) -> bool:
        """Read after the menu click has dispatched its native sort action."""
        raw = self.folder_sort(uri)
        current = (raw, self._effective_sort_from_override(raw))
        _log(
            "view.sort chosen after popover close "
            f"uri={uri!r} raw={raw!r} effective={current[1]!r} "
            f"baseline={previous!r} changed={current != previous}"
        )
        if current != previous:
            on_sort_changed(nautilus_win, uri, raw, current[1])
        _log("view.sort watch disarmed")
        GLib.idle_add(ext._restore_column_focus_after_sort, nautilus_win, btn)
        return GLib.SOURCE_REMOVE
