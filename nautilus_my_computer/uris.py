"""Stateless URI rules shared by every view.

Pure string functions: no GIO, no widgets, no I/O, so they hold for any
location (file, trash, network, ...) and can be tested without GTK. They
define string-level identity, which is what GSettings keys, the bookmarks
file and widget lookups compare. Where two URIs may differ by percent-encoding
too, compare through Gio.File.equal at the GIO boundary instead.
"""


def normalize(uri: str) -> str:
    """The canonical form used for comparisons and stored keys: trailing
    slashes dropped, except on a scheme root, which keeps its own form
    ("file:///" stays "file:///", never the bare "file:" a plain
    rstrip("/") would leave)."""
    stripped = uri.rstrip("/")
    if stripped.endswith(":") and stripped != uri:
        return f"{stripped}///"
    return stripped


def same_uri(a: str, b: str) -> bool:
    return normalize(a) == normalize(b)


def is_within(uri: str, folder: str) -> bool:
    """True if uri is strictly inside folder, at any depth. A sibling sharing a
    name prefix ("/a/foo" against "/a/fo") is not inside it."""
    u, f = normalize(uri), normalize(folder)
    prefix = f if f.endswith("/") else f"{f}/"
    return u != f and u.startswith(prefix)


def rebase(uri: str, old: str, new: str) -> str:
    """Where uri lives once old has moved to new (a rename or a move): new
    itself when uri is old, the same relative path under new when uri is
    inside old, uri unchanged otherwise."""
    if same_uri(uri, old):
        return new
    if not is_within(uri, old):
        return uri
    rest = normalize(uri)[len(normalize(old)) :].lstrip("/")
    base = normalize(new)
    return f"{base}{rest}" if base.endswith("/") else f"{base}/{rest}"


def admin_uri_if_needed(uri: str, can_read: bool, can_execute: bool) -> str:
    """The URI a deliberate open should navigate to. A local folder the user
    can't read or enter moves to the gvfs admin backend, which asks for a
    password through polkit. Mirrors Nautilus's own activation rewrite
    (nautilus-mime-actions.c:1703). Every other URI comes back unchanged.

    A file:// URI is already percent-encoded, so swapping the scheme keeps the
    path exactly as GIO built it (Nautilus concatenates the raw path instead)."""
    if not uri.startswith("file://") or (can_read and can_execute):
        return uri
    return "admin://" + uri[len("file://") :]
