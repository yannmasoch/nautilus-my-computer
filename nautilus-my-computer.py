import dataclasses
import gettext
import os
import re
import subprocess
import threading
import time

import gi

gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
gi.require_version("GObject", "2.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Nautilus, Pango

_custom_translation = None
_localedir = os.path.expanduser("~/.local/share/locale")
try:
    _custom_translation = gettext.translation("nautilus-my-computer", localedir=_localedir)
except Exception:
    pass

_nautilus_translation = None
try:
    _nautilus_translation = gettext.translation("nautilus")
except Exception:
    pass


def _(text: str) -> str:
    if _custom_translation is not None:
        val = _custom_translation.gettext(text)
        if val != text:
            return val
    if _nautilus_translation is not None:
        return _nautilus_translation.gettext(text)
    return text


DEBUG_LOG = os.environ.get("DEBUG", "").lower() in ("1", "true", "yes")
DEBUG_LOG_PREFIX = "MyComputer"  # prefix for all debug lines, to make them easy to filter in logs


# ── Per-site injection toggles (debugging) ────────────────────────────────────
# We catch/inject into Nautilus at four independent sites. Each flag gates EVERY
# entry point for that site so a site can be fully isolated while debugging the
# Nautilus templates-menu use-after-free (crash on navigation with non-empty
# ~/Templates). Set to False to disable that site entirely. Env override:
# e.g. MC_MAIN_VIEW=0. Default all on.
def _flag(name: str, default: bool = True) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.lower() in ("1", "true", "yes", "on")


DEBUG_MAIN_VIEW_ACTIVE = _flag("MC_MAIN_VIEW")  # main view: panel overlay over the file view
DEBUG_COMPUTER_BUTTON_ACTIVE = _flag("MC_COMPUTER_BUTTON")  # left sidebar: "Computer" row injection
DEBUG_PATHBAR_ACTIVE = _flag("MC_PATHBAR")  # top URL bar: chip icon pinning
DEBUG_SORT_WATCH_ACTIVE = _flag("MC_SORT_WATCH")  # top view-mode/sort buttons: sort metadata watch
DEBUG_SELFTEST = _flag("MC_SELFTEST", default=False)  # in-process navigation self-test driver

# ── Extension metadata (keep in sync with pyproject.toml) ────────────────────
EXT_NAME = "My Computer for Nautilus"
EXT_VERSION = "0.5.2"
EXT_AUTHOR = "Yann Masoch"
EXT_LICENSE = "MIT"
EXT_GITHUB = "https://github.com/yannmasoch/nautilus-my-computer"


DISKS_URI = "computer:///"
COMPUTER_LABEL = _("Computer")
COMPUTER_ICON = "computer-symbolic"  # icon used in sidebar and path bar
MENU_ITEM_LABEL = _("My Computer Settings")
PREFS_WIN_TITLE = _("My Computer Settings")
SCHEMA_ID = "io.github.yannmasoch.nautilus-my-computer"

VIEW_FILES = "files"  # visible_view token — files view (Overlay base)
VIEW_DISKINFO = "diskinfo"  # visible_view token — our panel (Overlay child)

METADATA_SORT_BY = "metadata::nautilus-icon-view-sort-by"
METADATA_SORT_REVERSED = "metadata::nautilus-icon-view-sort-reversed"

DBUS_FILE_MANAGER = "org.freedesktop.FileManager1"
DBUS_PATH_FILE_MANAGER = "/org/freedesktop/FileManager1"

# Nautilus' own file-operation engine, exposed over D-Bus. Calling it gives us the
# native transfer popup, conflict dialogs, and undo stack — the same engine the file
# manager uses internally. CopyURIs/MoveURIs: (as sources, s destination, a{sv}).
DBUS_NAUTILUS = "org.gnome.Nautilus"
DBUS_NAUTILUS_FILEOPS = "org.gnome.Nautilus.FileOperations2"
DBUS_PATH_NAUTILUS_FILEOPS = "/org/gnome/Nautilus/FileOperations2"

# All updates are event-driven (VolumeMonitor signals, /proc/mounts POLLPRI,
# GSettings changed, Gio.FileMonitor, Gtk.Application window-added). The values
# below are one-shot retry/debounce intervals, not continuous poll periods.
_REFRESH_DEBOUNCE_MS = 300  # coalesce rapid mount/unmount/plug events
_WIN_INIT_RETRY_MS = 20  # retry interval while waiting for NautilusWindow widget tree
_WIN_INIT_MAX_ATTEMPTS = 100  # ~2 s budget waiting for the first view load to settle
_NAV_RETRY_MS = 60  # retry interval while navigating to computer:///
_TAB_WAIT_MS = 50  # retry interval while waiting for a new tab slot
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

_FLOW_COLS_GRID = 8  # max columns in grid (FlowBox) view
_LIST_MAX_WIDTH = 450  # max width (px) of a card group in list view


# Resolve the display name Nautilus shows in the title bar when at DISKS_URI,
# so panel detection works regardless of which URI is configured.
try:
    _info = Gio.File.new_for_uri(DISKS_URI).query_info(
        "standard::display-name", Gio.FileQueryInfoFlags.NONE, None
    )
    _LOCATION_TITLE = _info.get_display_name()
except Exception:
    _LOCATION_TITLE = COMPUTER_LABEL

# Localized title Nautilus shows when browsing the user's home folder.
# Used to distinguish a "default new window" (opened at Home) from a window
# that was explicitly opened to a specific folder.
_HOME_TITLE: str = GLib.dgettext("nautilus", "Home")

# Transient title Nautilus shows while a location is still loading. Treated as
# "window not settled yet" so it never consumes the start-on-computer one-shot.
_LOADING_TITLE: str = GLib.dgettext("nautilus", "Loading…")


def _is_unsettled_title(title: str) -> bool:
    """True while the window hasn't resolved to a real location yet."""
    return not title or title == _LOADING_TITLE


REAL_FSTYPES = {
    "ext4",
    "ext3",
    "ext2",
    "xfs",
    "btrfs",
    "f2fs",
    "ntfs",
    "ntfs3",
    "vfat",
    "exfat",
    "zfs",
    "reiserfs",
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

# Internal sentinel values used for fstype — not shown in UI
_INTERNAL_FSTYPES = {"gvfs", "unmounted", "network-place"}

# Mountpoint prefixes that indicate removable / external media
EXTERNAL_PREFIXES = ("/media/", "/run/media/", "/mnt/")


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
    is_network_place: bool = False

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


def _gicon_renders(gicon) -> bool:
    """True if gicon is non-None and resolves in the current icon theme."""
    if gicon is None:
        return False
    if isinstance(gicon, Gio.ThemedIcon):
        try:
            theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        except Exception:
            return True
        return any(theme.has_icon(n) for n in gicon.get_names())
    return True


def _read_os_name() -> str:
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return ""


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


def _get_local_mount_tier(m: MountInfo) -> tuple[int, str]:
    """Return (tier, name) for hierarchical sorting within 'local' group.
    Tier: 0=root, 1=system partitions, 2=mounted, 3=unmounted
    Used by 'sort by type' mode."""
    name = (m.display_name or "").lower()
    if m.mountpoint == "/":
        return (0, name)
    if m.mountpoint in ("/boot", "/boot/efi", "/efi") or m.fstype == "swap":
        return (1, name)
    if m.is_mounted:
        return (2, name)
    return (3, name)


# Icon per group category
_GROUP_ICON = {
    "system": "drive-harddisk",
    "local": "drive-harddisk",
    "removable": "drive-removable-media",
    "disc": "media-optical",
    "network": "folder-remote",
}

# Ordered group spec: (key, display_label, gsettings_key)
# "local" is the merge target for other groups -- always visible, no gsettings key
_GROUP_SPEC: list[tuple[str, str, str | None]] = [
    ("system", "System", "visibility-system"),
    ("local", "On this Computer", None),
    ("removable", "Removable", "visibility-removable"),
    ("disc", "Disc", "visibility-disc"),
    ("network", "Network", "visibility-network"),
]


@dataclasses.dataclass
class DiskGroup:
    key: str
    label: str
    visible: bool = True
    merged: bool = False
    items: list = dataclasses.field(default_factory=list)

    def add_item(self, m) -> None:
        self.items.append(m)

    def sort_items(self, key_func, reverse: bool = False) -> None:
        self.items.sort(key=key_func, reverse=reverse)


_disk_data: dict[str, MountInfo] = {}
_network_places: list[MountInfo] = []  # populated async from network:///

_CSS = b"""
* {
    /* Mirrors Nautilus's own --accent-bg-color override from its bundled style.css
       (.nautilus-grid-view gridview rule). Theme-safe: GTK themes load at priority
       200 (THEME), this loads at 600 (APPLICATION) - themes cannot override it.
       Only user stylesheets at priority 800 (USER) can, which is correct behavior. */
    --diskinfo-selection-grey: #959595;
}
.diskinfo-panel {
    background-color: @view_bg_color;
}
.diskinfo-panel flowbox {
    --accent-bg-color: var(--diskinfo-selection-grey);
}
.diskinfo-subtext {
    color: @insensitive_fg_color;
}
.unmounted {
    opacity: 0.5;
}
.vanilla-diskinfo-view-hidden {
    background: @view_bg_color;
}
.vanilla-diskinfo-view-hidden > * {
    opacity: 0;
}
/* For testing/debugging: shows injected panel outline vs native sidebar. */
.gap-debug {
    margin: 0;
    padding: 0;
}
/* Zero margin/padding on both listboxes so there is no gap between our
   injected Computer row and the native sidebar list. */
#my_computer_list {
    margin: 6px 0 0 0;
    padding: 0;
}
/* libadwaita: `placessidebar .navigation-sidebar > row > revealer { padding: 0 14px }`.
   Our listbox is NOT inside placessidebar so that rule never fires.
   Our fallback row has no revealer, so replicate the inset on the row. */
#my_computer_list > row {
    padding: 0 14px;
}
/* Fallback row only (row > box > image, not row > revealer > box > image).
   Mirrors nautilus-sidebar-row.blp Image { margin-end: 8 }. */
#my_computer_list > row > box > image {
    margin-end: 8px;
}
.places-sidebar-list {
    margin: 0;
    padding: 0;
}
"""


def _log(msg: str) -> None:
    """Print a prefixed debug line. Set DEBUG_LOG = False to silence all logs."""
    if DEBUG_LOG:
        print(f"{DEBUG_LOG_PREFIX}: {msg}", flush=True)


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


def _get_gsettings() -> Gio.Settings | None:
    try:
        return Gio.Settings.new(SCHEMA_ID)
    except Exception:
        return None


def _format_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if n < 1000:
            return f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} EB"


def _scan_mounts(show_system_partitions: bool = False) -> list[MountInfo]:
    mounts: list[MountInfo] = []
    seen: set[str] = set()
    uuid_map = _build_uuid_map()

    # Build mountpoint → Gio.Icon / Gio.Mount from VolumeMonitor so we can
    # attach the real hardware icon and GIO handle to each /proc/mounts entry.
    # Also build a UUID fallback for mounts whose root path doesn't match the
    # /proc/mounts mountpoint (e.g. root on LUKS/dm-crypt).
    icon_by_path: dict[str, Gio.Icon] = {}
    mount_by_path: dict[str, object] = {}
    mount_by_uuid: dict[str, object] = {}
    try:
        vm = Gio.VolumeMonitor.get()
        for gm in vm.get_mounts():
            root = gm.get_root()
            path = root.get_path()
            if path:
                icon_by_path[path] = gm.get_icon()
                mount_by_path[path] = gm
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
                gvfs_show = "x-gvfs-show" in opts
                is_external = any(mountpoint.startswith(p) for p in EXTERNAL_PREFIXES)
                if (
                    fstype not in REAL_FSTYPES and not gvfs_show and not is_external
                ) or device in seen:
                    continue
                if not show_system_partitions and mountpoint in ("/boot", "/boot/efi", "/efi"):
                    continue
                seen.add(device)
                try:
                    st = os.statvfs(mountpoint)
                    total = st.f_blocks * st.f_frsize
                    free = st.f_bavail * st.f_frsize
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
                            nav_uri=Gio.File.new_for_path(mountpoint).get_uri(),
                            gio_icon=icon_by_path.get(mountpoint),
                            gio_mount=gio_mount,
                            gio_volume=gio_volume,
                            is_removable=gio_drive.is_removable() if gio_drive else False,
                            can_eject=bool(
                                (gio_volume and gio_volume.can_eject())
                                or (gio_mount and gio_mount.can_eject())
                                or (gio_drive and gio_drive.can_eject())
                            ),
                        )
                    )
                except OSError:
                    pass
    except OSError:
        pass
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


_ZOOM_TO_PX = {"small": 48, "standard": 64, "large": 96, "x-large": 128}


