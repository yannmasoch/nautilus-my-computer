"""My Computer view target: the computer:/// disk/mount panel end-to-end --
mount scanning, classification, usage polling, panel population, card
interaction, and mount/unmount/eject/format actions.

Data helpers take gsettings/mounts explicitly; UI/behaviour helpers take `ext`
(the MyComputerExtension instance) for window/state access, following the
same convention as bookmarks.py/preferred_folders.py. No app state of its own
beyond the module-level mount/folder/network caches below -- everything else
lives on `ext`, set up once by init_data_watchers(). Must not import main.py.

The panel is injected per-slot, as an extra named child of each
NautilusWindowSlot's own GtkStack (issue #133), the same approach Column
View uses (column_view.py, issue #118): every tab gets its own panel
instance (selection, filter, scroll), keyed off `slot._mc_computer`.
"""

from __future__ import annotations

import dataclasses
import functools
import os
import re
import threading
import time

import gi

gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from nautilus_my_computer import common, preferred_folders
from nautilus_my_computer.common import (
    _CARD_WIDTH,
    _DISK_CARD_ROW_SPACING,
    _DISK_CARD_SPACING,
    _FLOW_COLS_GRID,
    _FOLDER_CARD_ROW_SPACING,
    _FOLDER_CARD_SPACING,
    _FOLDER_FLOW_COLS_GRID,
    N_,
    _,
    _all_widgets,
    _find_widget,
    _format_item_count,
    _format_permissions,
    _log,
    _mc_date_to_str,
    _native,
    _resolve_custom_gicon,
    _uri_is_hidden,
)
from nautilus_my_computer.context_menu import (
    ContextMenu,
    ContextMenuItem,
    ContextMenuSection,
    open_section,
    properties_section,
)
from nautilus_my_computer.preferred_folders import PreferredFolder
from nautilus_my_computer.widgets import (
    MyComputerCardSection,
    MyComputerDiskCard,
    MyComputerFolderCard,
)

DISKS_URI = "computer:///"
_DISKS_FILE = Gio.File.new_for_uri(DISKS_URI)
METADATA_SORT_BY = "metadata::nautilus-icon-view-sort-by"
METADATA_SORT_REVERSED = "metadata::nautilus-icon-view-sort-reversed"
_REFRESH_DEBOUNCE_MS = 300  # coalesce rapid mount/unmount/plug events
_USAGE_GATE_MS = 1000  # idle cadence: try a statvfs sweep this often, skip while disk is busy
_USAGE_POLL_FAST_MS = 250  # fast cadence while writes are buffered (Dirty+Writeback elevated)
_USAGE_BUSY_RATIO = (
    0.50  # io_ticks delta / interval above this == disk busy → skip statvfs (avoid I/O contention)
)

_DIRTY_ACTIVE_THRESHOLD = (
    4 * 1000 * 1000
)  # /proc/meminfo Dirty+Writeback ≥ this → poll fast (above resting journal noise ~1–2 MB)
_USAGE_POLL_NETWORK_MS = 5000  # async D-Bus usage poll interval for GVfs/network mounts
_SORT_POLL_MS = 250  # gvfs sort-metadata poll cadence (only while header is hovered)
_STALE_RELEASE_FRAMES = 2  # keep detached panel generations alive across this many frame ticks
REAL_FSTYPES = {
    "ext4",
    "ext3",
    "ext2",
    "xfs",
    "btrfs",
    "bcachefs",
    "f2fs",
    "ntfs",
    "ntfs3",
    "vfat",
    "exfat",
    "zfs",
    "reiserfs",
    "jfs",
    "ufs",
    "minix",
    "hfsplus",
    "apfs",
    "erofs",
    "fuseblk",
}
NETWORK_FSTYPES = {
    "nfs",
    "nfs4",
    "cifs",
    "smb",
    "smb2",
    "smbfs",
    "fuse",
    "fuse.sshfs",
    "fuse.rclone",
    "fuse.s3fs",
    "fuse.davfs2",
    "davfs",
    "sshfs",
    "ftpfs",
    "gvfsd-fuse",
}
OPTICAL_FSTYPES = {"iso9660", "udf"}
EXTERNAL_PREFIXES = ("/media/", "/run/media/", "/mnt/")

VIEW_FILES = "files"  # visible_view token -- slot's own native content
VIEW_DISKINFO = "diskinfo"  # visible_view token -- our panel, elected on this slot's own GtkStack

# Name the panel is added under on each slot's own GtkStack (see
# watch_tab_view/_do_inject_into_slot below). Nautilus's own two stack
# children (vbox, global_search_page) are added via gtk_stack_add_child
# with no name, so this name can never collide with anything of theirs.
_SLOT_STACK_CHILD_NAME = "mc-computer"
_SLOT_INIT_RETRY_MS = 20  # retry interval while waiting for a new slot to settle
_SLOT_INIT_MAX_ATTEMPTS = 100  # ~2s budget, mirrors main.py's _WIN_INIT_MAX_ATTEMPTS


def _disk_context_menu(ext, win, m) -> ContextMenu:
    """Build a disk card's right-click menu from live mount state.

    Same three-section layout as before: open actions, then mount/eject/unmount +
    format (skipped for protected system/home mounts), then Properties (mounted
    only). Unmounted disks mount first, then open in the requested target.
    """
    nav_uri = m.nav_uri or (Gio.File.new_for_path(m.mountpoint).get_uri() if m.mountpoint else "")
    is_mounted = m.is_mounted
    device = m.device or ""
    if not device.startswith("/dev/") and m.gio_volume:
        unix_dev = m.gio_volume.get_identifier(Gio.VOLUME_IDENTIFIER_KIND_UNIX_DEVICE)
        if unix_dev:
            device = unix_dev

    # Section 0: open actions (all disks). Mounted disks navigate directly;
    # unmounted disks mount first, then open in the requested target.
    if is_mounted and nav_uri:
        # See _do_open_with(). Only local mounts would ever
        # resolve in the system app chooser; network mounts (smb://, sftp://) have
        # no app handler, so omit it there entirely, like native Nautilus.
        open_actions = open_section(
            lambda: ext._do_open(nav_uri, win),
            open_tab_action=lambda: ext._do_open_tab(nav_uri, win, make_active=False),
            open_window_action=lambda: ext._do_open_window(nav_uri),
            open_with_action=(
                (lambda: ext._do_open_with(nav_uri, win)) if nav_uri.startswith("file://") else None
            ),
        )
    else:
        open_actions = open_section(
            lambda: _do_mount_then_open(ext, m, win, "current"),
            open_tab_action=lambda: _do_mount_then_open(ext, m, win, "tab"),
            open_window_action=lambda: _do_mount_then_open(ext, m, win, "window"),
        )
    sections = [open_actions]

    # Section 1: mount / unmount / eject + format (non-protected only).
    device_items = []
    if not _is_protected_mount(m):
        if not is_mounted:
            if m.can_mount:
                device_items.append(
                    ContextMenuItem(_("Mount"), action=lambda: _do_mount(ext, m, win))
                )
        elif m.can_eject:
            device_items.append(ContextMenuItem(_native("Eject"), action=lambda: _do_eject(ext, m)))
        elif m.can_unmount:
            device_items.append(
                ContextMenuItem(_native("Unmount"), action=lambda: _do_unmount(ext, m))
            )
        if device.startswith("/dev/"):
            device_items.append(
                ContextMenuItem(_native("Format…"), action=lambda: _do_format(ext, device))
            )
    if device_items:
        sections.append(ContextMenuSection(device_items))

    # Section 2: properties (mounted disks only).
    if is_mounted and nav_uri:
        sections.append(properties_section(lambda: ext._do_properties(nav_uri, win)))

    return ContextMenu(sections)


@dataclasses.dataclass
class MountInfo:
    """Typed representation of a single mounted/unmounted storage entry."""

    # Stable identity
    key: str  # "uuid:<uuid>" when UUID is known; otherwise device path or URI
    uuid: str | None  # filesystem UUID from /dev/disk/by-uuid (None for GVfs/unmounted)

    # Device info
    device: str  # /dev/sda1 or GVfs URI
    mountpoint: str  # local path or GVfs URI (empty for unmounted)
    fstype: str  # "ext4", "gvfs", "unmounted", "network-place", …
    opts: set  # mount options from /proc/mounts

    # Navigation
    nav_uri: str  # file:///… or smb://… (empty for unmounted)
    display_name: str  # user-facing label

    # Usage (updated by poll workers via dataclasses.replace)
    total: int
    free: int

    # GIO handles
    gio_icon: object | None = None
    gio_mount: object | None = None
    gio_volume: object | None = None

    # Flags
    is_gio: bool = False
    is_mounted: bool = True
    is_removable: bool = False
    can_eject: bool = False
    can_mount: bool = False
    can_unmount: bool = False
    is_network_place: bool = False
    is_hidden: bool = False  # standard::is-hidden on the mount root, local mounts only

    # Right-click menu factory menu(ext, win, m) -> ContextMenu (built at show-time).
    menu: object = _disk_context_menu

    @property
    def used(self) -> int:
        return self.total - self.free

    @property
    def percent(self) -> float:
        return round(self.used / self.total * 100, 1) if self.total > 0 else 0.0


_MOUNT_ESCAPE_RE = re.compile(r"\\([0-7]{3})")


def _unescape_mount_field(s: str) -> str:
    """Decode octal escapes written by the kernel in /proc/mounts (space=\\040, etc.)."""
    return _MOUNT_ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 8)), s)


def _read_os_name() -> str:
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return ""


def _is_ostree_booted() -> bool:
    """True on OSTree/image-based systems, including bootc distributions."""
    return os.path.exists("/run/ostree-booted")


def _is_ostree_implementation_mount(mountpoint: str) -> bool:
    """True for implementation mounts that should not be shown as drives."""
    if not _is_ostree_booted():
        return False
    return mountpoint in ("/etc", "/var", "/sysroot") or mountpoint.startswith("/sysroot/")


def _statvfs_usage(path: str) -> tuple[int, int] | None:
    """Return total/free bytes for a path, or None when unavailable."""
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    return st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize


def _root_usage() -> tuple[int, int] | None:
    """Return user-meaningful root capacity.

    On OSTree/bootc systems, / may be the small immutable image view. Prefer
    the writable/backing deployment filesystem for the displayed root card
    while still navigating to /.
    """
    if _is_ostree_booted():
        candidates = [_statvfs_usage(path) for path in ("/var", "/sysroot") if os.path.exists(path)]
        candidates = [usage for usage in candidates if usage is not None]
        if candidates:
            return max(candidates, key=lambda usage: usage[0])
    return _statvfs_usage("/")


def _root_mount_info() -> MountInfo | None:
    """Build a canonical root entry when /proc/mounts does not expose one cleanly."""
    usage = _root_usage()
    if usage is None:
        return None
    total, free = usage
    return MountInfo(
        key="path:/",
        uuid=None,
        device="/",
        mountpoint="/",
        fstype="rootfs",
        opts=set(),
        total=total,
        free=free,
        display_name=_read_os_name() or "/",
        nav_uri=Gio.File.new_for_path("/").get_uri(),
    )


def _build_uuid_map() -> dict[str, str]:
    """Return {real_device_path: uuid_string} from /dev/disk/by-uuid."""
    result: dict[str, str] = {}
    by_uuid = "/dev/disk/by-uuid"
    if not os.path.isdir(by_uuid):
        return result
    try:
        for entry in os.scandir(by_uuid):
            if entry.is_symlink():
                try:
                    result[os.path.realpath(entry.path)] = entry.name
                except OSError:
                    pass
    except OSError:
        pass
    return result


def _is_system_mount(m: MountInfo) -> bool:
    """True for root, boot, EFI, and swap - mounts that belong to the System group."""
    return (
        m.mountpoint == "/" or m.mountpoint in ("/boot", "/boot/efi", "/efi") or m.fstype == "swap"
    )


def _is_protected_mount(m: MountInfo) -> bool:
    """True if Unmount/Eject/Format should be hidden for this mount.

    Used only for context-menu action gating, not display grouping - unlike
    _is_system_mount, a protected mount may still appear under "On this Computer".
    Backed by Gio.unix_mount_is_system_internal(), the same heuristic GNOME uses,
    so it covers most system mounts across distros without a hardcoded list. Two
    cases it can miss, kept as an explicit fallback: a per-user /home/<user> mount
    (e.g. encrypted home) which the signal doesn't flag but is still home; and the
    EFI System Partition, which some distros mount without marking it internal.
    """
    if m.is_gio or not m.mountpoint.startswith("/"):
        return False
    if m.mountpoint == "/home" or m.mountpoint.startswith("/home/"):
        return True
    if m.mountpoint in ("/boot/efi", "/efi"):
        return True
    entry = Gio.unix_mount_at(m.mountpoint)
    if isinstance(entry, tuple):
        entry = entry[0]
    return bool(entry and Gio.unix_mount_is_system_internal(entry))


def _classify_mount(m: MountInfo) -> str:
    """Return 'system', 'local', 'removable', 'disc', or 'network' for a mount entry."""
    # Unmounted volumes are never part of the running system.
    # Removable (USB, optical) -> "Removable"; others -> "On this Computer"
    if not m.is_mounted:
        return "removable" if m.is_removable else "local"

    # GVfs mounts -- phones/cameras (MTP, PTP) go to removable; rest are network
    if m.is_gio:
        if m.nav_uri.startswith(("mtp://", "gphoto2://", "afc://", "obex://")):
            return "removable"
        return "network"

    # Removable-media paths: check path before fstype so USB drives (including live Linux
    # USBs with iso9660 partitions) are not misclassified as discs. Exception: loop-mounted
    # ISO images also land under /run/media/ but their device is /dev/loopN -- those are discs.
    if any(m.mountpoint.startswith(p) for p in EXTERNAL_PREFIXES):
        if m.fstype in OPTICAL_FSTYPES and m.device.startswith("/dev/loop"):
            return "disc"
        return "removable" if m.is_removable else "local"

    # Optical filesystems not under external paths -> physical disc or image
    if m.fstype in OPTICAL_FSTYPES:
        return "disc"

    # x-gvfs-show fstab entries and known network fstypes -> network
    if "x-gvfs-show" in m.opts or m.fstype in NETWORK_FSTYPES or m.fstype.startswith("fuse"):
        return "network"

    # Root, boot/EFI, swap -> System group
    if _is_system_mount(m):
        return "system"

    return "local"


def _get_local_mount_tier(m: MountInfo) -> tuple[int, bool, str]:
    """Return (tier, is_hidden, name) for hierarchical sorting within 'local'
    group. Tier: 0=root, 1=system partitions, 2=mounted, 3=unmounted.
    is_hidden is a sub-bucket within each tier -- disk mounts have no
    filename-based sort-last convention the way Column View's files do, so
    this stays its own, deliberately simpler model. Used by 'sort by type'
    mode."""
    name = (m.display_name or "").lower()
    if m.mountpoint == "/":
        return (0, m.is_hidden, name)
    if m.mountpoint in ("/boot", "/boot/efi", "/efi") or m.fstype == "swap":
        return (1, m.is_hidden, name)
    if m.is_mounted:
        return (2, m.is_hidden, name)
    return (3, m.is_hidden, name)


# Ordered group spec: (key, display_label, gsettings_key)
# "local" is the merge target for other groups -- always visible, no gsettings key
_GROUP_SPEC: list[tuple[str, str, str | None]] = [
    ("system", N_("System"), "visibility-system"),
    ("local", N_("On this Computer"), None),
    ("removable", N_("Removable"), "visibility-removable"),
    ("disc", N_("Disc"), "visibility-disc"),
    # "Network" always resolves through _native() (see _populate), so it needs
    # no N_() marker -- gvfs already has the exact wording in every language.
    ("network", "Network", "visibility-network"),
]


@dataclasses.dataclass
class PanelGroup:
    """A rendered group on the Computer view: a heading + a grid/list of cards.

    kind selects the card builder used in _populate(): "disk" for MountInfo
    items (the existing disk groups), "folder" for PreferredFolder items.
    """

    key: str
    label: str
    visible: bool = True
    merged: bool = False
    kind: str = "disk"
    items: list = dataclasses.field(default_factory=list)

    def add_item(self, m) -> None:
        self.items.append(m)

    def sort_items(self, key_func, reverse: bool = False) -> None:
        self.items.sort(key=key_func, reverse=reverse)


_disk_data: dict[str, MountInfo] = {}
_folder_data: dict[str, "PreferredFolder"] = {}
_network_places: list[MountInfo] = []  # populated async from network:///

# Raw resolved caption attributes per folder key, independent of which of the
# 3 GSettings caption tokens are currently active -- so switching tokens never
# needs to re-query a field already fetched. Keys among "content_type",
# "mtime", "atime", "ctime", "owner", "group", "mode", "item_count".
_folder_caption_data: dict[str, dict] = {}

_CSS = b"""
* {
    /* Mirrors Nautilus's own --accent-bg-color override from its bundled style.css
       (.nautilus-grid-view gridview rule). Theme-safe: GTK themes load at priority
       200 (THEME), this loads at 600 (APPLICATION) - themes cannot override it.
       Only user stylesheets at priority 800 (USER) can, which is correct behavior. */
    --diskinfo-selection-grey: #959595;
}
.diskinfo-panel {
}
.diskinfo-panel flowbox {
    --accent-bg-color: var(--diskinfo-selection-grey);
    padding: 0;
    margin: 0;
}
.mc-icon-grid {
    --accent-bg-color: var(--diskinfo-selection-grey);
}
.diskinfo-subtext {
    color: @insensitive_fg_color;
}
.unmounted {
    opacity: 0.5;
}
/* Same class/value Nautilus's own grid/list cells use to dim hidden-file icons
   (nautilus-grid-cell.c, nautilus-name-cell.c, style.css's ".view .hidden-file").
   Unscoped here (no .view ancestor requirement) since Column View's rows aren't
   inside Nautilus's own view widget tree. */
.hidden-file {
    opacity: 0.55;
}
/* For testing/debugging: shows injected panel outline vs native sidebar. */
.debug {
    background: red;
}
.debug-gap {
    margin: 0;
    padding: 0;
}
/* Zero the theme's default flowboxchild padding/margin so all card spacing is
   controlled by our own widgets (col/row spacing on the FlowBox, margins on
   the card itself) instead of fighting the theme's built-in wrapper inset. */
.diskinfo-panel flowboxchild {
    padding: 0;
    margin: 0;
}
/* Reordering moves the dragged card into its new slot live, so GTK's default
   active-drop outline is redundant and visually conflicts with that preview. */
.diskinfo-panel .nautilus-view-cell:drop(active) {
    box-shadow: none;
}
.mc-selected {
    background-color: alpha(@window_fg_color, 0.07);
    border-radius: 12px;
}
/* Miller view preview column: 12px inner inset on every edge. */
.mc-preview-column {
    padding: 12px;
}
/* Persistent early-access marker for Column View. The warm red follows the
   destructive/action-warning family without borrowing the user's accent. */
.mc-beta-badge {
    background-color: #c01c28;
    color: #ffffff;
    border-radius: 4px;
    font-size: 0.78em;
    font-weight: 700;
    padding: 3px 8px;
}
/* Preview thumbnail: rounded corners. The Gtk.Picture sets overflow:hidden so
   this radius clips the drawn image. 12px matches .card / .nautilus-view-cell. */
.mc-preview-image {
    border-radius: 12px;
}
/* Column View row thumbnail: rounded corners. The Gtk.Picture sets
   overflow:hidden so this radius clips the COVER-cropped square texture. */
.mc-row-thumbnail {
    border-radius: 3px;
}
/* GtkListBoxRow normally applies the sidebar's horizontal padding around its
   child. Move that inset into the Miller child box so it fills the outer row
   allocation exactly, while its contents retain the native 8px inset. */
.mc-column-list > row.mc-column-row {
    padding-left: 0;
    padding-right: 0;
}
.mc-column-list > row.mc-column-row > .mc-column-row-content {
    padding-left: 8px;
    padding-right: 8px;
}
.mc-column-list > row.mc-column-row.mc-row-cut {
    opacity: 0.50;
}
/* Miller columns reuse .navigation-sidebar for its rounded-corner selection
   shape (see widgets.py's MyComputerColumn), but a sidebar's native
   :selected fill is a neutral grey, not accent -- correct for a places
   sidebar, wrong for a browsing view where the last-selected row (folder or
   file) should read like a normal content-view selection. Re-tint just the
   selected state to the native accent tokens, keep everything else
   (hover, shape, spacing) untouched.

   Scoped to .mc-current-column, not plain :selected: only the column that
   was last clicked (column_view.py's tracked focused_index, applied via
   MyComputerColumn.set_current_column) reads as accent -- an ancestor
   column still further back on the committed path keeps its row selected
   internally (so navigating still works) but falls back to the plain
   native :selected grey instead of competing for attention with accent
   color. This is plain Python-tracked state, not GTK keyboard focus -- no
   dependency on any focus-grabbing. */
.mc-column-list.navigation-sidebar.mc-current-column row:selected {
    background-color: @accent_bg_color;
    color: @accent_fg_color;
}
/* Grid/List/Column segmented switcher (see widgets.MyComputerToggleButton).
   Hand-built from Gtk.Box/Gtk.ToggleButton/Gtk.Separator, not Adw.ToggleGroup
   (libadwaita 1.7+ only), so it renders identically on GNOME 47 and 48+.
   Reuses the same alpha(@window_fg_color, 0.07) hover-overlay formula as
   the panel's grid-cell hover overlay for both the pill background and the
   button hover tint. */
.mc-toggle-group {
    background-color: alpha(@window_fg_color, 0.07);
    border-radius: 9px;
    padding: 2px;
}
.mc-toggle-btn {
    border-radius: 7px;
    /* Overrides the theme's own button padding/min-height, which is taller
       than our target pill height and otherwise forces the whole group
       (and its Widget.set_size_request() floor in widgets.py) to grow past
       it -- a size request is only a minimum, it cannot shrink a child
       whose CSS-driven natural size already exceeds it. */
    padding: 0 0;
    min-height: 0;
    transition: background-color 200ms ease;
}
.mc-toggle-btn:hover {
    background-color: alpha(@window_fg_color, 0.07);
}
.mc-toggle-btn:checked {
    background-color: @view_bg_color;
}
/* Toggled via widgets.MyComputerToggleButton._update_separators() (a CSS
   class, not a direct opacity property set) so this transition actually
   animates -- GTK4 only animates CSS-driven property changes, not
   Widget.set_opacity() calls from code. */
.mc-toggle-sep {
    opacity: 1;
    transition: opacity 200ms ease;
}
.mc-toggle-sep.mc-toggle-sep-hidden {
    opacity: 0;
}
"""


def _read_io_busy() -> tuple:
    """Return (io_ticks_ms, ios_in_progress) summed over physical block devices.

    Reads /proc/diskstats — a pure procfs read with no filesystem/journal
    involvement, so unlike statvfs it never blocks or contends with an in-flight
    file operation. Used purely as a disk-busy gate: while the disk has I/O in
    flight we must NOT call statvfs (statvfs blocks for seconds under ext4 journal
    load and contends with the very operation in progress — confirmed cause of
    sluggish copy/delete when the panel was visible). io_ticks counts wall-time the
    device had at least one request in flight; its delta over an interval gives the
    busy fraction. ios_in_progress is the instantaneous queue depth.

    Note: this is NOT the previously-removed diskstats *estimation* approach — we
    never derive free space from it, only gate when it is safe to call statvfs."""
    ticks = inflight = 0
    try:
        with open("/proc/diskstats") as f:
            for line in f:
                p = line.split()
                if len(p) < 14:
                    continue
                name = p[2]
                if name.startswith(("loop", "ram", "zram", "dm-", "sr")):
                    continue
                try:
                    inflight += int(p[11])  # field 12: I/Os currently in progress
                    ticks += int(p[12])  # field 13: ms spent doing I/Os (io_ticks)
                except ValueError:
                    continue
    except OSError:
        pass
    return ticks, inflight


def _read_dirty_bytes() -> int:
    """Return Dirty + Writeback bytes from /proc/meminfo (a pure procfs read).

    This is the one *forward* signal for an in-progress file operation: it rises
    while writes are buffered in the page cache, *before* the kernel flushes them
    to the device (the moment statvfs/diskstats finally change). It is used ONLY
    as a cadence hint — poll faster while it is elevated, and force one definitive
    sweep when it drains (the flush). It is global (not per-device), so it must
    NEVER be used to estimate or display free space — only to time statvfs."""
    dirty = writeback = 0
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("Dirty:"):
                    dirty = int(line.split()[1]) * 1024  # reported in KiB
                elif line.startswith("Writeback:"):
                    writeback = int(line.split()[1]) * 1024
                    break  # Writeback follows Dirty in /proc/meminfo; both seen
    except (OSError, ValueError, IndexError):
        pass
    return dirty + writeback


