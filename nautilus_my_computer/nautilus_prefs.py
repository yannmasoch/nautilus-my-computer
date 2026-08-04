"""Adapter for Nautilus's own settings: view mode, click policy, sort order,
icon zoom, hidden files. Consolidates reads that used to be scattered ad hoc
GSettings/GVfs-metadata calls across main.py into one cached object, so a
second view module (Column View) can read the same values without duplicating
the GSettings handles or the per-folder sort-metadata polling hack.

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

# Per-folder sort override, stored as GVfs metadata (not GSettings -- there is
# no signal for this; the metadata daemon writes via mmap so file monitors
# never fire). Read by polling, gated to while the sort popover is open.
METADATA_SORT_BY = "metadata::nautilus-icon-view-sort-by"
METADATA_SORT_REVERSED = "metadata::nautilus-icon-view-sort-reversed"

_SORT_POLL_MS = 250  # gvfs sort-metadata poll cadence (only while the sort popover is open)

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

        self.sort_column: str = "name"
        self.sort_reverse: bool = False
        self.view_mode: str = "icon-view"
        self.click_policy: str = "double"  # Nautilus "click-policy": 'single' or 'double'

        self._sort_poll_id: int | None = None
        self._sort_hover: bool = False
        self._resolve_sort_target = None

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

    def refresh_folder_sort(self, uri: str) -> bool:
        """Read `uri`'s sort override into self.sort_column/sort_reverse.
        Returns True when the column or direction changed since last read."""
        col, rev = self.folder_sort(uri) or ("name", False)
        if col != self.sort_column or rev != self.sort_reverse:
            self.sort_column = col
            self.sort_reverse = rev
            return True
        return False

    def resolve_column_sort(self, root_uri: str) -> tuple[str, bool]:
        """Single source of truth for Column View's sort: read root_uri's
        per-folder override (falling back to the global default), applied
        uniformly across the whole Miller chain -- per-column sort makes no
        UX sense when several folders are visible side by side. Folders-first
        grouping is a separate, orthogonal concern: it's read live from
        sort_directories_first() and applied as its own pass in
        MyComputerColumn._populate_rows (widgets.py), not baked into this
        (column, reverse) tuple."""
        folder = self.folder_sort(root_uri)
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
        flow.set_activate_on_single_click) -- Column View's Miller columns are
        always single-click regardless (drill-down UX, see MyComputerColumn)
        and its preview column reads click_policy live at click time
        (common.py's _is_activating_click()), so bundling this with
        view-mode's full _repopulate_visible() would force a needless
        re-enumeration/resort of every open Miller column on a setting that
        doesn't affect their content at all."""
        policy = self._prefs.get_string("click-policy")
        changed = policy != self.click_policy
        self.click_policy = policy
        return changed

    # ── Watcher wiring ───────────────────────────────────────────────────────

    def watch_global(self, ext) -> None:
        """Subscribe to GSettings so view-mode/click-policy/hidden-files changes
        are instant. Call once (not per-window)."""
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

    def watch_sort_button(self, ext, nautilus_win: Gtk.Window, *, resolve_sort_target) -> None:
        """Watch the sort GtkMenuButton's active state -- arm poll when the sort
        popover opens, disarm (with one final read) when it closes. Call once
        per window (may be attempted from more than one call site -- the
        header_motion slot below marks "already attached" so only the first
        actually wires anything up).

        `resolve_sort_target(ext, win) -> tuple[str, Callable[[], None]] | None`
        is re-invoked on every open and every poll tick, returning the URI to
        read sort metadata from and the callback to run when it changes, for
        whichever of our views is currently visible -- or None while none of
        ours is showing. This lets one watch serve every view that has its
        own notion of "current sort" (the disk panel, Column View) without
        hardcoding a single URI/view pairing at attach time."""
        self._resolve_sort_target = resolve_sort_target
        state = ext._windows.get(nautilus_win)
        if not state or state.get("header_motion"):
            return
        # Once inject_column_view_entry has run, the view-options popover
        # lives on our own MenuButton (the native split button's popover was
        # moved there), not on the native MenuButton find_sort_button locates.
        btn = state.get("view_options_menu_button") or self.find_sort_button(nautilus_win)
        if btn is None:
            _log("sort button not found in toolbar")
            return
        btn.connect("notify::active", self._on_sort_button_active, ext, nautilus_win)
        state["header_motion"] = btn  # reuse slot -- just marks "already attached"
        _log(f"sort button watch attached ({type(btn).__name__})")

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
        self, btn: Gtk.MenuButton, _param, ext, nautilus_win: Gtk.Window
    ) -> None:
        state = ext._windows.get(nautilus_win)
        if not state:
            return
        if self._resolve_sort_target(ext, nautilus_win) is None:
            return  # none of our views is currently visible in this window
        if btn.get_active():
            self._sort_hover = True
            if self._sort_poll_id is None:
                _log("sort menu opened → sort poll armed")
                self._sort_poll_id = GLib.timeout_add(
                    _SORT_POLL_MS, self._poll_sort, ext, nautilus_win
                )
        else:
            self._sort_hover = False
            _log("sort menu closed → sort poll disarming")

    def _poll_sort(self, ext, nautilus_win: Gtk.Window) -> bool:
        target = self._resolve_sort_target(ext, nautilus_win)
        if target is None:
            # The visible view changed away from any of ours while the poll
            # was armed (e.g. user navigated out mid-drag) -- nothing left to
            # track, disarm.
            self._sort_poll_id = None
            return GLib.SOURCE_REMOVE
        uri, on_changed = target
        if self.refresh_folder_sort(uri):
            _log(f"sort changed → col='{self.sort_column}' rev={self.sort_reverse}")
            on_changed()
            _log(f"sort applied → col='{self.sort_column}' rev={self.sort_reverse}")
        if not self._sort_hover:
            # Menu closed -- one final read already done above, now disarm.
            _log("sort poll disarmed")
            self._sort_poll_id = None
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE
