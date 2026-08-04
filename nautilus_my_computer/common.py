"""Stateless leaf utilities shared by main.py, target modules, and widgets.py.

No app state, no GSettings here -- only pure functions and constants so this
module can be imported from anywhere without import cycles. Includes the
generic native-widget/menu-model primitives (tree walking, menu-section
lookup, icon pinning) used by every native-UI injection target.
"""

import gettext
import os
from xml.etree import ElementTree

import gi

gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
gi.require_version("GObject", "2.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk

_custom_translation = None
for _localedir in (os.path.expanduser("~/.local/share/locale"), None):
    try:
        _custom_translation = gettext.translation("nautilus-my-computer", localedir=_localedir)
        break
    except Exception:
        continue

# Ordered so Nautilus's own file-manager wording wins on any msgid gtk40 or gvfs
# also happen to translate (e.g. "Open", "Properties").
_PLATFORM_DOMAINS = ("nautilus", "gtk40", "gvfs")

_platform_translations = []
for _domain in _PLATFORM_DOMAINS:
    try:
        _platform_translations.append(gettext.translation(_domain))
    except Exception:
        continue


def _(text: str) -> str:
    if _custom_translation is not None:
        val = _custom_translation.gettext(text)
        if val != text:
            return val
    for _translation in _platform_translations:
        val = _translation.gettext(text)
        if val != text:
            return val
    return text


def _native(text: str) -> str:
    """Translate via Nautilus/GTK/gvfs's own gettext domains only, ignoring our
    po files. For labels that duplicate a concept the platform's native UI already
    names (Home/Recent/Starred/Network/Cut/Copy/Paste...), this guarantees the exact
    same wording as the rest of the desktop in every language, rather than risking a
    differently worded translation from our own translators (issue #64, #120)."""
    for _translation in _platform_translations:
        val = _translation.gettext(text)
        if val != text:
            return val
    return text


def N_(text: str) -> str:
    """No-op marker for module-level string tables (e.g. _GROUP_SPEC, _SEGMENTS)
    whose entries are translated later via _(variable) at render time, a form
    xgettext cannot see. Wrapping the literal here makes it extractable while
    leaving the value unchanged; add --keyword=N_ wherever xgettext runs."""
    return text


def _n(singular: str, plural: str, n: int) -> str:
    if _custom_translation is not None:
        val = _custom_translation.ngettext(singular, plural, n)
        if val != (singular if n == 1 else plural):
            return val
    for _translation in _platform_translations:
        val = _translation.ngettext(singular, plural, n)
        if val != (singular if n == 1 else plural):
            return val
    return singular if n == 1 else plural


def _format_size(n: float) -> str:
    return GLib.format_size(int(n))


def _format_item_count(n: int) -> str:
    return _n("{n} item", "{n} items", n).format(n=n)


def _format_permissions(mode: int) -> str:
    """POSIX rwx string (e.g. "rwxr-xr-x") from a unix::mode value."""
    perm = mode & 0o777
    chars = "rwx"
    return "".join(chars[i % 3] if perm & (1 << (8 - i)) else "-" for i in range(9))


# ── Relative date formatting (replica of Nautilus's nautilus_date_to_str) ────
#
# Nautilus renders file dates as "Today, 4:45 PM", "Yesterday", "Last month",
# "3 months ago", ... rather than an absolute timestamp. That formatting lives
# in src/nautilus-date-utilities.c (nautilus_date_to_str), which is a thin
# wrapper around ICU's URelativeDateTimeFormatter (libicu). ICU has no
# GObject-introspection binding, so the extension cannot call that function --
# this is a pure-Python re-creation of its behaviour: same day/week/month/year
# bucketing thresholds, same midnight-boundary day math, same "append the time
# only within +/-2 days" rule, and the same two GSettings inputs Nautilus reads
# (clock-format for 12h/24h, date-time-format for the detailed/simple toggle).
# Locale wording comes from our own gettext catalog instead of ICU's CLDR data,
# so it is not guaranteed pixel-identical in every language, but matches the
# English forms and overall shape. Like Nautilus's own ICU fallback path, it
# falls back to an absolute date if anything goes wrong.
#
# The two settings handles are cached at module scope, mirroring
# nautilus-date-utilities.c's file-static use_24_hour / use_detailed_date_format
# statics. They are read live on each call so a settings change is reflected
# without needing a "changed" subscription.
_interface_settings = None
_nautilus_date_settings = None


def _date_prefs() -> tuple[bool, bool]:
    """(use_24_hour, use_detailed) from the same GSettings keys Nautilus reads."""
    global _interface_settings, _nautilus_date_settings
    use_24_hour = False
    use_detailed = False
    try:
        if _interface_settings is None:
            _interface_settings = Gio.Settings.new("org.gnome.desktop.interface")
        use_24_hour = _interface_settings.get_string("clock-format") == "24h"
    except Exception:
        pass
    try:
        if _nautilus_date_settings is None:
            _nautilus_date_settings = Gio.Settings.new("org.gnome.nautilus.preferences")
        use_detailed = _nautilus_date_settings.get_string("date-time-format") == "detailed"
    except Exception:
        pass
    return use_24_hour, use_detailed


def _format_time_of_day(dt, use_24_hour: bool) -> str:
    if use_24_hour:
        return dt.format("%H:%M")
    # "%I:%M %p" -> "04:45 PM"; strip the leading zero to match ICU/Nautilus ("4:45 PM").
    return dt.format("%I:%M %p").lstrip("0")


def _relative_date_string(unit: str, offset: int) -> str:
    """One relative-date phrase for (unit, offset), where offset is negative for
    the past (e.g. unit="month", offset=-3 -> "3 months ago"). Mirrors the
    strings ICU's URelativeDateTimeFormatter produces at UDAT_STYLE_LONG."""
    n = -offset  # positive magnitude
    if unit == "day":
        if offset == 0:
            return _("Today")
        if offset == -1:
            return _("Yesterday")
        if offset == 1:
            return _("Tomorrow")
        if offset < 0:
            return _n("{n} day ago", "{n} days ago", n).format(n=n)
        return _n("In {n} day", "In {n} days", offset).format(n=offset)
    if unit == "week":
        if offset == 0:
            return _("This week")
        if offset == -1:
            return _("Last week")
        if offset == 1:
            return _("Next week")
        if offset < 0:
            return _n("{n} week ago", "{n} weeks ago", n).format(n=n)
        return _n("In {n} week", "In {n} weeks", offset).format(n=offset)
    if unit == "month":
        if offset == 0:
            return _("This month")
        if offset == -1:
            return _("Last month")
        if offset == 1:
            return _("Next month")
        if offset < 0:
            return _n("{n} month ago", "{n} months ago", n).format(n=n)
        return _n("In {n} month", "In {n} months", offset).format(n=offset)
    # year
    if offset == 0:
        return _("This year")
    if offset == -1:
        return _("Last year")
    if offset == 1:
        return _("Next year")
    if offset < 0:
        return _n("{n} year ago", "{n} years ago", n).format(n=n)
    return _n("In {n} year", "In {n} years", offset).format(n=offset)


def _mc_date_to_str(unix_time: int) -> str:
    """Relative, localized date string for a unix timestamp, matching how
    Nautilus renders file dates ("Today, 4:45 PM", "Last month", "3 months
    ago", ...). Re-creation of nautilus_date_to_str(); see the block comment
    above. Returns "" for a zero/missing timestamp (same as an unknown date)."""
    if not unix_time:
        return ""
    timestamp = GLib.DateTime.new_from_unix_local(unix_time)
    if timestamp is None:
        return ""
    use_24_hour, use_detailed = _date_prefs()

    # Detailed mode: Nautilus shows an absolute date+time (with seconds), never
    # a relative phrase (nautilus-date-utilities.c: the relative branch is gated
    # on `!detailed_date`).
    if use_detailed:
        time_part = (
            timestamp.format("%H:%M:%S")
            if use_24_hour
            else timestamp.format("%I:%M:%S %p").lstrip("0")
        )
        return f"{timestamp.format('%x')}, {time_part}"

    now = GLib.DateTime.new_now_local()
    today_midnight = GLib.DateTime.new_local(
        now.get_year(), now.get_month(), now.get_day_of_month(), 0, 0, 0
    )
    date_midnight = GLib.DateTime.new_local(
        timestamp.get_year(), timestamp.get_month(), timestamp.get_day_of_month(), 0, 0, 0
    )
    if today_midnight is None or date_midnight is None:
        return timestamp.format("%x")

    # Positive = in the past. Whole days, since both ends are snapped to midnight.
    midnight_diff = today_midnight.difference(date_midnight)  # microseconds (GTimeSpan)
    relative_value = midnight_diff / GLib.TIME_SPAN_DAY

    # Same bucketing thresholds and divisors as ICU's get_relative_day_month_year.
    if relative_value < 7.0:
        unit = "day"
    elif relative_value < 31:
        unit = "week"
        relative_value /= 7.0
    elif relative_value < 365:
        unit = "month"
        relative_value /= 30.4
    else:
        unit = "year"
        relative_value /= 365.25
    offset = int(-relative_value)  # truncate toward zero; negative = past

    relative = _relative_date_string(unit, offset)

    # Append the time-of-day only for Today/Yesterday/Tomorrow, matching
    # Nautilus's `add_time = with_time && |midnight_difference| < 2 days`.
    if abs(midnight_diff) < 2 * GLib.TIME_SPAN_DAY:
        return f"{relative}, {_format_time_of_day(timestamp, use_24_hour)}"
    return relative


def _is_activating_click(ext, n_press: int) -> bool:
    """True if a Gtk.GestureClick "pressed" event (n_press) should activate,
    given Nautilus' own click-policy setting (ext._nautilus_prefs.click_policy,
    'single' or 'double').

    Only needed for raw GestureClick wiring on plain widgets that have no
    built-in "activate-on-single-click" (Gtk.FlowBox/Gtk.ListBox already expose
    that as a widget property -- see widgets.py's flow.set_activate_on_single_click
    calls, which don't need this helper)."""
    single_click = ext._nautilus_prefs.click_policy == "single"
    return (single_click and n_press == 1) or (not single_click and n_press == 2)


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


def _icon_name_renders(icon_name: str) -> bool:
    """True if icon_name resolves in the current icon theme."""
    try:
        theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
    except Exception:
        return True
    return theme.has_icon(icon_name)


_ICONS_DIR = os.path.join(os.path.dirname(__file__), "icons")


def _bundled_gicon(icon_name: str) -> Gio.FileIcon | None:
    """Gio.FileIcon for an SVG bundled under nautilus_my_computer/icons/, for
    use when no installed icon theme has icon_name. The file must be named
    "*-symbolic.svg" -- GtkIconPaintable.is_symbolic() is true for any
    *-symbolic.svg loaded from a plain file, so GTK repaints it with the
    widget's foreground color like any other symbolic icon (no light/dark
    handling needed here)."""
    path = os.path.join(_ICONS_DIR, f"{icon_name}.svg")
    if not os.path.exists(path):
        return None
    return Gio.FileIcon.new(Gio.File.new_for_path(path))


def _resolve_custom_gicon(info: Gio.FileInfo) -> Gio.Icon | None:
    """Mirrors Nautilus's own get_custom_icon() precedence (nautilus-file.c):
    metadata::custom-icon (a URI) then metadata::custom-icon-name, ahead of
    the regular content-type icon. Neither key is folded into GIO's
    standard::icon attribute (confirmed via `gio info`), so callers must
    request both explicitly alongside standard::icon.
    """
    uri = info.get_attribute_as_string("metadata::custom-icon")
    if uri:
        return Gio.FileIcon.new(Gio.File.new_for_uri(uri))
    name = info.get_attribute_as_string("metadata::custom-icon-name")
    if name:
        return Gio.ThemedIcon.new_with_default_fallbacks(name)
    return None


def _uri_is_hidden(uri: str) -> bool:
    """True if the location's standard::is-hidden attribute is set.

    Local stat only -- callers must not use this on a GVfs/network URI without
    a local FUSE path, since query_info() on those can block on the network.
    """
    if not uri:
        return False
    try:
        info = Gio.File.new_for_uri(uri).query_info(
            "standard::is-hidden", Gio.FileQueryInfoFlags.NONE, None
        )
        return info.get_attribute_boolean("standard::is-hidden")
    except GLib.Error:
        return False


DEBUG_LOG = os.environ.get("DEBUG", "").lower() in ("1", "true", "yes")
DEBUG_LOG_PREFIX = "MyComputer"  # prefix for all debug lines, to make them easy to filter in logs


def _log(msg: str) -> None:
    """Print a prefixed debug line. Set DEBUG_LOG = False to silence all logs."""
    if DEBUG_LOG:
        print(f"{DEBUG_LOG_PREFIX}: {msg}", flush=True)


def slot_view_owner(slot) -> str | None:
    """Which per-slot view target ("column" or "computer") currently owns
    the slot's shared GtkStack -- the arbiter both column_view.py and
    my_computer_view.py consult before touching that stack. Without it, a
    navigation that makes both targets want the same slot at once can drive
    their independent notify::visible-child reassert handlers into fighting
    each other forever (issue #137): each handler used to trust only its own
    module-local "elected" flag, which can be stale relative to the other
    module's, so both kept reasserting against each other with no
    termination condition."""
    return getattr(slot, "_mc_view_owner", None)


def set_slot_view_owner(slot, owner: str) -> None:
    slot._mc_view_owner = owner


def release_slot_view_owner(slot, owner: str) -> None:
    """Clear the owner token only if `owner` is still the current holder --
    a stale/late release must never clobber a newer claim."""
    if getattr(slot, "_mc_view_owner", None) == owner:
        slot._mc_view_owner = None


def _all_widgets(widget):
    """Depth-first walk of widget and every descendant (widget itself first)."""
    if not widget:
        return
    yield widget
    # Using observe_children instead of get_first_child/get_next_sibling
    # is safer in some GTK4 contexts but let's stick to the basic tree walker.
    child = widget.get_first_child()
    while child:
        yield from _all_widgets(child)
        child = child.get_next_sibling()


def _find_tab_view(win: Gtk.Window) -> Adw.TabView | None:
    return next((w for w in _all_widgets(win) if isinstance(w, Adw.TabView)), None)


def _find_slot_stack(slot: Gtk.Widget) -> Gtk.Stack | None:
    """The slot's own top-level GtkStack (nautilus-window-slot.c: self->stack,
    the slot's only direct child). The first Gtk.Stack found in a depth-first
    walk from the slot is always this one, before any stack nested deeper in
    its content view."""
    return next((w for w in _all_widgets(slot) if isinstance(w, Gtk.Stack)), None)


def watch_slots(win: Gtk.Window, on_slot) -> None:
    """Call on_slot(win, slot) for every current tab of `win` and for every
    future tab (Adw.TabView "page-attached"). Shared by any feature that
    injects a stack child into each NautilusWindowSlot's own GtkStack
    (Column View, issue #118; Computer View, issue #133)."""
    tab_view = _find_tab_view(win)
    if tab_view is None:
        _log("watch_slots: no Adw.TabView found")
        return
    for i in range(tab_view.get_n_pages()):
        page = tab_view.get_nth_page(i)
        slot = page.get_child() if page is not None else None
        if slot is not None:
            on_slot(win, slot)
    tab_view.connect(
        "page-attached",
        lambda _tv, page, _pos, win=win, on_slot=on_slot: on_slot(win, page.get_child()),
    )


def _slot_settled(slot: Gtk.Widget) -> bool:
    try:
        return slot.get_property("location") is not None
    except TypeError:
        return True


def schedule_slot_init(
    slot: Gtk.Widget,
    marker_attr: str,
    do_inject,
    *,
    retry_ms: int = 20,
    max_attempts: int = 100,
    is_settled=_slot_settled,
) -> None:
    """Defer `do_inject(slot)` until `is_settled(slot)` (by default, once
    `slot`'s location resolves), mirroring main.py's
    _schedule_window_init/_deferred_init_window (issue #4: widget-tree
    changes during files_view_begin_loading race Nautilus-core's
    templates-menu rebuild). Idempotent via marker_attr, an attribute
    `do_inject` itself sets on `slot` once injection succeeds -- callers pass
    a distinct attribute name per feature so two features can each inject
    into the same slot independently."""
    if getattr(slot, marker_attr, None) is not None:
        return
    attempts = [0]

    def _try() -> bool:
        if getattr(slot, marker_attr, None) is not None:
            return GLib.SOURCE_REMOVE
        attempts[0] += 1
        settled = is_settled(slot)
        if not settled and attempts[0] <= max_attempts:
            return GLib.SOURCE_CONTINUE
        GLib.idle_add(do_inject, slot, priority=GLib.PRIORITY_LOW)
        return GLib.SOURCE_REMOVE

    GLib.timeout_add(retry_ms, _try)


_NAUTILUS_VERSION_CACHE = None
_NAUTILUS_VERSION_READ = False


def _nautilus_version() -> tuple[int, ...] | None:
    """Parse Nautilus's own compiled-in AppStream metadata to get its running app
    version (e.g. (50, 2, 2)), reading the same GResource its own About dialog uses
    (nautilus-window.c: adw_about_dialog_new_from_appdata("/org/gnome/nautilus/appdata")).
    Works in-process with no subprocess, filesystem guessing, or Flatpak concerns --
    the resource is compiled into the binary and registered process-globally, and we
    run inside that same process. Returns None if the resource or a <release> tag is
    unexpectedly missing (e.g. a future Nautilus restructures its appdata)."""
    global _NAUTILUS_VERSION_CACHE, _NAUTILUS_VERSION_READ
    if _NAUTILUS_VERSION_READ:
        return _NAUTILUS_VERSION_CACHE
    _NAUTILUS_VERSION_READ = True
    try:
        data = Gio.resources_lookup_data(
            "/org/gnome/nautilus/appdata", Gio.ResourceLookupFlags.NONE
        )
        root = ElementTree.fromstring(data.get_data().decode("utf-8"))
        version = root.find("releases/release").get("version")
        _NAUTILUS_VERSION_CACHE = tuple(int(p) for p in version.split("."))
    except Exception as e:
        _log(f"_nautilus_version: could not read Nautilus appdata version ({e})")
    return _NAUTILUS_VERSION_CACHE


def _resolve_gtype(*names: str) -> int | None:
    """Return the GType of the first name in `names` that is registered, or None if
    none are. GObject.type_from_name() raises RuntimeError (not TYPE_INVALID) for an
    unknown name, so each candidate must be tried in its own try/except. Centralizes
    the pattern needed whenever Nautilus renames an internal GObject type across
    releases (e.g. NautilusGtkSidebarRow -> NautilusSidebarRow in 48)."""
    for name in names:
        try:
            return GObject.type_from_name(name)
        except RuntimeError:
            continue
    return None


def _current_location_uri(win) -> str | None:
    """Return the URI of the active tab's current location, or None.

    Reads the NautilusWindowSlot "location" GFile property on demand (same
    approach as _window_is_at_disks in main.py, generalized to any URI rather
    than just DISKS_URI). No persistent signal, no set_child (safe re: issue
    #11). Prefers the active slot so tabs are handled; falls back to the
    first slot with a location.
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
                return loc.get_uri()
        except TypeError:
            pass
        fallback = loc
    return fallback.get_uri() if fallback is not None else None


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


def _menu_section_with_action(model, action_name):
    """Return the section GMenu of `model` that contains an item bound to
    `action_name`, or None. Used to append into a native menu's existing group
    (e.g. the Remove/Rename section) rather than tacking on a new section."""
    str_type = GLib.VariantType.new("s")
    for i in range(model.get_n_items()):
        section = model.get_item_link(i, Gio.MENU_LINK_SECTION)
        if section is None:
            continue
        for j in range(section.get_n_items()):
            av = section.get_item_attribute_value(j, "action", str_type)
            if av is not None and av.get_string() == action_name:
                return section
    return None


def _menu_item_index(section, action_name):
    """Return the index of the item bound to `action_name` within `section`,
    or None. Used to insert right after a specific native item (e.g. directly
    below "Add to Bookmarks") instead of appending to the end of the section."""
    str_type = GLib.VariantType.new("s")
    for j in range(section.get_n_items()):
        av = section.get_item_attribute_value(j, "action", str_type)
        if av is not None and av.get_string() == action_name:
            return j
    return None


def _menu_section_index_with_action(model, action_name):
    """Return the index, within `model` itself, of the top-level section that
    contains an item bound to `action_name`, or None. Used to insert a whole
    new section right before/after an existing one (e.g. right before the
    trailing Properties section), unlike _menu_item_index which indexes
    within a section."""
    str_type = GLib.VariantType.new("s")
    for i in range(model.get_n_items()):
        section = model.get_item_link(i, Gio.MENU_LINK_SECTION)
        if section is None:
            continue
        for j in range(section.get_n_items()):
            av = section.get_item_attribute_value(j, "action", str_type)
            if av is not None and av.get_string() == action_name:
                return i
    return None


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

    The target icon is stored as img._diskinfo_pin_name so _repin_icon() can
    update it without reconnecting signal handlers.
    """
    img._diskinfo_pin_name = icon_name
    img.set_from_icon_name(icon_name)
    img.set_visible(True)
    if getattr(img, "_diskinfo_pinned", False):
        return  # already watching — _diskinfo_pin_name update above is enough
    img._diskinfo_pinned = True
    img._diskinfo_restoring = False

    def _on_changed(image: Gtk.Image, _pspec) -> None:
        if image._diskinfo_restoring:
            return  # we triggered this notification ourselves – skip
        target = getattr(image, "_diskinfo_pin_name", None)
        if target is None:
            return
        # Detect overwrite: storage type not ICON_NAME, wrong name, or visibility dropped.
        if (
            getattr(image, "get_storage_type", lambda: None)() != Gtk.ImageType.ICON_NAME
            or image.get_icon_name() != target
            or not image.get_visible()
        ):
            image._diskinfo_restoring = True
            image.set_from_icon_name(target)
            image.set_visible(True)
            image._diskinfo_restoring = False

    img.connect("notify::icon-name", _on_changed)
    img.connect("notify::gicon", _on_changed)
    img.connect("notify::paintable", _on_changed)
    img.connect("notify::storage-type", _on_changed)
    img.connect("notify::visible", _on_changed)


# Some icon themes ship a monochrome (symbolic-looking) variant of an
# otherwise full-color icon in their small fixed-size dirs (confirmed:
# MacTahoe-A ships places/24/folder.svg single-color, full color only under
# scalable/). GTK's automatic size selection at a small display size (<=24)
# resolves that monochrome dir, so a plain set_from_icon_name + set_pixel_size(24)
# renders a gray glyph. Look the icon up at this larger nominal size instead --
# it resolves the colored scalable/ variant (verified: 24->monochrome dir,
# 32/48->scalable colored) -- then scale the resulting paintable down to the
# display size. FORCE_REGULAR alone does NOT fix this (the small regular icon
# is itself monochrome); the large lookup size is what matters.
#
# Content-view use only (list view / column view). Never use this for sidebar
# or bookmark-row icons -- those must follow Nautilus' native sidebar rendering.
_COLOR_ICON_LOOKUP_SIZE = 48


def _set_regular_icon(image: Gtk.Image, size: int, *, icon_name=None, gicon=None) -> None:
    """Set a full-color (non-symbolic) icon on `image`, drawn at `size` px.

    Bypasses GTK's automatic small-size theme lookup (which resolves a
    monochrome fixed-size variant for small icons on some themes) by looking
    the icon up at _COLOR_ICON_LOOKUP_SIZE and scaling the paintable down.
    Pass exactly one of `icon_name` / `gicon`.
    """
    display = Gdk.Display.get_default()
    if display is None:
        # No display (should not happen inside a running Nautilus); fall back to
        # a plain set so the image is at least populated.
        if icon_name is not None:
            image.set_from_icon_name(icon_name)
        elif gicon is not None:
            image.set_from_gicon(gicon)
        image.set_pixel_size(size)
        return

    theme = Gtk.IconTheme.get_for_display(display)
    scale = image.get_scale_factor() or 1
    flags = Gtk.IconLookupFlags.FORCE_REGULAR
    if icon_name is not None:
        paintable = theme.lookup_icon(
            icon_name, None, _COLOR_ICON_LOOKUP_SIZE, scale, Gtk.TextDirection.NONE, flags
        )
    else:
        paintable = theme.lookup_by_gicon(
            gicon, _COLOR_ICON_LOOKUP_SIZE, scale, Gtk.TextDirection.NONE, flags
        )
    image.set_from_paintable(paintable)
    image.set_pixel_size(size)


def _repin_icon(img: Gtk.Image, icon_name: str) -> None:
    """Change the pinned icon target on an already-pinned Gtk.Image.
    The existing signal handlers read _diskinfo_pin_name dynamically, so
    updating the attribute and setting the new icon is all that is needed."""
    img._diskinfo_pin_name = icon_name
    img._diskinfo_restoring = True
    img.set_from_icon_name(icon_name)
    img.set_visible(True)
    img._diskinfo_restoring = False


def _find_row_start_image(row: Gtk.Widget) -> Gtk.Image | None:
    """Find a NautilusSidebarRow's start-icon Gtk.Image, skipping the eject
    button's image (same in_button walk as _build_place_sidebar_row's
    _pin_row_icon)."""
    for w in _all_widgets(row):
        if not isinstance(w, Gtk.Image):
            continue
        parent = w.get_parent()
        in_button = False
        while parent and parent is not row:
            if isinstance(parent, Gtk.Button):
                in_button = True
                break
            parent = parent.get_parent()
        if not in_button:
            return w
    return None


# ── Zoom -> icon px ──────────────────────────────────────────────────────────────
# Zoom -> icon px, one table per (card kind, view). Grid and list are separate
# native scales (different keys/stops/px -- see nautilus-enums.h). Folder cards
# mirror native px exactly; disk cards aren't native grid cells (own width,
# usage bars) so they ride a gentler curve of our own, anchored at medium = the
# sizes they used before (64 grid / 36 list). Tweak the disk tables to taste.
_ICON_VIEW_SCHEMA = "org.gnome.nautilus.icon-view"  # grid zoom key
_LIST_VIEW_SCHEMA = "org.gnome.nautilus.list-view"  # list zoom key

# Folder cards - native px.
_GRID_ZOOM_PX = {"small": 48, "small-plus": 64, "medium": 96, "large": 168, "extra-large": 256}
_LIST_ZOOM_PX = {"small": 16, "medium": 32, "large": 64}
# Disk cards - our own curve.
_DISK_GRID_ZOOM_PX = {"small": 42, "small-plus": 48, "medium": 64, "large": 112, "extra-large": 168}
_DISK_LIST_ZOOM_PX = {"small": 24, "medium": 36, "large": 48}


def _zoom_icon_px(schema: str, table: dict[str, int], default: int) -> int:
    try:
        zoom = Gio.Settings.new(schema).get_string("default-zoom-level")
        return table.get(zoom, default)
    except Exception:
        return default


def _nautilus_icon_size() -> int:
    return _zoom_icon_px(_ICON_VIEW_SCHEMA, _GRID_ZOOM_PX, 96)


def _nautilus_list_icon_size() -> int:
    return _zoom_icon_px(_LIST_VIEW_SCHEMA, _LIST_ZOOM_PX, 32)


def _disk_icon_size() -> int:
    return _zoom_icon_px(_ICON_VIEW_SCHEMA, _DISK_GRID_ZOOM_PX, 64)


def _disk_list_icon_size() -> int:
    return _zoom_icon_px(_LIST_VIEW_SCHEMA, _DISK_LIST_ZOOM_PX, 36)


def _folder_card_width() -> int:
    """Native Nautilus grid-cell width: icon plus two 18px emblem gutters."""
    return _nautilus_icon_size() + 36


# ── Card geometry constants ──────────────────────────────────────────────────────
# (disk icon px now comes from _disk_icon_size()/_disk_list_icon_size() above)
_FLOW_COLS_GRID = 8  # max columns in grid (FlowBox) view
_CARD_WIDTH = 280  # disk grid card width cap (px); beyond this,
# the grid gains another column instead of stretching cards further
_LIST_BAR_MAX_WIDTH = 240  # max width (px) of the usage bar at the end of a list-view row
_DISK_CARD_SPACING = 16  # disk card FlowBox column spacing (px)
_DISK_CARD_ROW_SPACING = 6  # disk card FlowBox row spacing (px)
_DISK_CARD_ICON_SPACING = 18  # gap between icon and details column inside a grid disk card (px)
_DISK_CARD_MARGIN_START = 8  # disk card own start inset inside its total width (px)
_DISK_CARD_MARGIN_END = 8  # disk card own end inset inside its total width (px)
_DISK_CARD_MARGIN_TOP = 6  # disk card own top inset inside its total height (px)
_DISK_CARD_MARGIN_BOTTOM = 6  # disk card own bottom inset inside its total height (px)

_FOLDER_FLOW_COLS_GRID = 20  # matches native Nautilus folder view's max column count
_FOLDER_CARD_SPACING = 6  # matches Nautilus gridview's border-spacing
_FOLDER_CARD_ROW_SPACING = 6

# ── Column View geometry constants ───────────────────────────────────────────────
_COLUMN_WIDTH = 300  # column view: default/fixed folder column width (px)
_COLUMN_MIN_WIDTH = 180  # column view: floor a column can be dragged down to (px)
_COLUMN_MAX_WIDTH = 580  # column view: ceiling a column can be dragged up to (px)
_COLUMN_ROW_ICON_SIZE = (
    24  # column view: gap (px) between a row's leading icon and its label/chevron
)
_COLUMN_ROW_SPACING = 8  # column view: gap (px) between a row's icon/label/chevron
_COLUMN_PREVIEW_WIDTH = 400  # column view: default preview column width (px)
_COLUMN_PREVIEW_IMAGE_SIZE = 1024  # preview lookup size (px); big enough for scalable variant
_COLUMN_PREVIEW_IMAGE_MAX_WIDTH = 1024  # max preview image width (px); larger images scaled to fit

_INTERNAL_FSTYPES = {"gvfs", "unmounted", "network-place"}

# Icon per group category
_GROUP_ICON = {
    "system": "drive-harddisk",
    "local": "drive-harddisk",
    "removable": "drive-removable-media",
    "disc": "media-optical",
    "network": "folder-remote",
}