def _scan_mounts(show_system_partitions: bool = False) -> list[MountInfo]:
    mounts: list[MountInfo] = []
    seen: set[str] = set()
    uuid_map = _build_uuid_map()
    has_root = False

    # Build mountpoint → Gio.Icon / Gio.Mount from VolumeMonitor so we can
    # attach the real hardware icon and GIO handle to each /proc/mounts entry.
    # Also build a UUID fallback for mounts whose root path doesn't match the
    # /proc/mounts mountpoint (e.g. root on LUKS/dm-crypt).
    icon_by_path: dict[str, Gio.Icon] = {}
    mount_by_path: dict[str, object] = {}
    mount_by_uuid: dict[str, object] = {}
    # Mount roots GIO reports as shadowed -- a mount superseded by another at the
    # same location (e.g. an autofs trigger overmounted by the real cifs mount).
    # This is the exact signal Nautilus uses to drop the duplicate row
    # (nautilus-sidebar.c: `if (g_mount_is_shadowed (mount)) continue;`).
    shadowed_paths: set[str] = set()
    try:
        vm = Gio.VolumeMonitor.get()
        for gm in vm.get_mounts():
            root = gm.get_root()
            path = root.get_path()
            if path:
                icon_by_path[path] = gm.get_icon()
                mount_by_path[path] = gm
                if gm.is_shadowed():
                    shadowed_paths.add(path)
            vol = gm.get_volume()
            if vol:
                uid = vol.get_identifier(Gio.VOLUME_IDENTIFIER_KIND_UUID)
                if uid:
                    mount_by_uuid[uid] = gm
    except Exception:
        pass

    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 4:
                    continue
                device = _unescape_mount_field(parts[0])
                mountpoint = _unescape_mount_field(parts[1])
                fstype, options = parts[2], parts[3]
                opts = set(options.split(","))
                if _is_ostree_implementation_mount(mountpoint):
                    continue
                # GIO already told us this mount is superseded by another at the
                # same path -- skip it, exactly as Nautilus does (see shadowed_paths).
                if mountpoint in shadowed_paths:
                    continue
                gvfs_show = "x-gvfs-show" in opts
                # Admit-list: a /proc/mounts line is a real drive only if its fstype
                # is a known real-storage, network, or optical filesystem (or it is an
                # fstab entry flagged x-gvfs-show, or the root fs). This structurally
                # excludes pseudo/trigger filesystems -- autofs, tmpfs, proc, sysfs,
                # squashfs, ... -- which would otherwise be listed as phantom or
                # duplicate drives when mounted under /media or /mnt (issue #57). The
                # previous "any path under an external prefix" escape hatch was the leak.
                is_real_fs = (
                    fstype in REAL_FSTYPES or fstype in NETWORK_FSTYPES or fstype in OPTICAL_FSTYPES
                )
                if (not is_real_fs and not gvfs_show and mountpoint != "/") or device in seen:
                    continue
                if not show_system_partitions and mountpoint in ("/boot", "/boot/efi", "/efi"):
                    continue
                seen.add(device)
                try:
                    usage = _root_usage() if mountpoint == "/" else _statvfs_usage(mountpoint)
                    if usage is None:
                        continue
                    total, free = usage
                    real_dev = os.path.realpath(device)
                    uuid = uuid_map.get(real_dev)
                    gio_mount = mount_by_path.get(mountpoint) or (
                        mount_by_uuid.get(uuid) if uuid else None
                    )
                    name = (
                        (gio_mount.get_name() if gio_mount else None)
                        or (mountpoint == "/" and _read_os_name())
                        or os.path.basename(mountpoint)
                        or "/"
                    )
                    gio_volume = gio_mount.get_volume() if gio_mount else None
                    gio_drive = gio_volume.get_drive() if gio_volume else None
                    key = f"uuid:{uuid}" if uuid else device
                    if mountpoint == "/":
                        has_root = True
                    nav_uri = Gio.File.new_for_path(mountpoint).get_uri()
                    mounts.append(
                        MountInfo(
                            key=key,
                            uuid=uuid,
                            device=device,
                            mountpoint=mountpoint,
                            fstype=fstype,
                            opts=opts,
                            total=total,
                            free=free,
                            display_name=name,
                            nav_uri=nav_uri,
                            is_hidden=_uri_is_hidden(nav_uri),
                            gio_icon=icon_by_path.get(mountpoint),
                            gio_mount=gio_mount,
                            gio_volume=gio_volume,
                            is_removable=gio_drive.is_removable() if gio_drive else False,
                            can_eject=bool(
                                (gio_volume and gio_volume.can_eject())
                                or (gio_mount and gio_mount.can_eject())
                                or (gio_drive and gio_drive.can_eject())
                            ),
                            can_unmount=bool(gio_mount and gio_mount.can_unmount()),
                        )
                    )
                except OSError:
                    pass
    except OSError:
        pass
    # Collapse any remaining same-mountpoint duplicates to the effective (last)
    # mount -- our local analogue of GIO shadowing, for overmounts GIO does not
    # flag. When two lines share a mountpoint only the topmost is reachable in the
    # VFS, and /proc/mounts lists it last, so last-wins keeps the visible drive.
    if mounts:
        by_mountpoint: dict[str, MountInfo] = {}
        for mi in mounts:
            by_mountpoint[mi.mountpoint] = mi
        mounts = list(by_mountpoint.values())
    if not has_root:
        root = _root_mount_info()
        if root is not None:
            mounts.insert(0, root)
    return mounts


def _scan_gio_mounts() -> list[MountInfo]:
    """Enumerate GVfs/network mounts via Gio.VolumeMonitor.

    Returns mounts that are NOT file:// (those are already covered by
    _scan_mounts via /proc/mounts), e.g. smb://, sftp://, mtp://, dav://.
    """
    results: list[MountInfo] = []
    try:
        vm = Gio.VolumeMonitor.get()
        for mount in vm.get_mounts():
            root = mount.get_root()
            uri = root.get_uri()

            # Skip regular local filesystems — already in /proc/mounts
            if uri.startswith("file://"):
                continue
            # Skip virtual/meta locations
            if uri.startswith(("trash://", "recent://", "burn://")):
                continue

            name = mount.get_name() or uri
            local_path = root.get_path()  # FUSE path, may be None

            total = free = 0
            if local_path:
                try:
                    st = os.statvfs(local_path)
                    total = st.f_blocks * st.f_frsize
                    free = st.f_bavail * st.f_frsize
                except OSError:
                    pass

            gio_volume = mount.get_volume()
            gio_drive = gio_volume.get_drive() if gio_volume else None
            # Only stat via the local FUSE path -- query_info() on the bare GVfs URI
            # would hit the network synchronously and could block this scan.
            is_hidden = (
                _uri_is_hidden(Gio.File.new_for_path(local_path).get_uri()) if local_path else False
            )
            results.append(
                MountInfo(
                    key=uri,
                    uuid=None,
                    device=uri,
                    mountpoint=local_path or uri,
                    fstype="gvfs",
                    opts=set(),
                    total=total,
                    free=free,
                    display_name=name,
                    nav_uri=uri,
                    is_hidden=is_hidden,
                    is_gio=True,
                    gio_icon=mount.get_icon(),
                    gio_mount=mount,
                    gio_volume=gio_volume,
                    is_removable=gio_drive.is_removable() if gio_drive else False,
                    can_eject=bool(
                        (gio_volume and gio_volume.can_eject())
                        or mount.can_eject()
                        or (gio_drive and gio_drive.can_eject())
                    ),
                    can_unmount=bool(mount.can_unmount()),
                )
            )
    except Exception:
        pass
    return results


def _scan_gio_volumes() -> list[MountInfo]:
    """Enumerate Gio volumes that are connected but not yet mounted.

    Volumes already mounted are covered by _scan_mounts / _scan_gio_mounts,
    so we skip them here to avoid duplicates.
    """
    results: list[MountInfo] = []
    try:
        vm = Gio.VolumeMonitor.get()
        for volume in vm.get_volumes():
            if volume.get_mount() is not None:
                continue  # already mounted — covered elsewhere
            name = volume.get_name() or "Unknown Device"
            drive = volume.get_drive()
            is_removable = drive.is_removable() if drive else True
            results.append(
                MountInfo(
                    key=f"vol:{name}",
                    uuid=None,
                    device=f"vol:{name}",
                    mountpoint="",
                    fstype="unmounted",
                    opts=set(),
                    total=0,
                    free=0,
                    display_name=name,
                    nav_uri="",
                    is_mounted=False,
                    is_removable=is_removable,
                    gio_icon=volume.get_icon(),
                    gio_volume=volume,
                    can_eject=bool(volume.can_eject() or (drive and drive.can_eject())),
                    can_mount=bool(volume.can_mount()),
                )
            )
    except Exception:
        pass
    return results


def _refresh_network_places(on_done=None) -> None:
    """Enumerate network:/// in a background thread.

    GVfs returns both recent ("Previous") and discovered ("Available on
    Current Network") entries.  Calls on_done() on the main thread when
    finished so the caller can repopulate the view.
    """

    def _worker():
        global _network_places
        results: list[MountInfo] = []
        try:
            gfile = Gio.File.new_for_uri("network:///")
            enumerator = gfile.enumerate_children(
                "standard::name,standard::display-name,standard::icon,standard::target-uri",
                Gio.FileQueryInfoFlags.NONE,
                None,
            )
            while True:
                info = enumerator.next_file(None)
                if info is None:
                    break
                name = info.get_display_name() or info.get_name()
                icon = info.get_icon()
                target = info.get_attribute_string("standard::target-uri") or ""
                nav_uri = target or gfile.get_child(info.get_name()).get_uri()
                if not nav_uri or nav_uri.startswith("network:///"):
                    if not target:
                        continue
                results.append(
                    MountInfo(
                        key=f"netplace:{nav_uri}",
                        uuid=None,
                        device=nav_uri,
                        mountpoint=nav_uri,
                        fstype="network-place",
                        opts=set(),
                        total=0,
                        free=0,
                        display_name=name,
                        nav_uri=nav_uri,
                        gio_icon=icon,
                        is_network_place=True,
                    )
                )
            enumerator.close(None)
        except Exception as e:
            _log(f"network:/// enumerate: {e}")
        _network_places = results
        if on_done:
            GLib.idle_add(on_done)

    threading.Thread(target=_worker, daemon=True).start()


def _refresh(mounts: list[MountInfo]) -> bool:
    global _disk_data
    new_data = {m.key: m for m in mounts}
    changed = new_data != _disk_data
    _disk_data = new_data
    return changed


def _window_is_at_disks(win) -> bool:
    """True if the window's active slot is currently showing DISKS_URI.

    Reads the NautilusWindowSlot "location" GFile property on demand. No
    persistent signal, no set_child (safe re: issue #11). Prefers the active
    slot so tabs are handled; falls back to the first slot with a location.
    """
    fallback = None
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
                return loc.equal(_DISKS_FILE)
        except TypeError:
            pass
        fallback = loc
    return fallback is not None and fallback.equal(_DISKS_FILE)


def _slot_panel_state(slot) -> dict | None:
    """The per-slot panel state dict injected by _do_inject_into_slot, or
    None if `slot` hasn't been injected yet (or is None)."""
    return getattr(slot, "_mc_computer", None) if slot is not None else None


def watch_tab_view(ext, win: Gtk.Window) -> None:
    """Inject the Computer panel into every current and future tab of `win`.

    Nautilus creates one NautilusWindowSlot per tab, each owning its own
    GtkStack that already holds two sibling children of its own (vbox and
    global_search_page, nautilus-window-slot.c:869-892). We add the panel as
    a third sibling of that same stack instead of the single window-wide
    overlay used before, so tab switching needs no resync: each tab's panel
    state (selection, filter, scroll) lives on its own slot and is untouched
    by switching away from it. See issue #133 (mirrors Column View, #118)."""
    common.watch_slots(win, lambda w, slot, ext=ext: _schedule_slot_init(ext, w, slot))


def _slot_idle(slot: Gtk.Widget) -> bool:
    """Location resolved AND the slot's initial native load has finished
    (allow-stop false). Column View only waits on location (its own
    injection predates this and has not shown the issue this guards
    against), but adding a second stack child (add_named does not change
    the visible child, but still triggers a size/layout pass) while the
    slot's *initial* native enumeration for a real folder is still in
    flight has been observed to produce a burst of Nautilus-core
    "Unexpected plugin response" warnings (stale async context-menu-provider
    jobs) -- most visible right after a background tab is opened straight to
    a real folder (e.g. "Open in New Tab" on a Preferred Folder card).
    Waiting for the load to finish avoids injecting into that window."""
    try:
        if slot.get_property("location") is None:
            return False
        return not slot.get_property("allow-stop")
    except TypeError:
        return True


def _schedule_slot_init(ext, win: Gtk.Window, slot: Gtk.Widget | None) -> None:
    if slot is None:
        return
    common.schedule_slot_init(
        slot,
        "_mc_computer",
        functools.partial(_do_inject_into_slot, ext, win),
        retry_ms=_SLOT_INIT_RETRY_MS,
        max_attempts=_SLOT_INIT_MAX_ATTEMPTS,
        is_settled=_slot_idle,
    )


def _do_inject_into_slot(ext, win: Gtk.Window, slot: Gtk.Widget) -> bool:
    if getattr(slot, "_mc_computer", None) is not None:
        return GLib.SOURCE_REMOVE
    stack = common._find_slot_stack(slot)
    if stack is None:
        _log("_do_inject_into_slot: no GtkStack found on slot")
        return GLib.SOURCE_REMOVE
    panel, grid_host, grid_box = _build_panel(ext, win)
    stack.add_named(panel, _SLOT_STACK_CHILD_NAME)
    slot._mc_computer = {
        "window": win,
        "slot": slot,
        "panel": panel,
        "grid_host": grid_host,
        "grid_box": grid_box,
        "section_flows": [],
        "card_widgets": {},  # key → MyComputerDiskCard
        "folder_card_widgets": {},
        "stale_generations": [],
        "stale_release_tick_id": None,
        "stale_release_ticks": 0,
        "_deselecting": False,
        "selected_mount_key": None,
        "selected_folder_key": None,
        "filter_query": "",
        "location_filter_owned": False,
        "visible_view": VIEW_FILES,
        "previous_child": None,  # stack child to restore when leaving the panel
    }
    ext._panel_slots.add(slot)
    stack.connect("notify::visible-child", _on_slot_stack_child_changed, slot)
    slot.connect("notify::location", _on_slot_location_changed, ext, win)
    loc = slot.get_property("location")
    if loc is not None and loc.equal(_DISKS_FILE):
        _enter_panel(ext, win, slot)
    return GLib.SOURCE_REMOVE