def _nautilus_icon_size() -> int:
    try:
        settings = Gio.Settings.new("org.gnome.nautilus.icon-view")
        zoom = settings.get_string("default-zoom-level")
        return _ZOOM_TO_PX.get(zoom, 64)
    except Exception:
        return 64


def _all_widgets(widget):
    if not widget:
        return
    yield widget
    # Using observe_children instead of get_first_child/get_next_sibling
    # is safer in some GTK4 contexts but let's stick to the basic tree walker.
    child = widget.get_first_child()
    while child:
        yield from _all_widgets(child)
        child = child.get_next_sibling()


def _find_widget(root, *, buildable_id=None, class_name=None, css_class=None, site=""):
    """Find a widget by layered fallback: buildable_id → class_name → css_class.

    Rejects GtkBuilder auto-placeholders (___object_N___). Logs drift when falling
    back past tier 1 so Nautilus API changes surface without breaking the extension.
    """
    tier1 = tier2 = tier3 = None
    for w in _all_widgets(root):
        if tier1 is None and buildable_id is not None:
            bid = w.get_buildable_id() if hasattr(w, "get_buildable_id") else None
            if bid and bid == buildable_id and not bid.startswith("___object_"):
                tier1 = w
        if tier2 is None and class_name is not None:
            if type(w).__name__ == class_name:
                tier2 = w
        if tier3 is None and css_class is not None:
            if hasattr(w, "has_css_class") and w.has_css_class(css_class):
                tier3 = w
        if tier1 is not None:
            break
    result = tier1 or tier2 or tier3
    if result is not None and result is not tier1 and buildable_id is not None and site:
        tier_name = "css_class" if result is tier3 else "class_name"
        _log(f"{site}: buildable_id {buildable_id!r} not found, matched via {tier_name}")
    elif result is None and site:
        _log(f"{site}: no match (id={buildable_id!r} class={class_name!r} css={css_class!r})")
    return result


def _is_nautilus_window(win: Gtk.Window) -> bool:
    """Identify a Nautilus application window by layered fallback.

    Tier 1: buildable_id == 'NautilusWindow'
    Tier 2: class name  == 'NautilusWindow'
    Tier 3: css class      'nautilus-window'
    Tier 4: structural  — contains Adw.OverlaySplitView
    """
    bid = win.get_buildable_id() if hasattr(win, "get_buildable_id") else None
    if bid and bid == "NautilusWindow":
        return True
    if type(win).__name__ == "NautilusWindow":
        if bid != "NautilusWindow":
            _log("is_nautilus_window: matched via class_name (buildable_id drift)")
        return True
    if hasattr(win, "has_css_class") and win.has_css_class("nautilus-window"):
        _log("is_nautilus_window: matched via css class (class/id drift)")
        return True
    if any(isinstance(w, Adw.OverlaySplitView) for w in _all_widgets(win)):
        _log("is_nautilus_window: matched via structural navigation (significant drift)")
        return True
    return False


def _pin_icon(img: Gtk.Image, icon_name: str) -> None:
    """Set img's icon and keep it locked against Nautilus's async overwrites.

    Nautilus may overwrite the icon via set_from_icon_name(), set_from_gicon(),
    or set_from_paintable().  We watch all three relevant notify signals.

    Subtle bug avoided: after set_from_gicon(), get_icon_name() can still
    return the *stale* previous icon name while the displayed icon has already
    changed to the GVfs one.  We therefore also check get_gicon() to detect
    that case.  A simple boolean flag prevents re-entrance (handler_block_by_func
    has cross-signal edge-cases when one function is connected to multiple
    signals simultaneously).
    """
    img.set_from_icon_name(icon_name)
    img.set_visible(True)
    if getattr(img, "_diskinfo_pinned", False):
        return  # already watching
    img._diskinfo_pinned = True
    img._diskinfo_restoring = False

    def _on_changed(image: Gtk.Image, _pspec) -> None:
        if image._diskinfo_restoring:
            return  # we triggered this notification ourselves – skip
        # Detect overwrite: storage type not ICON_NAME, wrong name, or visibility dropped.
        if (
            getattr(image, "get_storage_type", lambda: None)() != Gtk.ImageType.ICON_NAME
            or image.get_icon_name() != icon_name
            or not image.get_visible()
        ):
            image._diskinfo_restoring = True
            image.set_from_icon_name(icon_name)
            image.set_visible(True)
            image._diskinfo_restoring = False

    img.connect("notify::icon-name", _on_changed)
    img.connect("notify::gicon", _on_changed)
    img.connect("notify::paintable", _on_changed)
    img.connect("notify::storage-type", _on_changed)
    img.connect("notify::visible", _on_changed)


class LeftClamp(Gtk.Widget):
    """Constrain a single child to a maximum width, anchored to the left.

    Adw.Clamp does the "grow up to a cap, then stop" behaviour we want but
    always centres its child (no alignment property).  GTK4 CSS max-width is
    not enforced as a layout constraint either.  This minimal layout widget
    allocates its child at the left edge with width = min(available, max_width),
    so it caps at max_width on wide windows yet still shrinks responsively when
    the window is narrower.
    """

    __gtype_name__ = "DiskinfoLeftClamp"

    def __init__(self, max_width: int):
        super().__init__()
        self._max_width = max_width
        self._child = None

    def set_child(self, child: Gtk.Widget) -> None:
        if self._child is not None:
            self._child.unparent()
        self._child = child
        if child is not None:
            child.set_parent(self)

    def do_measure(self, orientation, for_size):
        if self._child is None:
            return (0, 0, -1, -1)
        if orientation == Gtk.Orientation.VERTICAL and for_size > self._max_width:
            for_size = self._max_width
        min_size, nat_size, min_base, nat_base = self._child.measure(orientation, for_size)
        if orientation == Gtk.Orientation.HORIZONTAL:
            nat_size = min(nat_size, self._max_width)
            min_size = min(min_size, self._max_width)
        return (min_size, nat_size, min_base, nat_base)

    def do_size_allocate(self, width, height, baseline):
        if self._child is None:
            return
        self._child.allocate(min(width, self._max_width), height, baseline, None)

    def do_dispose(self):
        if self._child is not None:
            self._child.unparent()
            self._child = None
        Gtk.Widget.do_dispose(self)


