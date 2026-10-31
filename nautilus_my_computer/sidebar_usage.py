"""Disk-usage bars under mounted drive names in Nautilus' native sidebar."""

from gi.repository import Gio, GLib, Gtk

from nautilus_my_computer.common import _, _find_row_child, _format_size, _log

_BAR_CLASS = "mc-sidebar-usage-bar"
_BOX_NAME = "mc_sidebar_usage_box"
_REFRESH_SECONDS = 30
_ATTRS = f"{Gio.FILE_ATTRIBUTE_FILESYSTEM_SIZE},{Gio.FILE_ATTRIBUTE_FILESYSTEM_FREE}"

# Thib bar coloured using .diskinfo-bar rule
CSS = b"""
levelbar.mc-sidebar-usage-bar trough,
levelbar.mc-sidebar-usage-bar block {
    min-height: 3px;
}
"""

_timer_id = 0


def _row_mount(row: Gtk.ListBoxRow) -> Gio.Mount | None:
    try:
        mount = row.get_property("mount")
    except TypeError:
        return None
    return mount if isinstance(mount, Gio.Mount) else None


def _ensure_bar(row: Gtk.ListBoxRow) -> Gtk.LevelBar | None:
    """Return the row's usage bar, wrapping the label on first use."""
    bar = getattr(row, "_mc_usage_bar", None)
    if bar is not None:
        return bar
    label = _find_row_child(row, Gtk.Label)
    if label is None:
        return None
    hbox = label.get_parent()
    if not isinstance(hbox, Gtk.Box) or hbox.get_name() == _BOX_NAME:
        return None

    # move native label into vbox
    vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
    vbox.set_name(_BOX_NAME)
    vbox.set_hexpand(True)
    vbox.set_valign(Gtk.Align.CENTER)
    hbox.insert_child_after(vbox, label)
    hbox.remove(label)
    vbox.append(label)

    # show usage bar under the label
    bar = Gtk.LevelBar()
    bar.set_mode(Gtk.LevelBarMode.CONTINUOUS)
    bar.set_min_value(0.0)
    bar.set_max_value(1.0)
    bar.set_margin_end(2)
    bar.add_css_class("diskinfo-bar")
    bar.add_css_class(_BAR_CLASS)
    bar.set_visible(False)
    vbox.append(bar)

    row._mc_usage_bar = bar
    return bar


def _show_usage(bar: Gtk.LevelBar, usage: str | None) -> None:
    """Show/hide the bar and display disk usage info"""
    bar.set_visible(usage is not None)
    vbox = bar.get_parent()
    if usage is None:
        vbox.set_tooltip_text(None)
        return
    row = bar.get_ancestor(Gtk.ListBoxRow)
    native = row.get_tooltip_text() if row is not None else None
    vbox.set_tooltip_text(f"{native}\n{usage}" if native else usage)


def _on_info_ready(gfile: Gio.File, result: Gio.AsyncResult, bar: Gtk.LevelBar) -> None:
    bar._mc_inflight = False
    try:
        info = gfile.query_filesystem_info_finish(result)
    except GLib.Error as e:
        _log(f"sidebar usage query failed for {gfile.get_uri()}: {e.message}")
        _show_usage(bar, None)
        return
    total = info.get_attribute_uint64(Gio.FILE_ATTRIBUTE_FILESYSTEM_SIZE)
    free = info.get_attribute_uint64(Gio.FILE_ATTRIBUTE_FILESYSTEM_FREE)

    # hide the bar if the Fs doesn't report size
    if total <= 0:
        _show_usage(bar, None)
        return
    bar.set_value((total - free) / total)
    _show_usage(
        bar, _("{free} free of {total}").format(free=_format_size(free), total=_format_size(total))
    )


def _query(bar: Gtk.LevelBar, mount: Gio.Mount) -> None:
    if getattr(bar, "_mc_inflight", False):
        return
    root = mount.get_root()
    if root is None:
        return
    bar._mc_inflight = True
    root.query_filesystem_info_async(_ATTRS, GLib.PRIORITY_LOW, None, _on_info_ready, bar)


def apply_usage_bars(native_listbox: Gtk.ListBox) -> None:
    """Add / refresh a usage bar on every mounted drive row."""
    for row in native_listbox:
        mount = _row_mount(row)
        if mount is None:
            bar = getattr(row, "_mc_usage_bar", None)
            if bar is not None:
                bar.set_visible(False)
            continue
        bar = _ensure_bar(row)
        if bar is not None:
            _query(bar, mount)


def start_refresh(ext) -> None:
    """Start the shared refresh timer once for the whole extension."""
    global _timer_id
    if _timer_id:
        return

    def _tick() -> bool:
        for state in list(ext._windows.values()):
            listbox = state.get("sidebar_listbox")
            if listbox is not None and listbox.get_mapped():
                apply_usage_bars(listbox)
        return GLib.SOURCE_CONTINUE

    _timer_id = GLib.timeout_add_seconds(_REFRESH_SECONDS, _tick)