def _on_slot_stack_child_changed(stack, _pspec, slot: Gtk.Widget) -> None:
    """Nautilus reasserts its own stack child on its own initiative (e.g.
    leaving global search, nautilus-window-slot.c:1045/1091). Reassert the
    panel if it is currently elected for this slot (mirrors Column View's
    _on_slot_stack_child_changed, issue #118).

    Also requires common.slot_view_owner(slot) == "computer": Column View
    (column_view.py) shares this same stack and has its own reassert handler
    with its own local elected flag, which can be stale relative to this
    one. Without the shared owner token both handlers could reassert against
    each other with no termination condition (issue #137)."""
    if getattr(slot, "_mc_computer_reasserting", False):
        return
    state = getattr(slot, "_mc_computer", None)
    if (
        state is None
        or state.get("visible_view") != VIEW_DISKINFO
        or common.slot_view_owner(slot) != "computer"
    ):
        return
    panel = state["panel"]
    if stack.get_visible_child() is panel:
        return
    slot._mc_computer_reasserting = True
    stack.set_visible_child(panel)
    slot._mc_computer_reasserting = False


def _on_slot_location_changed(slot, _pspec, ext, win: Gtk.Window) -> None:
    """Keep this slot's own panel in sync with real Nautilus navigation on it
    (address bar, pathbar, back/forward, a bookmark, our own navigation
    echo) -- scoped to exactly the slot that navigated, unlike the
    window-wide title watch this replaces (issue #133)."""
    state = getattr(slot, "_mc_computer", None)
    if state is None:
        return
    loc = slot.get_property("location")
    at_disks = loc is not None and loc.equal(_DISKS_FILE)
    stack = state["panel"].get_parent()
    showing = stack is not None and stack.get_visible_child() is state["panel"]
    if at_disks:
        if not showing:
            _enter_panel(ext, win, slot)
    elif showing:
        _leave_panel(ext, win, slot)


def _unselect_all_cards(state: dict) -> None:
    """Clear every section FlowBox's selection, guarded by _deselecting so
    _on_flow_selection_changed doesn't read it back as a user action."""
    state["_deselecting"] = True
    for flow in state.get("section_flows", []):
        flow.unselect_all()
    state["_deselecting"] = False


def _claim_panel_focus(state: dict, intended_mount, intended_folder) -> None:
    """Take focus onto the scroller so no card does, then restore the
    selection that was actually intended (as captured before GTK's
    focus-driven auto-select had a chance to run).

    Callers must hold state["_deselecting"] = True from before the panel is
    shown through this call: grab_focus() itself, or GTK's own deferred focus
    resolution during mapping, can select a GtkFlowBoxChild in SINGLE mode
    (unlike Nautilus's GtkSingleSelection there is no autoselect property to
    turn off), and without the guard that bogus selection reaches
    _on_flow_selection_changed exactly like a real user click -- overwriting
    selected_mount_key/selected_folder_key so the bogus selection then
    persists across every future populate, indistinguishable from a
    legitimate one. This function is the one place that lifts the guard, so
    it must also be the one place that re-asserts the true intended value."""
    grid_host = state.get("grid_host")
    if grid_host is not None:
        grid_host.grab_focus()
    if not intended_mount and not intended_folder:
        for flow in state.get("section_flows", []):
            flow.unselect_all()
    state["_deselecting"] = False
    state["selected_mount_key"] = intended_mount
    state["selected_folder_key"] = intended_folder


def _claim_panel_focus_when_mapped(state: dict) -> None:
    """Defer _claim_panel_focus() past GTK's own initial-focus resolution.

    grab_focus() on an unmapped/unrealized widget is a no-op. GTK runs its own
    focus resolution as part of mapping (notably at window present() time) and
    picks the first focusable descendant -- a GtkFlowBoxChild, which SINGLE
    selection mode then selects. Claiming focus inside the "map" handler
    itself can still be overridden by that resolution, so the claim is pushed
    one further idle iteration past it.

    One idle iteration is enough for ordinary navigation, but a *cold* window
    present() still wins the race: GTK's initial-focus resolution on first
    present() is itself driven across more than one idle-priority pass, so a
    single idle_add here can still land before it. A second nested one-shot
    idle covers that final pass too. Caller must set state["_deselecting"] =
    True before calling this (see _claim_panel_focus)."""
    panel = state["panel"]
    intended_mount = state.get("selected_mount_key")
    intended_folder = state.get("selected_folder_key")

    def _settle() -> bool:
        def _settle_again() -> bool:
            _claim_panel_focus(state, intended_mount, intended_folder)
            return GLib.SOURCE_REMOVE

        GLib.idle_add(_settle_again)
        return GLib.SOURCE_REMOVE

    def _on_map(w: Gtk.Widget) -> None:
        w.disconnect(handler_id)
        GLib.idle_add(_settle)

    if panel.get_mapped():
        _settle()
    else:
        handler_id = panel.connect("map", _on_map)


def _enter_panel(ext, win: Gtk.Window, slot: Gtk.Widget) -> None:
    """Show the panel for `slot`, populated fresh. Works the same whether
    `slot` is the window's active tab or a background one (e.g. "Open in New
    Tab" on the Computer sidebar row) -- window-singleton chrome (sidebar
    highlight, pathbar chip, sort watch) is handled separately by
    main.py's _on_navigation, scoped to the active slot only."""
    state = slot._mc_computer
    stack = state["panel"].get_parent()
    if stack is None:
        return
    # Release Column View's own state first if it currently owns this slot's
    # stack -- otherwise its notify::visible-child reassert handler still
    # trusts its own (now stale) elected flag and fights the
    # set_visible_child below (issue #137's per-slot view-election arbiter).
    # Must run before previous_child is captured, or it would capture the
    # Column View widget itself instead of the native content beneath it.
    ext._leave_column_view_for_slot(slot)
    state["previous_child"] = stack.get_visible_child()
    _populate_slot(ext, slot)
    # Cancel the covered native load before unmapping it (stack.set_visible_child
    # below unmaps every non-visible child) -- gives Nautilus's own job-cancellation
    # machinery a clean chance to tear down in-flight per-file async work (e.g.
    # context-menu-provider queries) before the forced unmap, rather than after.
    ext._stop_hidden_native_slot(win, slot)
    # Held True from here until _claim_panel_focus_when_mapped's final settle:
    # showing the panel is what triggers GTK's focus-driven auto-select on a
    # card, and _on_flow_selection_changed must not record that as a real
    # selection in the meantime (see _claim_panel_focus).
    state["_deselecting"] = True
    common.set_slot_view_owner(slot, "computer")
    stack.set_visible_child(state["panel"])
    state["visible_view"] = VIEW_DISKINFO
    state["location_filter_owned"] = False
    _claim_panel_focus_when_mapped(state)
    ext._ensure_usage_poll_running()


def _leave_panel(ext, win: Gtk.Window, slot: Gtk.Widget) -> None:
    """Hide the panel for `slot`, restoring whatever stack child was showing
    before it was elected."""
    state = slot._mc_computer
    stack = state["panel"].get_parent()
    # Clear "elected" state before touching the stack: set_visible_child()
    # fires notify::visible-child synchronously, and _on_slot_stack_child_changed
    # reasserts the panel whenever it still reads VIEW_DISKINFO here.
    state["visible_view"] = VIEW_FILES
    common.release_slot_view_owner(slot, "computer")
    state["filter_query"] = ""
    state["location_filter_owned"] = False
    if stack is not None and stack.get_visible_child() is state["panel"]:
        previous = state.get("previous_child")
        if previous is not None:
            stack.set_visible_child(previous)
    _unselect_all_cards(state)
    state["selected_mount_key"] = None
    state["selected_folder_key"] = None
    ext._stop_usage_poll_if_idle()


def init_data_watchers(ext) -> None:
    """Initial mount scan + live-watch wiring: /proc/mounts POLLPRI and
    VolumeMonitor signals, called once from MyComputerExtension.__init__."""
    _show_sys_parts = (
        ext._gsettings.get_boolean("show-system-partitions") if ext._gsettings else False
    )
    _refresh(_scan_mounts(_show_sys_parts) + _scan_gio_mounts() + _scan_gio_volumes())

    # Watch /proc/mounts at the kernel level — POLLPRI fires on any
    # mount/unmount regardless of how it happened (udisks, manual, FUSE…)
    try:
        ext._mounts_file = open("/proc/mounts", "r")
        GLib.io_add_watch(
            ext._mounts_file,
            GLib.PRIORITY_DEFAULT,
            GLib.IOCondition.ERR | GLib.IOCondition.PRI,
            lambda *a, ext=ext: _on_proc_mounts_changed(ext, *a),
        )
    except OSError:
        ext._mounts_file = None

    # VolumeMonitor signals — catch drive plug/unplug and GVfs events
    ext._volume_monitor = Gio.VolumeMonitor.get()
    for sig in (
        "mount-added",
        "mount-removed",
        "volume-added",
        "volume-removed",
        "drive-connected",
        "drive-disconnected",
        "drive-changed",
    ):
        ext._volume_monitor.connect(sig, lambda *a, ext=ext: _on_disk_event(ext, *a))

    # Kick off async network:/// discovery immediately
    _refresh_network_places(on_done=lambda: _do_live_refresh(ext))

    # Icon theme can change live (GNOME Settings). Disk/folder cards decide
    # gicon vs icon_name vs "folder" fallback at build time in widgets.py, so a
    # repopulate is enough to re-resolve everyone against the new theme --
    # without this watcher, cards keep whatever they rendered at last build
    # until the panel is torn down and rebuilt (e.g. Nautilus restart).
    icon_theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
    icon_theme.connect("changed", lambda *_a, ext=ext: GLib.idle_add(ext._repopulate_visible))


def _apply_bar_color(ext) -> None:
    if not ext._gsettings or ext._bar_css_display is None:
        return
    mode = ext._gsettings.get_string("color-mode")
    if mode == "flat":
        color = ext._gsettings.get_string("custom-color")
        css = f".diskinfo-bar block.filled {{ background: {color}; }}".encode()
    elif mode == "gradient":
        c1 = ext._gsettings.get_string("custom-gradient-color-1")
        c2 = ext._gsettings.get_string("custom-gradient-color-2")
        # Use CSS :dir() so GTK resolves direction per-widget at render time.
        # Gradient spans the filled area directly — no background-size trickery,
        # which is unreliable on older GTK4 (e.g. Ubuntu 22.04 / GTK 4.6.x).
        css = (
            f".diskinfo-bar:dir(ltr) block.filled {{"
            f" background: linear-gradient(to right, {c1} 20%, {c2} 100%); }}"
            f".diskinfo-bar:dir(rtl) block.filled {{"
            f" background: linear-gradient(to left, {c1} 20%, {c2} 100%); }}"
        ).encode()
    else:
        css = b".diskinfo-bar block.filled { background: @accent_bg_color; }"
    ext._bar_css_provider.load_from_data(css)
    Gtk.StyleContext.add_provider_for_display(
        ext._bar_css_display,
        ext._bar_css_provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1,
    )


def _read_sort_metadata(ext) -> bool:
    """Read sort order from GVfs metadata on computer:///.
    Returns True when the column or direction changed since last read."""
    try:
        f = Gio.File.new_for_uri(DISKS_URI)
        info = f.query_info(
            f"{METADATA_SORT_BY},{METADATA_SORT_REVERSED}",
            Gio.FileQueryInfoFlags.NONE,
            None,
        )
        col = info.get_attribute_string(METADATA_SORT_BY) or "name"
        rev_str = info.get_attribute_string(METADATA_SORT_REVERSED) or "false"
        rev = rev_str == "true"
        if col != ext._sort_column or rev != ext._sort_reverse:
            ext._sort_column = col
            ext._sort_reverse = rev
            return True
    except Exception:
        pass
    return False


def _attach_sort_button_watch(ext, nautilus_win: Gtk.Window) -> None:
    """Watch the sort GtkMenuButton's active state — arm poll when the sort
    popover opens, disarm (with one final read) when it closes."""
    state = ext._windows.get(nautilus_win)
    if not state or state.get("header_motion"):
        return
    btn = _find_sort_button(ext, nautilus_win)
    if btn is None:
        _log("sort button not found in toolbar")
        return
    btn.connect("notify::active", functools.partial(_on_sort_button_active, ext), nautilus_win)
    state["header_motion"] = btn  # reuse slot — just marks "already attached"
    _log(f"sort button watch attached ({type(btn).__name__})")