class MyComputerExtension(GObject.GObject, Nautilus.MenuProvider):
    def __init__(self):
        super().__init__()
        # Maps each NautilusWindow to its per-window state dict:
        #   overlay, panel, content_box, force_disks, initial_title
        self._windows: dict = {}
        self._polling_started = False
        self._refresh_pending = False  # debounce flag for live-refresh
        self._local_poll_stop: threading.Event | None = None
        self._net_poll_timer_id: int | None = None
        self._net_poll_cancellable: Gio.Cancellable | None = None

        self._sort_column: str = "name"
        self._sort_reverse: bool = False
        self._view_mode: str = "icon-view"
        # Sort is read from per-folder GVfs metadata. There is no usable event
        # for it (the metadata daemon writes via mmap so file monitors never
        # fire, and the GTK4 Python bindings don't expose get_action_group, so
        # we can't subscribe to Nautilus's "view.sort" GAction). We therefore
        # poll — but only while the pointer is over the header bar (where the
        # sort menu lives) and the Computer panel is visible.
        # _sort_hover tracks whether the pointer is currently inside the navbar.
        # The poll arms on enter and disarms on leave, with a short grace period
        # to cover the gap when the pointer moves from the navbar into the sort
        # popover (which is a separate native surface and triggers a leave event).
        self._sort_poll_id = None  # GLib source id while polling, else None
        self._sort_hover = False  # True while pointer is inside the navbar
        self._nautilus_prefs = None  # Gio.Settings for org.gnome.nautilus.preferences
        self._bar_css_provider = Gtk.CssProvider()
        self._bar_css_display = None

        self._gsettings = _get_gsettings()
        if self._gsettings:
            self._start_on_disks: bool = self._gsettings.get_boolean("start-on-disks")
            self._gsettings.connect("changed", self._on_settings_changed)
        else:
            self._start_on_disks = False

        _show_sys_parts = (
            self._gsettings.get_boolean("show-system-partitions") if self._gsettings else False
        )
        _refresh(_scan_mounts(_show_sys_parts) + _scan_gio_mounts() + _scan_gio_volumes())

        # Watch /proc/mounts at the kernel level — POLLPRI fires on any
        # mount/unmount regardless of how it happened (udisks, manual, FUSE…)
        try:
            self._mounts_file = open("/proc/mounts", "r")
            GLib.io_add_watch(
                self._mounts_file,
                GLib.PRIORITY_DEFAULT,
                GLib.IOCondition.ERR | GLib.IOCondition.PRI,
                self._on_proc_mounts_changed,
            )
        except OSError:
            self._mounts_file = None

        # VolumeMonitor signals — catch drive plug/unplug and GVfs events
        self._volume_monitor = Gio.VolumeMonitor.get()
        for sig in (
            "mount-added",
            "mount-removed",
            "volume-added",
            "volume-removed",
            "drive-connected",
            "drive-disconnected",
            "drive-changed",
        ):
            self._volume_monitor.connect(sig, self._on_disk_event)

        # Kick off async network:/// discovery immediately
        _refresh_network_places(on_done=self._do_live_refresh)

        GLib.idle_add(self._late_init)

    # ── Initialisation ────────────────────────────────────────────────────────

    def _late_init(self) -> bool:
        # Catch any windows that already existed before we connected signals.
        self._check_new_windows()

        if not self._polling_started:
            self._polling_started = True
            # Instant detection of new windows via signal (no polling needed).
            app = Gtk.Application.get_default()
            if app:
                app.connect("window-added", self._on_window_added)
            self._read_sort_metadata()
            self._read_view_mode()
            self._watch_view_mode()

        return False

    def _on_window_added(self, _app, win: Gtk.Window) -> None:
        """Instant handler for new Nautilus windows — defers injection until load settles."""
        self._schedule_window_init(win)

    def _schedule_window_init(self, win: Gtk.Window) -> None:
        """Wait for the window's first view load to settle, then inject on a
        low-priority idle.

        Injecting our Gtk.Overlay reparents the AdwToolbarView content. Doing that
        during Nautilus's files_view_begin_loading races with its templates
        context-menu rebuild: with a non-empty ~/Templates,
        slot_on_templates_menu_changed rebuilds a GtkPopoverMenu whose internal
        GtkStack our tree surgery destabilises, hitting a Nautilus-core
        use-after-free that segfaults on GTK 4.22 / GNOME 50 (GTK_IS_STACK
        assertion → SIGSEGV). Deferring until the load has finished, and running
        the injection at PRIORITY_LOW (after Nautilus's loading idles drain),
        removes the overlap. See issue #4. Empty ~/Templates never triggers it.
        """
        if not _is_nautilus_window(win) or win in self._windows:
            return
        attempts = [0]

        def _try() -> bool:
            if win in self._windows:
                return GLib.SOURCE_REMOVE
            attempts[0] += 1
            # Hold off until the first load has settled (title resolved to a
            # real location, not "Loading…"). Measured: title-settle is the
            # latest of the real readiness signals (tree/mapped/title), lagging
            # by ~20-40ms — typically settling within ~20-65ms of window-added.
            # No fixed floor: PRIORITY_LOW on the injection idle (below) is what
            # actually avoids the issue #4 templates-menu race, not extra delay.
            if _is_unsettled_title(win.get_title() or ""):
                if attempts[0] > _WIN_INIT_MAX_ATTEMPTS:
                    # Window never settled (rare) — inject anyway so the
                    # extension still works; route through the low-prio idle.
                    GLib.idle_add(self._deferred_init_window, win, priority=GLib.PRIORITY_LOW)
                    return GLib.SOURCE_REMOVE
                return GLib.SOURCE_CONTINUE
            GLib.idle_add(self._deferred_init_window, win, priority=GLib.PRIORITY_LOW)
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(_WIN_INIT_RETRY_MS, _try)

    def _deferred_init_window(self, win: Gtk.Window) -> bool:
        """Low-priority idle wrapper around _init_window (always one-shot)."""
        if win not in self._windows and _is_nautilus_window(win):
            self._init_window(win)
        return GLib.SOURCE_REMOVE

    def _init_window(self, win: Gtk.Window) -> bool:
        css = Gtk.CssProvider()
        css.load_from_data(_CSS)
        display = win.get_display()
        Gtk.StyleContext.add_provider_for_display(
            display,
            css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        if self._bar_css_display is None:
            self._bar_css_display = display
            self._apply_bar_color()
        if self._inject_overlay(win):
            win.connect("destroy", self._on_window_destroyed)
            win.connect("notify::title", self._on_title_changed)

            if DEBUG_COMPUTER_BUTTON_ACTIVE:
                self._inject_sidebar_link(win)
            self._on_title_changed(win, None)

            if DEBUG_SELFTEST and not getattr(self, "_selftest_started", False):
                self._selftest_started = True
                GLib.timeout_add(3000, lambda: self._run_selftest(win))

            return True
        return False

    def _run_selftest(self, win) -> bool:
        """Debug-only: drive in-process navigation (no keyboard/focus needed) so
        the templates-menu crash can be reproduced deterministically."""
        home = os.path.expanduser
        steps = [
            DISKS_URI,
            Gio.File.new_for_path(home("~/Downloads")).get_uri(),
            DISKS_URI,
            Gio.File.new_for_path(home("~/Documents")).get_uri(),
            DISKS_URI,
            Gio.File.new_for_path(home("~/Downloads")).get_uri(),
        ]
        idx = [0]

        def step():
            if win not in self._windows:
                _log("SELFTEST: window gone")
                return GLib.SOURCE_REMOVE
            if idx[0] >= len(steps):
                _log("SELFTEST DONE: survived all navigations")
                return GLib.SOURCE_REMOVE
            uri = steps[idx[0]]
            idx[0] += 1
            _log(f"SELFTEST step -> {uri}")
            for w in _all_widgets(win):
                if "Slot" in type(w).__name__:
                    try:
                        if w.activate_action("open-location", GLib.Variant("s", uri)):
                            break
                    except Exception:
                        pass
            return GLib.SOURCE_CONTINUE

        GLib.timeout_add(2500, step)
        return GLib.SOURCE_REMOVE

    def _check_new_windows(self) -> bool:
        toplevels = Gtk.Window.list_toplevels()
        found_any = False
        for win in toplevels:
            if _is_nautilus_window(win) and win not in self._windows:
                found_any = True
                # Route through the deferred path: a window present at
                # extension-load time may still be mid-load (see issue #4).
                self._schedule_window_init(win)
        if toplevels and not found_any and not self._windows:
            names = [type(w).__name__ for w in toplevels]
            _log(f"check_new_windows: no NautilusWindow found among {names} — class renamed?")
        return True

    def _on_window_destroyed(self, win: Gtk.Window) -> None:
        state = self._windows.pop(win, None)
        if state:
            tick_id = state.get("stale_release_tick_id")
            overlay = state.get("overlay")
            if (
                tick_id is not None
                and overlay is not None
                and hasattr(overlay, "remove_tick_callback")
            ):
                overlay.remove_tick_callback(tick_id)
            state["stale_release_tick_id"] = None
            state["stale_release_ticks"] = 0
            state.get("stale_generations", []).clear()
        # Stop usage poll workers if this was the last window showing our panel.
        self._stop_usage_poll_if_idle()

    def _on_overlay_finalized(self, win: Gtk.Window, state: dict) -> None:
        if state.get("overlay") is None and not state.get("overlay_alive", True):
            return
        was_visible = state.get("visible_view") == VIEW_DISKINFO
        state["overlay"] = None
        state["overlay_alive"] = False
        state["visible_view"] = None
        _log(f"overlay finalized before window destroy for {type(win).__name__}")
        if was_visible:
            GLib.idle_add(self._stop_usage_poll_if_idle)

    def _has_live_overlay(self, state: dict, site: str) -> bool:
        if state.get("overlay") is None or not state.get("overlay_alive", True):
            _log(f"{site}: skip dead overlay")
            return False
        return True

    def _trace_view_set(self, overlay: Gtk.Widget, name: str, site: str) -> None:
        _log(f"{site}: show view '{name}' on {type(overlay).__name__}@0x{id(overlay):x}")

    def _set_visible_view(self, state: dict, name: str, site: str) -> bool:
        if not self._has_live_overlay(state, site):
            return False
        panel = state.get("panel")
        if panel is None:
            return False

        self._trace_view_set(state["overlay"], name, site)
        # files_widget is the always-present Overlay base — never hidden (hiding
        # it would reparent/unmap and risk the GTK_IS_STACK crash). Toggle only
        # the panel overlay's visibility. The panel is opaque and FILL/FILL, so
        # it absorbs all pointer events without needing set_sensitive() on the
        # base. Keyboard type-ahead is blocked separately by the capture-phase
        # key guard (_on_window_key_capture). Do NOT call set_sensitive() on the
        # base (AdwTabView): it covers all tabs, so toggling it disrupts Nautilus
        # keyboard controllers on other tabs and causes freezes in multi-tab.
        panel.set_visible(name == VIEW_DISKINFO)

        state["visible_view"] = name
        # Whichever view we land on is now correct to show: either the panel
        # covers the vanilla computer:/// view, or we genuinely landed on a
        # normal folder that must never be blanked.
        self._blank_vanilla_view(state, False)
        return True

    def _blank_vanilla_view(self, state: dict, hidden: bool) -> None:
        """Toggle a CSS class that paints over the vanilla computer:/// file view
        with the panel's background before our overlay panel becomes visible.

        Adding/removing a style class is a same-frame paint change, not a tree
        mutation - it cannot trigger the GTK_IS_STACK reparenting crash class
        (see _set_visible_view). We arm this the moment we know navigation is
        heading to computer:/// (earlier than the title-settle signal that drives
        the overlay toggle), so the vanilla grid is never painted at all on the
        paths we initiate ourselves."""
        files_widget = state.get("files_widget")
        if files_widget is None:
            return
        if hidden:
            files_widget.add_css_class("vanilla-diskinfo-view-hidden")
        else:
            files_widget.remove_css_class("vanilla-diskinfo-view-hidden")

    def _on_settings_changed(self, settings: Gio.Settings, key: str) -> None:
        if key == "start-on-disks":
            self._start_on_disks = settings.get_boolean(key)
        elif key in ("color-mode", "custom-color", "gradient-color-1", "gradient-color-2"):
            self._apply_bar_color()
        elif key == "show-system-partitions":
            # Needs a rescan because filtered mounts must be re-collected
            self._schedule_live_refresh()
        elif key.startswith("visibility-"):
            # Grouping change only -- no rescan needed, just re-render
            self._repopulate_visible()

    def _apply_bar_color(self) -> None:
        if not self._gsettings or self._bar_css_display is None:
            return
        mode = self._gsettings.get_string("color-mode")
        if mode == "flat":
            color = self._gsettings.get_string("custom-color")
            css = f".diskinfo-bar block.filled {{ background: {color}; }}".encode()
        elif mode == "gradient":
            c1 = self._gsettings.get_string("gradient-color-1")
            c2 = self._gsettings.get_string("gradient-color-2")
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
        self._bar_css_provider.load_from_data(css)
        Gtk.StyleContext.add_provider_for_display(
            self._bar_css_display,
            self._bar_css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1,
        )

    def _read_sort_metadata(self) -> bool:
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
            if col != self._sort_column or rev != self._sort_reverse:
                self._sort_column = col
                self._sort_reverse = rev
                return True
        except Exception:
            pass
        return False

    def _attach_sort_button_watch(self, nautilus_win: Gtk.Window) -> None:
        """Watch the sort GtkMenuButton's active state — arm poll when the sort
        popover opens, disarm (with one final read) when it closes."""
        state = self._windows.get(nautilus_win)
        if not state or state.get("header_motion"):
            return
        btn = self._find_sort_button(nautilus_win)
        if btn is None:
            _log("sort button not found in toolbar")
            return
        btn.connect("notify::active", self._on_sort_button_active, nautilus_win)
        state["header_motion"] = btn  # reuse slot — just marks "already attached"
        _log(f"sort button watch attached ({type(btn).__name__})")

    def _find_sort_button(self, nautilus_win: Gtk.Window):
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

    def _on_sort_button_active(self, btn: Gtk.MenuButton, _param, nautilus_win: Gtk.Window) -> None:
        state = self._windows.get(nautilus_win)
        if not state or not self._has_live_overlay(state, "sort button"):
            return
        if state.get("visible_view") != VIEW_DISKINFO:
            return
        if btn.get_active():
            self._sort_hover = True
            if self._sort_poll_id is None:
                _log("sort menu opened → sort poll armed")
                self._sort_poll_id = GLib.timeout_add(_SORT_POLL_MS, self._poll_sort)
        else:
            self._sort_hover = False
            _log("sort menu closed → sort poll disarming")

    def _poll_sort(self) -> bool:
        if self._read_sort_metadata():
            _log(f"sort changed → col='{self._sort_column}' rev={self._sort_reverse}")
            self._repopulate_visible()
            _log(f"sort applied → col='{self._sort_column}' rev={self._sort_reverse}")
        if not self._sort_hover:
            # Menu closed — one final read already done above, now disarm.
            _log("sort poll disarmed")
            self._sort_poll_id = None
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def _read_view_mode(self) -> None:
        """Read current view mode from Nautilus preferences GSettings."""
        try:
            settings = Gio.Settings.new("org.gnome.nautilus.preferences")
            self._view_mode = settings.get_string("default-folder-viewer")
        except Exception:
            pass

    def _watch_view_mode(self) -> None:
        """Subscribe to GSettings so view-mode changes are instant, not polled."""
        try:
            settings = Gio.Settings.new("org.gnome.nautilus.preferences")
            settings.connect("changed::default-folder-viewer", self._on_view_mode_changed)
            self._nautilus_prefs = settings  # keep reference
        except Exception:
            pass

    def _on_view_mode_changed(self, settings: Gio.Settings, _key: str) -> None:
        prev = self._view_mode
        self._view_mode = settings.get_string("default-folder-viewer")
        if self._view_mode != prev:
            _log(f"view changed → mode='{self._view_mode}'")
            self._repopulate_visible()

    # ── Live-refresh helpers ──────────────────────────────────────────────────

    def _on_disk_event(self, _monitor, *_args) -> None:
        """VolumeMonitor signal handler — debounced."""
        self._schedule_live_refresh()

    def _on_proc_mounts_changed(self, _source, _condition) -> bool:
        """/proc/mounts POLLPRI handler — any kernel mount change."""
        self._schedule_live_refresh()
        return GLib.SOURCE_CONTINUE  # keep watching

    def _schedule_live_refresh(self) -> None:
        """Coalesce rapid events (plug → volume-added → mount-added) into one update."""
        if self._refresh_pending:
            return
        self._refresh_pending = True
        GLib.timeout_add(_REFRESH_DEBOUNCE_MS, self._do_live_refresh)

    def _do_live_refresh(self) -> bool:
        self._refresh_pending = False
        _show_sys_parts = (
            self._gsettings.get_boolean("show-system-partitions") if self._gsettings else False
        )
        _refresh(_scan_mounts(_show_sys_parts) + _scan_gio_mounts() + _scan_gio_volumes())
        # Re-discover network places in background; callback will repopulate
        _refresh_network_places(on_done=self._repopulate_visible)
        self._repopulate_visible()
        return GLib.SOURCE_REMOVE

    def _repopulate_visible(self) -> bool:
        """Repopulate whichever windows are showing the disk view."""
        for win, state in list(self._windows.items()):
            if not self._has_live_overlay(state, "repopulate_visible"):
                continue
            if state.get("visible_view") == VIEW_DISKINFO:
                self._populate(win)
        return GLib.SOURCE_REMOVE

    # ── Usage poll workers (armed while panel is visible) ─────────────────────

    def _sweep_local_usage(self) -> None:
        """Worker-thread only: statvfs every local mount, queue changed usage to
        the main thread. Pure-read — never writes _disk_data here (that happens on
        the main thread in _apply_usage_updates via dataclasses.replace)."""
        updates: dict[str, tuple[int, int]] = {}
        for key, m in list(_disk_data.items()):
            if m.is_gio or not m.is_mounted or not m.mountpoint:
                continue
            try:
                st = os.statvfs(m.mountpoint)
                total = st.f_blocks * st.f_frsize
                free = st.f_bavail * st.f_frsize
                if free != m.free or total != m.total:
                    updates[key] = (total, free)
            except OSError:
                pass
        if updates:
            GLib.idle_add(self._apply_usage_updates, updates, priority=GLib.PRIORITY_DEFAULT)

    def _local_usage_worker(self, stop_event: threading.Event) -> None:
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

            self._sweep_local_usage()

    def _net_usage_tick(self) -> bool:
        """GLib timer callback: fire async D-Bus usage queries for all GVfs/network mounts."""
        attrs = f"{Gio.FILE_ATTRIBUTE_FILESYSTEM_SIZE},{Gio.FILE_ATTRIBUTE_FILESYSTEM_FREE}"
        for key, m in list(_disk_data.items()):
            if not m.is_gio:
                continue
            Gio.File.new_for_uri(m.nav_uri).query_filesystem_info_async(
                attrs,
                GLib.PRIORITY_DEFAULT,
                self._net_poll_cancellable,
                self._on_net_info_ready,
                key,
            )
        return GLib.SOURCE_CONTINUE

    def _on_net_info_ready(self, gfile: Gio.File, result: Gio.AsyncResult, key: str) -> None:
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
            self._apply_usage_updates({key: (total, free)})

    def _apply_usage_updates(self, updates: dict) -> bool:
        """Main-thread callback: patch _disk_data and update card widgets in place."""
        global _disk_data
        for key, (total, free) in updates.items():
            if key not in _disk_data:
                continue
            _disk_data[key] = dataclasses.replace(_disk_data[key], total=total, free=free)
            for state in self._windows.values():
                if not self._has_live_overlay(state, "apply_usage_updates"):
                    continue
                if state.get("visible_view") != VIEW_DISKINFO:
                    continue
                self._update_card_usage(state, key, total, free)
        return GLib.SOURCE_REMOVE

    def _update_card_usage(self, state: dict, key: str, total: int, free: int) -> None:
        """Update LevelBar and subtext label for a card via the O(1) card_widgets registry."""
        entry = state.get("card_widgets", {}).get(key)
        if entry is None:
            return
        bar, sub = entry
        if bar is not None and total > 0:
            bar.set_value(min(1.0, (total - free) / total))
        if sub is not None and total > 0:
            sub.set_label(
                _("{free} free of {total}").format(
                    free=_format_size(free), total=_format_size(total)
                )
            )

    def _ensure_usage_poll_running(self) -> None:
        """Arm both usage poll workers if not already running."""
        if self._local_poll_stop is None:
            ev = threading.Event()
            self._local_poll_stop = ev
            threading.Thread(target=self._local_usage_worker, args=(ev,), daemon=True).start()
        if self._net_poll_timer_id is None:
            self._net_poll_cancellable = Gio.Cancellable()
            self._net_usage_tick()
            self._net_poll_timer_id = GLib.timeout_add(_USAGE_POLL_NETWORK_MS, self._net_usage_tick)

    def _stop_usage_poll_if_idle(self) -> None:
        """Disarm poll workers when no window is showing the disk panel."""
        any_visible = any(
            st.get("overlay") is not None
            and st.get("overlay_alive", True)
            and st.get("visible_view") == VIEW_DISKINFO
            for st in self._windows.values()
        )
        if not any_visible:
            if self._local_poll_stop is not None:
                self._local_poll_stop.set()
                self._local_poll_stop = None
            if self._net_poll_timer_id is not None:
                GLib.source_remove(self._net_poll_timer_id)
                self._net_poll_timer_id = None
            if self._net_poll_cancellable is not None:
                self._net_poll_cancellable.cancel()
                self._net_poll_cancellable = None

    def _inject_overlay(self, nautilus_win: Gtk.Window) -> bool:
        split_view = None
        for w in _all_widgets(nautilus_win):
            if isinstance(w, Adw.OverlaySplitView):
                split_view = w
                break
        if not split_view:
            _log("inject_overlay: Adw.OverlaySplitView not found — widget tree may have changed")
            return False

        toolbar_view = None
        right = split_view.get_content()
        if right:
            for w in _all_widgets(right):
                if isinstance(w, Adw.ToolbarView):
                    toolbar_view = w
                    break

        panel, grid_host, grid_box = self._build_panel(nautilus_win)
        # Use Gtk.Overlay: files view is the always-present base; our panel floats
        # on top as an overlay child, visible only on computer:///. The Overlay type
        # is opaque to Nautilus's gtk_widget_get_ancestor(view, GTK_TYPE_STACK) walk,
        # so it never confuses Nautilus's internal view GtkStack (unlike a Gtk.Stack
        # wrapper, which caused the GTK_IS_STACK assertion / SIGSEGV on GNOME 43+).
        overlay = Gtk.Overlay()

        # Panel must fill the full Overlay area so the files view doesn't show through.
        panel.set_halign(Gtk.Align.FILL)
        panel.set_valign(Gtk.Align.FILL)

        if not DEBUG_MAIN_VIEW_ACTIVE:
            # Main-view site disabled: keep the overlay/panel ORPHAN (never inserted
            # into Nautilus's tree) so other sites can still be exercised/isolated.
            files_widget = None
            _log("inject_overlay: DEBUG_MAIN_VIEW_ACTIVE=False — overlay kept orphan")
        elif toolbar_view:
            files_widget = toolbar_view.get_content()
            if not files_widget:
                return False
            toolbar_view.set_content(overlay)
            overlay.set_child(files_widget)
            overlay.add_overlay(panel)
        else:
            files_widget = right
            if not files_widget:
                return False
            split_view.set_content(overlay)
            overlay.set_child(files_widget)
            overlay.add_overlay(panel)

        # Start with the files view — panel hidden until title changes to computer:///.
        self._trace_view_set(overlay, VIEW_FILES, "inject_overlay initial")
        panel.set_visible(False)

        self._windows[nautilus_win] = {
            "overlay": overlay,
            "overlay_alive": True,
            "visible_view": VIEW_FILES,
            "files_widget": files_widget,
            "panel": panel,
            "grid_host": grid_host,
            "grid_box": grid_box,
            "section_flows": [],
            "card_widgets": {},  # key → (Gtk.LevelBar | None, Gtk.Label | None)
            "stale_generations": [],
            "stale_release_tick_id": None,
            "stale_release_ticks": 0,
            "_deselecting": False,
            "force_disks": False,
            "initial_title": None,
            "start_on_computer": self._start_on_disks,
            "awaiting_disks": False,
            "selected_key": None,
            "header_motion": None,  # Gtk.EventControllerMotion on the header bar
        }
        overlay.weak_ref(
            lambda w=nautilus_win, st=self._windows.get(nautilus_win): (
                self._on_overlay_finalized(w, st) if st is not None else None
            )
        )

        # Capture-phase key guard on the window: Nautilus's "type to search"
        # type-ahead is hooked above keyboard focus, so neither hiding nor
        # de-focusing the covered file view stops it. A controller at the top of
        # the capture chain sees keystrokes first and swallows plain printable
        # ones while the panel is shown — so typing doesn't reopen the vanilla
        # computer:/// search. Modified shortcuts (Ctrl/Alt/Super) and control
        # keys (arrows, Tab, Enter, Esc) always pass through.
        key_guard = Gtk.EventControllerKey()
        key_guard.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key_guard.connect("key-pressed", self._on_window_key_capture, nautilus_win)
        nautilus_win.add_controller(key_guard)

        # If this window is headed to computer:///, let the later title-change
        # path do the first populate + switch once Nautilus has settled.

        return True

    def _on_window_key_capture(self, _ctrl, keyval, _keycode, gtk_state, win) -> bool:
        """Swallow plain printable keystrokes while our panel is shown, so
        Nautilus's window-level type-ahead search doesn't reopen the file view."""
        state = self._windows.get(win)
        if not state or state.get("visible_view") != VIEW_DISKINFO:
            return False
        # Let modified shortcuts through (Ctrl+L, Alt+Left, Super, …).
        if gtk_state & (
            Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SUPER_MASK
        ):
            return False
        # Only swallow printable characters (>= space). Control keys — arrows,
        # Tab, Enter, Esc, function keys — map to unicode < 0x20 and pass through.
        if Gdk.keyval_to_unicode(keyval) < 0x20:
            return False
        # If the user opened a text entry (Ctrl+L location bar, Ctrl+F search),
        # the focused widget is an Editable — let it receive the keystroke.
        focused = win.get_focus()
        if focused is not None and isinstance(focused, Gtk.Editable):
            return False
        return True

    # ── Panel construction ────────────────────────────────────────────────────

    def _new_grid_box(self) -> Gtk.Box:
        grid_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        grid_box.set_hexpand(True)
        grid_box.set_valign(Gtk.Align.START)
        grid_box.set_margin_start(18)
        grid_box.set_margin_end(18)
        grid_box.set_margin_top(18)
        grid_box.set_margin_bottom(18)
        return grid_box

    def _release_stale_generations(self, state: dict) -> bool:
        state.get("stale_generations", []).clear()
        state["stale_release_tick_id"] = None
        state["stale_release_ticks"] = 0
        return GLib.SOURCE_REMOVE

    def _queue_stale_generation_release(self, state: dict, root: Gtk.Widget) -> None:
        stale = state.setdefault("stale_generations", [])
        stale.append(root)
        state["stale_release_ticks"] = _STALE_RELEASE_FRAMES
        if state.get("stale_release_tick_id") is not None:
            return

        owner = state.get("overlay")
        if owner is None or not hasattr(owner, "add_tick_callback"):
            GLib.timeout_add(50, lambda st=state: self._release_stale_generations(st))
            return

        def _release_on_tick(_widget, _frame_clock, st=state):
            ticks_left = max(0, st.get("stale_release_ticks", 0) - 1)
            st["stale_release_ticks"] = ticks_left
            if ticks_left > 0:
                return GLib.SOURCE_CONTINUE
            return self._release_stale_generations(st)

        state["stale_release_tick_id"] = owner.add_tick_callback(_release_on_tick)

    def _build_panel(self, win: Gtk.Window) -> tuple:
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        panel.set_hexpand(True)
        panel.set_vexpand(True)
        panel.get_style_context().add_class("diskinfo-panel")

        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        grid_box = self._new_grid_box()

        scroll.set_child(grid_box)
        panel.append(scroll)

        bg_deselect = Gtk.GestureClick()
        bg_deselect.set_button(1)
        bg_deselect.connect("pressed", self._on_panel_clicked, win)
        scroll.add_controller(bg_deselect)

        bg_right_click = Gtk.GestureClick()
        bg_right_click.set_button(3)
        bg_right_click.connect("pressed", self._on_panel_right_clicked, win)
        scroll.add_controller(bg_right_click)

        return panel, scroll, grid_box

    def _populate(self, win: Gtk.Window) -> None:
        state = self._windows.get(win)
        if state is None:
            return

        grid_box = self._new_grid_box()
        section_flows: list[Gtk.FlowBox] = []
        card_widgets = {}

        col = self._sort_column
        rev = self._sort_reverse

        def _sort_key(m: MountInfo):
            if col == "size":
                return m.total
            return (m.display_name or "").lower()

        # Build DiskGroup objects, reading visibility state from gsettings
        groups: dict[str, DiskGroup] = {}
        for gkey, glabel, gskey in _GROUP_SPEC:
            if gskey is None:
                # "On this Computer" is the merge target -- always visible, never merged
                groups[gkey] = DiskGroup(key=gkey, label=_(glabel), visible=True, merged=False)
                continue
            vis_str = self._gsettings.get_string(gskey) if self._gsettings else "visible"
            visible = vis_str != "hidden"
            merged = vis_str == "merged"
            groups[gkey] = DiskGroup(key=gkey, label=_(glabel), visible=visible, merged=merged)

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

        # Build render order: fixed spec order, only groups that are visible and not merged
        size_group = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        is_list = self._view_mode == "list-view"

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
                            sub = (0 if m.is_mounted else 1, (m.display_name or "").lower())
                        return (group_tier,) + sub

                    render_items.sort(key=_merged_type_key)
            else:
                render_items = [(m, gkey) for m in group.items]

            if not render_items:
                continue

            heading = Gtk.Label()
            heading.set_label(group.label)
            heading.set_xalign(0.0)
            heading.get_style_context().add_class("heading")
            heading.set_margin_top(12)
            heading.set_margin_start(6)
            grid_box.append(heading)

            container = Gtk.FlowBox()
            container.set_homogeneous(True)
            container.set_max_children_per_line(1 if is_list else _FLOW_COLS_GRID)
            container.set_column_spacing(16)
            container.set_row_spacing(6)
            container.set_margin_bottom(12)
            container.set_selection_mode(Gtk.SelectionMode.SINGLE)
            container.set_activate_on_single_click(False)
            container.set_hexpand(True)
            container.set_valign(Gtk.Align.START)
            container.get_style_context().add_class("view")
            container.connect("child-activated", self._on_card_activated, win)
            container.connect("selected-children-changed", self._on_flow_selection_changed, win)
            section_flows.append(container)

            for m, origin_key in render_items:
                card = self._build_disk_card(m, origin_key, win, card_widgets)
                size_group.add_widget(card)
                container.append(card)

            if is_list:
                clamp = LeftClamp(_LIST_MAX_WIDTH)
                clamp.set_child(container)
                grid_box.append(clamp)
            else:
                grid_box.append(container)

        old_grid_box = state.get("grid_box")
        state["grid_box"] = grid_box
        state["section_flows"] = section_flows
        state["card_widgets"] = card_widgets
        state["grid_host"].set_child(grid_box)
        if old_grid_box is not None:
            self._queue_stale_generation_release(state, old_grid_box)

        self._apply_bar_color()

    def _build_disk_card(
        self, m: MountInfo, group_key: str, win: Gtk.Window, card_widgets: dict
    ) -> Gtk.Widget:
        nav_uri = m.nav_uri or (
            Gio.File.new_for_path(m.mountpoint).get_uri() if m.mountpoint else ""
        )

        card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        card.get_style_context().add_class("nautilus-view-cell")
        if not m.is_mounted:
            card.get_style_context().add_class("unmounted")
        card.set_margin_start(6)
        card.set_margin_end(6)
        card.set_margin_top(6)
        card.set_margin_bottom(6)
        card.set_focusable(True)
        card.set_focus_on_click(True)

        icon = Gtk.Image()
        icon.set_pixel_size(_nautilus_icon_size())
        icon.set_valign(Gtk.Align.CENTER)
        icon.set_margin_end(12)
        if _gicon_renders(m.gio_icon):
            icon.set_from_gicon(m.gio_icon)
        else:
            icon.set_from_icon_name(_GROUP_ICON.get(group_key, "drive-harddisk"))
        card.append(icon)

        details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        details.set_hexpand(True)
        details.set_valign(Gtk.Align.CENTER)

        display_name = m.display_name or os.path.basename(m.mountpoint) or "/"
        name_lbl = Gtk.Label(label=display_name)
        name_lbl.set_xalign(0.0)
        name_lbl.set_ellipsize(3)
        details.append(name_lbl)

        bar = Gtk.LevelBar()
        bar.set_mode(Gtk.LevelBarMode.CONTINUOUS)
        bar.set_min_value(0.0)
        bar.set_max_value(1.0)
        bar.set_hexpand(True)
        bar.get_style_context().add_class("diskinfo-bar")

        has_size = m.total > 0
        if not m.is_mounted:
            bar.set_visible(False)
            sub_text = _("Not mounted")
        elif has_size:
            v = min(m.percent / 100.0, 1.0)
            bar.set_value(v)
            bar.set_visible(True)
            sub_text = _("{free} free of {total}").format(
                free=_format_size(m.free), total=_format_size(m.total)
            )
        else:
            bar.set_visible(False)
            sub_text = nav_uri

        details.append(bar)

        sub_lbl = Gtk.Label(label=sub_text)
        sub_lbl.set_xalign(0.0)
        sub_lbl.set_ellipsize(3)
        sub_lbl.get_style_context().add_class("diskinfo-subtext")
        sub_lbl.get_style_context().add_class("caption")
        details.append(sub_lbl)

        card.append(details)

        if m.mountpoint:
            fstype_part = f" ({m.fstype})" if m.fstype and m.fstype not in _INTERNAL_FSTYPES else ""
            card.set_tooltip_text(f"{m.mountpoint}{fstype_part}")

        card._mount_key = m.key
        card._nav_uri = nav_uri
        card._disk_group = group_key

        # Register bar and sublabel for O(1) in-place usage updates
        card_widgets[m.key] = (
            bar if has_size else None,
            sub_lbl if has_size else None,
        )

        right_click = Gtk.GestureClick()
        right_click.set_button(3)
        right_click.connect("pressed", self._on_disk_right_clicked, win, card)
        card.add_controller(right_click)

        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.connect("key-pressed", self._on_row_key_pressed, win, card)
        card.add_controller(key_ctrl)

        if m.is_mounted and (m.mountpoint or m.nav_uri):
            drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
            drop.connect("motion", self._on_drop_motion)
            drop.connect("drop", self._on_files_dropped, m, win)
            card.add_controller(drop)

        return card

    def _on_card_activated(self, _flow_box, child: Gtk.FlowBoxChild, win: Gtk.Window) -> None:
        card = child.get_child()
        if card is None:
            return
        mount_key = getattr(card, "_mount_key", None)
        m = _disk_data.get(mount_key) if mount_key else None
        nav_uri = getattr(card, "_nav_uri", "")
        if m and not m.is_mounted:
            self._do_mount(m, win)
            return
        GLib.idle_add(self._navigate_to, nav_uri, win)

    def _on_flow_selection_changed(self, flow_box: Gtk.FlowBox, win: Gtk.Window) -> None:
        state = self._windows.get(win)
        if not state or state.get("_deselecting"):
            return
        selected = flow_box.get_selected_children()
        if selected:
            card = selected[0].get_child()
            state["selected_key"] = getattr(card, "_mount_key", None)
        else:
            state["selected_key"] = None
            return
        state["_deselecting"] = True
        for other_flow in state.get("section_flows", []):
            if other_flow is not flow_box:
                other_flow.unselect_all()
        state["_deselecting"] = False

    # ── Location change handler ───────────────────────────────────────────────

    def _on_title_changed(self, win: Gtk.Window, _param) -> None:
        state = self._windows.get(win)
        if not state:
            return
        if not self._has_live_overlay(state, "title changed"):
            return

        current_title = win.get_title() or ""
        in_view = _LOCATION_TITLE in current_title

        # A transient/empty title ("Loading…") means the window hasn't resolved
        # its location yet. Never act on it: it must not consume the one-shot
        # start-on-computer flag, nor flip the overlay to the file view.
        if _is_unsettled_title(current_title):
            return

        # While the startup navigation to computer:/// is still in flight, keep
        # the panel pinned. Intermediate titles (e.g. a lingering "Home") must
        # not flip the overlay to the file view and cause a flash.
        if state.get("awaiting_disks"):
            if in_view:
                state["awaiting_disks"] = False  # arrived, fall through
            else:
                return

        if state.get("start_on_computer"):
            state["start_on_computer"] = False
            if current_title == _HOME_TITLE:
                self._navigate_to_disks(win)
                return

        if state["force_disks"]:
            if state["initial_title"] is None:
                state["initial_title"] = current_title
            elif current_title != state["initial_title"] and not in_view:
                state["force_disks"] = False
            else:
                in_view = True

        current = state.get("visible_view")
        if in_view:
            if current != VIEW_DISKINFO:
                self._populate(win)
                if not self._set_visible_view(state, VIEW_DISKINFO, "title changed show diskinfo"):
                    return
                self._ensure_usage_poll_running()
                GLib.idle_add(
                    lambda: [f.unselect_all() for f in state.get("section_flows", [])] and False
                )
                sidebar_lb = state.get("sidebar_listbox")
                sidebar_row = state.get("sidebar_row")
                if sidebar_lb and sidebar_row:
                    GLib.idle_add(lambda lb=sidebar_lb, r=sidebar_row: lb.select_row(r) or False)

            # Re-pin the chrome icons (path-bar chip + sidebar row) every time we
            # arrive at the computer view. This must run even when the overlay is
            # already showing the panel — on the start-on-disks path the panel is
            # pre-shown before navigation completes, so the chip only gains its
            # "Computer" label here, after the overlay is already DISKINFO.
            if DEBUG_PATHBAR_ACTIVE:
                GLib.idle_add(lambda w=win: self._fix_pathbar_icon(w) or False)
            if DEBUG_SORT_WATCH_ACTIVE:
                GLib.idle_add(lambda w=win: self._attach_sort_button_watch(w) or False)
        elif not in_view and current != VIEW_FILES:
            if state:
                state["_deselecting"] = True
                for flow in state.get("section_flows", []):
                    flow.unselect_all()
                state["_deselecting"] = False
                state["selected_key"] = None
            sidebar_lb = state.get("sidebar_listbox")
            if sidebar_lb:
                GLib.idle_add(lambda lb=sidebar_lb: lb.unselect_all() or False)
            if not self._set_visible_view(state, VIEW_FILES, "title changed show files"):
                return
            self._stop_usage_poll_if_idle()

    # ── Callbacks ─────────────────────────────────────────────────────────────

    def _on_row_key_pressed(
        self, ctrl, keyval, keycode, state, win: Gtk.Window, row: Gtk.Box
    ) -> bool:
        if keyval not in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_ISO_Enter):
            return False
        mount_key = getattr(row, "_mount_key", None)
        m = _disk_data.get(mount_key) if mount_key else None
        nav_uri = getattr(row, "_nav_uri", "")
        is_mounted = m.is_mounted if m else True
        ctrl_held = bool(state & Gdk.ModifierType.CONTROL_MASK)
        shift_held = bool(state & Gdk.ModifierType.SHIFT_MASK)
        alt_held = bool(state & Gdk.ModifierType.ALT_MASK)

        if alt_held and not ctrl_held and not shift_held:
            if is_mounted and nav_uri:
                self._do_properties(nav_uri, win)
        elif ctrl_held and not shift_held and not alt_held:
            if is_mounted:
                self._do_open_tab(nav_uri, win)
        elif shift_held and not ctrl_held and not alt_held:
            if is_mounted:
                self._do_open_window(nav_uri)
        else:
            if not is_mounted:
                self._do_mount(m, win)
            else:
                self._do_open(nav_uri, win)
        return True

    def _on_drop_motion(self, target: Gtk.DropTarget, x: float, y: float) -> Gdk.DragAction:
        drop = target.get_current_drop()
        if drop is None:
            return Gdk.DragAction(0)
        offered = drop.get_actions()
        if offered & Gdk.DragAction.COPY:
            action = Gdk.DragAction.COPY
        elif offered & Gdk.DragAction.MOVE:
            action = Gdk.DragAction.MOVE
        else:
            return Gdk.DragAction(0)
        target._last_action = action
        return action

    def _on_files_dropped(
        self,
        target: Gtk.DropTarget,
        value: Gdk.FileList,
        x: float,
        y: float,
        m: "MountInfo",
        win: Gtk.Window,
    ) -> bool:
        if not m.is_mounted:
            return False
        files = list(value.get_files())
        if not files:
            return False
        sources = [f.get_uri() for f in files]
        if m.mountpoint:
            destination = Gio.File.new_for_path(m.mountpoint).get_uri()
        else:
            destination = m.nav_uri
        if not destination:
            return False
        use_move = getattr(target, "_last_action", Gdk.DragAction.COPY) == Gdk.DragAction.MOVE
        method = "MoveURIs" if use_move else "CopyURIs"
        self._nautilus_file_op(method, sources, destination, win)
        return True

    def _nautilus_file_op(
        self, method: str, sources: list, destination: str, win: Gtk.Window
    ) -> None:
        """Hand a copy/move job to Nautilus' own engine via D-Bus, async.

        Uses CopyURIs/MoveURIs on org.gnome.Nautilus.FileOperations2 so the transfer
        shows the native progress popup, conflict dialogs, and undo support.
        Passes a Wayland xdg-foreign parent-handle in platform_data so Nautilus
        attaches the progress window to win correctly - no polling needed.
        """

        def _call(platform_data: dict) -> None:
            def _on_call(bus, result, _):
                try:
                    bus.call_finish(result)
                except Exception as e:
                    _log(f"{method} failed: {e}")

            def _on_bus(_, result):
                try:
                    bus = Gio.bus_get_finish(result)
                    bus.call(
                        DBUS_NAUTILUS,
                        DBUS_PATH_NAUTILUS_FILEOPS,
                        DBUS_NAUTILUS_FILEOPS,
                        method,
                        GLib.Variant("(assa{sv})", (sources, destination, platform_data)),
                        None,
                        Gio.DBusCallFlags.NONE,
                        -1,
                        None,
                        _on_call,
                        None,
                    )
                except Exception as e:
                    _log(f"{method} bus error: {e}")

            Gio.bus_get(Gio.BusType.SESSION, None, _on_bus)

        try:
            gi.require_version("GdkWayland", "4.0")
            from gi.repository import GdkWayland

            surface = win.get_surface()
            if isinstance(surface, GdkWayland.WaylandToplevel):

                def _got_handle(toplevel, handle, _user_data):
                    if handle:
                        # Keep the handle exported for the lifetime of the operation.
                        # Nautilus creates the conflict dialog in a background thread,
                        # long after the D-Bus method returns. If we unexported here
                        # (on D-Bus completion), the handle would be gone before
                        # dialog_realize_cb calls gdk_wayland_toplevel_set_transient_for_exported.
                        # The export is freed automatically when the surface is destroyed.
                        _call({"parent-handle": GLib.Variant("s", f"wayland:{handle}")})
                    else:
                        _call({})

                surface.export_handle(_got_handle, None)
                return
        except Exception:
            pass

        # X11: the handle is the window XID as hex, prefixed "x11:". Nautilus parses
        # it with strtol(handle, NULL, 16) in set_transient_for. No async export needed.
        try:
            gi.require_version("GdkX11", "4.0")
            from gi.repository import GdkX11

            surface = win.get_surface()
            if isinstance(surface, GdkX11.X11Surface):
                xid = surface.get_xid()
                _call({"parent-handle": GLib.Variant("s", f"x11:{xid:x}")})
                return
        except Exception:
            pass

        _call({})

    def _on_panel_clicked(self, _gesture, _n, _x, _y, win: Gtk.Window) -> None:
        state = self._windows.get(win)
        if not state:
            return
        state["_deselecting"] = True
        for flow in state.get("section_flows", []):
            flow.unselect_all()
        state["_deselecting"] = False
        state["selected_key"] = None

    def _on_panel_right_clicked(self, gesture, _n, x, y, win: Gtk.Window) -> None:
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        state = self._windows.get(win)
        if not state:
            return

        menu = Gio.Menu()
        ag = Gio.SimpleActionGroup()

        connect_sec = Gio.Menu()
        connect_sec.append(_("Connect to Server…"), "panelbg.connect-server")
        menu.append_section(None, connect_sec)

        settings_sec = Gio.Menu()
        settings_sec.append(MENU_ITEM_LABEL, "panelbg.settings")
        menu.append_section(None, settings_sec)

        connect_act = Gio.SimpleAction.new("connect-server", None)
        connect_act.connect("activate", lambda *_: self._show_connect_dialog(win))
        ag.add_action(connect_act)

        settings_act = Gio.SimpleAction.new("settings", None)
        settings_act.connect("activate", lambda *_: self._launch_prefs(win))
        ag.add_action(settings_act)

        panel = state.get("panel")
        if panel is None:
            return
        panel.insert_action_group("panelbg", ag)

        popover = Gtk.PopoverMenu.new_from_model(menu)
        popover.set_has_arrow(False)
        popover.set_parent(panel)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        popover.set_pointing_to(rect)
        popover.popup()

    # ── Connect to Server dialog ──────────────────────────────────────────────

    # Protocol definitions: (display_label, uri_scheme, default_port, fields)
    # fields is a tuple of booleans: (share, folder, port, user, domain)
    _PROTOCOLS = [
        ("Windows Share (SMB)",  "smb",    None, (True,  True,  False, False, True)),
        ("SSH / SFTP",          "sftp",   22,   (False, True,  True,  True,  False)),
        ("FTP",                 "ftp",    21,   (False, True,  True,  True,  False)),
        ("FTP (" + _("encrypted") + ")", "ftps", 990, (False, True, True, True, False)),
        ("WebDAV (HTTP)",       "dav",    80,   (False, True,  True,  True,  False)),
        ("WebDAV (HTTPS)",      "davs",   443,  (False, True,  True,  True,  False)),
    ]

    def _show_connect_dialog(self, win: Gtk.Window) -> None:
        """Show a dialog to enter network server details and connect."""
        dialog = Adw.Dialog()
        dialog.set_title(_("Connect to Server"))
        dialog.set_content_width(480)
        dialog.set_content_height(-1)

        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar_view.add_top_bar(header)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        content.set_margin_start(24)
        content.set_margin_end(24)
        content.set_margin_top(12)
        content.set_margin_bottom(24)

        # ── Protocol selector ─────────────────────────────────────────────
        proto_group = Adw.PreferencesGroup()
        proto_row = Adw.ComboRow()
        proto_row.set_title(_("Protocol"))
        proto_labels = [p[0] for p in self._PROTOCOLS]
        proto_row.set_model(Gtk.StringList.new(proto_labels))
        proto_row.set_selected(0)
        proto_group.add(proto_row)
        content.append(proto_group)

        # ── Server details group ──────────────────────────────────────────
        details_group = Adw.PreferencesGroup()
        details_group.set_margin_top(18)

        server_row = Adw.EntryRow()
        server_row.set_title(_("Server address"))
        details_group.add(server_row)

        share_row = Adw.EntryRow()
        share_row.set_title(_("Share"))
        details_group.add(share_row)

        folder_row = Adw.EntryRow()
        folder_row.set_title(_("Folder"))
        details_group.add(folder_row)

        port_row = Adw.EntryRow()
        port_row.set_title(_("Port"))
        details_group.add(port_row)

        content.append(details_group)

        # ── Authentication group ──────────────────────────────────────────
        auth_group = Adw.PreferencesGroup()
        auth_group.set_title(_("Authentication"))
        auth_group.set_margin_top(18)

        user_row = Adw.EntryRow()
        user_row.set_title(_("User"))
        auth_group.add(user_row)

        domain_row = Adw.EntryRow()
        domain_row.set_title(_("Domain"))
        auth_group.add(domain_row)

        content.append(auth_group)

        # ── Connect button ────────────────────────────────────────────────
        connect_btn = Gtk.Button(label=_("Connect"))
        connect_btn.add_css_class("suggested-action")
        connect_btn.add_css_class("pill")
        connect_btn.set_margin_top(24)
        connect_btn.set_halign(Gtk.Align.CENTER)
        connect_btn.set_size_request(200, -1)
        content.append(connect_btn)

        # ── Status label (shown on error) ─────────────────────────────────
        status_label = Gtk.Label()
        status_label.set_margin_top(12)
        status_label.set_wrap(True)
        status_label.add_css_class("error")
        status_label.set_visible(False)
        content.append(status_label)

        toolbar_view.set_content(content)
        dialog.set_child(toolbar_view)

        # ── Field visibility per protocol ─────────────────────────────────
        def _update_fields(*_args):
            idx = proto_row.get_selected()
            _label, _scheme, default_port, (f_share, f_folder, f_port, f_user, f_domain) = (
                self._PROTOCOLS[idx]
            )
            share_row.set_visible(f_share)
            folder_row.set_visible(f_folder)
            port_row.set_visible(f_port)
            user_row.set_visible(f_user)
            domain_row.set_visible(f_domain)
            if f_port and default_port is not None:
                port_row.set_text(str(default_port))
            elif not f_port:
                port_row.set_text("")

        proto_row.connect("notify::selected", _update_fields)
        _update_fields()  # initial state

        # ── Connect action ────────────────────────────────────────────────
        def _do_connect(*_args):
            idx = proto_row.get_selected()
            _label, scheme, default_port, (f_share, f_folder, f_port, f_user, f_domain) = (
                self._PROTOCOLS[idx]
            )
            server = server_row.get_text().strip()
            if not server:
                status_label.set_label(_("Server address is required."))
                status_label.set_visible(True)
                return

            # Build the URI
            user_part = ""
            if f_user:
                user = user_row.get_text().strip()
                if f_domain:
                    domain = domain_row.get_text().strip()
                    if domain and user:
                        user_part = f"{domain};{user}@"
                    elif user:
                        user_part = f"{user}@"
                elif user:
                    user_part = f"{user}@"

            port_part = ""
            if f_port:
                port_text = port_row.get_text().strip()
                if port_text:
                    try:
                        port_num = int(port_text)
                        if port_num != default_port:
                            port_part = f":{port_num}"
                    except ValueError:
                        status_label.set_label(_("Invalid port number."))
                        status_label.set_visible(True)
                        return

            path_part = ""
            if f_share:
                share = share_row.get_text().strip().strip("/")
                folder = folder_row.get_text().strip().strip("/")
                if share:
                    path_part = f"/{share}"
                    if folder:
                        path_part += f"/{folder}"
            elif f_folder:
                folder = folder_row.get_text().strip().strip("/")
                if folder:
                    path_part = f"/{folder}"

            uri = f"{scheme}://{user_part}{server}{port_part}{path_part}"
            _log(f"connect-to-server: mounting {uri}")

            status_label.set_visible(False)
            connect_btn.set_sensitive(False)
            connect_btn.set_label(_("Connecting…"))

            gfile = Gio.File.new_for_uri(uri)
            op = Gtk.MountOperation.new(win)

            def _mount_done(source, result):
                try:
                    gfile.mount_enclosing_volume_finish(result)
                except GLib.Error as e:
                    if e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.ALREADY_MOUNTED):
                        _log(f"connect-to-server: already mounted, opening {uri}")
                    elif e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                        _log("connect-to-server: cancelled by user")
                        connect_btn.set_sensitive(True)
                        connect_btn.set_label(_("Connect"))
                        return
                    else:
                        _log(f"connect-to-server: mount failed: {e.message}")
                        status_label.set_label(str(e.message))
                        status_label.set_visible(True)
                        connect_btn.set_sensitive(True)
                        connect_btn.set_label(_("Connect"))
                        return

                dialog.close()
                self._schedule_live_refresh()
                GLib.idle_add(self._navigate_to, uri, win)

            gfile.mount_enclosing_volume(
                Gio.MountMountFlags.NONE, op, None, _mount_done
            )

        connect_btn.connect("clicked", _do_connect)
        # Also allow Enter in any entry row to trigger connect
        for entry in (server_row, share_row, folder_row, port_row, user_row, domain_row):
            entry.connect("entry-activated", _do_connect)

        dialog.present(win)

    def _on_disk_right_clicked(self, gesture, _n, x, y, win: Gtk.Window, row: Gtk.Box) -> None:
        mount_key = getattr(row, "_mount_key", None)
        m = _disk_data.get(mount_key) if mount_key else None
        nav_uri = getattr(row, "_nav_uri", "")
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)

        if not m:
            return

        is_mounted = m.is_mounted
        is_system = m.mountpoint == "/"
        device = m.device or ""
        if not device.startswith("/dev/") and m.gio_volume:
            unix_dev = m.gio_volume.get_identifier(Gio.VOLUME_IDENTIFIER_KIND_UNIX_DEVICE)
            if unix_dev:
                device = unix_dev

        def _accel_item(label, action, accel):
            item = Gio.MenuItem.new(label, action)
            item.set_attribute_value("accel", GLib.Variant("s", accel))
            return item

        menu = Gio.Menu()
        ag = Gio.SimpleActionGroup()

        # Section 1: open actions (all disks)
        open_sec = Gio.Menu()
        open_sec.append_item(_accel_item(_("Open"), "diskrow.open", "Return"))
        open_sec.append_item(
            _accel_item(_("Open in New Tab"), "diskrow.open-tab", "<Control>Return")
        )
        open_sec.append_item(
            _accel_item(_("Open in New Window"), "diskrow.open-window", "<Shift>Return")
        )
        menu.append_section(None, open_sec)

        open_act = Gio.SimpleAction.new("open", None)
        if is_mounted and nav_uri:
            open_act.connect("activate", lambda *_: self._do_open(nav_uri, win))
        else:
            open_act.connect("activate", lambda *_: self._do_mount_then_open(m, win, "current"))
        ag.add_action(open_act)
        tab_act = Gio.SimpleAction.new("open-tab", None)
        if is_mounted and nav_uri:
            tab_act.connect("activate", lambda *_: self._do_open_tab(nav_uri, win))
        else:
            tab_act.connect("activate", lambda *_: self._do_mount_then_open(m, win, "tab"))
        ag.add_action(tab_act)
        win_act = Gio.SimpleAction.new("open-window", None)
        if is_mounted and nav_uri:
            win_act.connect("activate", lambda *_: self._do_open_window(nav_uri))
        else:
            win_act.connect("activate", lambda *_: self._do_mount_then_open(m, win, "window"))
        ag.add_action(win_act)

        # Section 2: mount / unmount / eject + format (non-system only)
        if not is_system:
            mid_sec = Gio.Menu()
            if not is_mounted:
                mid_sec.append(_("Mount"), "diskrow.mount")
                mount_act = Gio.SimpleAction.new("mount", None)
                mount_act.connect("activate", lambda *_: self._do_mount(m, win))
                ag.add_action(mount_act)
            elif m.can_eject:
                mid_sec.append(_("Eject"), "diskrow.eject")
                eject_act = Gio.SimpleAction.new("eject", None)
                eject_act.connect("activate", lambda *_: self._do_eject(m))
                ag.add_action(eject_act)
            else:
                mid_sec.append(_("Unmount"), "diskrow.unmount")
                unmount_act = Gio.SimpleAction.new("unmount", None)
                unmount_act.connect("activate", lambda *_: self._do_unmount(m))
                ag.add_action(unmount_act)
            if device.startswith("/dev/"):
                mid_sec.append(_("Format…"), "diskrow.format")
                fmt_act = Gio.SimpleAction.new("format", None)
                fmt_act.connect("activate", lambda *_: self._do_format(device))
                ag.add_action(fmt_act)
            menu.append_section(None, mid_sec)

        # Section 3: properties (mounted disks only)
        if is_mounted and nav_uri:
            props_sec = Gio.Menu()
            props_sec.append_item(_accel_item(_("Properties"), "diskrow.props", "<Alt>Return"))
            menu.append_section(None, props_sec)
            props_act = Gio.SimpleAction.new("props", None)
            props_act.connect("activate", lambda *_: self._do_properties(nav_uri, win))
            ag.add_action(props_act)

        popover = Gtk.PopoverMenu.new_from_model(menu)
        popover.set_has_arrow(False)
        popover.set_parent(row)
        popover.insert_action_group("diskrow", ag)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        popover.set_pointing_to(rect)
        popover.popup()

    def _do_open(self, nav_uri: str, win: Gtk.Window) -> None:
        GLib.idle_add(self._navigate_to, nav_uri, win)

    def _do_open_tab(self, nav_uri: str, win: Gtk.Window) -> None:
        uri = nav_uri

        tab_view = next(
            (w for w in _all_widgets(win) if isinstance(w, Adw.TabView)),
            None,
        )
        pages_before = tab_view.get_n_pages() if tab_view else 0

        # Switch to the files view first — new-tab action requires the TabView to be visible.
        state = self._windows.get(win)
        if state and self._set_visible_view(state, VIEW_FILES, "open_tab show files"):
            self._stop_usage_poll_if_idle()

        attempt = [0]

        def _fire_and_wait():
            Gio.ActionGroup.activate_action(win, "new-tab", None)

            def _wait_for_tab():
                n = tab_view.get_n_pages() if tab_view else 0
                if not (tab_view and n > pages_before):
                    attempt[0] += 1
                    if attempt[0] >= 20:
                        return GLib.SOURCE_REMOVE
                    return GLib.SOURCE_CONTINUE

                # Navigate by index, not selected page — avoids racing with
                # concurrent rapid tab-opens that share the same pages_before.
                page = tab_view.get_nth_page(pages_before)
                if page:
                    slot = page.get_child()
                    if slot and slot.activate_action("slot.open-location", GLib.Variant("s", uri)):
                        return GLib.SOURCE_REMOVE

                attempt[0] += 1
                if attempt[0] >= 40:
                    return GLib.SOURCE_REMOVE
                return GLib.SOURCE_CONTINUE

            GLib.timeout_add(_TAB_WAIT_MS, _wait_for_tab)
            return GLib.SOURCE_REMOVE

        GLib.idle_add(_fire_and_wait)

    def _do_open_window(self, mountpoint: str) -> None:
        subprocess.Popen(["nautilus", "--new-window", mountpoint])

    def _do_mount(self, m: MountInfo, win: Gtk.Window) -> None:
        if not m or not m.gio_volume:
            return
        op = Gio.MountOperation.new()
        m.gio_volume.mount(Gio.MountMountFlags.NONE, op, None, self._on_mount_finish, win)

    def _on_mount_finish(self, volume, result, win) -> None:
        try:
            volume.mount_finish(result)
        except GLib.Error as e:
            _log(f"mount failed: {e.message}")
        GLib.idle_add(self._repopulate_visible)

    def _do_mount_then_open(self, m: MountInfo, win: Gtk.Window, mode: str) -> None:
        if not m or not m.gio_volume:
            return
        op = Gio.MountOperation.new()
        op.set_password_save(Gio.PasswordSave.NEVER)
        m.gio_volume.mount(
            Gio.MountMountFlags.NONE, op, None, self._on_mount_then_open_finish, (win, mode)
        )

    def _on_mount_then_open_finish(self, volume, result, user_data) -> None:
        win, mode = user_data
        try:
            volume.mount_finish(result)
        except GLib.Error as e:
            _log(f"mount-then-open failed: {e.message}")
            GLib.idle_add(self._repopulate_visible)
            return
        mount = volume.get_mount()
        if not mount:
            GLib.idle_add(self._repopulate_visible)
            return
        uri = mount.get_root().get_uri()
        GLib.idle_add(self._repopulate_visible)
        if mode == "tab":
            GLib.idle_add(self._do_open_tab, uri, win)
        elif mode == "window":
            GLib.idle_add(self._do_open_window, uri)
        else:
            GLib.idle_add(self._do_open, uri, win)

    def _do_unmount(self, m: MountInfo) -> None:
        if not m or not m.gio_mount:
            return
        op = Gio.MountOperation.new()
        m.gio_mount.unmount_with_operation(
            Gio.MountUnmountFlags.NONE, op, None, self._on_unmount_finish
        )

    def _on_unmount_finish(self, mount, result) -> None:
        try:
            mount.unmount_with_operation_finish(result)
        except GLib.Error as e:
            _log(f"unmount failed: {e.message}")
        GLib.idle_add(self._repopulate_visible)

    def _do_eject(self, m: MountInfo) -> None:
        if not m:
            return
        op = Gio.MountOperation.new()
        if m.gio_volume and m.gio_volume.can_eject():
            m.gio_volume.eject_with_operation(
                Gio.MountUnmountFlags.NONE, op, None, self._on_eject_finish
            )
        elif m.gio_mount and m.gio_mount.can_eject():
            m.gio_mount.eject_with_operation(
                Gio.MountUnmountFlags.NONE, op, None, self._on_eject_finish
            )

    def _on_eject_finish(self, source, result) -> None:
        try:
            source.eject_with_operation_finish(result)
        except GLib.Error as e:
            _log(f"eject failed: {e.message}")
        GLib.idle_add(self._repopulate_visible)

    def _do_format(self, device: str) -> None:
        try:
            Gio.Subprocess.new(
                ["gnome-disks", "--block-device", device, "--format-device"],
                Gio.SubprocessFlags.NONE,
            )
        except GLib.Error as e:
            _log(f"format launch failed: {e.message}")

    def _do_properties(self, nav_uri: str, win: Gtk.Window) -> None:
        uri = nav_uri

        # The native properties window is created in-process by Nautilus via the
        # D-Bus ShowItemProperties call. It is NOT registered with the
        # GtkApplication, so "window-added" never fires — we must poll
        # list_toplevels() to find it. Once found, set it transient-for our
        # window and modal so the compositor visually binds it to the parent
        # (centered, above, moves/closes with it) instead of floating free.
        before_ids = {id(w) for w in Gtk.Window.list_toplevels()}
        state = {"done": False}

        def _try_parent(attempt=0):
            if state["done"]:
                return GLib.SOURCE_REMOVE
            for w in Gtk.Window.list_toplevels():
                if id(w) not in before_ids and w is not win:
                    w.set_transient_for(win)
                    w.set_modal(True)
                    state["done"] = True
                    return GLib.SOURCE_REMOVE
            if attempt < 40:
                GLib.timeout_add(25, lambda: _try_parent(attempt + 1))
            return GLib.SOURCE_REMOVE

        def _on_call(bus, result, _):
            try:
                bus.call_finish(result)
            except Exception:
                pass

        def _on_bus(_, result):
            try:
                bus = Gio.bus_get_finish(result)
                bus.call(
                    DBUS_FILE_MANAGER,
                    DBUS_PATH_FILE_MANAGER,
                    DBUS_FILE_MANAGER,
                    "ShowItemProperties",
                    GLib.Variant("(ass)", ([uri], "")),
                    None,
                    Gio.DBusCallFlags.NONE,
                    5000,
                    None,
                    _on_call,
                    None,
                )
            except Exception:
                pass

        # Start polling immediately so we catch the window as early as possible.
        _try_parent()
        Gio.bus_get(Gio.BusType.SESSION, None, _on_bus)

    def _launch_prefs(self, win: Gtk.Window | None = None) -> None:
        if not self._gsettings:
            return

        pref_win = Adw.PreferencesDialog()
        pref_win.set_title(PREFS_WIN_TITLE)
        pref_win.set_search_enabled(False)

        page = Adw.PreferencesPage()
        pref_win.add(page)

        gen_group = Adw.PreferencesGroup()
        gen_group.set_title(_("General"))
        page.add(gen_group)

        start_row = Adw.SwitchRow()
        start_row.set_title(_("Start on the Computer view"))
        start_row.set_subtitle(_("Open Nautilus directly to the Computer view instead of Home"))
        self._gsettings.bind("start-on-disks", start_row, "active", Gio.SettingsBindFlags.DEFAULT)
        gen_group.add(start_row)

        vis_group = Adw.PreferencesGroup()
        vis_group.set_title(_("Visibility"))
        vis_group.set_description(
            _(
                "Choose how each group appears. "
                "Visible: shows the group as a normal separated section. "
                "Merged: folds the group into the On this Computer group. "
                "Hidden: hides the group entirely."
            )
        )
        page.add(vis_group)

        _vis_map = ["visible", "merged", "hidden"]
        _vis_labels = [_("Visible"), _("Merged"), _("Hidden")]

        for gkey, glabel, gskey in _GROUP_SPEC:
            if gskey is None:
                continue  # "On this Computer" is always visible -- no control needed

            combo = Adw.ComboRow()
            combo.set_title(_(glabel))
            combo.set_model(Gtk.StringList.new(_vis_labels))
            current = self._gsettings.get_string(gskey)
            combo.set_selected(_vis_map.index(current) if current in _vis_map else 0)

            def _on_vis_changed(c, _param, _gskey=gskey):
                idx = c.get_selected()
                if 0 <= idx < len(_vis_map):
                    self._gsettings.set_string(_gskey, _vis_map[idx])

            combo.connect("notify::selected", _on_vis_changed)
            vis_group.add(combo)

        show_sys_parts_row = Adw.SwitchRow()
        show_sys_parts_row.set_title(_("Show system partitions"))
        show_sys_parts_row.set_subtitle(_("Include boot and EFI partitions in the System group"))
        self._gsettings.bind(
            "show-system-partitions", show_sys_parts_row, "active", Gio.SettingsBindFlags.DEFAULT
        )
        vis_group.add(show_sys_parts_row)

        color_group = Adw.PreferencesGroup()
        color_group.set_title(_("Usage Bar Color"))
        color_group.set_description(_("Select or customize the usage bar color."))
        page.add(color_group)

        mode_row = Adw.ComboRow()
        mode_row.set_title(_("Color mode"))
        mode_model = Gtk.StringList.new([_("System accent"), _("Custom color"), _("Gradient")])
        mode_row.set_model(mode_model)
        _mode_map = ["accent", "flat", "gradient"]
        current_mode = self._gsettings.get_string("color-mode")
        mode_row.set_selected(_mode_map.index(current_mode) if current_mode in _mode_map else 0)
        color_group.add(mode_row)

        color_dialog = Gtk.ColorDialog()
        color_dialog.set_with_alpha(False)

        def _hex_to_rgba(hex_str: str) -> Gdk.RGBA:
            rgba = Gdk.RGBA()
            rgba.parse(hex_str)
            return rgba

        def _rgba_to_hex(rgba: Gdk.RGBA) -> str:
            r = int(rgba.red * 255)
            g = int(rgba.green * 255)
            b = int(rgba.blue * 255)
            return f"#{r:02X}{g:02X}{b:02X}"

        flat_row = Adw.ActionRow()
        flat_row.set_title(_("Color"))
        flat_btn = Gtk.ColorDialogButton(dialog=color_dialog)
        flat_btn.set_valign(Gtk.Align.CENTER)
        flat_btn.set_rgba(_hex_to_rgba(self._gsettings.get_string("custom-color")))
        flat_btn.connect(
            "notify::rgba",
            lambda btn, _: self._gsettings.set_string("custom-color", _rgba_to_hex(btn.get_rgba())),
        )
        flat_row.add_suffix(flat_btn)
        color_group.add(flat_row)

        grad_row1 = Adw.ActionRow()
        grad_row1.set_title(_("Start color"))
        grad_btn1 = Gtk.ColorDialogButton(dialog=color_dialog)
        grad_btn1.set_valign(Gtk.Align.CENTER)
        grad_btn1.set_rgba(_hex_to_rgba(self._gsettings.get_string("gradient-color-1")))
        grad_btn1.connect(
            "notify::rgba",
            lambda btn, _: self._gsettings.set_string(
                "gradient-color-1", _rgba_to_hex(btn.get_rgba())
            ),
        )
        grad_row1.add_suffix(grad_btn1)
        color_group.add(grad_row1)

        grad_row2 = Adw.ActionRow()
        grad_row2.set_title(_("End color"))
        grad_btn2 = Gtk.ColorDialogButton(dialog=color_dialog)
        grad_btn2.set_valign(Gtk.Align.CENTER)
        grad_btn2.set_rgba(_hex_to_rgba(self._gsettings.get_string("gradient-color-2")))
        grad_btn2.connect(
            "notify::rgba",
            lambda btn, _: self._gsettings.set_string(
                "gradient-color-2", _rgba_to_hex(btn.get_rgba())
            ),
        )
        grad_row2.add_suffix(grad_btn2)
        color_group.add(grad_row2)

        def _update_color_rows(selected: int) -> None:
            flat_row.set_visible(selected == 1)
            grad_row1.set_visible(selected == 2)
            grad_row2.set_visible(selected == 2)

        def _on_mode_changed(row, _) -> None:
            idx = row.get_selected()
            self._gsettings.set_string("color-mode", _mode_map[idx])
            _update_color_rows(idx)

        mode_row.connect("notify::selected", _on_mode_changed)
        _update_color_rows(mode_row.get_selected())

        about_group = Adw.PreferencesGroup()
        about_group.set_title(_("About"))
        page.add(about_group)

        def _about_row(title: str, value: str) -> Adw.ActionRow:
            row = Adw.ActionRow()
            row.set_title(title)
            lbl = Gtk.Label(label=value)
            lbl.get_style_context().add_class("dim-label")
            lbl.set_valign(Gtk.Align.CENTER)
            row.add_suffix(lbl)
            return row

        about_group.add(_about_row(_("Extension"), EXT_NAME))
        about_group.add(_about_row(_("Version"), EXT_VERSION))
        about_group.add(_about_row(_("Author"), EXT_AUTHOR))
        about_group.add(_about_row(_("License"), EXT_LICENSE))

        github_row = Adw.ActionRow()
        github_row.set_title(_("Source code"))
        github_btn = Gtk.LinkButton(uri=EXT_GITHUB, label=_("GitHub ↗"))
        github_btn.get_style_context().add_class("flat")
        github_btn.set_valign(Gtk.Align.CENTER)
        github_row.add_suffix(github_btn)
        about_group.add(github_row)

        pref_win.present(win)

    def _navigate_to_disks(self, win: Gtk.Window) -> None:
        """Navigate a window to computer:/// at startup, retrying until the slot
        is ready. The slot often isn't navigable the instant the window settles
        on Home, so a single open-location call silently no-ops; we retry on a
        short bounded poll and stop as soon as the location actually changes.
        While awaiting arrival the panel stays pinned (see _on_title_changed)."""
        state = self._windows.get(win)
        if state is not None:
            state["awaiting_disks"] = True
            self._blank_vanilla_view(state, True)

        attempts = [0]

        def _try() -> bool:
            st = self._windows.get(win)
            if st is None:
                return GLib.SOURCE_REMOVE
            if _LOCATION_TITLE in (win.get_title() or ""):
                st["awaiting_disks"] = False  # arrived
                return GLib.SOURCE_REMOVE
            attempts[0] += 1
            if attempts[0] > 25:  # ~1.5 s budget, then give up
                st["awaiting_disks"] = False
                self._blank_vanilla_view(st, False)
                return GLib.SOURCE_REMOVE
            self._navigate_to(DISKS_URI, win)
            return GLib.SOURCE_CONTINUE

        GLib.timeout_add(_NAV_RETRY_MS, _try)

    def _navigate_to(self, uri: str, win: Gtk.Window) -> bool:
        state = self._windows.get(win) if uri == DISKS_URI else None

        def _arm_blank() -> None:
            if state is not None:
                self._blank_vanilla_view(state, True)

        for w in _all_widgets(win):
            if "Slot" in type(w).__name__:
                try:
                    if w.activate_action("open-location", GLib.Variant("s", uri)):
                        _arm_blank()
                        return False
                except Exception:
                    pass
        try:
            if win.activate_action("slot.open-location", GLib.Variant("s", uri)):
                _arm_blank()
                return False
        except Exception:
            pass

        def _on_proxy(_, result):
            try:
                proxy = Gio.DBusProxy.new_for_bus_finish(result)
                proxy.call(
                    "ShowFolders",
                    GLib.Variant("(ass)", ([uri], "")),
                    Gio.DBusCallFlags.NONE,
                    -1,
                    None,
                    None,
                )
            except Exception:
                pass

        Gio.DBusProxy.new_for_bus(
            Gio.BusType.SESSION,
            Gio.DBusProxyFlags.NONE,
            None,
            DBUS_FILE_MANAGER,
            DBUS_PATH_FILE_MANAGER,
            DBUS_FILE_MANAGER,
            None,
            _on_proxy,
        )
        return False

    # ── Chrome icon fix (path bar chip) ─────────────────────────────────────

    def _inject_sidebar_link(self, win: Gtk.Window) -> bool:
        """Prototype: inject a dummy 'My Computer' row above NautilusSidebar.

        Wraps NautilusSidebar in a vertical GtkBox (our_row + NautilusSidebar) and
        sets that as the AdwToolbarView content. Our row is a direct sibling of
        NautilusSidebar, outside the GtkListBox that update_places() clears.
        One-time injection per window; no re-injection needed.
        """
        split_view = next(
            (w for w in _all_widgets(win) if isinstance(w, Adw.OverlaySplitView)), None
        )
        sidebar_toolbar = split_view.get_sidebar() if split_view else None
        if not isinstance(sidebar_toolbar, Adw.ToolbarView):
            _log(
                f"_inject_sidebar_link: expected AdwToolbarView from get_sidebar(), "
                f"got {type(sidebar_toolbar).__name__ if sidebar_toolbar else 'None'}"
            )
            return False

        nautilus_sidebar = sidebar_toolbar.get_content()
        if nautilus_sidebar is None:
            _log("_inject_sidebar_link: AdwToolbarView content is None")
            return False

        _log(f"_inject_sidebar_link: content={type(nautilus_sidebar).__name__}")

        # Try to instantiate NautilusSidebarRow directly from the Nautilus GObject
        # type system. It is registered at runtime when Nautilus loads, so
        # GObject.type_from_name() can find it. uri is construct-only.
        our_listbox = Gtk.ListBox()
        our_listbox.set_name("my_computer_list")
        our_listbox.add_css_class("navigation-sidebar")
        our_listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)

        list_row = None
        try:
            row_gtype = GObject.type_from_name("NautilusSidebarRow")
            list_row = GObject.new(
                row_gtype,
                **{
                    "uri": DISKS_URI,
                    "place-type": 1,  # NAUTILUS_SIDEBAR_ROW_BUILT_IN
                    "section-type": 1,  # NAUTILUS_SIDEBAR_SECTION_DEFAULT_LOCATIONS
                },
            )
            list_row.set_name("my_computer")
            list_row.set_property("label", _LOCATION_TITLE)
            list_row.set_property("eject-tooltip", _("Unmount"))
            list_row.set_has_tooltip(True)
            list_row.set_tooltip_text(_("Open My Computer"))
            _log(f"_inject_sidebar_link: NautilusSidebarRow created ✓ (uri={DISKS_URI})")
        except Exception as e:
            _log(f"_inject_sidebar_link: NautilusSidebarRow unavailable ({e}), using GtkListBoxRow")

        if list_row is None:
            list_row = Gtk.ListBoxRow()
            list_row.set_name("my_computer")
            # No revealer: box is a direct child of row so CSS `row > box` works.
            # libadwaita's rule targets `row > revealer`; we replicate the inset
            # via `#my_computer_list > row { padding: 0 14px }` in _CSS instead.
            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
            row_box.set_name("my_computer_box")
            icon = Gtk.Image.new_from_icon_name(COMPUTER_ICON)
            icon.set_name("my_computer_icon")
            icon.set_icon_size(Gtk.IconSize.NORMAL)
            label = Gtk.Label(label=_LOCATION_TITLE)
            label.set_name("my_computer_label")
            label.set_xalign(0.0)
            label.set_hexpand(True)
            label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            row_box.append(icon)
            row_box.append(label)
            list_row.set_child(row_box)

        our_listbox.append(list_row)
        our_listbox.connect(
            "row-activated",
            lambda _lb, _row: self._navigate_to(DISKS_URI, win),
        )

        def _pin_row_icon():
            for w in _all_widgets(list_row):
                if not isinstance(w, Gtk.Image):
                    continue
                parent = w.get_parent()
                in_button = False
                while parent and parent is not list_row:
                    if isinstance(parent, Gtk.Button):
                        in_button = True
                        break
                    parent = parent.get_parent()
                if not in_button:
                    _pin_icon(w, COMPUTER_ICON)
                    break
            return GLib.SOURCE_REMOVE

        GLib.idle_add(_pin_row_icon)

        def _on_computer_right_clicked(gesture, _n, x, y):
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)

            on_computer = self._windows.get(win, {}).get("visible_view") == VIEW_DISKINFO

            menu = Gio.Menu()
            ag = Gio.SimpleActionGroup()

            primary = Gio.Menu()
            primary.append(_("Open"), "comprow.open")
            primary.append(_("Open in New Tab"), "comprow.open-tab")
            primary.append(_("Open in New Window"), "comprow.open-window")
            menu.append_section(None, primary)

            settings_section = Gio.Menu()
            settings_section.append(MENU_ITEM_LABEL, "comprow.settings")
            menu.append_section(None, settings_section)

            open_act = Gio.SimpleAction.new("open", None)
            open_act.set_enabled(not on_computer)
            open_act.connect("activate", lambda *_: self._do_open(DISKS_URI, win))
            ag.add_action(open_act)

            tab_act = Gio.SimpleAction.new("open-tab", None)
            tab_act.connect("activate", lambda *_: self._do_open_tab(DISKS_URI, win))
            ag.add_action(tab_act)

            win_act = Gio.SimpleAction.new("open-window", None)
            win_act.connect("activate", lambda *_: self._do_open_window(DISKS_URI))
            ag.add_action(win_act)

            settings_act = Gio.SimpleAction.new("settings", None)
            settings_act.connect("activate", lambda *_: self._launch_prefs(win))
            ag.add_action(settings_act)

            list_row.insert_action_group("comprow", ag)

            popover = Gtk.PopoverMenu.new_from_model(menu)
            popover.set_has_arrow(False)
            popover.set_parent(list_row)
            rect = Gdk.Rectangle()
            rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
            popover.set_pointing_to(rect)
            popover.popup()

        right_click = Gtk.GestureClick()
        right_click.set_button(3)
        right_click.connect("pressed", _on_computer_right_clicked)
        our_listbox.add_controller(right_click)

        # Hide the eject button — not applicable for the Computer entry.
        btn = _find_widget(list_row, buildable_id="eject_button")
        if isinstance(btn, Gtk.Button):
            btn.set_visible(False)

        wrapper = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        outer_scroll = Gtk.ScrolledWindow()
        outer_scroll.set_vexpand(True)
        outer_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        outer_scroll.set_child(wrapper)

        # set_content() unparents nautilus_sidebar; then we can append it to wrapper.
        sidebar_toolbar.set_content(outer_scroll)
        wrapper.append(our_listbox)
        wrapper.append(nautilus_sidebar)

        # Disable NautilusSidebar's own scroll so it expands to full height
        # and our outer_scroll drives all scrolling.
        native_listbox = None
        for w in _all_widgets(nautilus_sidebar):
            if isinstance(w, Gtk.ScrolledWindow):
                w.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.NEVER)
                w.set_margin_top(0)
                _log("_inject_sidebar_link: inner scroll disabled ✓")
            elif isinstance(w, Gtk.ListBox):
                native_listbox = w

        nautilus_sidebar.set_margin_top(0)
        if native_listbox is not None:
            native_listbox.add_css_class("places-sidebar-list")

        # Cross-deselect: selecting in one listbox clears the other so only
        # one row appears selected across both groups at any time.
        if native_listbox is not None:
            our_listbox.connect(
                "row-selected",
                lambda _lb, row: native_listbox.unselect_all() if row else None,
            )
            native_listbox.connect(
                "row-selected",
                lambda _lb, row: our_listbox.unselect_all() if row else None,
            )
            _log("_inject_sidebar_link: cross-deselect wired ✓")

        state = self._windows.get(win)
        if state is not None:
            state["sidebar_listbox"] = our_listbox
            state["sidebar_row"] = list_row

        _log("_inject_sidebar_link: outer scroll wrapper set as content ✓")
        return True

    def _fix_pathbar_icon(self, win: Gtk.Window) -> bool:
        """Non-invasive chip icon update. Called on each title-change arrival at
        computer:///. Scans the window for the chip label, finds the existing
        Gtk.Image in the chip, and pins it to computer-symbolic.

        Never connects signals to Nautilus's internal pathbar GtkStack or box
        models, and never calls set_child() — those caused the GTK_IS_STACK crash
        (issue #11). The notify::title trigger already fires on every navigation,
        so no persistent watcher is needed."""
        target_labels = {COMPUTER_LABEL, _LOCATION_TITLE}

        for w in _all_widgets(win):
            if not isinstance(w, Gtk.Label):
                continue
            label_text = w.get_label()
            if not label_text or label_text.strip() not in target_labels:
                continue

            # Skip labels inside the sidebar
            ancestor = w.get_parent()
            in_sidebar = False
            while ancestor:
                cls = type(ancestor).__name__
                if "Sidebar" in cls or "PlacesView" in cls:
                    in_sidebar = True
                    break
                if cls in ("NautilusPathBarButton", "GtkButton", "AdwButton"):
                    break
                ancestor = ancestor.get_parent()
            if in_sidebar:
                continue

            # Walk up to the chip container
            container = w.get_parent()
            while container and type(container).__name__ not in (
                "NautilusPathBarButton",
                "GtkButton",
                "GtkBox",
                "Button",
                "Box",
            ):
                container = container.get_parent()

            if not container:
                continue

            # Pin the existing chip image — no structural changes to Nautilus's tree
            for sub in _all_widgets(container):
                if isinstance(sub, Gtk.Image):
                    _pin_icon(sub, COMPUTER_ICON)
                    break

        return False

    # ── MenuProvider (required stub) ──────────────────────────────────────────

    def get_file_items(self, *args):
        return []

    def get_background_items(self, *args):
        return []