def _find_sort_button(ext, nautilus_win: Gtk.Window):
    """Find the GtkMenuButton inside NautilusViewControls (the sort/view popover button)."""
    # NautilusViewControls has no real buildable_id (auto-generated) and no css class.
    # Tier 2 (class name) is the primary match; tier 4 structural is the fallback.
    view_controls = _find_widget(
        nautilus_win,
        class_name="NautilusViewControls",
        site="_find_sort_button",
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
                    _log("_find_sort_button: matched via structural nav (NautilusViewControls)")
                    return w
    return None


def _on_sort_button_active(ext, btn: Gtk.MenuButton, _param, nautilus_win: Gtk.Window) -> None:
    state = ext._active_panel_state(nautilus_win)
    if not state or state.get("visible_view") != VIEW_DISKINFO:
        return
    if btn.get_active():
        ext._sort_hover = True
        if ext._sort_poll_id is None:
            _log("sort menu opened → sort poll armed")
            ext._sort_poll_id = GLib.timeout_add(_SORT_POLL_MS, functools.partial(_poll_sort, ext))
    else:
        ext._sort_hover = False
        _log("sort menu closed → sort poll disarming")


def _poll_sort(ext) -> bool:
    if ext._read_sort_metadata():
        _log(f"sort changed → col='{ext._sort_column}' rev={ext._sort_reverse}")
        ext._repopulate_visible()
        _log(f"sort applied → col='{ext._sort_column}' rev={ext._sort_reverse}")
    if not ext._sort_hover:
        # Menu closed — one final read already done above, now disarm.
        _log("sort poll disarmed")
        ext._sort_poll_id = None
        return GLib.SOURCE_REMOVE
    return GLib.SOURCE_CONTINUE


def apply_card_filter(ext, win: Gtk.Window, query: str) -> None:
    """Forward `query` to every section's own filter (see
    MyComputerCardSection.set_query in widgets.py -- each group filters its
    own cards and self-hides when empty). Stored on state so _populate()
    re-applies it after a live refresh or a navigate-away-and-back."""
    state = ext._active_panel_state(win)
    if not state:
        return
    state["filter_query"] = query
    for flow in state.get("section_flows", []):
        section = flow.get_parent()
        if section is not None and hasattr(section, "set_query"):
            section.set_query(query)


def _read_view_mode(ext) -> None:
    """Read current view mode and click policy from Nautilus preferences."""
    try:
        settings = Gio.Settings.new("org.gnome.nautilus.preferences")
        ext._view_mode = settings.get_string("default-folder-viewer")
        ext._click_policy = settings.get_string("click-policy")
    except Exception:
        pass


def _watch_view_mode(ext) -> None:
    """Subscribe to GSettings so view-mode/click-policy changes are instant."""
    try:
        settings = Gio.Settings.new("org.gnome.nautilus.preferences")
        settings.connect(
            "changed::default-folder-viewer", functools.partial(_on_view_mode_changed, ext)
        )
        settings.connect("changed::click-policy", functools.partial(_on_click_policy_changed, ext))
        ext._view_mode_gsettings = settings  # keep reference
    except Exception:
        pass


def _on_view_mode_changed(ext, settings: Gio.Settings, _key: str) -> None:
    prev = ext._view_mode
    ext._view_mode = settings.get_string("default-folder-viewer")
    if ext._view_mode != prev:
        _log(f"view changed → mode='{ext._view_mode}'")
        ext._repopulate_visible()


def _on_click_policy_changed(ext, settings: Gio.Settings, _key: str) -> None:
    prev = ext._click_policy
    ext._click_policy = settings.get_string("click-policy")
    if ext._click_policy != prev:
        _log(f"click-policy changed → '{ext._click_policy}'")
        ext._repopulate_visible()


def _on_disk_event(ext, _monitor, *_args) -> None:
    """VolumeMonitor signal handler — debounced."""
    ext._schedule_live_refresh()


def _on_proc_mounts_changed(ext, _source, _condition) -> bool:
    """/proc/mounts POLLPRI handler — any kernel mount change."""
    ext._schedule_live_refresh()
    return GLib.SOURCE_CONTINUE  # keep watching


def _schedule_live_refresh(ext) -> None:
    """Coalesce rapid events (plug → volume-added → mount-added) into one update."""
    if ext._refresh_pending:
        return
    ext._refresh_pending = True
    GLib.timeout_add(_REFRESH_DEBOUNCE_MS, functools.partial(_do_live_refresh, ext))


def _do_live_refresh(ext) -> bool:
    ext._refresh_pending = False
    _show_sys_parts = (
        ext._gsettings.get_boolean("show-system-partitions") if ext._gsettings else False
    )
    _refresh(_scan_mounts(_show_sys_parts) + _scan_gio_mounts() + _scan_gio_volumes())
    # Re-discover network places in background; callback will repopulate
    _refresh_network_places(on_done=ext._repopulate_visible)
    ext._repopulate_visible()
    return GLib.SOURCE_REMOVE


def _sweep_local_usage(ext) -> None:
    """Worker-thread only: statvfs every local mount, queue changed usage to
    the main thread. Pure-read — never writes _disk_data here (that happens on
    the main thread in _apply_usage_updates via dataclasses.replace)."""
    updates: dict[str, tuple[int, int]] = {}
    for key, m in list(_disk_data.items()):
        if m.is_gio or not m.is_mounted or not m.mountpoint:
            continue
        usage = _root_usage() if m.mountpoint == "/" else _statvfs_usage(m.mountpoint)
        if usage is None:
            continue
        total, free = usage
        if free != m.free or total != m.total:
            updates[key] = (total, free)
    if updates:
        GLib.idle_add(
            functools.partial(_apply_usage_updates, ext), updates, priority=GLib.PRIORITY_DEFAULT
        )


def _local_usage_worker(ext, stop_event: threading.Event) -> None:
    """Background thread: refresh local-mount usage, adapting cadence to write
    activity and gating on disk-busy.

    statvfs blocks for *seconds* and contends with in-flight file operations
    under ext4 journal load (confirmed: polling statvfs during a copy/delete
    made those operations sluggish while the panel was visible). So normally we
    check /proc/diskstats first (cheap, no contention): if the disk has I/O in
    flight we skip the sweep — no statvfs, no contention.

    Two refinements make the panel feel live without breaking that gate:
      • An immediate ungated sweep on entry, so arriving at the panel (e.g.
        navigating back after a copy) shows fresh numbers at once instead of
        the stale cache _populate() rendered.
      • A /proc/meminfo Dirty+Writeback forward signal (cadence only, never
        used to estimate free space): poll fast while writes are buffered, and
        force one definitive sweep the instant dirty pages drain — the flush,
        i.e. exactly when statvfs finally changes — even if the busy-gate would
        otherwise skip it.

    Self-disarms when the panel is hidden (stop_event)."""
    prev_ticks, _ = _read_io_busy()
    prev_t = time.monotonic()
    was_active = _read_dirty_bytes() >= _DIRTY_ACTIVE_THRESHOLD
    while True:
        interval = _USAGE_POLL_FAST_MS if was_active else _USAGE_GATE_MS
        if stop_event.wait(interval / 1000.0):
            break

        now = time.monotonic()
        ticks, inflight = _read_io_busy()
        busy_ms = ticks - prev_ticks
        elapsed_ms = (now - prev_t) * 1000
        prev_ticks, prev_t = ticks, now

        is_active = _read_dirty_bytes() >= _DIRTY_ACTIVE_THRESHOLD
        just_flushed = was_active and not is_active  # buffered writes hit disk
        was_active = is_active

        # Skip while the disk is busy — except right after a flush, when the
        # post-flush value is exactly what we need and must not be missed.
        if not just_flushed and (inflight > 0 or busy_ms > _USAGE_BUSY_RATIO * elapsed_ms):
            continue

        _sweep_local_usage(ext)


def _net_usage_tick(ext) -> bool:
    """GLib timer callback: fire async D-Bus usage queries for all GVfs/network mounts."""
    attrs = f"{Gio.FILE_ATTRIBUTE_FILESYSTEM_SIZE},{Gio.FILE_ATTRIBUTE_FILESYSTEM_FREE}"
    for key, m in list(_disk_data.items()):
        if not m.is_gio:
            continue
        Gio.File.new_for_uri(m.nav_uri).query_filesystem_info_async(
            attrs,
            GLib.PRIORITY_DEFAULT,
            ext._net_poll_cancellable,
            functools.partial(_on_net_info_ready, ext),
            key,
        )
    return GLib.SOURCE_CONTINUE


def _on_net_info_ready(ext, gfile: Gio.File, result: Gio.AsyncResult, key: str) -> None:
    """Async callback (main thread): apply network mount usage update."""
    try:
        info = gfile.query_filesystem_info_finish(result)
    except GLib.Error as e:
        if not e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
            _log(f"net usage query failed: {e.message}")
        return
    total = info.get_attribute_uint64(Gio.FILE_ATTRIBUTE_FILESYSTEM_SIZE)
    free = info.get_attribute_uint64(Gio.FILE_ATTRIBUTE_FILESYSTEM_FREE)
    if total <= 0 or key not in _disk_data:
        return
    m = _disk_data[key]
    if total != m.total or free != m.free:
        _apply_usage_updates(ext, {key: (total, free)})


def _apply_usage_updates(ext, updates: dict) -> bool:
    """Main-thread callback: patch _disk_data and update card widgets in place."""
    global _disk_data
    for key, (total, free) in updates.items():
        if key not in _disk_data:
            continue
        _disk_data[key] = dataclasses.replace(_disk_data[key], total=total, free=free)
        for state in ext._iter_panel_states():
            if state.get("visible_view") != VIEW_DISKINFO:
                continue
            _update_card_usage(ext, state, key, total, free)
    return GLib.SOURCE_REMOVE


def _update_card_usage(ext, state: dict, key: str, total: int, free: int) -> None:
    """Patch a disk card's LevelBar/sub-label in place via the O(1) card_widgets registry."""
    card = state.get("card_widgets", {}).get(key)
    if card is not None:
        card.update_usage(_disk_data[key])


def _sweep_folder_icons(ext) -> None:
    """Re-query display-name/icon/caption metadata for every rendered Preferred
    Folder. Renames and deletes reach us live via the file monitors in
    _sync_folder_rename_watchers, but a custom-icon-only change (Nautilus'
    "Properties > Icon") never fires a file-monitor event at all -- gvfs metadata
    is an mmap-backed store, not inotify-visible (confirmed empirically; same
    root cause as the sort-metadata polling need). There is no change signal we
    can subscribe to either (org.gtk.vfs.Metadata.AttributeChanged does not fire
    for these writes, confirmed empirically -- issue #78). Firing this cheap
    async query on window focus-in, scoped to "panel visible", catches the
    common case (user edits the icon via Properties, then returns to the
    Nautilus window) without an always-on timer (issue #71, #78)."""
    for pf in list(_folder_data.values()):
        if pf.key not in preferred_folders.PREFERRED_TOKENS:
            _refresh_folder_metadata_async(ext, pf)
        elif pf.is_special_place and not preferred_folders.PREFERRED_TOKENS[pf.key].get("gio_icon"):
            _refresh_special_place_icon_async(ext, pf)
        else:
            _refresh_folder_icon_async(ext, pf)
        _refresh_folder_captions_async(ext, pf)


def _on_window_active_changed(ext, win: Gtk.Window) -> None:
    """notify::is-active handler: sweep folder icons/captions when a window
    showing the disk panel regains focus (issue #78 -- see _sweep_folder_icons
    for why this replaces a poll instead of a change signal)."""
    state = ext._active_panel_state(win)
    if not state or not win.get_property("is-active"):
        return
    if state.get("visible_view") != VIEW_DISKINFO:
        return
    _sweep_folder_icons(ext)


def _ensure_usage_poll_running(ext) -> None:
    """Arm both usage poll workers if not already running."""
    if ext._local_poll_stop is None:
        ev = threading.Event()
        ext._local_poll_stop = ev
        threading.Thread(
            target=functools.partial(_local_usage_worker, ext), args=(ev,), daemon=True
        ).start()
    if ext._net_poll_timer_id is None:
        ext._net_poll_cancellable = Gio.Cancellable()
        _net_usage_tick(ext)
        ext._net_poll_timer_id = GLib.timeout_add(
            _USAGE_POLL_NETWORK_MS, functools.partial(_net_usage_tick, ext)
        )


def _stop_usage_poll_if_idle(ext) -> None:
    """Disarm poll workers when no slot anywhere has the panel elected."""
    any_visible = any(st.get("visible_view") == VIEW_DISKINFO for st in ext._iter_panel_states())
    if not any_visible:
        if ext._local_poll_stop is not None:
            ext._local_poll_stop.set()
            ext._local_poll_stop = None
        if ext._net_poll_timer_id is not None:
            GLib.source_remove(ext._net_poll_timer_id)
            ext._net_poll_timer_id = None
        if ext._net_poll_cancellable is not None:
            ext._net_poll_cancellable.cancel()
            ext._net_poll_cancellable = None


def _new_grid_box(ext) -> Gtk.Box:
    grid_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    grid_box.set_hexpand(True)
    grid_box.set_valign(Gtk.Align.START)
    grid_box.set_margin_start(18)
    grid_box.set_margin_end(18)
    grid_box.set_margin_top(18)
    grid_box.set_margin_bottom(18)
    return grid_box


def _release_stale_generations(ext, state: dict) -> bool:
    state.get("stale_generations", []).clear()
    state["stale_release_tick_id"] = None
    state["stale_release_ticks"] = 0
    return GLib.SOURCE_REMOVE


def _queue_stale_generation_release(ext, state: dict, root: Gtk.Widget) -> None:
    stale = state.setdefault("stale_generations", [])
    stale.append(root)
    state["stale_release_ticks"] = _STALE_RELEASE_FRAMES
    if state.get("stale_release_tick_id") is not None:
        return

    owner = state.get("panel")
    if owner is None or not hasattr(owner, "add_tick_callback"):
        GLib.timeout_add(50, lambda st=state, ext=ext: _release_stale_generations(ext, st))
        return

    def _release_on_tick(_widget, _frame_clock, st=state):
        ticks_left = max(0, st.get("stale_release_ticks", 0) - 1)
        st["stale_release_ticks"] = ticks_left
        if ticks_left > 0:
            return GLib.SOURCE_CONTINUE
        return _release_stale_generations(ext, st)

    state["stale_release_tick_id"] = owner.add_tick_callback(_release_on_tick)


def _build_panel(ext, win: Gtk.Window) -> tuple:
    panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    panel.set_hexpand(True)
    panel.set_vexpand(True)
    panel.get_style_context().add_class("diskinfo-panel")
    panel.add_css_class("nautilus-grid-view")

    scroll = Gtk.ScrolledWindow()
    scroll.set_vexpand(True)
    scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    # A Gtk.FlowBox in SINGLE mode selects whatever child takes focus, so if a
    # card were the panel's first focusable widget, showing the panel would
    # select it on its own. Keeping the scroller focusable puts it ahead of
    # the cards in focus order, the same way Nautilus's own view holds initial
    # focus without selecting anything.
    scroll.set_focusable(True)

    grid_box = _new_grid_box(ext)

    scroll.set_child(grid_box)
    panel.append(scroll)

    bg_deselect = Gtk.GestureClick()
    bg_deselect.set_button(0)
    bg_deselect.connect("pressed", functools.partial(_on_panel_clicked, ext), win)
    scroll.add_controller(bg_deselect)

    # Ctrl+scroll zoom passthrough. Native Nautilus wires this on its own
    # NautilusListBase, which is the hidden "files" child while our panel shows,
    # so the gesture never reaches us and the ScrolledWindow just pages up/down.
    # Forward to Nautilus's real "view.zoom-in"/"view.zoom-out" actions (they
    # live on the window, an ancestor of this panel) instead of reimplementing
    # the zoom stepping. Ctrl+= / Ctrl+- already work via the window accelerator.
    zoom_scroll = Gtk.EventControllerScroll()
    # DISCRETE makes GTK accumulate smooth (touchpad) deltas internally and emit
    # one unit step per notch, so we get native "one zoom step per notch" feel
    # without a manual accumulator/reset timer.
    zoom_scroll.set_flags(
        Gtk.EventControllerScrollFlags.VERTICAL | Gtk.EventControllerScrollFlags.DISCRETE
    )
    zoom_scroll.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
    zoom_scroll.connect("scroll", _on_ctrl_scroll_zoom, panel)
    scroll.add_controller(zoom_scroll)

    return panel, scroll, grid_box


def _on_ctrl_scroll_zoom(
    controller: Gtk.EventControllerScroll, _dx: float, dy: float, panel: Gtk.Widget
) -> bool:
    """Ctrl+scroll -> step Nautilus's own zoom action; plain scroll passes through."""
    state = controller.get_current_event_state()
    if not (state & Gdk.ModifierType.CONTROL_MASK) or dy == 0:
        return Gdk.EVENT_PROPAGATE
    panel.activate_action("view.zoom-in" if dy < 0 else "view.zoom-out", None)
    return Gdk.EVENT_STOP


def _populate(ext, win: Gtk.Window) -> None:
    """Populate whichever panel the window's active slot owns. Most call
    sites only ever care about "the panel currently in front of the user in
    this window" -- direct per-slot population (background tabs, the
    enter/leave machinery above) goes through _populate_slot instead."""
    slot = ext._active_slot_widget(win)
    if slot is not None:
        _populate_slot(ext, slot)


def _populate_slot(ext, slot) -> None:
    state = getattr(slot, "_mc_computer", None)
    if state is None:
        return
    win = state["window"]

    grid_box = _new_grid_box(ext)
    section_flows: list[Gtk.FlowBox] = []
    card_widgets = {}
    folder_card_widgets = {}

    col = ext._sort_column
    rev = ext._sort_reverse

    def _sort_key(m: MountInfo):
        if col == "size":
            return m.total
        # Hidden bucket mirrors Column View's confirmed-against-Nautilus name
        # sort (widgets.py's _SORT_KEY_BUILDERS["name"]): normal items sorted
        # alpha-num first, then hidden items sorted alpha-num, as one flat
        # 2-bucket ordering that flips whole under reverse=True too.
        return (m.is_hidden, (m.display_name or "").lower())

    # Build PanelGroup objects, reading visibility state from gsettings
    groups: dict[str, PanelGroup] = {}
    for gkey, glabel, gskey in _GROUP_SPEC:
        # "Network" matches gvfs's own wording; the rest (including "System",
        # our own disk-group meaning, not GTK's generic setting label) are ours.
        label = _native(glabel) if gkey == "network" else _(glabel)
        if gskey is None:
            # "On this Computer" is the merge target -- always visible, never merged
            groups[gkey] = PanelGroup(key=gkey, label=label, visible=True, merged=False)
            continue
        vis_str = ext._gsettings.get_string(gskey) if ext._gsettings else "visible"
        visible = vis_str != "hidden"
        merged = vis_str == "merged"
        groups[gkey] = PanelGroup(key=gkey, label=label, visible=visible, merged=merged)

    # Classify each mount into its group
    for m in _disk_data.values():
        groups[_classify_mount(m)].add_item(m)

    active_uris = {m.nav_uri for m in _disk_data.values()}
    for place in _network_places:
        if place.nav_uri not in active_uris:
            groups["network"].add_item(place)

    # Sort each group's items
    for gkey, group in groups.items():
        if gkey in ("system", "local"):
            if col == "type":
                group.sort_items(key_func=_get_local_mount_tier, reverse=False)
            else:
                mounted = [m for m in group.items if m.is_mounted]
                unmounted = [m for m in group.items if not m.is_mounted]
                mounted.sort(key=_sort_key, reverse=rev)
                unmounted.sort(key=_sort_key, reverse=rev)
                group.items = mounted + unmounted
        elif gkey == "removable":
            mounted = [m for m in group.items if m.is_mounted]
            unmounted = [m for m in group.items if not m.is_mounted]
            mounted.sort(key=_sort_key, reverse=rev)
            unmounted.sort(key=_sort_key, reverse=rev)
            group.items = mounted + unmounted
        else:
            group.sort_items(key_func=_sort_key, reverse=rev)

    # Merge pass: fold items from merged groups into "local", preserving origin key
    # Each entry in local_extra is (MountInfo, origin_group_key)
    local_extra: list[tuple] = []
    # Fixed group-level order for sort-by-type within the merged "On this Computer" group:
    # system=0, local=1, removable=2, disc=3, network=4
    _merge_type_order = {"system": 0, "local": 1, "removable": 2, "disc": 3, "network": 4}
    for gkey, _gl, _gs in _GROUP_SPEC:
        group = groups[gkey]
        if gkey != "local" and group.merged:
            for m in group.items:
                local_extra.append((m, gkey))

    # Preferred Folders group (issue #30): rendered above the disk groups
    show_folders = (
        ext._gsettings.get_string("visibility-preferred-folders") != "hidden"
        if ext._gsettings
        else True
    )
    if show_folders:
        folders = preferred_folders.load_preferred_folders(ext._gsettings)
        _folder_data.clear()
        _folder_data.update({pf.key: pf for pf in folders})
        # Drop caption data for folders no longer present (removed/renamed) so
        # a stale entry can't linger under a reused key.
        live_keys = set(_folder_data.keys())
        for stale_key in list(_folder_caption_data.keys()):
            if stale_key not in live_keys:
                del _folder_caption_data[stale_key]
        for pf in folders:
            if pf.key not in preferred_folders.PREFERRED_TOKENS:
                _refresh_folder_metadata_async(ext, pf)
            elif pf.is_special_place and not preferred_folders.PREFERRED_TOKENS[pf.key].get(
                "gio_icon"
            ):
                _refresh_special_place_icon_async(ext, pf)
            else:
                _refresh_folder_icon_async(ext, pf)
            _refresh_folder_captions_async(ext, pf)
        _sync_folder_rename_watchers(ext, folders)
        if folders:
            section = MyComputerCardSection(
                ext,
                win,
                _("Preferred Folders"),
                ext._view_mode,
                max_cols=_FOLDER_FLOW_COLS_GRID,
                col_spacing=_FOLDER_CARD_SPACING,
                row_spacing=_FOLDER_CARD_ROW_SPACING,
                always_grid=True,
            )
            section_flows.append(section.flow)
            for pf in folders:
                card = MyComputerFolderCard(ext, win, ext._view_mode, pf)
                section.add_card(card)
                folder_card_widgets[pf.key] = card
            grid_box.append(section)
    else:
        _folder_data.clear()
        _folder_caption_data.clear()
        _sync_folder_rename_watchers(ext, [])

    for gkey, _glabel, _gskey in _GROUP_SPEC:
        group = groups[gkey]
        # "local" is the merge target: render it whenever it has its own items
        # OR has received merged items, even if the group itself is set to hidden.
        if gkey == "local":
            if not group.visible and not local_extra:
                continue
        elif not group.visible or group.merged:
            continue

        # For "local", append any merged items (with their origin keys).
        # If local itself is hidden, only the merged extras show.
        render_items: list[tuple]  # (MountInfo, icon_group_key)
        if gkey == "local":
            own = [(m, "local") for m in group.items] if group.visible else []
            render_items = own + local_extra
            if col == "type" and local_extra:
                # Sort the combined list by group-level tier, then intra-group tier
                def _merged_type_key(entry, _order=_merge_type_order):
                    m, origin = entry
                    group_tier = _order.get(origin, 5)
                    if origin in ("system", "local"):
                        sub = _get_local_mount_tier(m)
                    else:
                        sub = (
                            0 if m.is_mounted else 1,
                            m.is_hidden,
                            (m.display_name or "").lower(),
                        )
                    return (group_tier,) + sub

                render_items.sort(key=_merged_type_key)
        else:
            render_items = [(m, gkey) for m in group.items]

        if not render_items:
            continue

        section = MyComputerCardSection(
            ext,
            win,
            group.label,
            ext._view_mode,
            max_cols=_FLOW_COLS_GRID,
            col_spacing=_DISK_CARD_SPACING,
            row_spacing=_DISK_CARD_ROW_SPACING,
            homogeneous=True,
            max_card_width=_CARD_WIDTH,
        )
        section_flows.append(section.flow)

        for m, origin_key in render_items:
            card = MyComputerDiskCard(ext, win, ext._view_mode, m, origin_key)
            section.add_card(card)
            card_widgets[m.key] = card

        grid_box.append(section)

    old_grid_box = state.get("grid_box")
    state["grid_box"] = grid_box
    state["section_flows"] = section_flows
    state["card_widgets"] = card_widgets
    state["folder_card_widgets"] = folder_card_widgets
    # Sections are rebuilt from scratch every populate, so a filter active
    # before a live-refresh (or a navigate-away-and-back) must be re-applied
    # to the freshly-built section widgets here. Unconditional: even with no
    # search query, sections must re-evaluate visibility (a hidden disk may
    # be a group's only card, and Show Hidden Files may have changed).
    apply_card_filter(ext, win, state.get("filter_query") or "")
    # Render any already-cached caption data immediately (e.g. re-populate
    # after a live-refresh) rather than waiting for the next async fetch.
    for folder_key in folder_card_widgets:
        _show_folder_captions(ext, folder_key)
    state["grid_host"].set_child(grid_box)
    if old_grid_box is not None:
        _queue_stale_generation_release(ext, state, old_grid_box)

    # Restore the previously selected card, or explicitly clear all selections.
    # Needed on every populate (first show AND live refresh): FlowBox with SINGLE
    # selection mode can auto-select a child when the widget gains keyboard focus,
    # so we must be explicit here rather than relying on the widget's default state.
    _unselect_all_cards(state)
    sel_mount = state.get("selected_mount_key")
    sel_folder = state.get("selected_folder_key")
    if sel_mount and sel_mount in card_widgets:
        wrapper = card_widgets[sel_mount].get_parent()
        if isinstance(wrapper, Gtk.FlowBoxChild):
            wrapper.get_parent().select_child(wrapper)
    elif sel_folder and sel_folder in folder_card_widgets:
        wrapper = folder_card_widgets[sel_folder].get_parent()
        if isinstance(wrapper, Gtk.FlowBoxChild):
            wrapper.get_parent().select_child(wrapper)
    else:
        state["selected_mount_key"] = None
        state["selected_folder_key"] = None

    ext._apply_bar_color()


def _refresh_folder_metadata_async(ext, pf: "PreferredFolder") -> None:
    """Resolve real display-name/icon for a raw-URI preferred folder without blocking,
    then patch any rendered cards in place via the folder_card_widgets registry."""
    gfile = Gio.File.new_for_uri(pf.nav_uri)
    gfile.query_info_async(
        "standard::display-name,standard::icon,standard::is-hidden,"
        "metadata::custom-icon,metadata::custom-icon-name",
        Gio.FileQueryInfoFlags.NONE,
        GLib.PRIORITY_DEFAULT,
        ext._folder_refresh_cancellable,
        functools.partial(_on_folder_metadata_ready, ext),
        pf.key,
    )


def _on_folder_metadata_ready(
    ext, gfile: Gio.File, result: Gio.AsyncResult, folder_key: str
) -> None:
    try:
        info = gfile.query_info_finish(result)
    except GLib.Error:
        return
    pf = _folder_data.get(folder_key)
    if pf is None:
        return
    display_name = info.get_display_name() or pf.display_name
    gio_icon = _resolve_custom_gicon(info) or info.get_icon()
    is_hidden = info.get_attribute_boolean("standard::is-hidden")
    new_pf = dataclasses.replace(
        pf, display_name=display_name, gio_icon=gio_icon, is_hidden=is_hidden
    )
    _folder_data[folder_key] = new_pf
    for state in ext._iter_panel_states():
        card = state.get("folder_card_widgets", {}).get(folder_key)
        if card is not None:
            card.update_metadata(new_pf)


def _refresh_special_place_icon_async(ext, pf: "PreferredFolder") -> None:
    """Custom-icon-only refresh for the Recent/Starred/Network virtual tokens
    (issue #83). Unlike _refresh_folder_icon_async, this never touches
    display_name or falls back to standard::icon -- these locations have no
    real filesystem content-type icon to defer to, only the fixed token icon
    in PREFERRED_TOKENS (icon_name) and, layered on top of it, whatever the
    user set via Properties > Icon (gio_icon). metadata::custom-icon(-name)
    is keyed by URI, so it works for recent:/// exactly like a real folder."""
    gfile = Gio.File.new_for_uri(pf.nav_uri)
    gfile.query_info_async(
        "metadata::custom-icon,metadata::custom-icon-name",
        Gio.FileQueryInfoFlags.NONE,
        GLib.PRIORITY_DEFAULT,
        ext._folder_refresh_cancellable,
        functools.partial(_on_special_place_icon_ready, ext),
        pf.key,
    )


def _on_special_place_icon_ready(
    ext, gfile: Gio.File, result: Gio.AsyncResult, folder_key: str
) -> None:
    try:
        info = gfile.query_info_finish(result)
    except GLib.Error:
        return
    pf = _folder_data.get(folder_key)
    if pf is None:
        return
    gio_icon = _resolve_custom_gicon(info)
    new_pf = dataclasses.replace(pf, gio_icon=gio_icon)
    _folder_data[folder_key] = new_pf
    for state in ext._iter_panel_states():
        card = state.get("folder_card_widgets", {}).get(folder_key)
        if card is not None:
            card.update_metadata(new_pf)


def _refresh_folder_icon_async(ext, pf: "PreferredFolder") -> None:
    """Icon/name refresh for built-in real-folder tokens (Documents/Downloads/
    Music/Videos/Pictures/Home). GIO's standard::icon already resolves the
    correct native icon for these paths (folder-download, user-home, ...) --
    no hardcoded icon table needed -- and metadata::custom-icon(-name) layers
    a user-set custom icon on top, exactly like Nautilus itself does.
    standard::display-name is queried too (issue #64): the real folder name
    comes from xdg-user-dirs at creation time and can diverge from our own
    gettext label (renamed by the user, or created under a different locale),
    so the actual filesystem name must win -- our label is only the initial
    placeholder in PREFERRED_TOKENS until this query resolves."""
    gfile = Gio.File.new_for_uri(pf.nav_uri)
    gfile.query_info_async(
        "standard::display-name,standard::icon,metadata::custom-icon,metadata::custom-icon-name",
        Gio.FileQueryInfoFlags.NONE,
        GLib.PRIORITY_DEFAULT,
        ext._folder_refresh_cancellable,
        functools.partial(_on_folder_icon_ready, ext),
        pf.key,
    )


def _on_folder_icon_ready(ext, gfile: Gio.File, result: Gio.AsyncResult, folder_key: str) -> None:
    try:
        info = gfile.query_info_finish(result)
    except GLib.Error:
        return
    pf = _folder_data.get(folder_key)
    if pf is None:
        return
    # "home" has no xdg-user-dirs name to defer to -- its real basename is just
    # the username, which GIO happily reports but Nautilus itself never shows
    # (nautilus-file-utilities.c / nautilus-bookmark.c always substitute their
    # own translated "Home" instead). Keep our _native("Home") label there;
    # only the named special-dir tokens (Documents/Downloads/...) defer to GIO.
    display_name = (
        pf.display_name if folder_key == "home" else (info.get_display_name() or pf.display_name)
    )
    gio_icon = _resolve_custom_gicon(info) or info.get_icon()
    new_pf = dataclasses.replace(pf, display_name=display_name, gio_icon=gio_icon)
    _folder_data[folder_key] = new_pf
    for state in ext._iter_panel_states():
        card = state.get("folder_card_widgets", {}).get(folder_key)
        if card is not None:
            card.update_metadata(new_pf)


# ── Preferred Folders captions (issue #72) ──────────────────────────────────

# Nautilus's "captions" tokens that need a query_info() attribute, mapped to
# the attribute string to request. "size" (item count) and "where" (parent
# path) are handled separately below -- size needs enumerate_children, and
# where is derived from the URI itself with no I/O at all.
_CAPTION_TOKEN_ATTRS: dict[str, str] = {
    "type": "standard::content-type",
    "detailed_type": "standard::content-type",
    "mime_type": "standard::content-type",
    "date_modified": "time::modified",
    "date_accessed": "time::access",
    "date_created": "time::created",
    "recency": "time::access",
    "owner": "owner::user",
    "group": "owner::group",
    "permissions": "unix::mode",
}


def _refresh_folder_captions_async(ext, pf: "PreferredFolder") -> None:
    """Resolve whichever caption attributes the 3 active tokens need, then
    patch any rendered card in place via _show_folder_captions. Virtual
    places (recent:///, starred:///, x-network-view:///) have no real file to
    query -- Nautilus itself shows no captions for them either."""
    if pf.is_special_place or not ext._gsettings.get_boolean("show-preferred-folder-captions"):
        return
    tokens = ext._nautilus_prefs.captions()
    attrs = {_CAPTION_TOKEN_ATTRS[t] for t in tokens if t in _CAPTION_TOKEN_ATTRS}
    gfile = Gio.File.new_for_uri(pf.nav_uri)
    if attrs:
        gfile.query_info_async(
            ",".join(attrs),
            Gio.FileQueryInfoFlags.NONE,
            GLib.PRIORITY_DEFAULT,
            ext._folder_refresh_cancellable,
            functools.partial(_on_folder_caption_info_ready, ext),
            pf.key,
        )
    if "size" in tokens:
        _count_folder_children_async(ext, gfile, pf.key)


def _on_folder_caption_info_ready(
    ext, gfile: Gio.File, result: Gio.AsyncResult, folder_key: str
) -> None:
    try:
        info = gfile.query_info_finish(result)
    except GLib.Error:
        return
    data = _folder_caption_data.setdefault(folder_key, {})
    if info.has_attribute("standard::content-type"):
        data["content_type"] = info.get_content_type()
    if info.has_attribute("time::modified"):
        data["mtime"] = info.get_attribute_uint64("time::modified")
    if info.has_attribute("time::access"):
        data["atime"] = info.get_attribute_uint64("time::access")
    if info.has_attribute("time::created"):
        data["ctime"] = info.get_attribute_uint64("time::created")
    if info.has_attribute("owner::user"):
        data["owner"] = info.get_attribute_string("owner::user")
    if info.has_attribute("owner::group"):
        data["group"] = info.get_attribute_string("owner::group")
    if info.has_attribute("unix::mode"):
        data["mode"] = info.get_attribute_uint32("unix::mode")
    _show_folder_captions(ext, folder_key)


def _count_folder_children_async(ext, gfile: Gio.File, folder_key: str) -> None:
    gfile.enumerate_children_async(
        "standard::name",
        Gio.FileQueryInfoFlags.NONE,
        GLib.PRIORITY_DEFAULT,
        ext._folder_refresh_cancellable,
        functools.partial(_on_folder_children_enumerated, ext, folder_key, 0),
    )


def _on_folder_children_enumerated(
    ext, folder_key: str, running_count: int, gfile: Gio.File, result: Gio.AsyncResult
) -> None:
    try:
        enumerator = gfile.enumerate_children_finish(result)
    except GLib.Error:
        return
    _drain_folder_children(ext, enumerator, folder_key, running_count)


def _drain_folder_children(
    ext, enumerator: Gio.FileEnumerator, folder_key: str, running_count: int
) -> None:
    enumerator.next_files_async(
        200,
        GLib.PRIORITY_DEFAULT,
        ext._folder_refresh_cancellable,
        functools.partial(_on_folder_children_batch, ext, folder_key, running_count),
    )


def _on_folder_children_batch(
    ext,
    folder_key: str,
    running_count: int,
    enumerator: Gio.FileEnumerator,
    result: Gio.AsyncResult,
) -> None:
    try:
        infos = enumerator.next_files_finish(result)
    except GLib.Error:
        return
    running_count += len(infos)
    if infos:
        _drain_folder_children(ext, enumerator, folder_key, running_count)
        return
    enumerator.close_async(GLib.PRIORITY_DEFAULT, None, lambda *_a: None)
    _folder_caption_data.setdefault(folder_key, {})["item_count"] = running_count
    _show_folder_captions(ext, folder_key)


def _resolve_caption_line(token: str, pf: "PreferredFolder", data: dict) -> str | None:
    """One caption token's display string, or None if it's "none", not yet
    resolved, or (for "where") the folder has no meaningful parent."""
    if token == "none":
        return None
    if token == "where":
        parent = Gio.File.new_for_uri(pf.nav_uri).get_parent()
        return parent.get_parse_name() if parent is not None else None
    if token == "size":
        item_count = data.get("item_count")
        return _format_item_count(item_count) if item_count is not None else None
    if token in ("type", "detailed_type", "mime_type"):
        content_type = data.get("content_type")
        if content_type is None:
            return None
        if token == "mime_type":
            return content_type
        return Gio.content_type_get_description(content_type) or content_type
    if token in ("date_modified", "date_accessed", "date_created", "recency"):
        field = {
            "date_modified": "mtime",
            "date_accessed": "atime",
            "date_created": "ctime",
            "recency": "atime",
        }[token]
        unix_time = data.get(field)
        return _mc_date_to_str(unix_time) if unix_time else None
    if token == "owner":
        return data.get("owner")
    if token == "group":
        return data.get("group")
    if token == "permissions":
        mode = data.get("mode")
        return _format_permissions(mode) if mode is not None else None
    return None


def _show_folder_captions(ext, folder_key: str) -> None:
    """Recompute the 3 caption lines from cached data + the current GSettings
    tokens and patch any rendered card in place. Called both when fresh data
    arrives (async callbacks above) and when the tokens themselves change
    (main.py's _reapply_folder_captions)."""
    pf = _folder_data.get(folder_key)
    if pf is None:
        return
    show_captions = ext._gsettings.get_boolean("show-preferred-folder-captions")
    tokens = ext._nautilus_prefs.captions()
    data = _folder_caption_data.get(folder_key, {})
    lines = (
        [None, None, None]
        if pf.is_special_place or not show_captions
        else [_resolve_caption_line(tok, pf, data) for tok in tokens]
    )
    for state in ext._iter_panel_states():
        card = state.get("folder_card_widgets", {}).get(folder_key)
        if card is not None:
            card.set_captions(lines)


def _sync_folder_rename_watchers(ext, folders: list) -> None:
    """Arm a Gio.FileMonitor (WATCH_MOVES) on the parent directory of each URI
    preferred folder so a rename/move is caught live and the stored GSettings URI
    is corrected -- without this, a renamed folder keeps showing its old name
    forever (the stored URI no longer resolves, so the async metadata refresh just
    fails silently).

    Monitoring the folder itself only sees the "self" side of a move (a bare
    DELETED, no paired new path) -- the parent directory is the only vantage point
    that sees both the move-out and move-in and can pair them into RENAMED.

    Token-based folders (Home, Documents, ...) aren't watched: their URI is
    resolved fresh from GLib.get_user_special_dir() every load, so they can't go
    stale the same way.
    """
    live_folders = [pf for pf in folders if pf.key not in preferred_folders.PREFERRED_TOKENS]
    # Map the resolved URI reported by GIO back to the exact value stored in
    # GSettings.  They differ for portable file://~/… entries.
    ext._watched_folder_keys = {pf.nav_uri: pf.key for pf in live_folders}
    live_parents = set()
    for pf in live_folders:
        parent = Gio.File.new_for_uri(pf.nav_uri).get_parent()
        if parent is not None:
            live_parents.add(parent.get_uri())

    for parent_uri in list(ext._folder_monitors):
        if parent_uri not in live_parents:
            ext._folder_monitors.pop(parent_uri).cancel()
    for parent_uri in live_parents:
        if parent_uri in ext._folder_monitors:
            continue
        try:
            monitor = Gio.File.new_for_uri(parent_uri).monitor(
                Gio.FileMonitorFlags.WATCH_MOVES, None
            )
            monitor.connect("changed", functools.partial(_on_preferred_folder_file_changed, ext))
            ext._folder_monitors[parent_uri] = monitor
        except GLib.Error as e:
            _log(f"_sync_folder_rename_watchers: monitor failed for {parent_uri}: {e.message}")


def _on_preferred_folder_file_changed(
    ext,
    _monitor: Gio.FileMonitor,
    file: Gio.File,
    other_file: Gio.File | None,
    event_type: Gio.FileMonitorEvent,
) -> None:
    """React to the watched folder itself moving or disappearing so the
    Preferred Folders group updates live instead of only on next view entry.

    RENAMED (paired: other_file set) covers a move/rename within the same
    watched parent -- GIO gives us the real destination, so the stored entry
    is corrected in place.

    DELETED (permanent delete) and MOVED_OUT (unpaired: other_file is None --
    Nautilus' default "move to Trash", or a move to any directory we aren't
    also watching) both mean the folder is gone from the one place we can
    see. GIO/inotify has no way to report a destination outside the watched
    parent (confirmed: even self-watching the folder's own inode yields a
    bare DELETED with no path -- see issue #71 investigation), so there is no
    reliable way to follow it. Per product decision, silently keeping a pin
    to a location we can no longer verify is worse than dropping it: remove
    it from Preferred Folders rather than leaving a stale or "missing"
    placeholder behind."""
    if not ext._gsettings:
        return
    old_uri = file.get_uri()
    stored_key = ext._watched_folder_keys.get(old_uri)
    if stored_key is None:
        return
    entries = ext._get_preferred_folders()
    if stored_key not in entries:
        return

    if event_type == Gio.FileMonitorEvent.RENAMED and other_file is not None:
        entries[entries.index(stored_key)] = other_file.get_uri()
    elif event_type in (Gio.FileMonitorEvent.DELETED, Gio.FileMonitorEvent.MOVED_OUT):
        entries.remove(stored_key)
    else:
        return
    ext._gsettings.set_value("preferred-folders", GLib.Variant("as", entries))


def _on_card_activated(ext, _flow_box, child: Gtk.FlowBoxChild, win: Gtk.Window) -> None:
    card = child.get_child()
    if card is None:
        return
    if isinstance(card, MyComputerDiskCard) and not card.model.is_mounted:
        _do_mount(ext, card.model, win)
        return
    GLib.idle_add(ext._navigate_to, card.nav_uri, win)


def _on_flow_selection_changed(ext, flow_box: Gtk.FlowBox, win: Gtk.Window) -> None:
    state = ext._active_panel_state(win)
    if not state or state.get("_deselecting"):
        return
    selected = flow_box.get_selected_children()
    if selected:
        card = selected[0].get_child()
        is_disk = isinstance(card, MyComputerDiskCard)
        is_folder = isinstance(card, MyComputerFolderCard)
        state["selected_mount_key"] = card.model.key if is_disk else None
        state["selected_folder_key"] = card.model.key if is_folder else None
    else:
        state["selected_mount_key"] = None
        state["selected_folder_key"] = None
        return
    state["_deselecting"] = True
    for other_flow in state.get("section_flows", []):
        if other_flow is not flow_box:
            other_flow.unselect_all()
    state["_deselecting"] = False


def _attach_flow_shortcuts(ext, flow_box: Gtk.FlowBox, win: Gtk.Window) -> None:
    """Declarative Ctrl/Shift/Alt+Return shortcuts for the focused card,
    mirroring how native Nautilus wires a Gtk.ShortcutController onto its
    grid cells, rather than hand-parsing modifier bits off a raw key event.

    Must live on the FlowBox itself, not the card: FlowBoxChild (not our
    card widget) is the actual keyboard focus target, and GTK's shortcut
    search walks up from the focused widget through its ancestors -- the
    FlowBox is one, the card is not. Plain Return is left alone here; it
    already works natively via FlowBox's own "child-activated" binding
    (see _on_card_activated), so duplicating it would be redundant.
    """
    controller = Gtk.ShortcutController()
    controller.set_scope(Gtk.ShortcutScope.LOCAL)
    for accel, kind in (
        ("<Control>Return", "tab"),
        ("<Shift>Return", "window"),
        ("<Alt>Return", "properties"),
    ):
        trigger = Gtk.ShortcutTrigger.parse_string(accel)
        action = Gtk.CallbackAction.new(
            lambda w, _args, win=win, kind=kind, ext=ext: _activate_focused_card(ext, w, win, kind)
        )
        controller.add_shortcut(Gtk.Shortcut.new(trigger, action))
    flow_box.add_controller(controller)


def _on_panel_clicked(ext, _gesture, _n, _x, _y, win: Gtk.Window) -> None:
    state = ext._active_panel_state(win)
    if not state:
        return
    _unselect_all_cards(state)
    state["selected_mount_key"] = None
    state["selected_folder_key"] = None


def _select_single_card(card: Gtk.Widget) -> None:
    """Anticipate selection on the card's FlowBoxChild, mirroring
    select_single_item_if_not_selected (nautilus-list-base.c:287)."""
    wrapper = card.get_parent()
    if isinstance(wrapper, Gtk.FlowBoxChild):
        flow = wrapper.get_parent()
        if isinstance(flow, Gtk.FlowBox):
            flow.select_child(wrapper)


def _on_card_pressed(
    ext, gesture, n_press: int, x: float, y: float, win: Gtk.Window, card: Gtk.Box
) -> None:
    """Button dispatch on "pressed", mirroring on_item_click_pressed
    (nautilus-list-base.c:270-292). Primary is left unclaimed (activation
    stays on FlowBox's own child-activated binding, _on_card_activated)."""
    button = gesture.get_current_button()
    if button == Gdk.BUTTON_SECONDARY and n_press == 1:
        _on_card_right_clicked(ext, gesture, n_press, x, y, win, card)
    elif button == Gdk.BUTTON_MIDDLE and n_press == 1:
        # Middle opens a background tab; Ctrl+middle opens a new window. Native
        # cells never honor Ctrl+middle (nautilus-list-base.c:175-187, 285-291)
        # -- only the sidebar does (nautilus-sidebar.c:3236-3241, #116) -- but
        # Column View rows already diverged from cell parity for this exact
        # reason (#131): cards, like Miller rows, are a browsing surface where
        # the sidebar's modifier reads more naturally than strict cell parity.
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        _select_single_card(card)
        ctrl = bool(gesture.get_current_event_state() & Gdk.ModifierType.CONTROL_MASK)
        if isinstance(card, MyComputerDiskCard) and not card.model.is_mounted:
            _do_mount_then_open(ext, card.model, win, "window" if ctrl else "tab")
        elif ctrl:
            ext._do_open_window(card.nav_uri)
        else:
            ext._do_open_tab(card.nav_uri, win, make_active=False)


def _on_card_right_clicked(ext, gesture, _n, x, y, win: Gtk.Window, row: Gtk.Box) -> None:
    gesture.set_state(Gtk.EventSequenceState.CLAIMED)

    if isinstance(row, MyComputerDiskCard):
        m = _disk_data.get(row.model.key)
        if not m or not callable(m.menu):
            return
        ctx_menu = m.menu(ext, win, m)
    elif isinstance(row, MyComputerFolderCard):
        pf = _folder_data.get(row.model.key)
        if not pf or not callable(pf.menu):
            return
        ctx_menu = pf.menu(ext, win, pf)
    else:
        return

    # A Preferred Folder lives below the panel's scrolling viewport. Parenting
    # its menu to the card lets GTK constrain the popover to that tiny viewport
    # subtree; anchor it to the full panel instead, as Column View does for its
    # scrollable content, and translate the click point into panel coordinates.
    state = ext._active_panel_state(win)
    popover_parent = state.get("panel") if isinstance(row, MyComputerFolderCard) and state else row
    point = row.translate_coordinates(popover_parent, x, y)
    point_x, point_y = point if point is not None else (x, y)
    popover = ctx_menu.build_popover(popover_parent, "diskrow")
    rect = Gdk.Rectangle()
    rect.x, rect.y, rect.width, rect.height = int(point_x), int(point_y), 1, 1
    popover.set_pointing_to(rect)
    popover.popup()


def _do_mount(ext, m: MountInfo, win: Gtk.Window) -> None:
    if not m or not m.gio_volume or not m.can_mount:
        return
    op = Gio.MountOperation.new()
    m.gio_volume.mount(
        Gio.MountMountFlags.NONE, op, None, functools.partial(_on_mount_finish, ext), win
    )


def _on_mount_finish(ext, volume, result, win) -> None:
    try:
        volume.mount_finish(result)
    except GLib.Error as e:
        _log(f"mount failed: {e.message}")
    GLib.idle_add(ext._repopulate_visible)


def _do_mount_then_open(ext, m: MountInfo, win: Gtk.Window, mode: str) -> None:
    if not m or not m.gio_volume or not m.can_mount:
        return
    op = Gio.MountOperation.new()
    op.set_password_save(Gio.PasswordSave.NEVER)
    m.gio_volume.mount(
        Gio.MountMountFlags.NONE,
        op,
        None,
        functools.partial(_on_mount_then_open_finish, ext),
        (win, mode),
    )


def _on_mount_then_open_finish(ext, volume, result, user_data) -> None:
    win, mode = user_data
    try:
        volume.mount_finish(result)
    except GLib.Error as e:
        _log(f"mount-then-open failed: {e.message}")
        GLib.idle_add(ext._repopulate_visible)
        return
    mount = volume.get_mount()
    if not mount:
        GLib.idle_add(ext._repopulate_visible)
        return
    uri = mount.get_root().get_uri()
    GLib.idle_add(ext._repopulate_visible)
    if mode == "tab":
        GLib.idle_add(lambda: ext._do_open_tab(uri, win, make_active=False))
    elif mode == "window":
        GLib.idle_add(ext._do_open_window, uri)
    else:
        GLib.idle_add(ext._do_open, uri, win)


def _do_unmount(ext, m: MountInfo) -> None:
    if not m or not m.gio_mount or not m.can_unmount:
        return
    op = Gio.MountOperation.new()
    m.gio_mount.unmount_with_operation(
        Gio.MountUnmountFlags.NONE, op, None, functools.partial(_on_unmount_finish, ext)
    )


def _on_unmount_finish(ext, mount, result) -> None:
    try:
        mount.unmount_with_operation_finish(result)
    except GLib.Error as e:
        _log(f"unmount failed: {e.message}")
    GLib.idle_add(ext._repopulate_visible)


def _do_eject(ext, m: MountInfo) -> None:
    if not m:
        return
    op = Gio.MountOperation.new()
    if m.gio_volume and m.gio_volume.can_eject():
        m.gio_volume.eject_with_operation(
            Gio.MountUnmountFlags.NONE, op, None, functools.partial(_on_eject_finish, ext)
        )
    elif m.gio_mount and m.gio_mount.can_eject():
        m.gio_mount.eject_with_operation(
            Gio.MountUnmountFlags.NONE, op, None, functools.partial(_on_eject_finish, ext)
        )


def _on_eject_finish(ext, source, result) -> None:
    try:
        source.eject_with_operation_finish(result)
    except GLib.Error as e:
        _log(f"eject failed: {e.message}")
    GLib.idle_add(ext._repopulate_visible)


def _do_format(ext, device: str) -> None:
    try:
        Gio.Subprocess.new(
            ["gnome-disks", "--block-device", device, "--format-device"],
            Gio.SubprocessFlags.NONE,
        )
    except GLib.Error as e:
        _log(f"format launch failed: {e.message}")


def _activate_focused_card(ext, flow_box: Gtk.FlowBox, win: Gtk.Window, kind: str) -> bool:
    focus_child = flow_box.get_focus_child()
    if focus_child is None:
        return False
    row = focus_child.get_child()
    if row is None:
        return False

    nav_uri = row.nav_uri
    if isinstance(row, MyComputerDiskCard) and not row.model.is_mounted:
        return False

    if kind == "tab":
        ext._do_open_tab(nav_uri, win, make_active=False)
    elif kind == "window":
        ext._do_open_window(nav_uri)
    elif kind == "properties":
        if not nav_uri:
            return False
        ext._do_properties(nav_uri, win)
