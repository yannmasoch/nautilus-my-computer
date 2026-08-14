"""Reusable, self-contained card/section widgets for the Computer panel.

Each card renders itself from a single model object (a MountInfo or
PreferredFolder, accessed by duck-typing -- this module never imports those
classes) and adapts its layout to the current view mode ("icon-view" grid vs
"list-view" row). Cards never import the entry file; behaviour that needs the
extension (right-click menus, file-op D-Bus calls, navigation) is reached
through the injected `ext` instance.
"""

import bisect
import concurrent.futures
import dataclasses
import html as html_escaping
import json
import math
import os
import posixpath
import re
import secrets
import selectors
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import zipfile
from urllib.parse import unquote as url_unquote
from urllib.parse import urlsplit
from xml.etree import ElementTree

import cairo
import gi

gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Gtk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, GObject, Graphene, Gsk, Gtk, Pango

from . import paddle_ocr_client

# GNOME's own thumbnail engine: it drives the system thumbnailers installed in
# /usr/share/thumbnailers (Evince/Papers for PDF, glycin for images, gsf-office
# for documents, ...), the same cheap, already-present mechanism Nautilus uses.
# Guarded: on a system without the gnome-desktop typelib the preview simply
# falls back to the file's icon rather than breaking the whole extension.
try:
    gi.require_version("GnomeDesktop", "4.0")
    from gi.repository import GnomeDesktop

    _thumb_factory = GnomeDesktop.DesktopThumbnailFactory.new(
        GnomeDesktop.DesktopThumbnailSize.LARGE
    )
except (ValueError, ImportError):
    GnomeDesktop = None
    _thumb_factory = None

# Bounds concurrent thumbnailer subprocess spawns for Column View row
# thumbnails (see MyComputerColumnRow._row_thumbnail_worker) -- a folder with
# hundreds of un-cached files would otherwise launch that many subprocesses
# at once.
_ROW_THUMBNAIL_SEMAPHORE = threading.Semaphore(4)
_ROW_THUMBNAIL_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=8, thread_name_prefix="mc-thumbnail"
)
# All rich previews share a bounded pool. PDF scrolling used to start an
# unbounded thread (and often one external process) per newly visible page.
_PREVIEW_WORKER_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=6, thread_name_prefix="mc-preview"
)
_COLUMN_CHILD_REFRESH_DEBOUNCE_MS = 150
_MULTI_INFO_CONCURRENCY = 16
_MULTI_SELECTION_ICON_NAME = "edit-select-all-symbolic"
_TEXT_PREVIEW_MAX_BYTES = 512 * 1024
# Per-row thumbnail work is useful in ordinary folders, but scheduling one
# job per entry in a directory with thousands of files delays navigation and
# puts stale work ahead of the next directory the user opens. Large columns
# keep their resolved file icons; their rich preview still works normally.
_COLUMN_ROW_THUMBNAIL_LIMIT = 500
_COLUMN_FILE_ATTRIBUTES = (
    "standard::name,standard::display-name,standard::icon,"
    "standard::is-hidden,standard::is-backup,standard::type,standard::content-type,"
    "standard::size,time::modified,time::created,time::access,"
    "access::can-execute,metadata::custom-icon,metadata::custom-icon-name"
)
# PDF and EPUB helpers require a local path.  GVfs files are copied to a
# private temporary file first, but refuse unexpectedly large downloads from
# a simple selection change.
_REMOTE_PREVIEW_STAGE_MAX_BYTES = 256 * 1024 * 1024
# Headless office conversion is isolated/cancellable but still has to parse
# the full workbook. Refuse pathological local/remote sheets from a mere
# selection change; the normal application remains available via Enter.
_SPREADSHEET_PREVIEW_MAX_BYTES = 128 * 1024 * 1024
_SPREADSHEET_HTML_MAX_BYTES = 64 * 1024 * 1024
_DOCUMENT_PREVIEW_MAX_BYTES = 128 * 1024 * 1024
# Archive listings are metadata-only, but hostile archives can contain
# millions of tiny entries. Bound both the parser input and the UI model.
_ARCHIVE_LIST_MAX_OUTPUT_BYTES = 16 * 1024 * 1024
_ARCHIVE_LIST_MAX_ENTRIES = 20_000
_ARCHIVE_LIST_TIMEOUT_SECONDS = 20
_TAR_ARCHIVE_EXTENSIONS = (
    ".tar.bz2",
    ".tar.gz",
    ".tar.lzma",
    ".tar.xz",
    ".tar.zst",
    ".tbz",
    ".tbz2",
    ".tgz",
    ".txz",
    ".tzst",
    ".tar",
)
# Text/OCR geometry can outweigh the rendered-page cache on very long PDFs.
# Keep only a bounded, last-used set; revisiting an evicted page reloads it.
_PDF_WORD_CACHE_MAX_PAGES = 64

# Named pages of MyComputerPreviewColumn's stable preview surface.  The
# contents of a page may evolve (for example, video can later become a real
# player), but callers only select a semantic slot and never rebuild the UI.
PREVIEW_SLOT_LOADING = "loading"
PREVIEW_SLOT_ICON = "icon"
PREVIEW_SLOT_IMAGE = "image"
PREVIEW_SLOT_VIDEO = "video"
PREVIEW_SLOT_DOCUMENT = "document"
PREVIEW_SLOT_PDF = "pdf"
PREVIEW_SLOT_EPUB = "epub"
PREVIEW_SLOT_SPREADSHEET = "spreadsheet"
PREVIEW_SLOT_ARCHIVE = "archive"


_SPREADSHEET_CONTENT_TYPES = {
    "application/vnd.ms-excel",
    "application/x-msexcel",
    "application/xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel.sheet.macroenabled.12",
    "application/vnd.ms-excel.sheet.binary.macroenabled.12",
    "application/vnd.ms-excel.template.macroenabled.12",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.template",
    "application/vnd.oasis.opendocument.spreadsheet",
    "application/x-gnumeric",
    "text/csv",
    "text/tab-separated-values",
}
_SPREADSHEET_EXTENSIONS = {
    ".xls",
    ".xlsx",
    ".xlsm",
    ".xlsb",
    ".xlt",
    ".xltx",
    ".xltm",
    ".ods",
    ".fods",
    ".gnumeric",
    ".csv",
    ".tsv",
}
_DOCUMENT_CONTENT_TYPES = {
    "application/msword",
    "application/rtf",
    "application/vnd.ms-word.document.macroenabled.12",
    "application/vnd.ms-word.template.macroenabled.12",
    "application/vnd.oasis.opendocument.text",
    "application/vnd.oasis.opendocument.text-template",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.template",
    "application/vnd.wordperfect",
    "application/x-abiword",
    "text/rtf",
}
_DOCUMENT_EXTENSIONS = {
    ".abw",
    ".doc",
    ".docm",
    ".docx",
    ".dot",
    ".dotm",
    ".dotx",
    ".fodt",
    ".odt",
    ".ott",
    ".rtf",
    ".sxw",
    ".wpd",
}
_AUDIO_EXTENSIONS = {
    ".aac",
    ".aif",
    ".aiff",
    ".alac",
    ".flac",
    ".m4a",
    ".mp3",
    ".oga",
    ".ogg",
    ".opus",
    ".wav",
    ".wma",
}

_ARCHIVE_EXTENSIONS = tuple(
    sorted(
        {
            ".tar.bz2",
            ".tar.gz",
            ".tar.lz",
            ".tar.lzma",
            ".tar.lzo",
            ".tar.xz",
            ".tar.zst",
            ".tbz",
            ".tbz2",
            ".tgz",
            ".txz",
            ".tzst",
            ".7z",
            ".arj",
            ".bz2",
            ".cab",
            ".cb7",
            ".cbr",
            ".cbz",
            ".cpio",
            ".gz",
            ".lha",
            ".lzh",
            ".lz",
            ".lzma",
            ".rar",
            ".tar",
            ".taz",
            ".tpz",
            ".xz",
            ".zip",
            ".zipx",
            ".zst",
        },
        key=len,
        reverse=True,
    )
)
_ARCHIVE_CONTENT_TYPES = {
    "application/gzip",
    "application/vnd.ms-cab-compressed",
    "application/vnd.rar",
    "application/x-7z-compressed",
    "application/x-bzip2",
    "application/x-cab",
    "application/x-compress",
    "application/x-cpio",
    "application/x-gzip",
    "application/x-lha",
    "application/x-lzma",
    "application/x-rar",
    "application/x-rar-compressed",
    "application/x-tar",
    "application/x-xz",
    "application/zip",
    "application/zstd",
}
_ZIP_BASED_DOCUMENT_EXTENSIONS = {
    ".docx",
    ".docm",
    ".dotm",
    ".dotx",
    ".epub",
    ".key",
    ".numbers",
    ".odg",
    ".odp",
    ".ods",
    ".odt",
    ".pages",
    ".potm",
    ".potx",
    ".ppsm",
    ".ppsx",
    ".pptm",
    ".pptx",
    ".vsdx",
    ".xlsb",
    ".xlsm",
    ".xlsx",
    ".xltm",
    ".xltx",
}
_ARCHIVE_PREVIEW_EXCLUDED_EXTENSIONS = _ZIP_BASED_DOCUMENT_EXTENSIONS | {
    ".apk",
    ".ar",
    ".chm",
    ".deb",
    ".dmg",
    ".ear",
    ".iso",
    ".jar",
    ".msi",
    ".pkg",
    ".rpm",
    ".vhd",
    ".vhdx",
    ".vmdk",
    ".war",
    ".wim",
    ".xar",
    ".xip",
    ".xpi",
}


def _is_spreadsheet_file(content_type: str | None, basename: str) -> bool:
    return bool(
        (content_type or "").lower() in _SPREADSHEET_CONTENT_TYPES
        or os.path.splitext(basename.lower())[1] in _SPREADSHEET_EXTENSIONS
    )


def _is_document_file(content_type: str | None, basename: str) -> bool:
    return bool(
        (content_type or "").lower() in _DOCUMENT_CONTENT_TYPES
        or os.path.splitext(basename.lower())[1] in _DOCUMENT_EXTENSIONS
    )


def _is_audio_file(content_type: str | None, basename: str) -> bool:
    return bool(
        (content_type and content_type.lower().startswith("audio/"))
        or os.path.splitext(basename.lower())[1] in _AUDIO_EXTENSIONS
    )


def _is_archive_file(content_type: str | None, basename: str) -> bool:
    folded = basename.casefold()
    suffix = os.path.splitext(folded)[1]
    if suffix in _ARCHIVE_PREVIEW_EXCLUDED_EXTENSIONS:
        return False
    return bool(
        folded.endswith(_ARCHIVE_EXTENSIONS)
        or (content_type or "").lower() in _ARCHIVE_CONTENT_TYPES
    )


@dataclasses.dataclass(frozen=True, slots=True)
class _ArchiveMember:
    path: str
    is_dir: bool
    size: int
    packed_size: int
    modified: str
    encrypted: bool = False


@dataclasses.dataclass(frozen=True, slots=True)
class _ArchiveListing:
    members: tuple[_ArchiveMember, ...]
    truncated: bool = False


@dataclasses.dataclass(frozen=True, slots=True)
class _ArchiveChild:
    name: str
    path: str
    is_dir: bool
    size: int
    packed_size: int
    modified: str
    encrypted: bool = False


class _ArchiveListItem(GObject.Object):
    def __init__(self, entry: _ArchiveChild) -> None:
        super().__init__()
        self.entry = entry


def _normalize_archive_member_path(path: str) -> str:
    parts = []
    for part in path.replace("\\", "/").split("/"):
        part = part.replace("\x00", "")
        if not part or part in {".", ".."}:
            continue
        parts.append(part)
    return "/".join(parts)


def _parse_7z_listing(output: str, *, truncated: bool = False) -> _ArchiveListing:
    members: list[_ArchiveMember] = []
    fields: dict[str, str] = {}

    def finish_block() -> None:
        if not fields or len(members) >= _ARCHIVE_LIST_MAX_ENTRIES:
            fields.clear()
            return
        path = _normalize_archive_member_path(fields.get("Path", ""))
        if not path:
            fields.clear()
            return
        is_dir = fields.get("Folder") == "+" or fields.get("Attributes", "").startswith("D")
        try:
            size = max(0, int(fields.get("Size", "0") or 0))
        except ValueError:
            size = 0
        try:
            packed_size = max(0, int(fields.get("Packed Size", "0") or 0))
        except ValueError:
            packed_size = 0
        members.append(
            _ArchiveMember(
                path=path,
                is_dir=is_dir,
                size=size,
                packed_size=packed_size,
                modified=fields.get("Modified", ""),
                encrypted=fields.get("Encrypted") == "+",
            )
        )
        fields.clear()

    for line in output.splitlines():
        if not line.strip():
            finish_block()
            continue
        key, separator, value = line.partition(" = ")
        if separator:
            fields[key] = value
    finish_block()
    return _ArchiveListing(
        tuple(members),
        truncated=truncated or len(members) >= _ARCHIVE_LIST_MAX_ENTRIES,
    )


def _archive_children(listing: _ArchiveListing, folder: str = "") -> list[_ArchiveChild]:
    prefix = tuple(part for part in folder.split("/") if part)
    children: dict[str, _ArchiveChild] = {}
    depth = len(prefix)
    for member in listing.members:
        parts = tuple(part for part in member.path.split("/") if part)
        if len(parts) <= depth or parts[:depth] != prefix:
            continue
        name = parts[depth]
        path = "/".join((*prefix, name))
        is_dir = member.is_dir or len(parts) > depth + 1
        existing = children.get(name)
        if is_dir:
            descendant_size = member.size if not member.is_dir else 0
            descendant_packed = member.packed_size if not member.is_dir else 0
            if existing is None:
                children[name] = _ArchiveChild(
                    name=name,
                    path=path,
                    is_dir=True,
                    size=descendant_size,
                    packed_size=descendant_packed,
                    modified=member.modified if member.is_dir else "",
                    encrypted=member.encrypted,
                )
            elif existing.is_dir:
                children[name] = dataclasses.replace(
                    existing,
                    size=existing.size + descendant_size,
                    packed_size=existing.packed_size + descendant_packed,
                    modified=existing.modified or (member.modified if member.is_dir else ""),
                    encrypted=existing.encrypted or member.encrypted,
                )
            else:
                children[name] = dataclasses.replace(existing, is_dir=True)
        elif existing is None:
            children[name] = _ArchiveChild(
                name=name,
                path=path,
                is_dir=False,
                size=member.size,
                packed_size=member.packed_size,
                modified=member.modified,
                encrypted=member.encrypted,
            )
    return sorted(
        children.values(),
        key=lambda entry: (
            0 if entry.is_dir else 1,
            GLib.utf8_collate_key_for_filename(entry.name, -1),
        ),
    )


def _negotiated_file_drop_action(target: Gtk.DropTarget) -> Gdk.DragAction:
    """Choose a safe action while respecting modifiers and the source offer."""
    event = target.get_current_event()
    state = event.get_modifier_state() if event is not None else 0
    drop = target.get_current_drop()
    offered = drop.get_actions() if drop is not None else Gdk.DragAction.COPY
    if state & Gdk.ModifierType.CONTROL_MASK and offered & Gdk.DragAction.COPY:
        return Gdk.DragAction.COPY
    if state & Gdk.ModifierType.SHIFT_MASK and offered & Gdk.DragAction.MOVE:
        return Gdk.DragAction.MOVE
    if drop is not None:
        drag = drop.get_drag()
        if drag is not None:
            selected = drag.get_selected_action()
            if selected & offered:
                return selected
            if offered & Gdk.DragAction.MOVE:
                return Gdk.DragAction.MOVE
    if offered & Gdk.DragAction.COPY:
        return Gdk.DragAction.COPY
    return Gdk.DragAction.MOVE


# EPUB chapters are XHTML+CSS, so rendering one properly means an HTML engine.
# Guarded like GnomeDesktop above: without the WebKit typelib an EPUB or video
# preview just
# falls back to its file icon, exactly as before this preview existed. The
# view itself is built lazily the first time an EPUB is actually previewed
# (see _ensure_epub_view) rather than in __init__ -- a preview column is
# rebuilt on every single file click, and each WebView costs a separate web
# process, so non-EPUB previews must never pay for one.
try:
    gi.require_version("WebKit", "6.0")
    from gi.repository import WebKit
except (ValueError, ImportError):
    WebKit = None


_TEXT_CONTENT_TYPES = {
    "application/ecmascript",
    "application/graphql",
    "application/json",
    "application/javascript",
    "application/ld+json",
    "application/sql",
    "application/typescript",
    "application/x-httpd-php",
    "application/x-javascript",
    "application/x-perl",
    "application/x-ruby",
    "application/yaml",
    "application/toml",
    "application/xml",
    "application/x-sh",
    "application/x-yaml",
}

_TEXT_FILE_EXTENSIONS = {
    # Plain text, documentation, markup, and structured data.
    ".adoc",
    ".asciidoc",
    ".bib",
    ".css",
    ".csv",
    ".dtd",
    ".geojson",
    ".htm",
    ".html",
    ".json",
    ".json5",
    ".jsonl",
    ".lock",
    ".markdown",
    ".md",
    ".mdx",
    ".ndjson",
    ".org",
    ".plist",
    ".properties",
    ".psv",
    ".rst",
    ".tex",
    ".text",
    ".toml",
    ".topojson",
    ".tsv",
    ".txt",
    ".wiki",
    ".xhtml",
    ".xml",
    ".xsd",
    ".xsl",
    ".xslt",
    ".yaml",
    ".yml",
    # Configuration, logs, patches, templates, and developer notes.
    ".ini",
    ".ipynb",
    ".cfg",
    ".conf",
    ".config",
    ".desktop",
    ".diff",
    ".editorconfig",
    ".env",
    ".http",
    ".log",
    ".patch",
    ".pem",
    ".pub",
    ".rest",
    ".trace",
    ".url",
    ".ejs",
    ".erb",
    ".hbs",
    ".handlebars",
    ".in",
    ".j2",
    ".jinja",
    ".jinja2",
    ".liquid",
    ".mustache",
    ".tmpl",
    ".tpl",
    # Databases, query languages, schemas, and API definitions.
    ".avsc",
    ".cql",
    ".cypher",
    ".ddl",
    ".dml",
    ".gql",
    ".graphql",
    ".prisma",
    ".proto",
    ".psql",
    ".rq",
    ".sparql",
    ".sql",
    ".thrift",
    # Web languages and frameworks.
    ".astro",
    ".cjs",
    ".coffee",
    ".htc",
    ".js",
    ".jsx",
    ".less",
    ".mjs",
    ".sass",
    ".scss",
    ".styl",
    ".svelte",
    ".ts",
    ".tsx",
    ".vue",
    ".jade",
    ".pug",
    ".slim",
    # Shells and scripting languages.
    ".awk",
    ".bash",
    ".bat",
    ".cmd",
    ".cr",
    ".fish",
    ".lua",
    ".nu",
    ".pl",
    ".pm",
    ".ps1",
    ".py",
    ".pyi",
    ".pyx",
    ".pxd",
    ".r",
    ".rb",
    ".sed",
    ".sh",
    ".tcl",
    ".zsh",
    # Compiled, systems, mobile, and scientific language sources.
    ".adb",
    ".ads",
    ".asm",
    ".c",
    ".cc",
    ".cbl",
    ".cl",
    ".clj",
    ".cljc",
    ".cljs",
    ".cob",
    ".cpp",
    ".cs",
    ".cxx",
    ".dart",
    ".d",
    ".edn",
    ".el",
    ".elm",
    ".erl",
    ".ex",
    ".exs",
    ".f",
    ".f03",
    ".f90",
    ".f95",
    ".for",
    ".fs",
    ".fsi",
    ".fsx",
    ".go",
    ".groovy",
    ".h",
    ".hh",
    ".hpp",
    ".hs",
    ".hxx",
    ".hx",
    ".inc",
    ".java",
    ".jl",
    ".kt",
    ".kts",
    ".lhs",
    ".lisp",
    ".ll",
    ".m",
    ".ml",
    ".mli",
    ".mm",
    ".move",
    ".nim",
    ".odin",
    ".pas",
    ".php",
    ".rkt",
    ".re",
    ".rei",
    ".roc",
    ".rs",
    ".s",
    ".scala",
    ".scm",
    ".sol",
    ".swift",
    ".td",
    ".vala",
    ".vapi",
    ".vb",
    ".vbs",
    ".wat",
    ".wast",
    ".zig",
    # Build systems, package recipes, and infrastructure-as-code.
    ".ac",
    ".am",
    ".bazel",
    ".buck",
    ".bzl",
    ".cmake",
    ".cue",
    ".gradle",
    ".gn",
    ".gni",
    ".gyp",
    ".gypi",
    ".hcl",
    ".mak",
    ".mk",
    ".ninja",
    ".nix",
    ".nomad",
    ".rego",
    ".sbt",
    ".tf",
    ".tfvars",
    # Assembly/HDL/GPU shader sources.
    ".comp",
    ".frag",
    ".geom",
    ".glsl",
    ".hlsl",
    ".sv",
    ".svh",
    ".v",
    ".vert",
    ".vh",
    ".vhd",
    ".vhdl",
    ".wgsl",
    # Test/specification formats.
    ".feature",
    ".robot",
}

_TEXT_FILE_NAMES = {
    ".babelrc",
    ".bash_profile",
    ".bashrc",
    ".dockerignore",
    ".editorconfig",
    ".eslintignore",
    ".eslintrc",
    ".gitattributes",
    ".gitignore",
    ".gitmodules",
    ".npmrc",
    ".prettierignore",
    ".prettierrc",
    ".profile",
    ".stylelintrc",
    ".vimrc",
    ".yarnrc",
    ".zprofile",
    ".zshrc",
    "authors",
    "brewfile",
    "changelog",
    "cmakelists.txt",
    "containerfile",
    "copying",
    "dockerfile",
    "gemfile",
    "gnumakefile",
    "go.mod",
    "go.sum",
    "jenkinsfile",
    "justfile",
    "license",
    "makefile",
    "meson.build",
    "meson_options.txt",
    "news",
    "pkgbuild",
    "procfile",
    "rakefile",
    "readme",
    "taskfile",
    "tiltfile",
    "todo",
    "vagrantfile",
}


def _is_text_preview_file(content_type: str | None, basename: str) -> bool:
    """Recognize text via shared-mime-info, then developer filename fallbacks."""
    normalized_type = (content_type or "").lower()
    if normalized_type and (
        normalized_type.startswith("text/")
        or normalized_type in _TEXT_CONTENT_TYPES
        or (
            normalized_type.startswith("application/")
            and normalized_type.endswith(("+json", "+xml"))
        )
        or Gio.content_type_is_a(normalized_type, "text/plain")
    ):
        return True

    name = basename.lower()
    suffix = os.path.splitext(name)[1]
    return bool(
        suffix in _TEXT_FILE_EXTENSIONS
        or name in _TEXT_FILE_NAMES
        or name.startswith((".env.", "containerfile.", "dockerfile.", "makefile."))
    )


def _run_cancellable_command(
    command: list[str],
    *,
    timeout: float,
    cancellable: Gio.Cancellable | None = None,
    text: bool = False,
) -> subprocess.CompletedProcess | None:
    """Run a worker command while allowing preview teardown to stop it promptly."""
    if cancellable is not None and cancellable.is_cancelled():
        return None
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
    )
    deadline = time.monotonic() + timeout
    while True:
        if cancellable is not None and cancellable.is_cancelled():
            process.terminate()
            try:
                process.communicate(timeout=0.5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            process.kill()
            stdout, stderr = process.communicate()
            raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr)
        try:
            stdout, stderr = process.communicate(timeout=min(0.1, remaining))
        except subprocess.TimeoutExpired:
            continue
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _probe_video_dimensions(
    path: str,
    cancellable: Gio.Cancellable | None = None,
    *,
    prober: str | None = None,
) -> tuple[int, int] | None:
    """Read the first video stream's geometry outside the Nautilus process."""
    prober = prober or shutil.which("ffprobe")
    if prober is None or not os.path.isfile(path):
        return None
    command = [
        prober,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "json",
        os.path.abspath(path),
    ]
    try:
        result = _run_cancellable_command(command, timeout=8, cancellable=cancellable, text=True)
    except (OSError, subprocess.TimeoutExpired, ValueError) as error:
        _log(f"Video metadata probe failed for {path!r}: {error}")
        return None
    if result is None or result.returncode != 0:
        return None
    try:
        streams = json.loads(result.stdout).get("streams", [])
        width = int(streams[0].get("width", 0))
        height = int(streams[0].get("height", 0))
    except (AttributeError, IndexError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return (width, height) if width > 0 and height > 0 else None


def _loopback_video_origin(uri: str) -> str | None:
    """Return a strict helper origin, rejecting redirects to anywhere else."""
    try:
        parsed = urlsplit(uri)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
    ):
        return None
    return f"http://127.0.0.1:{port}"


def _build_video_preview_html(uri: str) -> str:
    """Build the inert HTML shell rendered by the isolated WebKit process."""
    source = html_escaping.escape(uri, quote=True)
    origin = _loopback_video_origin(uri)
    media_source = origin or "'self' file:"
    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy"
 content="default-src 'none'; media-src {media_source}; style-src 'unsafe-inline';
 img-src data:; connect-src 'none'; object-src 'none'; frame-src 'none'">
<style>
html, body {{ margin: 0; width: 100%; height: 100%; overflow: hidden; background: #000; }}
video {{ display: block; width: 100%; height: 100%; object-fit: contain; background: #000; }}
</style></head><body>
<video preload="metadata" playsinline src="{source}"></video>
</body></html>"""


def _stop_worker_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _list_tar_contents(
    path: str, cancellable: Gio.Cancellable | None = None
) -> _ArchiveListing | None:
    members: list[_ArchiveMember] = []
    deadline = time.monotonic() + _ARCHIVE_LIST_TIMEOUT_SECONDS
    try:
        with tarfile.open(path, mode="r:*") as archive:
            for member in archive:
                if cancellable is not None and cancellable.is_cancelled():
                    return None
                if time.monotonic() >= deadline:
                    _log(f"Archive preview timed out for {path!r}")
                    return None
                normalized = _normalize_archive_member_path(member.name)
                if normalized:
                    try:
                        modified = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(member.mtime))
                    except (OverflowError, OSError, ValueError):
                        modified = ""
                    members.append(
                        _ArchiveMember(
                            path=normalized,
                            is_dir=member.isdir(),
                            size=max(0, member.size),
                            packed_size=0,
                            modified=modified,
                        )
                    )
                if len(members) >= _ARCHIVE_LIST_MAX_ENTRIES:
                    return _ArchiveListing(tuple(members), truncated=True)
    except (tarfile.TarError, OSError, EOFError):
        return None
    return _ArchiveListing(tuple(members))


def _list_archive_contents(
    path: str,
    cancellable: Gio.Cancellable | None = None,
    *,
    lister: str | None = None,
) -> _ArchiveListing | None:
    """List archive metadata with strict time/output/entry bounds.

    ``7z -slt`` is used because it provides stable key/value metadata across
    ZIP, RAR, 7z, tar and the other formats supported by the installed 7-Zip
    backend. No member is extracted or opened during preview.
    """
    if not os.path.isfile(path):
        return None
    if path.casefold().endswith(_TAR_ARCHIVE_EXTENSIONS):
        listing = _list_tar_contents(path, cancellable)
        if listing is not None:
            return listing
    lister = lister or shutil.which("7zz") or shutil.which("7z") or shutil.which("7za")
    if lister is None:
        return None
    environment = os.environ.copy()
    environment["LC_ALL"] = "C"
    try:
        process = subprocess.Popen(
            [lister, "l", "-slt", "-ba", "--", os.path.abspath(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        )
    except OSError as error:
        _log(f"Could not start archive preview for {path!r}: {error}")
        return None

    stdout = bytearray()
    stderr = bytearray()
    truncated = False
    timed_out = False
    selector = selectors.DefaultSelector()
    assert process.stdout is not None and process.stderr is not None
    selector.register(process.stdout, selectors.EVENT_READ, stdout)
    selector.register(process.stderr, selectors.EVENT_READ, stderr)
    deadline = time.monotonic() + _ARCHIVE_LIST_TIMEOUT_SECONDS
    try:
        while selector.get_map():
            if cancellable is not None and cancellable.is_cancelled():
                _stop_worker_process(process)
                return None
            if time.monotonic() >= deadline:
                timed_out = True
                _stop_worker_process(process)
                break
            for key, _mask in selector.select(timeout=0.1):
                try:
                    chunk = os.read(key.fileobj.fileno(), 64 * 1024)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                target: bytearray = key.data
                limit = _ARCHIVE_LIST_MAX_OUTPUT_BYTES if target is stdout else 64 * 1024
                remaining = limit - len(target)
                if remaining > 0:
                    target.extend(chunk[:remaining])
                if target is stdout and len(chunk) > remaining:
                    truncated = True
                    _stop_worker_process(process)
                    break
            if truncated:
                break
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()

    if timed_out:
        _log(f"Archive preview timed out for {path!r}")
        return None
    if process.poll() is None:
        process.wait()
    if not truncated and process.returncode not in (0, 1):
        message = stderr.decode("utf-8", errors="replace").strip()
        _log(f"Archive preview failed for {path!r} (exit {process.returncode}): {message}")
        return None
    return _parse_7z_listing(stdout.decode("utf-8", errors="replace"), truncated=truncated)


_SPREADSHEET_PREVIEW_HEAD = """
<meta http-equiv="Content-Security-Policy"
 content="default-src 'none'; img-src data:; style-src 'unsafe-inline';
 script-src 'nonce-{nonce}'; connect-src 'none'; media-src 'none';
 object-src 'none'; frame-src 'none'">
<style>
:root {{
  color-scheme: light dark;
  --mc-bg: #ffffff; --mc-fg: #1e1e1e; --mc-grid: #d7d7d7;
  --mc-head: #f2f2f2; --mc-accent: #3584e4;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --mc-bg: #242424; --mc-fg: #eeeeec; --mc-grid: #4a4a4a;
    --mc-head: #303030; --mc-accent: #78aeed;
  }}
}}
html, body {{
  margin: 0; width: 100%; height: 100%; overflow: hidden;
  background: var(--mc-bg);
  color: var(--mc-fg); font-family: system-ui, sans-serif;
}}
body > hr, a[name^="table"] {{ display: none !important; }}
#mc-sheet-scroll {{
  width: 100%; height: 100%; overflow: auto;
  overscroll-behavior: contain; scrollbar-width: thin;
}}
#mc-sheet-canvas {{
  width: max-content; min-width: 100%; min-height: 100%;
  background: var(--mc-bg);
}}
#mc-sheet-tabs {{
  display: flex; gap: 2px; padding: 6px;
  box-sizing: border-box; width: 100%; min-width: 100%;
  background: var(--mc-head); border-bottom: 1px solid var(--mc-grid);
}}
#mc-sheet-tabs button {{
  border: 0; border-radius: 6px; padding: 5px 10px; color: var(--mc-fg);
  background: transparent; white-space: nowrap; font: inherit;
}}
#mc-sheet-tabs button.active {{ color: #fff; background: var(--mc-accent); }}
table {{
  display: none; border-collapse: separate !important;
  border-spacing: 0 !important; margin: 8px;
  width: max-content !important; min-width: calc(100vw - 16px) !important;
  background: var(--mc-bg);
}}
table.mc-active-sheet {{ display: table; }}
td, th {{
  min-width: 4em; padding: 4px 7px !important;
  border-right: 1px solid var(--mc-grid) !important;
  border-bottom: 1px solid var(--mc-grid) !important;
  color: var(--mc-fg) !important; background: var(--mc-bg) !important;
  white-space: nowrap;
}}
td *, th * {{ color: var(--mc-fg) !important; background: transparent !important; }}
tr > :first-child {{ border-left: 1px solid var(--mc-grid) !important; }}
</style>
<script nonce="{nonce}">
document.addEventListener('DOMContentLoaded', () => {{
  const anchors = [...document.querySelectorAll('a[name^="table"]')];
  const sheets = anchors.map((anchor, index) => {{
    let table = anchor.nextElementSibling;
    while (table && table.tagName !== 'TABLE') table = table.nextElementSibling;
    const title = anchor.querySelector('em')?.textContent?.trim() || `Sheet ${{index + 1}}`;
    return {{ table, title }};
  }}).filter(sheet => sheet.table);
  if (!sheets.length) return;
  const viewport = document.createElement('div');
  viewport.id = 'mc-sheet-scroll';
  const canvas = document.createElement('div');
  canvas.id = 'mc-sheet-canvas';
  const tabs = document.createElement('nav');
  tabs.id = 'mc-sheet-tabs';
  const show = selected => {{
    sheets.forEach((sheet, index) =>
      sheet.table.classList.toggle('mc-active-sheet', index === selected));
    [...tabs.children].forEach((button, index) =>
      button.classList.toggle('active', index === selected));
    viewport.scrollTo(0, 0);
  }};
  sheets.forEach((sheet, index) => {{
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = sheet.title;
    button.addEventListener('click', () => show(index));
    tabs.appendChild(button);
  }});
  document.body.prepend(viewport);
  viewport.appendChild(canvas);
  canvas.appendChild(tabs);
  sheets.forEach(sheet => canvas.appendChild(sheet.table));
  show(0);
}});
</script>
"""


def _decorate_spreadsheet_html(path: str) -> bool:
    try:
        size = os.path.getsize(path)
        if size <= 0 or size > _SPREADSHEET_HTML_MAX_BYTES:
            return False
        with open(path, "r", encoding="utf-8", errors="replace") as stream:
            markup = stream.read(_SPREADSHEET_HTML_MAX_BYTES + 1)
    except OSError:
        return False
    if len(markup.encode("utf-8")) > _SPREADSHEET_HTML_MAX_BYTES:
        return False
    # Calc emits a malformed, pre-HTML5 ``<p><center>`` overview followed by
    # one link per sheet. Browsers repair that markup differently, so hiding
    # it with a child selector was unreliable. Remove everything between the
    # generated body opening and the first real sheet anchor instead. The
    # anchors themselves stay in the source solely as tab metadata and are
    # hidden by the preview CSS above.
    markup = re.sub(
        r"(<body(?:\s[^>]*)?>).*?(?=<a\s+name=[\"']table\d+[\"'])",
        lambda match: match.group(1),
        markup,
        count=1,
        flags=re.IGNORECASE | re.DOTALL,
    )
    nonce = secrets.token_urlsafe(24)
    preview_head = _SPREADSHEET_PREVIEW_HEAD.format(nonce=nonce)
    if re.search(r"<head(?:\s[^>]*)?>", markup, flags=re.IGNORECASE):
        markup = re.sub(
            r"(<head(?:\s[^>]*)?>)",
            lambda match: match.group(1) + preview_head,
            markup,
            count=1,
            flags=re.IGNORECASE,
        )
    else:
        markup = preview_head + markup
    try:
        with open(path, "w", encoding="utf-8") as stream:
            stream.write(markup)
    except OSError:
        return False
    return True


def _convert_spreadsheet_to_html(
    path: str,
    output_dir: str,
    profile_dir: str,
    cancellable: Gio.Cancellable | None = None,
    *,
    converter: str | None = None,
) -> str | None:
    """Convert one workbook to a tabbed HTML grid in an isolated profile.

    The dedicated profile avoids lock contention with the user's running
    LibreOffice session and prevents a preview from inheriting document
    macros or trusted-location settings. The worker process is terminated by
    _run_cancellable_command as soon as the preview column is discarded.
    """
    converter = converter or shutil.which("libreoffice") or shutil.which("soffice")
    if converter is None or not os.path.isfile(path):
        return None
    os.makedirs(output_dir, mode=0o700, exist_ok=True)
    os.makedirs(profile_dir, mode=0o700, exist_ok=True)
    profile_uri = Gio.File.new_for_path(profile_dir).get_uri()
    command = [
        converter,
        "--headless",
        "--nologo",
        "--nodefault",
        "--nolockcheck",
        "--norestore",
        "--safe-mode",
        f"-env:UserInstallation={profile_uri}",
        "--convert-to",
        "html:HTML (StarCalc)",
        "--outdir",
        output_dir,
        os.path.abspath(path),
    ]
    try:
        result = _run_cancellable_command(command, timeout=45, cancellable=cancellable, text=True)
    except (OSError, subprocess.TimeoutExpired) as error:
        _log(f"Spreadsheet preview conversion failed for {path!r}: {error}")
        return None
    if result is None or result.returncode != 0:
        if result is not None:
            message = (result.stderr or result.stdout or "").strip()
            _log(
                f"Spreadsheet preview conversion failed for {path!r} "
                f"(exit {result.returncode}): {message}"
            )
        return None
    try:
        candidates = sorted(
            entry.path
            for entry in os.scandir(output_dir)
            if entry.is_file(follow_symlinks=False)
            and entry.name.lower().endswith((".html", ".htm"))
        )
    except OSError as error:
        _log(f"Could not inspect spreadsheet preview output {output_dir!r}: {error}")
        return None
    if not candidates or not _decorate_spreadsheet_html(candidates[0]):
        return None
    return candidates[0]


def _convert_document_to_pdf(
    path: str,
    output_dir: str,
    profile_dir: str,
    cancellable: Gio.Cancellable | None = None,
    *,
    converter: str | None = None,
) -> str | None:
    """Convert a word-processing document to PDF in an isolated profile."""
    converter = converter or shutil.which("libreoffice") or shutil.which("soffice")
    if converter is None or not os.path.isfile(path):
        return None
    os.makedirs(output_dir, mode=0o700, exist_ok=True)
    os.makedirs(profile_dir, mode=0o700, exist_ok=True)
    profile_uri = Gio.File.new_for_path(profile_dir).get_uri()
    command = [
        converter,
        "--headless",
        "--nologo",
        "--nodefault",
        "--nolockcheck",
        "--norestore",
        "--safe-mode",
        f"-env:UserInstallation={profile_uri}",
        "--convert-to",
        "pdf:writer_pdf_Export",
        "--outdir",
        output_dir,
        os.path.abspath(path),
    ]
    try:
        result = _run_cancellable_command(command, timeout=45, cancellable=cancellable, text=True)
    except (OSError, subprocess.TimeoutExpired) as error:
        _log(f"Document preview conversion failed for {path!r}: {error}")
        return None
    if result is None or result.returncode != 0:
        if result is not None:
            message = (result.stderr or result.stdout or "").strip()
            _log(
                f"Document preview conversion failed for {path!r} "
                f"(exit {result.returncode}): {message}"
            )
        return None
    try:
        candidates = sorted(
            entry.path
            for entry in os.scandir(output_dir)
            if entry.is_file(follow_symlinks=False) and entry.name.lower().endswith(".pdf")
        )
    except OSError as error:
        _log(f"Could not inspect document preview output {output_dir!r}: {error}")
        return None
    return candidates[0] if candidates else None


def _generate_video_thumbnail_fallback(
    path: str, cancellable: Gio.Cancellable | None = None
) -> GdkPixbuf.Pixbuf | None:
    """Generate a video frame thumbnail using ffmpegthumbnailer or ffmpeg."""
    tmp_out = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    tmp_out_path = tmp_out.name
    tmp_out.close()
    try:
        if shutil.which("ffmpegthumbnailer"):
            cmd = ["ffmpegthumbnailer", "-i", path, "-o", tmp_out_path, "-s", "512", "-q", "8"]
            res = _run_cancellable_command(cmd, timeout=5, cancellable=cancellable)
            if (
                res is not None
                and res.returncode == 0
                and os.path.exists(tmp_out_path)
                and os.path.getsize(tmp_out_path) > 0
            ):
                return GdkPixbuf.Pixbuf.new_from_file(tmp_out_path)

        if shutil.which("ffmpeg"):
            cmd = [
                "ffmpeg",
                "-y",
                "-ss",
                "00:00:01",
                "-i",
                path,
                "-frames:v",
                "1",
                "-vf",
                "scale=512:-1",
                tmp_out_path,
            ]
            res = _run_cancellable_command(cmd, timeout=5, cancellable=cancellable)
            if (
                res is not None
                and res.returncode == 0
                and os.path.exists(tmp_out_path)
                and os.path.getsize(tmp_out_path) > 0
            ):
                return GdkPixbuf.Pixbuf.new_from_file(tmp_out_path)
    except Exception as e:
        _log(f"Video thumbnail fallback failed for {path}: {e}")
    finally:
        if os.path.exists(tmp_out_path):
            try:
                os.unlink(tmp_out_path)
            except OSError:
                pass
    return None


# Fallback on-screen width (px) for the PDF page at 100% zoom, used only
# before the preview column has ever been allocated a real size (see
# _pdf_viewport_width, which otherwise measures the live viewport).
_PDF_PREVIEW_BASE_WIDTH = 320
# Ceiling on that 100% width. The preview is the last column in the Miller
# chain and takes all the leftover room, so opening a PDF from a shallow
# location (nothing else open to share the width with) handed it the better
# part of the window and rendered the page absurdly large. A page wider than
# this stops being easier to read and just wastes render time, so 100% means
# "fill the column, up to a comfortable reading width".
_PDF_DISPLAY_MAX_WIDTH = 720
# Gap drawn between consecutive pages in the continuous view, and how many
# pages beyond the visible range to render ahead. One page of lookahead in
# each direction is what makes scrolling into the next page seamless -- it
# is already rendered by the time its top edge appears.
_PDF_PAGE_SPACING = 12
_PDF_PREFETCH_PAGES = 1
# Scroll events arrive far faster than pages can be rendered, so the
# visible-range recompute they trigger is debounced. Short enough to feel
# immediate, long enough that a fast flick through the document doesn't
# queue a render for every page it passes over.
_PDF_VISIBLE_DEBOUNCE_MS = 60
# Vertical slack, in points, when deciding whether the pointer is on a line of
# text. Keep it small so paragraph spacing is not mistaken for selectable text.
#
# There is no horizontal counterpart on purpose: no single slack can both
# bridge variable inter-word gaps and still treat a margin as empty. Instead,
# a point counts as on-text when it falls inside the horizontal span of a line
# (see _pdf_line_at), which covers every inter-word gap however wide, and stops
# exactly at the text edge.
_PDF_LINE_HIT_SLACK = 1.0
# Drag-bar range/step, as a direct on-screen percentage of the preview
# column's own width -- *not* tied 1:1 to pdftoppm's DPI (see
# _PDF_RENDER_DPI_MAX below). MIN is 100%, i.e. "page exactly fills the
# preview column", which is both the default and the smallest useful size:
# the column is already narrow, and anything below it just wasted the space
# while making the text unreadable. The Gtk.Range these bound enforces the
# range on its own (get_value() can never leave [lower, upper]) when
# dragged; STEP is only for the +/- buttons and keyboard arrows, which
# _apply_pdf_zoom clamps to the same bounds.
_PDF_ZOOM_PCT_MIN = 100
_PDF_ZOOM_PCT_MAX = 300
_PDF_ZOOM_PCT_STEP = 25
# EPUB zoom is a WebKit zoom level rather than a page scale, but the control
# is the same control, so it gets the same range and step -- two readers that
# look identical should not quietly behave differently.
_EPUB_ZOOM_PCT_MIN = _PDF_ZOOM_PCT_MIN
_EPUB_ZOOM_PCT_MAX = _PDF_ZOOM_PCT_MAX
_EPUB_ZOOM_PCT_STEP = _PDF_ZOOM_PCT_STEP
# Render resolution is derived from the size the page will actually occupy
# on screen (see _pdf_dpi_for_width), not from a fixed DPI. Rendering at a
# flat 300 DPI can produce far more pixels than the preview displays, wasting
# rendering time and memory before the result is downscaled.
#
# SUPERSAMPLE renders that much larger than the target display size, so the
# downscale has real detail to work with and small zoom changes stay crisp
# between quality upgrades. MIN/MAX bound the result: MAX caps the worst
# case (300% zoom on a wide page) at a resolution pdftoppm can still produce
# in a few seconds, past which extra DPI buys nothing the column can show.
_PDF_RENDER_SUPERSAMPLE = 2.0
_PDF_RENDER_DPI_MIN = 72
_PDF_RENDER_DPI_MAX = 300
# Page size assumed when pdfinfo can't report one (US Letter, in PostScript
# points -- 8.5in x 11in, times 72). Only affects the DPI estimate and the
# pre-render page layout, never correctness.
_PDF_DEFAULT_PAGE_WIDTH_PTS = 612.0
_PDF_DEFAULT_PAGE_HEIGHT_PTS = 792.0
# Rendered pages kept around keyed by (page number, dpi), so paging back to
# somewhere already visited is instant instead of re-running pdftoppm.
#
# Bounded by total pixels rather than entry count, because an entry's cost
# scales with the square of the DPI it was rendered at: at 100% zoom an A4
# page is ~0.9MP (~2.7MB decoded), at 300% zoom it is ~8MP (~24MB), so a
# flat count that is reasonable for one is wildly wrong for the other. A
# pixel budget self-adjusts -- many cheap pages or a couple of expensive
# ones -- and caps decoded bitmaps at roughly 72MB. The count cap only stops
# the dict itself growing unboundedly for very small pages.
_PDF_PAGE_CACHE_MAX_PIXELS = 24_000_000
_PDF_PAGE_CACHE_MAX_ENTRIES = 12
# How long the zoom slider has to sit still before the visible pages are
# re-rendered at the now-current zoom's DPI. Every tick during an active drag
# only rescales the pixbufs already in hand (see _update_pdf_page_sizes) --
# instant regardless of this value -- so this purely trades "how soon does it
# sharpen up after you let go" against "how many pdftoppm calls does a slow
# drag spawn"; short enough that the upgrade feels immediate once you stop,
# long enough that it never fires mid-drag.
_PDF_QUALITY_DEBOUNCE_MS = 250
# The preview stack crossfades for 100 ms. Its PDF child has no allocation
# until that transition starts, so the first-page fit is repeated just after
# the transition rather than depending on a zero-width pre-layout read.
_PDF_VIEWPORT_REFIT_DELAY_MS = 125


def _pdf_dpi_for_width(display_px: int, page_width_pts: float) -> int:
    """The DPI to hand pdftoppm so a page `page_width_pts` wide comes back at
    roughly `display_px` * _PDF_RENDER_SUPERSAMPLE pixels wide.

    PDF page dimensions are in PostScript points, 72 to the inch, and DPI is
    pixels per inch -- so the render width is entirely determined by these
    two numbers, and asking for more than the display can show is pure waste
    (see _PDF_RENDER_SUPERSAMPLE)."""
    inches = max(1.0, page_width_pts / 72.0)
    dpi = round(display_px * _PDF_RENDER_SUPERSAMPLE / inches)
    return int(max(_PDF_RENDER_DPI_MIN, min(_PDF_RENDER_DPI_MAX, dpi)))


def _pdf_render_timeout(dpi: int) -> float:
    """Seconds to allow pdftoppm before giving up on a render at `dpi`.

    Rendered pixel count scales with the *square* of DPI (both dimensions
    grow together), so the budget scales the same way rather than linearly.
    Deliberately generous at the low end so image-heavy pages are not treated
    like inexpensive text-only pages. Still capped, so a
    pathological file can't tie up a worker thread indefinitely."""
    return min(45.0, max(12.0, 12.0 * (dpi / 150.0) ** 2))


def _render_pdf_page_at_zoom(
    path: str,
    page_num: int,
    dpi: int = 300,
    cancellable: Gio.Cancellable | None = None,
) -> GdkPixbuf.Pixbuf | None:
    """Render page `page_num` of a PDF file at `dpi` resolution."""
    tmp_dir = tempfile.mkdtemp()
    out_prefix = os.path.join(tmp_dir, f"pdf_page_{page_num}")
    try:
        if shutil.which("pdftoppm"):
            cmd = [
                "pdftoppm",
                "-png",
                "-singlefile",
                "-r",
                str(dpi),
                "-f",
                str(page_num),
                "-l",
                str(page_num),
                path,
                out_prefix,
            ]
            res = _run_cancellable_command(
                cmd, timeout=_pdf_render_timeout(dpi), cancellable=cancellable
            )
            out_png = out_prefix + ".png"
            if (
                res is not None
                and res.returncode == 0
                and os.path.exists(out_png)
                and os.path.getsize(out_png) > 0
            ):
                return GdkPixbuf.Pixbuf.new_from_file(out_png)
    except Exception as e:
        _log(f"PDF render failed for {path} page {page_num}: {e}")
    finally:
        out_png = out_prefix + ".png"
        if os.path.exists(out_png):
            try:
                os.unlink(out_png)
            except OSError:
                pass
        if os.path.exists(tmp_dir):
            try:
                os.rmdir(tmp_dir)
            except OSError:
                pass
    return None


# Text extraction. A PDF page is rendered here as a bitmap, so there is
# nothing to select on it -- the text has to be pulled out separately. Two
# sources, in this order:
#
#   1. pdftotext, which reads the PDF's own text layer without rendering.
#   2. PaddleOCR of the rendered page, only when step 1 comes back empty and a
#      validated private runtime is installed. OCR is never run on pages that
#      already have an exact, inexpensive text layer.
_PDF_OCR_DPI = 300
_PDF_TEXT_TIMEOUT = 15.0
# Image OCR is selection metadata, not a prerequisite for showing the image.
# Keep pathological photos from occupying a worker/minutes after the user has
# moved on. Ordinary phone photos and screenshots remain comfortably inside
# both bounds, while cancellation still stops PaddleOCR immediately.
_IMAGE_OCR_MAX_FILE_BYTES = 128 * 1024 * 1024
_IMAGE_OCR_MAX_PIXELS = 50_000_000
_PADDLE_OCR_MIN_CONFIDENCE = 0.50
_PADDLE_LAYOUT_MIN_CONFIDENCE = 0.35
_OCR_SECTION_NAMES = {
    "abstract": "Abstract",
    "algorithm": "Algorithm",
    "aside_text": "Sidebar",
    "chart": "Chart",
    "chart_title": "Chart title",
    "content": "Content",
    "doc_title": "Document title",
    "figure_title": "Figure title",
    "footer": "Footer",
    "footer_image": "Footer image",
    "footnote": "Footnote",
    "formula": "Formula",
    "formula_number": "Formula number",
    "header": "Header",
    "header_image": "Header image",
    "image": "Image",
    "number": "Page number",
    "paragraph_title": "Section title",
    "reference": "Reference",
    "seal": "Seal",
    "table": "Table",
    "table_title": "Table title",
    "text": "Text",
}
_OCR_SECTION_PRIORITY = {
    "content": 1,
    "text": 1,
    "aside_text": 2,
    "abstract": 3,
    "algorithm": 3,
    "chart": 3,
    "figure_title": 3,
    "footnote": 3,
    "formula": 3,
    "header": 3,
    "footer": 3,
    "image": 3,
    "reference": 3,
    "table": 3,
    "paragraph_title": 4,
    "doc_title": 5,
}


def _ocr_section_name(label: str | None) -> str | None:
    if not label:
        return None
    known = _OCR_SECTION_NAMES.get(label)
    return _(known) if known is not None else label.replace("_", " ").capitalize()


def _image_ocr_available() -> bool:
    """Image OCR is enabled only by a validated private PaddleOCR runtime."""
    return paddle_ocr_client.available()


@dataclasses.dataclass(frozen=True)
class _PdfWord:
    """One word and where it sits on the page, in PDF points from the page's
    top-left corner. Points rather than pixels because the page is re-rendered
    at different resolutions as the zoom changes, while these do not move."""

    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    line: int
    section: int | None = None
    section_label: str | None = None


@dataclasses.dataclass(frozen=True)
class _OcrSection:
    """One semantic document region in source-image pixels."""

    identifier: int
    label: str
    score: float
    x0: float
    y0: float
    x1: float
    y1: float


@dataclasses.dataclass(frozen=True)
class _ImageOcrResult:
    """OCR geometry in source-image pixels plus words in reading order."""

    width: int
    height: int
    words: tuple[_PdfWord, ...]
    sections: tuple[_OcrSection, ...] = ()


@dataclasses.dataclass(frozen=True)
class _OcrLine:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    section: _OcrSection | None


def _projection_cut(
    items: list[tuple[tuple[str, int], float, float, float, float]], axis: str
) -> tuple[float, list, list] | None:
    """Largest whitespace cut across layout blocks on one axis."""
    start_index, end_index = (1, 3) if axis == "x" else (2, 4)
    ordered = sorted(items, key=lambda item: (item[start_index], item[end_index]))
    furthest_end = ordered[0][end_index]
    best: tuple[float, int] | None = None
    for index in range(1, len(ordered)):
        gap = ordered[index][start_index] - furthest_end
        # On the vertical axis the first full-width separation matters: a
        # title-to-columns gap can legitimately be smaller than the later gap
        # from column headings to their bodies. Horizontal cuts still want the
        # widest whitespace gutter.
        if gap > 0 and (best is None or (axis == "x" and gap > best[0])):
            best = gap, index
        furthest_end = max(furthest_end, ordered[index][end_index])
    if best is None:
        return None
    gap, index = best
    span = max(item[end_index] for item in ordered) - min(item[start_index] for item in ordered)
    return gap / max(1.0, span), ordered[:index], ordered[index:]


def _xy_cut_order(
    items: list[tuple[tuple[str, int], float, float, float, float]],
) -> list[tuple[str, int]]:
    """Reading order for blocks, including multi-column documents.

    Recursive whitespace cuts keep a full-width title above columns and read
    the left column before the right one. A simple (y, x) sort would interleave
    lines from both columns.
    """
    if len(items) <= 1:
        return [item[0] for item in items]
    y_cut = _projection_cut(items, "y")
    x_cut = _projection_cut(items, "x")
    chosen_cut = None
    if x_cut is not None:
        _weight, left, right = x_cut
        left_y0, left_y1 = min(item[2] for item in left), max(item[4] for item in left)
        right_y0, right_y1 = min(item[2] for item in right), max(item[4] for item in right)
        overlap = max(0.0, min(left_y1, right_y1) - max(left_y0, right_y0))
        smaller_span = min(left_y1 - left_y0, right_y1 - right_y0)
        # A whitespace column whose two sides occupy the same vertical band is
        # a stronger reading-order signal than the gaps between headings and
        # paragraphs inside those columns.
        if overlap / max(1.0, smaller_span) >= 0.2:
            chosen_cut = x_cut
    if chosen_cut is None:
        available = [cut for cut in (y_cut, x_cut) if cut is not None]
        chosen_cut = max(available, key=lambda cut: cut[0]) if available else None
    if chosen_cut:
        _weight, first, second = chosen_cut
        return _xy_cut_order(first) + _xy_cut_order(second)
    return [item[0] for item in sorted(items, key=lambda item: (item[2], item[1]))]


def _order_ocr_lines(lines: list[_OcrLine]) -> list[_OcrLine]:
    if len(lines) <= 1:
        return lines
    grouped: dict[tuple[str, int], list[_OcrLine]] = {}
    for source_index, line in enumerate(lines):
        key = (
            ("section", line.section.identifier)
            if line.section is not None
            else ("line", source_index)
        )
        grouped.setdefault(key, []).append(line)
    blocks = []
    for key, block_lines in grouped.items():
        section = block_lines[0].section
        blocks.append(
            (
                key,
                section.x0 if section is not None else min(line.x0 for line in block_lines),
                section.y0 if section is not None else min(line.y0 for line in block_lines),
                section.x1 if section is not None else max(line.x1 for line in block_lines),
                section.y1 if section is not None else max(line.y1 for line in block_lines),
            )
        )
    ordered: list[_OcrLine] = []
    for key in _xy_cut_order(blocks):
        ordered.extend(_order_ocr_block_lines(grouped[key]))
    return ordered


def _order_ocr_block_lines(lines: list[_OcrLine]) -> list[_OcrLine]:
    """Order visual rows top-to-bottom and overlapping fragments left-to-right."""
    visual_rows: list[list[_OcrLine]] = []
    for line in sorted(lines, key=lambda item: ((item.y0 + item.y1) / 2, item.x0)):
        matching_row = None
        for visual_row in reversed(visual_rows):
            row_y0 = min(item.y0 for item in visual_row)
            row_y1 = max(item.y1 for item in visual_row)
            overlap = max(0.0, min(row_y1, line.y1) - max(row_y0, line.y0))
            if overlap / max(1.0, min(row_y1 - row_y0, line.y1 - line.y0)) >= 0.50:
                matching_row = visual_row
                break
            if line.y0 > row_y1:
                break
        if matching_row is None:
            visual_rows.append([line])
        else:
            matching_row.append(line)
    visual_rows.sort(key=lambda row: min(item.y0 for item in row))
    return [line for row in visual_rows for line in sorted(row, key=lambda item: item.x0)]


def _parse_paddle_sections(payload: dict, width: int, height: int) -> list[_OcrSection]:
    raw_sections = payload.get("sections", [])
    if not isinstance(raw_sections, list):
        return []
    sections: list[_OcrSection] = []
    for identifier, item in enumerate(raw_sections):
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        try:
            score = float(item.get("score", 0.0))
            coordinates = [float(value) for value in item["box"]]
        except (KeyError, TypeError, ValueError):
            continue
        if len(coordinates) != 4 or not all(math.isfinite(value) for value in coordinates):
            continue
        x0, y0, x1, y1 = coordinates
        x0, y0 = max(0.0, x0), max(0.0, y0)
        x1, y1 = min(float(width), x1), min(float(height), y1)
        if not label or score < _PADDLE_LAYOUT_MIN_CONFIDENCE or x1 <= x0 or y1 <= y0:
            continue
        sections.append(_OcrSection(identifier, label, score, x0, y0, x1, y1))
    return _normalize_ocr_sections(sections)


def _section_intersection(first: _OcrSection, second: _OcrSection) -> float:
    return max(0.0, min(first.x1, second.x1) - max(first.x0, second.x0)) * max(
        0.0, min(first.y1, second.y1) - max(first.y0, second.y0)
    )


def _section_area(section: _OcrSection) -> float:
    return max(1.0, (section.x1 - section.x0) * (section.y1 - section.y0))


def _normalize_ocr_sections(sections: list[_OcrSection]) -> list[_OcrSection]:
    """Consolidate duplicate detector boxes into useful copyable regions."""
    merged = list(sections)
    changed = True
    while changed:
        changed = False
        for first_index, first in enumerate(merged):
            for second_index in range(first_index + 1, len(merged)):
                second = merged[second_index]
                overlap = _section_intersection(first, second)
                if (
                    first.label == second.label
                    and overlap / min(_section_area(first), _section_area(second)) >= 0.5
                ):
                    merged[first_index] = _OcrSection(
                        min(first.identifier, second.identifier),
                        first.label,
                        max(first.score, second.score),
                        min(first.x0, second.x0),
                        min(first.y0, second.y0),
                        max(first.x1, second.x1),
                        max(first.y1, second.y1),
                    )
                    merged.pop(second_index)
                    changed = True
                    break
            if changed:
                break

    # Detectors can label essentially the same rectangle both generically and
    # semantically. Keep Document title over Section title, and either over
    # Text; nearby but distinct regions remain untouched.
    chosen: list[_OcrSection] = []
    for section in sorted(
        merged,
        key=lambda item: (_OCR_SECTION_PRIORITY.get(item.label, 2), item.score),
        reverse=True,
    ):
        duplicate = False
        for existing_index, existing in enumerate(chosen):
            intersection = _section_intersection(section, existing)
            union = _section_area(section) + _section_area(existing) - intersection
            containment = intersection / min(_section_area(section), _section_area(existing))
            if intersection / max(1.0, union) >= 0.75 or containment >= 0.70:
                # Near-identical nested boxes often receive both a generic
                # and a semantic label. Keep the higher-priority label but
                # retain the union so lines near either detector edge do not
                # fall out of the copyable region.
                chosen[existing_index] = dataclasses.replace(
                    existing,
                    score=max(existing.score, section.score),
                    x0=min(existing.x0, section.x0),
                    y0=min(existing.y0, section.y0),
                    x1=max(existing.x1, section.x1),
                    y1=max(existing.y1, section.y1),
                )
                duplicate = True
                break
        if not duplicate:
            chosen.append(section)
    return [dataclasses.replace(section, identifier=index) for index, section in enumerate(chosen)]


def _ocr_line_clusters(lines: list[_OcrLine]) -> list[list[_OcrLine]]:
    """Spatially connected text lines that form one readable region."""
    if not lines:
        return []
    parents = list(range(len(lines)))

    def root(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def unite(first: int, second: int) -> None:
        left, right = root(first), root(second)
        if left != right:
            parents[right] = left

    for first_index, first in enumerate(lines):
        first_height = max(1.0, first.y1 - first.y0)
        first_width = max(1.0, first.x1 - first.x0)
        for second_index in range(first_index + 1, len(lines)):
            second = lines[second_index]
            second_height = max(1.0, second.y1 - second.y0)
            second_width = max(1.0, second.x1 - second.x0)
            vertical_gap = max(0.0, max(first.y0, second.y0) - min(first.y1, second.y1))
            if vertical_gap > max(24.0, first_height, second_height):
                continue
            horizontal_overlap = max(0.0, min(first.x1, second.x1) - max(first.x0, second.x0))
            if horizontal_overlap / min(first_width, second_width) < 0.18:
                continue
            unite(first_index, second_index)

    grouped: dict[int, list[_OcrLine]] = {}
    for index, line in enumerate(lines):
        grouped.setdefault(root(index), []).append(line)
    return list(grouped.values())


def _deduplicate_overlapping_ocr_lines(lines: list[_OcrLine]) -> list[_OcrLine]:
    """Remove a repeated edge word produced by overlapping OCR polygons."""
    result = list(lines)
    for right_index, right in enumerate(result):
        right_words = right.text.split()
        if not right_words:
            continue
        best_left = None
        best_overlap = 0.0
        for left in result:
            if left is right or not (left.x0 < right.x0 < left.x1 < right.x1):
                continue
            vertical_overlap = max(0.0, min(left.y1, right.y1) - max(left.y0, right.y0))
            min_height = max(1.0, min(left.y1 - left.y0, right.y1 - right.y0))
            if vertical_overlap / min_height < 0.50:
                continue
            left_words = left.text.split()
            repeated = right_words[0].casefold()
            if not left_words or not (
                left_words[-1].casefold() == repeated
                or (len(repeated) <= 3 and left.text.casefold().endswith(repeated))
            ):
                continue
            overlap = left.x1 - right.x0
            if overlap > best_overlap:
                best_left, best_overlap = left, overlap
        if best_left is None:
            continue
        first_word = right_words[0]
        trimmed = right.text[len(first_word) :].lstrip()
        if not trimmed:
            continue
        removed = len(right.text) - len(trimmed)
        new_x0 = right.x0 + (right.x1 - right.x0) * removed / max(1, len(right.text))
        result[right_index] = dataclasses.replace(right, text=trimmed, x0=new_x0)
    return result


def _coalesce_ocr_line_sections(
    lines: list[_OcrLine], sections: list[_OcrSection]
) -> tuple[list[_OcrLine], list[_OcrSection]]:
    """Make each spatial text region one coherent, copyable section."""
    consolidated_lines: list[_OcrLine] = []
    consolidated_sections: list[_OcrSection] = []
    for cluster in _ocr_line_clusters(lines):
        candidates: dict[int, tuple[_OcrSection, int]] = {}
        for line in cluster:
            if line.section is None:
                continue
            section, count = candidates.get(line.section.identifier, (line.section, 0))
            candidates[line.section.identifier] = section, count + 1
        chosen = None
        if candidates:
            chosen = max(
                candidates.values(),
                key=lambda item: (
                    item[1],
                    _OCR_SECTION_PRIORITY.get(item[0].label, 2),
                    item[0].score,
                ),
            )[0]
        elif len(cluster) > 1:
            chosen = _OcrSection(-1, "text", 1.0, 0, 0, 0, 0)

        output_section = None
        if chosen is not None:
            output_section = _OcrSection(
                len(consolidated_sections),
                chosen.label,
                chosen.score,
                min(line.x0 for line in cluster),
                min(line.y0 for line in cluster),
                max(line.x1 for line in cluster),
                max(line.y1 for line in cluster),
            )
            consolidated_sections.append(output_section)
        consolidated_lines.extend(
            dataclasses.replace(line, section=output_section) for line in cluster
        )
    return consolidated_lines, consolidated_sections


def _section_for_box(
    sections: list[_OcrSection], x0: float, y0: float, x1: float, y1: float
) -> _OcrSection | None:
    """Choose the most specific layout region containing an OCR line."""
    area = max(1.0, (x1 - x0) * (y1 - y0))
    center_x, center_y = (x0 + x1) / 2, (y0 + y1) / 2
    candidates: list[tuple[tuple[bool, float, float, float], _OcrSection]] = []
    for section in sections:
        intersection = max(0.0, min(x1, section.x1) - max(x0, section.x0)) * max(
            0.0, min(y1, section.y1) - max(y0, section.y0)
        )
        coverage = intersection / area
        contains_center = (
            section.x0 <= center_x <= section.x1 and section.y0 <= center_y <= section.y1
        )
        if not contains_center and coverage < 0.25:
            continue
        section_area = max(1.0, (section.x1 - section.x0) * (section.y1 - section.y0))
        # When regions overlap, prefer the tighter semantic region (for
        # example paragraph_title inside a broad content region).
        candidates.append(((contains_center, coverage, -section_area, section.score), section))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def _parse_paddle_ocr_result(payload: dict) -> _ImageOcrResult | None:
    """Convert Paddle's line polygons into the word geometry used by the UI.

    PaddleOCR exposes one polygon per recognized line. Split horizontal extent
    in proportion to character offsets so
    drag-selection remains word-granular.  The original line polygon still
    determines the vertical bounds, including rotated/skewed input.
    """
    try:
        width = int(payload["width"])
        height = int(payload["height"])
        lines = payload["lines"]
    except (KeyError, TypeError, ValueError):
        return None
    if width <= 0 or height <= 0 or not isinstance(lines, list):
        return None

    sections = _parse_paddle_sections(payload, width, height)
    parsed_lines: list[_OcrLine] = []
    for line in lines:
        if not isinstance(line, dict):
            continue
        text = str(line.get("text") or "").strip()
        try:
            confidence = float(line.get("score", 0.0))
            points = [(float(point[0]), float(point[1])) for point in line["polygon"]]
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if not text or confidence < _PADDLE_OCR_MIN_CONFIDENCE or len(points) < 4:
            continue
        coordinates = [coordinate for point in points for coordinate in point]
        if not all(math.isfinite(coordinate) for coordinate in coordinates):
            continue
        x0 = max(0.0, min(point[0] for point in points))
        y0 = max(0.0, min(point[1] for point in points))
        x1 = min(float(width), max(point[0] for point in points))
        y1 = min(float(height), max(point[1] for point in points))
        if x1 <= x0 or y1 <= y0:
            continue
        section = _section_for_box(sections, x0, y0, x1, y1)
        parsed_lines.append(_OcrLine(text, x0, y0, x1, y1, section))

    parsed_lines = _deduplicate_overlapping_ocr_lines(parsed_lines)
    parsed_lines, sections = _coalesce_ocr_line_sections(parsed_lines, sections)

    words: list[_PdfWord] = []
    for line_index, line in enumerate(_order_ocr_lines(parsed_lines)):
        text, x0, y0, x1, y1, section = (
            line.text,
            line.x0,
            line.y0,
            line.x1,
            line.y1,
            line.section,
        )
        matches = list(re.finditer(r"\S+", text))
        if not matches:
            continue
        character_count = max(1, len(text))
        for match in matches:
            word_x0 = x0 + (x1 - x0) * match.start() / character_count
            word_x1 = x0 + (x1 - x0) * match.end() / character_count
            words.append(
                _PdfWord(
                    word_x0,
                    y0,
                    word_x1,
                    y1,
                    match.group(),
                    line_index,
                    section.identifier if section is not None else None,
                    section.label if section is not None else None,
                )
            )
    # An empty, well-formed result means Paddle successfully found no text. It
    # is distinct from None, which represents a runtime or protocol failure.
    return _ImageOcrResult(width, height, tuple(words), tuple(sections))


def _ocr_image_words(
    path: str, cancellable: Gio.Cancellable | None = None
) -> _ImageOcrResult | None:
    """Recognize selectable image words, returning None on any soft failure."""
    try:
        if os.path.getsize(path) > _IMAGE_OCR_MAX_FILE_BYTES:
            return None
        _format, width, height = GdkPixbuf.Pixbuf.get_file_info(path)
        # Some modern GdkPixbuf/Glycin configurations deliberately decline
        # synchronous metadata reads for sandboxed formats. Paddle reports the
        # decoded dimensions, so apply the pixel ceiling whenever this cheap
        # metadata probe is available.
        if width > 0 and height > 0 and width * height > _IMAGE_OCR_MAX_PIXELS:
            return None
    except (GLib.Error, OSError, TypeError):
        return None

    if not paddle_ocr_client.available():
        return None
    paddle_result = paddle_ocr_client.recognize(path, cancellable)
    if paddle_result is None:
        return None
    return _parse_paddle_ocr_result(paddle_result)


_PDF_BBOX_NS = "{http://www.w3.org/1999/xhtml}"


def _pdf_words_from_text_layer(
    path: str, page_num: int, cancellable: Gio.Cancellable | None = None
) -> list[_PdfWord]:
    """Word boxes straight out of the PDF's text layer.

    This avoids rendering, making it inexpensive enough to try before OCR for
    every page the reader displays."""
    result = _run_cancellable_command(
        ["pdftotext", "-bbox-layout", "-f", str(page_num), "-l", str(page_num), path, "-"],
        text=True,
        timeout=_PDF_TEXT_TIMEOUT,
        cancellable=cancellable,
    )
    if result is None or result.returncode != 0 or not result.stdout.strip():
        return []
    root = ElementTree.fromstring(result.stdout)
    page = root.find(f".//{_PDF_BBOX_NS}page")
    if page is None:
        return []
    words: list[_PdfWord] = []
    # Lines are walked in document order and numbered, so a selection can be
    # rebuilt as text with the line breaks in the right places.
    for line_index, line in enumerate(page.findall(f".//{_PDF_BBOX_NS}line")):
        for word in line.findall(f"{_PDF_BBOX_NS}word"):
            text = (word.text or "").strip()
            if not text:
                continue
            try:
                words.append(
                    _PdfWord(
                        float(word.get("xMin")),
                        float(word.get("yMin")),
                        float(word.get("xMax")),
                        float(word.get("yMax")),
                        text,
                        line_index,
                    )
                )
            except (TypeError, ValueError):
                continue
    return words


def _pdf_words_from_ocr(
    path: str, page_num: int, cancellable: Gio.Cancellable | None = None
) -> list[_PdfWord]:
    """PaddleOCR word boxes for a PDF page without a native text layer."""
    if not paddle_ocr_client.available():
        return []
    tmpdir = tempfile.mkdtemp(prefix="mc-pdf-ocr-")
    try:
        prefix = os.path.join(tmpdir, "page")
        render = _run_cancellable_command(
            [
                "pdftoppm",
                "-png",
                "-singlefile",
                "-r",
                str(_PDF_OCR_DPI),
                "-f",
                str(page_num),
                "-l",
                str(page_num),
                path,
                prefix,
            ],
            timeout=_pdf_render_timeout(_PDF_OCR_DPI),
            cancellable=cancellable,
        )
        png = prefix + ".png"
        if render is None or render.returncode != 0 or not os.path.exists(png):
            return []
        payload = paddle_ocr_client.recognize(png, cancellable, layout=True)
        if payload is None:
            return []
        parsed = _parse_paddle_ocr_result(payload)
        if parsed is None:
            return []

        # Paddle's boxes use rendered-image pixels. Everything downstream
        # expects PDF points and pdftoppm rendered at the fixed DPI above.
        scale = 72.0 / _PDF_OCR_DPI
        return [
            _PdfWord(
                word.x0 * scale,
                word.y0 * scale,
                word.x1 * scale,
                word.y1 * scale,
                word.text,
                word.line,
                word.section,
                word.section_label,
            )
            for word in parsed.words
        ]
    except Exception as e:
        _log(f"OCR word boxes failed for {path} page {page_num}: {e}")
        return []
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _pdf_page_words(
    path: str, page_num: int, cancellable: Gio.Cancellable | None = None
) -> list[_PdfWord]:
    """Selectable words for a page: its text layer if it has one, OCR if not.

    Boxes come back in the orientation the page is *rendered* in, while the
    pdftotext <page> element can retain unrotated dimensions. Scale with
    _PdfPageGeometry.display_width/height, never with that element."""
    try:
        words = _pdf_words_from_text_layer(path, page_num, cancellable)
        if words:
            return words
    except Exception as e:
        _log(f"text-layer word boxes failed for {path} page {page_num}: {e}")
    return _pdf_words_from_ocr(path, page_num, cancellable)


@dataclasses.dataclass(frozen=True)
class _PdfPageGeometry:
    """One page's size in points and the rotation applied when rendering it.

    The distinction matters because pdftoppm bakes /Rotate into the bitmap it
    produces, while pdftotext reports word boxes in the page's *unrotated*
    coordinates."""

    width: float
    height: float
    rotation: int

    @property
    def display_width(self) -> float:
        return self.height if self.rotation in (90, 270) else self.width

    @property
    def display_height(self) -> float:
        return self.width if self.rotation in (90, 270) else self.height


_PDF_PAGE_SIZE_RE = re.compile(r"^Page\s+(\d+)\s+size:\s*([\d.]+)\s*x\s*([\d.]+)", re.M)
_PDF_PAGE_ROT_RE = re.compile(r"^Page\s+(\d+)\s+rot:\s*(-?\d+)", re.M)


def _get_pdf_info(path: str, cancellable: Gio.Cancellable | None = None) -> list[_PdfPageGeometry]:
    """Every page's geometry, via pdfinfo. Never empty: falls back to a single
    default-sized page so callers always have something to lay out.

    Read per page rather than once for the whole document because size and
    rotation genuinely vary within a file, and the continuous view needs each
    page's real proportions to reserve the right space for it before it has
    been rendered."""
    default = _PdfPageGeometry(_PDF_DEFAULT_PAGE_WIDTH_PTS, _PDF_DEFAULT_PAGE_HEIGHT_PTS, 0)
    try:
        summary = _run_cancellable_command(
            ["pdfinfo", path], timeout=5, cancellable=cancellable, text=True
        )
        if summary is None or summary.returncode != 0:
            return [default]
        count = 1
        for line in summary.stdout.splitlines():
            if line.startswith("Pages:"):
                try:
                    count = max(1, int(line.split(":", 1)[1].strip()))
                except ValueError:
                    pass
                break

        detail = _run_cancellable_command(
            ["pdfinfo", "-f", "1", "-l", str(count), path],
            text=True,
            timeout=15,
            cancellable=cancellable,
        )
        if detail is None:
            return [default]
        sizes = {
            int(m.group(1)): (float(m.group(2)), float(m.group(3)))
            for m in _PDF_PAGE_SIZE_RE.finditer(detail.stdout)
        }
        rotations = {
            int(m.group(1)): int(m.group(2)) % 360 for m in _PDF_PAGE_ROT_RE.finditer(detail.stdout)
        }
        if not sizes:
            return [default] * count
        # A page pdfinfo did not describe inherits the first one it did, which
        # is a better guess than the generic default.
        fallback = sizes.get(1) or next(iter(sizes.values()))
        return [
            _PdfPageGeometry(*sizes.get(number, fallback), rotations.get(number, 0))
            for number in range(1, count + 1)
        ]
    except Exception as e:
        _log(f"pdfinfo failed for {path}: {e}")
        return [default]


# An EPUB is a zip of XHTML, so previewing one means unpacking it somewhere
# WebKit can resolve each chapter's relative CSS/image links from. These bound
# what a malformed or hostile file can cost before that happens: zip metadata
# is attacker-controlled and trivially claims to hold far more than it does
# (a "zip bomb"), so the declared uncompressed size is checked *before*
# extracting anything.
_EPUB_MAX_EXTRACT_BYTES = 512 * 1024 * 1024
_EPUB_MAX_MEMBERS = 20_000
_EPUB_CONTAINER_NS = "{urn:oasis:names:tc:opendocument:xmlns:container}"
_EPUB_OPF_NS = "{http://www.idpf.org/2007/opf}"
_EPUB_DC_NS = "{http://purl.org/dc/elements/1.1/}"


def _path_is_within(root: str, path: str) -> bool:
    """Whether `path` resolves inside `root`, without unsafe prefix matching."""
    try:
        resolved_root = os.path.realpath(root)
        return os.path.commonpath((resolved_root, os.path.realpath(path))) == resolved_root
    except ValueError:
        return False


def _safe_epub_path(root: str, relative_path: str) -> str | None:
    """Resolve one EPUB-owned path, rejecting absolute and escaping paths."""
    if not relative_path or posixpath.isabs(relative_path):
        return None
    candidate = os.path.join(root, *relative_path.split("/"))
    return os.path.realpath(candidate) if _path_is_within(root, candidate) else None


def _extract_epub(
    path: str, cancellable: Gio.Cancellable | None = None
) -> tuple[str, list[str], str] | None:
    """Unpack `path` to a temporary directory and read its reading order.

    Returns (temp directory, chapter file paths in spine order, title), or
    None if the file isn't a usable EPUB. The caller owns the directory and
    must remove it (see MyComputerPreviewColumn._clear_epub).

    CPython's ZipFile extraction normalizes member names against the usual
    "zip slip" traversal trick. EPUB-owned references are checked separately
    below because container/manifest paths are metadata, not extraction
    targets. Declared member count and size are bounded before writing."""
    tmpdir = None
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) > _EPUB_MAX_MEMBERS:
                return None
            if sum(m.file_size for m in members) > _EPUB_MAX_EXTRACT_BYTES:
                return None
            tmpdir = tempfile.mkdtemp(prefix="mc-epub-")
            for member in members:
                if cancellable is not None and cancellable.is_cancelled():
                    raise InterruptedError
                archive.extract(member, tmpdir)

        container = ElementTree.parse(os.path.join(tmpdir, "META-INF", "container.xml"))
        rootfile = container.find(f".//{_EPUB_CONTAINER_NS}rootfile")
        if rootfile is None:
            raise ValueError("no rootfile")
        opf_rel = url_unquote(rootfile.get("full-path") or "")
        if not opf_rel:
            raise ValueError("rootfile has no full-path")
        opf_path = _safe_epub_path(tmpdir, opf_rel)
        if opf_path is None:
            raise ValueError("rootfile escapes the EPUB")

        opf = ElementTree.parse(opf_path)
        # Chapter hrefs in the manifest are relative to the OPF's own
        # directory, which is often but not always the archive root.
        opf_dir = posixpath.dirname(opf_rel)
        hrefs = {
            item.get("id"): item.get("href")
            for item in opf.findall(f".//{_EPUB_OPF_NS}manifest/{_EPUB_OPF_NS}item")
        }
        chapters = []
        for itemref in opf.findall(f".//{_EPUB_OPF_NS}spine/{_EPUB_OPF_NS}itemref"):
            href = hrefs.get(itemref.get("idref"))
            if not href:
                continue
            href_path = url_unquote(href.partition("#")[0])
            rel = posixpath.normpath(posixpath.join(opf_dir, href_path))
            full = _safe_epub_path(tmpdir, rel)
            if full is not None and os.path.exists(full):
                chapters.append(full)
        if not chapters:
            raise ValueError("empty spine")

        title_el = opf.find(f".//{_EPUB_DC_NS}title")
        title = (title_el.text or "").strip() if title_el is not None else ""
        return tmpdir, chapters, title
    except InterruptedError:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
        return None
    except Exception as e:
        _log(f"EPUB open failed for {path}: {e}")
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
        return None


# Ceiling on the concatenated document's markup, independent of the archive
# size check above -- chapters can expand considerably once combined.
_EPUB_MAX_DOCUMENT_BYTES = 64 * 1024 * 1024
_EPUB_BODY_RE = re.compile(r"<body[^>]*>(.*)</body>", re.I | re.S)
_EPUB_SCRIPT_RE = re.compile(r"<script\b.*?</script\s*>", re.I | re.S)
_EPUB_STYLESHEET_RE = re.compile(r"""<link\b[^>]*\brel\s*=\s*["']?stylesheet["']?[^>]*>""", re.I)
_EPUB_HREF_ATTR_RE = re.compile(r"""\bhref\s*=\s*(["'])(.*?)\1""", re.I | re.S)
_EPUB_URL_ATTR_RE = re.compile(
    r"""(\b(?:src|href|poster|xlink:href)\s*=\s*)(["'])(.*?)\2""", re.I | re.S
)
_EPUB_CSS_URL_RE = re.compile(r"""url\(\s*(["']?)([^)"']+)\1\s*\)""", re.I)
# Anything already carrying a scheme, protocol-relative, or a bare fragment.
# Also catches proprietary schemes (kindle:embed:...) found in converted books.
_EPUB_ABSOLUTE_URL_RE = re.compile(r"^(?:[a-zA-Z][a-zA-Z0-9+.\-]*:|//|#)")

# Same reading-width cap the PDF viewer uses (_PDF_DISPLAY_MAX_WIDTH), applied
# here as CSS since the content reflows rather than being a fixed-size page.
# Without it the text runs the full width of the preview column, which for a
# PDF meant an absurdly large page and for prose means unreadably long lines.
# The cap is on the text block only -- images may still use the full width --
# and it scales with the zoom level, exactly as the PDF page does.
_EPUB_READER_CSS = f"""
html, body {{ margin: 0; padding: 0; }}
.mc-epub-chapter {{
    max-width: {_PDF_DISPLAY_MAX_WIDTH}px;
    margin: 0 auto;
    padding: 1.5em 1.75em;
}}
.mc-epub-chapter + .mc-epub-chapter {{ border-top: 1px solid rgba(128,128,128,0.35); }}
img, svg, video {{ max-width: 100%; height: auto; }}
"""

# Reports the chapter under the viewport's midpoint back to the extension, so
# the chapter counter tracks scrolling the way the PDF page counter does --
# the midpoint rule is the same one _current_pdf_page uses. This is the only
# script permitted to run in the document: it carries the nonce that the
# generated Content-Security-Policy names, and that policy also denies
# connect-src outright, so neither this nor anything in the book can reach
# the network. Kept as a plain string (not an f-string) so its braces need
# no escaping; only the nonce on the <script> tag is substituted.
_EPUB_READER_JS = """
(function () {
  var sections = document.querySelectorAll('.mc-epub-chapter');
  var current = -1, ticking = false;
  function report() {
    var middle = window.innerHeight / 2, found = 0;
    for (var i = 0; i < sections.length; i++) {
      if (sections[i].getBoundingClientRect().top <= middle) { found = i; } else { break; }
    }
    if (found !== current) {
      current = found;
      try { window.webkit.messageHandlers.mcReader.postMessage(found); } catch (e) {}
    }
  }
  window.addEventListener('scroll', function () {
    if (ticking) { return; }
    ticking = true;
    window.requestAnimationFrame(function () { ticking = false; report(); });
  }, { passive: true });
  window.addEventListener('load', report);
  report();
})();
"""


def _rewrite_epub_url(
    raw: str, chapter_dir: str, anchors: dict[str, str], epub_root: str
) -> str | None:
    """Resolve one relative reference against the chapter it came from, or
    None to leave it untouched.

    Links pointing at another chapter become in-document fragments, so
    following one keeps the reader inside the single combined document
    instead of navigating away from it; everything else becomes an absolute
    file: URI, which makes chapters from different directories safe to
    concatenate. References that resolve to nothing are left exactly as
    they were -- they were already broken in the source book, and inventing
    a path for them would only hide that."""
    raw = raw.strip()
    if not raw or raw.startswith("#"):
        return None
    if _EPUB_ABSOLUTE_URL_RE.match(raw):
        # Embedded data remains useful for cover art. Every other external
        # scheme is replaced rather than merely relying on CSP: file: would
        # otherwise be allowed for the book's legitimate extracted assets.
        if raw.lower().startswith("data:"):
            return None
        if raw.lower().startswith("file:"):
            path = Gio.File.new_for_uri(raw.partition("#")[0]).get_path()
            if path is not None and _path_is_within(epub_root, path):
                return raw
        return "about:blank"
    ref, _, fragment = raw.partition("#")
    # Two separate decodings, in this order, to get from markup to a path:
    # the attribute value is HTML-escaped (&amp;), and the URL inside it is
    # percent-encoded (%20). Filesystem names are neither. Skipping the
    # percent step silently breaks every reference whose file name contains
    # a space, such as "cover%20image.jpg" on disk as "cover image.jpg".
    ref = url_unquote(html_escaping.unescape(ref))
    target = os.path.realpath(os.path.join(chapter_dir, ref))
    if not _path_is_within(epub_root, target):
        return "about:blank"
    anchor = anchors.get(os.path.realpath(target))
    if anchor is not None:
        return f"#{anchor}"
    if not os.path.exists(target):
        return None
    uri = GLib.filename_to_uri(target, None)
    return f"{uri}#{fragment}" if fragment else uri


def _absolutise_epub_urls(
    markup: str, chapter_dir: str, anchors: dict[str, str], epub_root: str
) -> str:
    def attr(match: "re.Match[str]") -> str:
        prefix, quote, url = match.group(1), match.group(2), match.group(3)
        new = _rewrite_epub_url(url, chapter_dir, anchors, epub_root)
        if new is None:
            return match.group(0)
        return f"{prefix}{quote}{html_escaping.escape(new, quote=True)}{quote}"

    def css(match: "re.Match[str]") -> str:
        quote, url = match.group(1), match.group(2)
        new = _rewrite_epub_url(url, chapter_dir, anchors, epub_root)
        return match.group(0) if new is None else f"url({quote}{new}{quote})"

    return _EPUB_CSS_URL_RE.sub(css, _EPUB_URL_ATTR_RE.sub(attr, markup))


def _build_epub_document(
    tmpdir: str,
    chapters: list[str],
    cancellable: Gio.Cancellable | None = None,
) -> tuple[str, list[str]] | None:
    """Concatenate every chapter into one HTML file and return its path.

    This is what gives the EPUB reader the same uninterrupted scrolling the
    PDF viewer has. Loading one chapter at a time could never do that: each
    load starts a fresh scroll region, so the end of a chapter is necessarily
    a hard stop. As a single document, reaching the end of a chapter is not
    an event at all -- the next one is simply the next thing on the page.

    Returns the document path alongside the chapters actually written into
    it, which is not always every chapter passed in -- one can be unreadable,
    or the size cap can cut the tail off. The caller must use the returned
    list, or the chapter counter would promise chapters the document has no
    anchor for and jumping to them would silently do nothing.

    Written as .html rather than .xhtml deliberately: chapter markup is
    third-party and frequently not well-formed XML, and the HTML parser is
    lenient exactly where the XML one would reject the entire document."""
    # First pass: read what is actually usable. Anchor numbering has to be
    # derived from this, not from the input list, so that anchor N always
    # means "the Nth section in the document".
    loaded: list[tuple[str, str]] = []
    total = 0
    for chapter in chapters:
        if cancellable is not None and cancellable.is_cancelled():
            return None
        try:
            with open(chapter, "rb") as handle:
                markup = handle.read().decode("utf-8", errors="replace")
        except OSError as e:
            _log(f"EPUB chapter unreadable, skipping {chapter}: {e}")
            continue
        total += len(markup)
        if total > _EPUB_MAX_DOCUMENT_BYTES:
            _log(f"EPUB truncated at {len(loaded)} of {len(chapters)} chapters (size cap)")
            break
        loaded.append((chapter, markup))

    if not loaded:
        return None

    included = [chapter for chapter, _ in loaded]
    anchors = {os.path.realpath(path): f"mc-ch-{index}" for index, path in enumerate(included)}
    stylesheets: list[str] = []
    seen_styles: set[str] = set()
    sections: list[str] = []

    for index, (chapter, markup) in enumerate(loaded):
        chapter_dir = os.path.dirname(chapter)

        # Chapter stylesheets are hoisted into the combined head; they are
        # resolved with no anchor map, since a stylesheet is never a chapter.
        for link in _EPUB_STYLESHEET_RE.findall(markup):
            href_match = _EPUB_HREF_ATTR_RE.search(link)
            if not href_match:
                continue
            resolved = _rewrite_epub_url(href_match.group(2), chapter_dir, {}, tmpdir)
            if resolved and resolved not in seen_styles:
                seen_styles.add(resolved)
                stylesheets.append(resolved)

        body_match = _EPUB_BODY_RE.search(markup)
        body = body_match.group(1) if body_match else markup
        # The book's own scripts are stripped outright. The policy below
        # would refuse to run them anyway (they carry no nonce), so this is
        # belt and braces -- but it also keeps the document readable.
        body = _EPUB_SCRIPT_RE.sub("", body)
        body = _absolutise_epub_urls(body, chapter_dir, anchors, tmpdir)
        sections.append(f'<section class="mc-epub-chapter" id="mc-ch-{index}">{body}</section>')

    links = "\n".join(
        f'<link rel="stylesheet" href="{html_escaping.escape(href, quote=True)}">'
        for href in stylesheets
    )
    # The book is untrusted, and the reader script needs JavaScript switched
    # on to follow the scroll position -- so the document carries a policy
    # that grants exactly that one script and nothing else. 'default-src
    # none' denies connect-src, which is what forbids fetch/XHR/WebSocket:
    # nothing here can call home, whether it came from the book or from us.
    # Scripts are allowed only with this nonce, which no chapter markup has,
    # so inline handlers (onclick=...) in the book stay dead too.
    nonce = secrets.token_urlsafe(16)
    policy = (
        "default-src 'none'; "
        "img-src file: data:; "
        "style-src file: 'unsafe-inline'; "
        "font-src file: data:; "
        "media-src file:; "
        f"script-src 'nonce-{nonce}'"
    )
    document = (
        '<!DOCTYPE html>\n<html><head><meta charset="utf-8">\n'
        f'<meta http-equiv="Content-Security-Policy" content="{policy}">\n'
        f"{links}\n<style>{_EPUB_READER_CSS}</style>\n</head>\n"
        f"<body>\n{''.join(sections)}\n"
        f'<script nonce="{nonce}">{_EPUB_READER_JS}</script>\n'
        "</body></html>"
    )
    out_path = os.path.join(tmpdir, "_mc_reader.html")
    try:
        with open(out_path, "w", encoding="utf-8") as handle:
            handle.write(document)
    except OSError as e:
        _log(f"EPUB document build failed: {e}")
        return None
    return out_path, included


from nautilus_my_computer.common import (
    _CARD_WIDTH,
    _COLUMN_MIN_WIDTH,
    _COLUMN_PREVIEW_IMAGE_MAX_WIDTH,
    _COLUMN_PREVIEW_IMAGE_SIZE,
    _COLUMN_PREVIEW_WIDTH,
    _COLUMN_ROW_ICON_SIZE,
    _COLUMN_ROW_SPACING,
    _COLUMN_WIDTH,
    _DISK_CARD_ICON_SPACING,
    _DISK_CARD_MARGIN_BOTTOM,
    _DISK_CARD_MARGIN_END,
    _DISK_CARD_MARGIN_START,
    _DISK_CARD_MARGIN_TOP,
    _GROUP_ICON,
    _INTERNAL_FSTYPES,
    _LIST_BAR_MAX_WIDTH,
    N_,
    _,
    _disk_icon_size,
    _disk_list_icon_size,
    _folder_card_width,
    _format_size,
    _gicon_renders,
    _icon_name_renders,
    _log,
    _n,
    _native,
    _nautilus_icon_size,
    _nautilus_list_icon_size,
    _resolve_custom_gicon,
    _set_regular_icon,
)
from nautilus_my_computer.components import set_row_active, set_row_selected


class MyComputerDiskCard(Gtk.Box):
    """Self-contained card: renders one MountInfo as a grid card or a list row."""

    __gtype_name__ = "MyComputerDiskCard"

    def __init__(self, ext, win: Gtk.Window, view_mode: str, model, group_key: str) -> None:
        super().__init__()
        self._ext = ext
        self._win = win
        self.view_mode = view_mode
        self.model = model
        self.group_key = group_key
        self.usage_bar: Gtk.LevelBar | None = None
        self.sub_label: Gtk.Label | None = None

        self.get_style_context().add_class("nautilus-view-cell")
        # Deliberately not set_focusable/set_focus_on_click (issue #161): a focusable inner
        # widget lets gtk_flow_box_child_focus's backward-entry branch grab focus on this Box
        # directly instead of the FlowBoxChild wrapper, so Shift+Tab landing fresh on this card
        # would skip the wrapper's own selection/focus-visible state entirely
        # (gtk_flow_box_child_set_focus never runs). Arrow-key nav is unaffected -- it goes
        # through GtkFlowBox's own move-cursor handler, which always focuses the wrapper.
        self._build()

        # One gesture on all buttons, dispatched from "pressed"/"released",
        # mirroring nautilus-list-base.c:880-886 (on_item_click_pressed /
        # button=0). Primary is claimed and driven end to end by
        # _on_card_pressed/_on_card_released (#161) rather than left to
        # FlowBox's own competing click gesture.
        click = Gtk.GestureClick()
        click.set_button(0)
        click.connect("pressed", self._ext._on_card_pressed, self._win, self)
        click.connect("released", self._ext._on_card_released, self._win, self)
        self.add_controller(click)

    @property
    def is_list(self) -> bool:
        return self.view_mode == "list-view"

    @property
    def nav_uri(self) -> str:
        m = self.model
        return m.nav_uri or (Gio.File.new_for_path(m.mountpoint).get_uri() if m.mountpoint else "")

    def _info(self) -> tuple:
        """(has_size, sub_text, bar_value) for the current model state."""
        m = self.model
        has_size = m.total > 0
        if not m.is_mounted:
            return has_size, _("Not mounted"), 0.0
        if has_size:
            sub_text = _("{free} free of {total}").format(
                free=_format_size(m.free), total=_format_size(m.total)
            )
            return has_size, sub_text, min(m.percent / 100.0, 1.0)
        return has_size, self.nav_uri, 0.0

    def _icon_name(self) -> str:
        return _GROUP_ICON.get(self.group_key, "drive-harddisk")

    def _build(self) -> None:
        m = self.model
        has_size, sub_text, bar_value = self._info()

        if not m.is_mounted:
            self.get_style_context().add_class("unmounted")
        if m.is_hidden:
            self.get_style_context().add_class("hidden-file")
        if m.mountpoint:
            fstype_part = f" ({m.fstype})" if m.fstype and m.fstype not in _INTERNAL_FSTYPES else ""
            self.set_tooltip_text(f"{m.mountpoint}{fstype_part}")

        if self.is_list:
            self._build_list(has_size, sub_text, bar_value)
        else:
            self._build_grid(has_size, sub_text, bar_value)

    def _build_grid(self, has_size: bool, sub_text: str, bar_value: float) -> None:
        m = self.model
        self.set_orientation(Gtk.Orientation.HORIZONTAL)
        self.set_spacing(_DISK_CARD_ICON_SPACING)
        self.set_margin_start(_DISK_CARD_MARGIN_START)
        self.set_margin_end(_DISK_CARD_MARGIN_END)
        self.set_margin_top(_DISK_CARD_MARGIN_TOP)
        self.set_margin_bottom(_DISK_CARD_MARGIN_BOTTOM)
        self.set_size_request(_CARD_WIDTH, -1)

        icon = Gtk.Image()
        icon.set_pixel_size(_disk_icon_size())
        icon.set_valign(Gtk.Align.CENTER)
        # icon.set_margin_end(12)
        if _gicon_renders(m.gio_icon):
            icon.set_from_gicon(m.gio_icon)
        else:
            icon.set_from_icon_name(self._icon_name())
        self.append(icon)

        details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        details.set_hexpand(True)
        details.set_valign(Gtk.Align.CENTER)

        display_name = m.display_name or self.nav_uri.rsplit("/", 1)[-1] or "/"
        name_lbl = Gtk.Label(label=display_name)
        name_lbl.set_xalign(0.0)
        name_lbl.set_ellipsize(3)
        name_lbl.set_max_width_chars(0)
        details.append(name_lbl)

        bar = Gtk.LevelBar()
        bar.set_mode(Gtk.LevelBarMode.CONTINUOUS)
        bar.set_min_value(0.0)
        bar.set_max_value(1.0)
        bar.set_hexpand(True)
        bar.set_value(bar_value)
        bar.set_visible(has_size)
        bar.get_style_context().add_class("diskinfo-bar")
        details.append(bar)

        sub_lbl = Gtk.Label(label=sub_text)
        sub_lbl.set_xalign(0.0)
        sub_lbl.set_ellipsize(3)
        sub_lbl.set_max_width_chars(0)
        sub_lbl.get_style_context().add_class("diskinfo-subtext")
        sub_lbl.get_style_context().add_class("caption")
        details.append(sub_lbl)

        self.append(details)
        self.usage_bar = bar if has_size else None
        self.sub_label = sub_lbl if has_size else None

    def _build_list(self, has_size: bool, sub_text: str, bar_value: float) -> None:
        m = self.model
        self.set_orientation(Gtk.Orientation.HORIZONTAL)
        self.set_spacing(12)
        self.set_margin_start(6)
        self.set_margin_end(6)
        self.set_margin_top(6)
        self.set_margin_bottom(6)
        self.set_hexpand(True)

        icon = Gtk.Image()
        icon.set_pixel_size(_disk_list_icon_size())
        icon.set_valign(Gtk.Align.CENTER)
        if _gicon_renders(m.gio_icon):
            icon.set_from_gicon(m.gio_icon)
        else:
            icon.set_from_icon_name(self._icon_name())
        self.append(icon)

        display_name = m.display_name or self.nav_uri.rsplit("/", 1)[-1] or "/"
        name_lbl = Gtk.Label(label=display_name)
        name_lbl.set_xalign(0.0)
        name_lbl.set_ellipsize(3)
        name_lbl.set_valign(Gtk.Align.CENTER)
        self.append(name_lbl)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        self.append(spacer)

        sub_lbl = Gtk.Label(label=sub_text)
        sub_lbl.set_xalign(1.0)
        sub_lbl.set_ellipsize(3)
        sub_lbl.set_valign(Gtk.Align.CENTER)
        sub_lbl.get_style_context().add_class("diskinfo-subtext")
        sub_lbl.get_style_context().add_class("caption")
        self.append(sub_lbl)

        bar = Gtk.LevelBar()
        bar.set_mode(Gtk.LevelBarMode.CONTINUOUS)
        bar.set_min_value(0.0)
        bar.set_max_value(1.0)
        bar.set_size_request(_LIST_BAR_MAX_WIDTH, -1)
        bar.set_valign(Gtk.Align.CENTER)
        bar.set_value(bar_value)
        bar.set_visible(has_size)
        bar.get_style_context().add_class("diskinfo-bar")
        self.append(bar)

        self.usage_bar = bar if has_size else None
        self.sub_label = sub_lbl if has_size else None

    def update_usage(self, m) -> None:
        """Patch the LevelBar + sub-label in place; called by poll workers."""
        self.model = m
        total, free = m.total, m.free
        if self.usage_bar is not None and total > 0:
            self.usage_bar.set_value(min(1.0, (total - free) / total))
        if self.sub_label is not None and total > 0:
            self.sub_label.set_label(
                _("{free} free of {total}").format(
                    free=_format_size(free), total=_format_size(total)
                )
            )


class MyComputerFolderCard(Gtk.Widget):
    """Self-contained card: renders one PreferredFolder as a grid card
    (native grid layout, mirroring NautilusGridCell) or a list row."""

    __gtype_name__ = "MyComputerFolderCard"

    def __init__(
        self,
        ext,
        win: Gtk.Window,
        view_mode: str,
        model,
        interactive: bool = True,
        reorderable: bool = True,
    ) -> None:
        super().__init__()
        self._ext = ext
        self._win = win
        self.view_mode = view_mode
        self.model = model
        self.icon: Gtk.Image | None = None
        self.name_label: Gtk.Label | None = None
        # Up to 3 caption lines below the name (Nautilus "Captions", grid
        # mode only -- list mode never creates them, so set_captions() is a
        # safe no-op there).
        self.caption_first: Gtk.Label | None = None
        self.caption_second: Gtk.Label | None = None
        self.caption_third: Gtk.Label | None = None
        # Only populated in list mode -- see _build_list/do_measure etc.
        self._list_box: Gtk.Box | None = None

        self.get_style_context().add_class("nautilus-view-cell")
        self._build()
        self._apply_hidden_state(model.is_hidden)

        # interactive=False skips right-click/drag-source wiring, for clones
        # that aren't draggable or right-clickable themselves (drag ghost,
        # reorder placeholder). reorderable=False additionally skips the
        # drop/motion controllers -- only the drag ghost needs that, as it
        # floats outside the FlowBox and is never a drop target. See
        # _build_drag_ghost / _build_reorder_placeholder.
        if interactive:
            # One gesture on all buttons, dispatched from "pressed"/"released",
            # mirroring nautilus-list-base.c:880-886 (on_item_click_pressed /
            # button=0). Primary is claimed and driven end to end by
            # _on_card_pressed/_on_card_released (#161) rather than left to
            # FlowBox's own competing click gesture.
            click = Gtk.GestureClick()
            click.set_button(0)
            click.connect("pressed", self._ext._on_card_pressed, self._win, self)
            click.connect("released", self._ext._on_card_released, self._win, self)
            self.add_controller(click)

            self._wire_drag()

        if reorderable:
            self._wire_reorder_preview()

    @property
    def is_list(self) -> bool:
        return self.view_mode == "list-view"

    @property
    def nav_uri(self) -> str:
        return self.model.nav_uri

    def _wire_reorder_preview(self) -> None:
        """Persist a FlowBox reorder only after a successful MOVE drop."""
        motion = Gtk.DropControllerMotion()
        motion.connect("enter", self._on_reorder_enter)
        self.add_controller(motion)

        drop = Gtk.DropTarget.new(MyComputerFolderCard, Gdk.DragAction.MOVE)
        drop.connect("drop", self._on_reorder_drop)
        self.add_controller(drop)

    def _on_reorder_drop(self, _target, value, _x, _y) -> bool:
        placeholder = getattr(self._ext, "_reorder_placeholder", None)
        # Without a placeholder the drag never set up (see _on_drag_begin);
        # committing here would skip `value` and drop it from the order.
        if placeholder is None:
            return False
        dst_child = self.get_parent()
        flow = dst_child.get_parent() if dst_child is not None else None
        if not isinstance(flow, Gtk.FlowBox):
            return False
        keys = []
        child = flow.get_first_child()
        while child is not None:
            card = child.get_child()
            if card is placeholder:
                keys.append(value.model.key)
            elif isinstance(card, MyComputerFolderCard) and card is not value:
                keys.append(card.model.key)
            child = child.get_next_sibling()
        _log(
            f"preferred folders dragging dropped: {value.model.display_name}/ "
            f"position {value.model.index}"
        )
        GLib.idle_add(self._ext._commit_preferred_order, keys)
        return True

    def _on_reorder_enter(self, _ctrl, _x, _y) -> None:
        """Move only the dimmed placeholder into the landing slot -- the real,
        stateful dragged card is never reparented mid-drag (see
        _on_drag_begin), so this can never lose/flicker its highlight the way
        reparenting the real FlowBoxChild wrapper on every crossing did."""
        if getattr(self._ext, "_folder_reordering", False):
            return
        placeholder = getattr(self._ext, "_reorder_placeholder", None)
        if placeholder is None or placeholder is self:
            return
        placeholder_child = placeholder.get_parent()
        dst_child = self.get_parent()
        if not isinstance(placeholder_child, Gtk.FlowBoxChild) or not isinstance(
            dst_child, Gtk.FlowBoxChild
        ):
            return
        flow = dst_child.get_parent()
        if not isinstance(flow, Gtk.FlowBox):
            return
        dst_index = dst_child.get_index()
        if placeholder_child.get_index() == dst_index:
            return

        self._ext._folder_reordering = True
        try:
            placeholder_child.set_child(None)
            flow.remove(placeholder_child)
            flow.insert(placeholder, dst_index)
            new_child = placeholder.get_parent()
            if isinstance(new_child, Gtk.FlowBoxChild):
                new_child.add_css_class("mc-selected")
        finally:
            self._ext._folder_reordering = False

    def _wire_drag(self) -> None:
        drag = Gtk.DragSource()
        drag.set_actions(Gdk.DragAction.MOVE)
        # CAPTURE, matching nautilus-list-base.c:1373 -- runs ahead of the
        # BUBBLE-phase click gesture above so a drag on primary press isn't
        # shadowed by it.
        drag.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        drag.connect("prepare", self._on_drag_prepare)
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-end", self._on_drag_end)
        drag.connect("drag-cancel", self._on_drag_cancel)
        self.add_controller(drag)

    def _on_drag_prepare(self, _source, _x, _y):
        return Gdk.ContentProvider.new_for_value(self)

    def _on_drag_begin(self, _source, drag) -> None:
        Gtk.DragIcon.get_for_drag(drag).set_child(self._build_drag_ghost())

        # Until the drop is confirmed, the drag is only a visual cue: hide
        # the real card's slot (it stays alive, untouched, off-layout) and
        # show a dimmed placeholder in its place. The placeholder -- not the
        # real card -- is what _on_reorder_enter moves between slots.
        src_wrapper = self.get_parent()
        placeholder = None
        if isinstance(src_wrapper, Gtk.FlowBoxChild):
            flow = src_wrapper.get_parent()
            if isinstance(flow, Gtk.FlowBox):
                src_index = src_wrapper.get_index()
                src_wrapper.set_visible(False)
                placeholder = self._build_reorder_placeholder()
                flow.insert(placeholder, src_index)
                new_child = placeholder.get_parent()
                if isinstance(new_child, Gtk.FlowBoxChild):
                    new_child.add_css_class("mc-selected")

        _log(
            f"preferred folders dragging started: {self.model.display_name}/ "
            f"position {self.model.index}"
        )
        self._ext._reorder_placeholder = placeholder
        self._ext._reorder_source_wrapper = src_wrapper

    def _on_drag_end(self, _source, _drag, _delete_data) -> None:
        placeholder = getattr(self._ext, "_reorder_placeholder", None)
        if placeholder is not None:
            placeholder_child = placeholder.get_parent()
            if isinstance(placeholder_child, Gtk.FlowBoxChild):
                flow = placeholder_child.get_parent()
                if isinstance(flow, Gtk.FlowBox):
                    placeholder_child.set_child(None)
                    flow.remove(placeholder_child)

        # Reveal the real card again. It was only hidden (never dimmed), so
        # nothing else needs restoring; a committed drop repopulates anyway.
        src_wrapper = getattr(self._ext, "_reorder_source_wrapper", None)
        if src_wrapper is not None:
            src_wrapper.set_visible(True)
            src_wrapper.remove_css_class("mc-selected")

        self._ext._reorder_placeholder = None
        self._ext._reorder_source_wrapper = None

    def _on_drag_cancel(self, _source, _drag, _reason) -> bool:
        self._on_drag_end(_source, _drag, False)
        return False

    def _build_clone(self, reorderable: bool, dim: bool) -> "MyComputerFolderCard":
        """Full-size, non-interactive clone of this card in the same view
        mode. interactive=False drops the right-click/drag-source wiring;
        `reorderable` and `dim` tailor it for its role (see the two callers)."""
        clone = MyComputerFolderCard(
            self._ext,
            self._win,
            self.view_mode,
            self.model,
            interactive=False,
            reorderable=reorderable,
        )
        clone.set_focusable(False)
        clone.set_captions([None, None, None])
        if dim:
            clone._set_content_opacity(0.55)
        return clone

    def _build_drag_ghost(self) -> "MyComputerFolderCard":
        """Clone that floats under the cursor via Gtk.DragIcon. It never sits
        in the FlowBox, so it needs no reorder controllers."""
        return self._build_clone(reorderable=False, dim=False)

    def _build_reorder_placeholder(self) -> "MyComputerFolderCard":
        """Dimmed stand-in shown in the FlowBox at the landing slot. It keeps
        the reorder drop/motion controllers so a drop landing directly on its
        own slot still fires _on_reorder_drop, but carries no drag source of
        its own -- only the real card is ever the drag source."""
        return self._build_clone(reorderable=True, dim=True)

    def do_measure(self, orientation, for_size):
        """Mirror NautilusGridCell's fixed-width, height-for-width measure
        (grid mode), or delegate straight through to the list row's box."""
        if self.is_list:
            return self._list_box.measure(orientation, for_size)

        icon_size = _nautilus_icon_size()
        width = _folder_card_width()
        if orientation == Gtk.Orientation.HORIZONTAL:
            labels_min, _, _, _ = self._labels_box.measure(orientation, -1)
            if labels_min > width:
                width = labels_min
            icon_min, _, _, _ = self.icon.measure(orientation, -1)
            if icon_min > icon_size:
                width += icon_min - icon_size
            emblems_min, _, _, _ = self._emblems_box.measure(orientation, -1)
            if emblems_min > 18:
                width += 2 * (emblems_min - 18)
            return (width, width, -1, -1)

        labels_min, labels_natural, min_baseline, nat_baseline = self._labels_box.measure(
            Gtk.Orientation.VERTICAL, width
        )
        if min_baseline != -1:
            min_baseline += icon_size + 6
        if nat_baseline != -1:
            nat_baseline += icon_size + 6
        return (
            icon_size + 6 + labels_min,
            icon_size + 6 + labels_natural,
            min_baseline,
            nat_baseline,
        )

    @staticmethod
    def _allocation_transform(x: int, y: int):
        return Gsk.Transform.new().translate(Graphene.Point().init(x, y))

    def do_size_allocate(self, width: int, height: int, baseline: int) -> None:
        """Allocate the icon, emblem gutter, and labels exactly as Nautilus
        does (grid mode), or the list row's box across the full cell."""
        if self.is_list:
            self._list_box.allocate(width, height, baseline, None)
            return

        icon_size = _nautilus_icon_size()
        self.icon.allocate(
            width - 36,
            icon_size,
            -1,
            self._allocation_transform(18, 0),
        )
        emblem_x = width - 18 if self.get_direction() == Gtk.TextDirection.LTR else 0
        self._emblems_box.allocate(
            18,
            icon_size,
            -1,
            self._allocation_transform(emblem_x, 0),
        )
        labels_baseline = baseline - icon_size - 6 if baseline != -1 else -1
        self._labels_box.allocate(
            width,
            max(0, height - icon_size - 6),
            labels_baseline,
            self._allocation_transform(0, icon_size + 6),
        )

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        if self.is_list:
            self.snapshot_child(self._list_box, snapshot)
            return
        for child in (self.icon, self._emblems_box, self._labels_box):
            self.snapshot_child(child, snapshot)

    def do_dispose(self) -> None:
        if self._list_box is not None:
            self._list_box.unparent()
        for child in (self.icon, self._emblems_box, self._labels_box):
            if child is not None:
                child.unparent()
        super().do_dispose()

    def _set_icon(self, icon: Gtk.Image) -> None:
        pf = self.model
        if _gicon_renders(pf.gio_icon):
            icon.set_from_gicon(pf.gio_icon)
        elif _icon_name_renders(pf.icon_name):
            icon.set_from_icon_name(pf.icon_name)
        else:
            icon.set_from_icon_name("folder")

    def _build(self) -> None:
        if self.is_list:
            self._build_list()
        else:
            self._build_grid()

    def _build_list(self) -> None:
        """List-view compact cell: keep Preferred Folders multi-column (the
        section's FlowBox stays in grid layout -- see always_grid on its
        MyComputerCardGroup) while rendering each card as a compact
        horizontal icon+name row instead of the full icon-grid cell."""
        pf = self.model
        self.set_valign(Gtk.Align.FILL)

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.set_margin_start(6)
        box.set_margin_end(6)
        box.set_margin_top(3)
        box.set_margin_bottom(3)
        box.set_halign(Gtk.Align.START)
        box.set_valign(Gtk.Align.CENTER)
        box.set_parent(self)

        icon = Gtk.Image()
        icon.set_pixel_size(_nautilus_list_icon_size())
        icon.set_valign(Gtk.Align.CENTER)
        self._set_icon(icon)
        box.append(icon)

        labels_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        labels_box.set_valign(Gtk.Align.CENTER)
        name_lbl = Gtk.Label(label=pf.display_name)
        name_lbl.set_xalign(0.0)
        name_lbl.set_valign(Gtk.Align.CENTER)
        name_lbl.set_max_width_chars(14)
        name_lbl.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        labels_box.append(name_lbl)
        box.append(labels_box)

        self._list_box = box
        self.icon = icon
        self.name_label = name_lbl

    def _build_grid(self) -> None:
        pf = self.model
        self.set_valign(Gtk.Align.START)

        icon = Gtk.Image()
        icon.set_pixel_size(_nautilus_icon_size())
        icon.set_halign(Gtk.Align.CENTER)
        icon.set_valign(Gtk.Align.CENTER)
        self._set_icon(icon)
        icon.set_parent(self)

        emblems_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        emblems_box.set_halign(Gtk.Align.END)
        emblems_box.set_margin_start(2)
        emblems_box.add_css_class("dim-label")
        emblems_box.set_parent(self)

        labels_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        labels_box.add_css_class("icon-ui-labels-box")
        labels_box.set_parent(self)

        name_lbl = Gtk.Label(label=pf.display_name)
        name_lbl.set_justify(Gtk.Justification.CENTER)
        name_lbl.set_halign(Gtk.Align.CENTER)
        name_lbl.set_wrap(True)
        name_lbl.set_wrap_mode(Pango.WrapMode.WORD)
        name_lbl.set_lines(3)
        name_lbl.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        attributes = Pango.AttrList()
        attributes.insert(Pango.attr_insert_hyphens_new(False))
        name_lbl.set_attributes(attributes)
        labels_box.append(name_lbl)

        # Up to 3 caption lines (Nautilus "Captions" feature, icon-view only).
        # Built empty/hidden -- set_captions() fills them in once resolved.
        # Style/wrap matches nautilus-grid-cell.c's caption_widget_new() exactly.
        caption_labels = []
        for _i in range(3):
            cap_lbl = Gtk.Label(label="")
            cap_lbl.get_style_context().add_class("caption")
            cap_lbl.get_style_context().add_class("dim-label")
            cap_lbl.set_justify(Gtk.Justification.CENTER)
            cap_lbl.set_halign(Gtk.Align.CENTER)
            cap_lbl.set_valign(Gtk.Align.START)
            cap_lbl.set_wrap(True)
            cap_lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            cap_lbl.set_lines(2)
            cap_lbl.set_ellipsize(Pango.EllipsizeMode.END)
            cap_lbl.set_visible(False)
            labels_box.append(cap_lbl)
            caption_labels.append(cap_lbl)

        self.icon = icon
        self._emblems_box = emblems_box
        self._labels_box = labels_box
        self.name_label = name_lbl
        self.caption_first, self.caption_second, self.caption_third = caption_labels

    def update_metadata(self, pf) -> None:
        """Patch the icon + name label in place; called once async metadata resolves."""
        self.model = pf
        # Full precedence (gicon -> icon_name -> "folder"), not just set_from_gicon:
        # a special place (recent/starred/network) whose custom icon was removed
        # comes back with gio_icon=None and must revert to its token icon_name
        # default, not keep the stale custom gicon (issue #83).
        if self.icon is not None:
            self._set_icon(self.icon)
        if self.name_label is not None:
            self.name_label.set_label(pf.display_name)
        self._apply_hidden_state(pf.is_hidden)

    def set_captions(self, lines: list) -> None:
        """Update the up-to-3 caption lines below the name (Nautilus
        "Captions"). lines[i] is None/empty to hide that line."""
        for label, text in zip(
            (self.caption_first, self.caption_second, self.caption_third), lines
        ):
            if label is None:
                continue
            if text:
                label.set_label(text)
                label.set_visible(True)
            else:
                label.set_visible(False)

    def _apply_hidden_state(self, is_hidden: bool) -> None:
        self._set_content_opacity(0.55 if is_hidden else 1.0)

    def _set_content_opacity(self, opacity: float) -> None:
        # Dim only the icon + label, not self -- self carries the "nautilus-view-cell"
        # class that draws Nautilus's native selection/focus/hover background and
        # border. GtkWidget.opacity dims a widget's own render as one group, so
        # setting it on self would dim that border/background too.
        if self.icon is not None:
            self.icon.set_opacity(opacity)
        if self.name_label is not None:
            self.name_label.set_opacity(opacity)


class MyComputerToggleButton(Gtk.Box):
    """Dynamic N-way segmented toggle: flat Gtk.ToggleButtons in a light
    pill, with a 1px separator between adjacent buttons that hides whenever
    either neighbor is selected or hovered -- that neighbor's own highlight
    already reads as the boundary, so the divider would be redundant.

    Built from Gtk.Box/Gtk.ToggleButton/Gtk.Separator instead of
    Adw.ToggleGroup (libadwaita 1.7+ only), so it renders identically on
    GNOME 47 and 48+. See .mc-toggle-group/.mc-toggle-btn in
    my_computer_view._CSS for the pill/button styling.
    """

    __gtype_name__ = "MyComputerToggleButton"
    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_FIRST, None, (str,))}

    def __init__(self, segments, height: int = -1) -> None:
        """segments: iterable of (name, icon, tooltip_text), where icon is
        either an icon name (str) or a Gio.Icon (e.g. a bundled fallback SVG,
        see column_view._resolve_column_icon). height defaults to -1 (natural
        size, stretched to fill via valign=FILL below so it matches the
        containing toolbar's height); pass an explicit value to pin it
        instead."""
        # 1px spacing matches the separator's own stroke width, so the gap
        # between buttons stays visually constant whether the separator is
        # shown or hidden (opacity toggle in _update_separators).
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=1)
        self.add_css_class("mc-toggle-group")
        self.set_size_request(-1, height)
        self.set_valign(Gtk.Align.FILL)

        self._buttons: dict[str, Gtk.ToggleButton] = {}
        self._hovered: dict[str, bool] = {}
        self._separators: list[Gtk.Separator] = []  # gap i sits between order[i]/order[i+1]
        self._order: list[str] = []
        self._active_name: str | None = None
        self._syncing = False

        first_btn = None
        for i, (name, icon, tooltip) in enumerate(segments):
            if i > 0:
                sep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
                sep.add_css_class("mc-toggle-sep")
                sep.set_margin_top(6)
                sep.set_margin_bottom(6)
                self.append(sep)
                self._separators.append(sep)

            btn = Gtk.ToggleButton(child=self._make_segment_image(icon), tooltip_text=tooltip)
            btn.add_css_class("flat")
            btn.add_css_class("mc-toggle-btn")
            if first_btn is None:
                first_btn = btn
            else:
                btn.set_group(first_btn)
            btn.connect("toggled", self._on_button_toggled, name)

            motion = Gtk.EventControllerMotion()
            motion.connect("enter", self._on_button_enter, name)
            motion.connect("leave", self._on_button_leave, name)
            btn.add_controller(motion)

            self.append(btn)
            self._buttons[name] = btn
            self._hovered[name] = False
            self._order.append(name)

        self._update_separators()

    @staticmethod
    def _make_segment_image(icon) -> Gtk.Image:
        """icon: an icon name (str) or a Gio.Icon (e.g. a bundled fallback
        SVG, see column_view._resolve_column_icon)."""
        image = (
            Gtk.Image.new_from_gicon(icon)
            if isinstance(icon, Gio.Icon)
            else Gtk.Image.new_from_icon_name(icon)
        )
        image.set_pixel_size(-1)
        image.set_margin_start(8)
        image.set_margin_end(8)
        image.set_valign(Gtk.Align.CENTER)
        image.set_halign(Gtk.Align.CENTER)
        return image

    def _on_button_enter(self, _ctrl, _x, _y, name: str) -> None:
        self._hovered[name] = True
        self._update_separators()

    def _on_button_leave(self, _ctrl, name: str) -> None:
        self._hovered[name] = False
        self._update_separators()

    def _on_button_toggled(self, btn: Gtk.ToggleButton, name: str) -> None:
        if self._syncing or not btn.get_active():
            return
        self._active_name = name
        self._update_separators()
        self.emit("changed", name)

    def _update_separators(self) -> None:
        # Toggling a CSS class (not Widget.set_opacity()) is what lets the
        # 200ms transition on .mc-toggle-sep actually animate.
        for i, sep in enumerate(self._separators):
            left, right = self._order[i], self._order[i + 1]
            hidden = (
                self._active_name in (left, right)
                or self._hovered.get(left)
                or self._hovered.get(right)
            )
            if hidden:
                sep.add_css_class("mc-toggle-sep-hidden")
            else:
                sep.remove_css_class("mc-toggle-sep-hidden")

    def get_active_name(self) -> str | None:
        return self._active_name

    def set_active_name(self, name: str) -> None:
        btn = self._buttons.get(name)
        if btn is None or btn.get_active():
            return
        self._syncing = True
        try:
            btn.set_active(True)
        finally:
            self._syncing = False
        self._active_name = name
        self._update_separators()

    def set_segment_enabled(self, name: str, enabled: bool) -> None:
        btn = self._buttons.get(name)
        if btn is not None:
            btn.set_sensitive(enabled)

    def set_segment_icon(self, name: str, icon) -> None:
        """Re-resolve a segment's icon after construction (icon: name or
        Gio.Icon, same contract as __init__'s segments) -- e.g. when the
        active icon theme changes live, see
        column_view._refresh_column_icon_all_windows."""
        btn = self._buttons.get(name)
        if btn is not None:
            btn.set_child(self._make_segment_image(icon))


class MyComputerCappedGridFlowBox(Gtk.FlowBox):
    """Homogeneous FlowBox that raises its column count as the window widens,
    capping each card's stretched width at max_card_width instead of letting a
    single row of cards grow past it -- same trigger native Nautilus grid view
    uses to add a column, applied to our homogeneous card cells."""

    __gtype_name__ = "MyComputerCappedGridFlowBox"

    def __init__(self, max_card_width: int, spacing: int, hard_max_cols: int) -> None:
        super().__init__()
        self._max_card_width = max_card_width
        self._spacing = spacing
        self._hard_max_cols = hard_max_cols
        self.set_max_children_per_line(hard_max_cols)

    def _cols_for_width(self, width: int) -> int:
        step = self._max_card_width + self._spacing
        cols = -(-(width + self._spacing) // step) if step > 0 else 1  # ceil division
        return max(1, min(cols, self._hard_max_cols))

    def do_measure(self, orientation, for_size):
        if orientation == Gtk.Orientation.VERTICAL and for_size > 0:
            self.set_max_children_per_line(self._cols_for_width(for_size))
        minimum, natural, _min_bl, _nat_bl = Gtk.FlowBox.do_measure(self, orientation, for_size)
        # A FlowBox has no baseline. Chaining up through PyGObject hands the
        # baseline out-params back as 0 instead of leaving them at -1, and GTK
        # warns on any non -1 horizontal baseline.
        return minimum, natural, -1, -1

    def do_size_allocate(self, width: int, height: int, baseline: int) -> None:
        self.set_max_children_per_line(self._cols_for_width(width))
        Gtk.FlowBox.do_size_allocate(self, width, height, baseline)


class MyComputerCardGroup(Gtk.Box):
    """A heading + FlowBox of cards. Dedups the section setup shared by the
    Preferred Folders block and each disk group in _populate()."""

    __gtype_name__ = "MyComputerCardGroup"

    def __init__(
        self,
        ext,
        win: Gtk.Window,
        label: str,
        view_mode: str,
        *,
        max_cols: int,
        col_spacing: int,
        row_spacing: int,
        always_grid: bool = False,
        homogeneous: bool = False,
        max_card_width: int = 0,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self._ext = ext
        self._size_group = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)

        heading = Gtk.Label(label=label)
        heading.set_xalign(0.0)
        heading.get_style_context().add_class("heading")
        heading.set_margin_top(12)
        heading.set_margin_start(6)
        self.append(heading)

        is_list = view_mode == "list-view" and not always_grid
        is_capped_grid = homogeneous and max_card_width > 0 and not is_list
        if is_capped_grid:
            self.flow = MyComputerCappedGridFlowBox(max_card_width, col_spacing, max_cols)
        else:
            self.flow = Gtk.FlowBox()
        self.flow.set_homogeneous(homogeneous)
        if not is_capped_grid:
            self.flow.set_max_children_per_line(1 if is_list else max_cols)
        self.flow.set_column_spacing(col_spacing)
        self.flow.set_row_spacing(row_spacing)
        self.flow.set_margin_bottom(12)
        self.flow.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.flow.set_activate_on_single_click(ext._click_policy == "single")
        self.flow.set_hexpand(True)
        # GTK 4.16 (used by Nautilus 47) does not allocate spare horizontal space to
        # an expanding FlowBox whose halign is START, leaving the justified folder
        # flow one card wide while its height is measured for a full-width row, so
        # following sections overlap it. FILL gives the flow the width its
        # spacing/allocation code expects on Nautilus 47 and newer, including
        # GTK 4.22.
        self.flow.set_halign(Gtk.Align.FILL)
        self.flow.set_valign(Gtk.Align.START)

        self.flow.connect("child-activated", ext._on_card_activated, win)
        self.flow.connect("selected-children-changed", ext._on_flow_selection_changed, win)
        ext._attach_flow_shortcuts(self.flow, win)

        self._query = ""
        self.flow.set_filter_func(self._filter_child)

        self.append(self.flow)

    def add_card(self, card: Gtk.Widget) -> None:
        self._size_group.add_widget(card)
        self.flow.append(card)

    def _filter_child(self, child: Gtk.FlowBoxChild) -> bool:
        inner = child.get_child()
        model = getattr(inner, "model", None)
        if model is None:
            # Not a card (e.g. the drag-reorder placeholder) -- always show it.
            return True
        # Issue #115: a disk whose mount root is hidden (dot-prefixed mountpoint,
        # .hidden entry) follows Nautilus' Show Hidden Files. Folder cards are
        # exempt: the user pinned those deliberately, as with sidebar bookmarks.
        if isinstance(inner, MyComputerDiskCard) and model.is_hidden:
            if not self._ext._nautilus_prefs.hidden_files():
                return False
        if not self._query:
            return True
        display_name = getattr(model, "display_name", None)
        return display_name is not None and self._query in display_name.lower()

    def set_query(self, query: str) -> None:
        """Filter this section's cards by `query` (case-insensitive substring
        of the card's display name). Self-hides the whole section (heading
        included) when nothing matches, so an empty group never lingers."""
        self._query = query.strip().lower()
        self.refresh_filter()

    def refresh_filter(self) -> None:
        """Re-evaluate the filter without changing the query (e.g. after cards
        are (re)built, or the Show Hidden Files preference changes). Self-hides
        the whole section (heading included) when nothing matches."""
        self.flow.invalidate_filter()
        child = self.flow.get_first_child()
        any_match = False
        while child is not None:
            if self._filter_child(child):
                any_match = True
                break
            child = child.get_next_sibling()
        self.set_visible(any_match)


def _droppable_uris(uris: list[str], destination_uri: str, *, is_move: bool) -> list[str]:
    """Drop the sources a drop into destination_uri cannot sensibly act on.

    Three cases, all of which Nautilus's own D-Bus file operations would
    either refuse or turn into a confusing error dialog:

    * the source *is* the destination (dropping a folder onto itself),
    * the destination lives inside the source (dropping a folder into its
      own descendant, which would move a tree into itself),
    * a move whose source already sits directly in the destination -- the
      file is where it is being dropped, so the move is a no-op. Only moves
      are filtered here: copying into the source's own folder is the normal
      way to duplicate a file, and Nautilus names the copy for us.

    Comparison goes through Gio.File rather than string prefixes so URI
    encoding and trailing slashes normalize the way GVfs itself normalizes
    them."""
    destination = Gio.File.new_for_uri(destination_uri)
    kept = []
    for uri in uris:
        source = Gio.File.new_for_uri(uri)
        if source.equal(destination) or destination.has_prefix(source):
            continue
        if is_move:
            parent = source.get_parent()
            if parent is not None and parent.equal(destination):
                continue
        kept.append(uri)
    return kept


class MyComputerColumnRow(Gtk.ListBoxRow):
    """One entry in a Column View column: icon, name, and a trailing chevron
    for folders (visual affordance that activating the row opens a child
    column). Holds the plain file attributes the column/orchestration layer
    needs -- no MountInfo/PreferredFolder model, this is native filesystem
    browsing, not a disk/folder-shortcut card."""

    __gtype_name__ = "MyComputerColumnRow"

    def __init__(
        self,
        uri: str,
        display_name: str,
        is_dir: bool,
        gio_icon=None,
        is_hidden: bool = False,
        content_type: str | None = None,
        mtime: int = 0,
        can_execute: bool = False,
        cancellable: Gio.Cancellable | None = None,
        icon_size: int = _COLUMN_ROW_ICON_SIZE,
    ) -> None:
        super().__init__()
        self.uri = uri
        self.display_name = display_name
        self.is_dir = is_dir
        self.content_type = content_type
        self.can_execute = can_execute
        self._is_cut = False
        self._thumbnail_future: concurrent.futures.Future | None = None
        self._thumbnail_cancellable: Gio.Cancellable | None = None
        self._row_icon_size = icon_size

        # No manual margin here -- .navigation-sidebar > row already carries
        # its own native inset (padding: 0 9px, margin-top: 3px between rows;
        # see Nautilus's resource style.css and gtk.css). A box
        # margin on top of that native row padding double-inset the content.
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=_COLUMN_ROW_SPACING)
        # box.get_style_context().add_class("nautilus-view-cell")
        box.add_css_class("mc-column-row-content")
        box.set_hexpand(True)
        box.set_vexpand(True)
        self.add_css_class("mc-column-row")

        self._is_hidden = is_hidden

        # Fixed 24x24 slot: the icon column's stable footprint, so every row's
        # label starts at the same x regardless of what's drawn inside (themed
        # icon or an aspect-preserving thumbnail -- see
        # _set_row_thumbnail_texture). Icon/thumbnail center inside it both
        # ways; slot itself is not visible (no background/border of its own).
        icon_slot = Gtk.Box()
        icon_slot.set_size_request(icon_size, icon_size)
        icon_slot.set_valign(Gtk.Align.CENTER)
        # A container that never sets hexpand explicitly inherits expand=True
        # from any child that has it -- the thumbnail Picture needs hexpand on
        # itself to center within this slot (see _set_row_thumbnail_texture),
        # which would otherwise propagate up and make this whole slot compete
        # with the name label for the row's leftover width. Pinning it False
        # here blocks that propagation at the slot's boundary.
        icon_slot.set_hexpand(False)

        icon = Gtk.Image()
        icon.set_halign(Gtk.Align.CENTER)
        icon.set_valign(Gtk.Align.CENTER)

        # gio_icon is already the fully-resolved icon by the time it gets here (custom
        # icon if the caller found one, else GIO's own real icon for the path -- see
        # _entries_from_infos) -- _set_regular_icon (not a plain set_from_icon_name +
        # set_pixel_size) forces the full-color variant: at this small 24px size GTK
        # would otherwise auto-select a monochrome/symbolic-looking fixed-size theme
        # variant on some themes. See common._set_regular_icon.
        if _gicon_renders(gio_icon):
            _set_regular_icon(icon, icon_size, gicon=gio_icon)
        else:
            _set_regular_icon(icon, icon_size, icon_name=("folder" if is_dir else "text-x-generic"))
        # Same class/opacity Nautilus's own grid/list cells use to dim hidden
        # entries (nautilus-grid-cell.c, nautilus-name-cell.c), applied only to
        # the icon -- not the label or the whole row -- to match native exactly.
        if is_hidden:
            icon.add_css_class("hidden-file")
        self._icon = icon

        # Normal state has its own stable pages too: a themed icon is shown
        # immediately, then a completed thumbnail simply becomes the other
        # page. The async thumbnail path consequently never mutates the row's
        # widget tree after construction.
        thumbnail = Gtk.Picture()
        thumbnail.set_hexpand(True)
        thumbnail.set_halign(Gtk.Align.CENTER)
        thumbnail.set_valign(Gtk.Align.CENTER)
        thumbnail.set_content_fit(Gtk.ContentFit.SCALE_DOWN)
        thumbnail.set_overflow(Gtk.Overflow.HIDDEN)
        thumbnail.add_css_class("mc-row-thumbnail")
        if is_hidden:
            thumbnail.add_css_class("hidden-file")
        self._thumbnail = thumbnail

        regular_stack = Gtk.Stack()
        regular_stack.set_transition_type(Gtk.StackTransitionType.NONE)
        regular_stack.set_size_request(icon_size, icon_size)
        regular_stack.add_named(icon, "icon")
        regular_stack.add_named(thumbnail, "thumbnail")
        regular_stack.set_visible_child_name("icon")
        icon_slot.append(regular_stack)
        self._regular_stack = regular_stack

        # Match NautilusNameCell's two-state visual ownership: the regular
        # visual (itself icon-or-thumbnail) and the cut glyph occupy the same
        # fixed-size bounds. set_cut() only switches the outer page.
        cut_slot = Gtk.Box()
        cut_slot.set_size_request(icon_size, icon_size)
        cut_slot.set_halign(Gtk.Align.CENTER)
        cut_slot.set_valign(Gtk.Align.CENTER)
        cut_slot.set_homogeneous(True)
        cut_icon = Gtk.Image()
        cut_icon.set_from_resource(
            "/org/gnome/nautilus/icons/scalable/actions/cut-large-symbolic.svg"
        )
        cut_icon.set_pixel_size(16)
        cut_icon.set_size_request(16, 16)
        cut_icon.set_halign(Gtk.Align.CENTER)
        cut_icon.set_valign(Gtk.Align.CENTER)
        cut_icon.set_opacity(0.7)
        cut_slot.append(cut_icon)
        self._cut_icon = cut_icon

        icon_stack = Gtk.Stack()
        icon_stack.set_transition_type(Gtk.StackTransitionType.NONE)
        icon_stack.set_size_request(icon_size, icon_size)
        icon_stack.set_halign(Gtk.Align.CENTER)
        icon_stack.set_valign(Gtk.Align.CENTER)
        icon_stack.add_named(icon_slot, "regular")
        icon_stack.add_named(cut_slot, "cut")
        icon_stack.set_visible_child_name("regular")
        box.append(icon_stack)
        self._icon_stack = icon_stack

        # macOS Finder-style: files show a real image/document thumbnail
        # instead of the generic icon when one is available. Same
        # GnomeDesktop.DesktopThumbnailFactory engine as the preview column
        # (see MyComputerPreviewColumn) -- a cached thumbnail is read inline
        # (cheap, already-scaled file read), a miss is generated on a daemon
        # thread and swapped in once ready, never on the main loop. Folders
        # never go through this: their icon is already the real folder icon.
        if not is_dir and content_type and _thumb_factory is not None and cancellable is not None:
            # Per-row cancellation matters for targeted monitor removals: the
            # column stays alive, but work for the one dead row must stop.
            self._thumbnail_cancellable = Gio.Cancellable()
            self._load_row_thumbnail(content_type, mtime, self._thumbnail_cancellable)

        name_lbl = Gtk.Label(label=display_name)
        name_lbl.set_xalign(0.0)
        name_lbl.set_hexpand(True)
        name_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        # Without a max-width-chars cap, an ellipsized label's NATURAL width is
        # still the full unellipsized text extent -- a single long filename
        # then dictates this row's (and thus the whole column's, and the
        # whole Paned chain's) natural width, silently overriding the
        # Paned's shrink_start_child(False) floor and _COLUMN_MIN/MAX_WIDTH
        # clamp (same class of bug as the recipe's wrapped-button-label
        # gotcha, just in a label instead of a button). max_width_chars(1)
        # + hexpand=True is the standard idiom: natural request shrinks to
        # near-zero, but it still fills the row via hexpand at allocation time.
        name_lbl.set_max_width_chars(1)
        box.append(name_lbl)

        if is_dir:
            chevron = Gtk.Image.new_from_icon_name("go-next-symbolic")
            chevron.set_pixel_size(12)
            chevron.get_style_context().add_class("dim-label")
            box.append(chevron)

        self.set_child(box)
        self._wire_dnd()

    def _wire_dnd(self) -> None:
        drag = Gtk.DragSource()
        drag.set_actions(Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        drag.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        drag.connect("prepare", self._on_drag_prepare)
        drag.connect("drag-begin", self._on_drag_begin)
        self.add_controller(drag)

        if self.is_dir:
            drop = Gtk.DropTarget.new(
                Gdk.FileList.__gtype__, Gdk.DragAction.COPY | Gdk.DragAction.MOVE
            )
            drop.set_preload(True)
            drop.connect("enter", self._on_drop_enter)
            drop.connect("leave", self._on_drop_leave)
            drop.connect("drop", self._on_drop)
            self.add_controller(drop)

    def _on_drag_prepare(self, _source, _x, _y):
        col = self.get_ancestor(MyComputerColumn)
        selected_rows = col.selected_rows() if col is not None else []
        if self in selected_rows and len(selected_rows) > 1:
            uris = [r.uri for r in selected_rows]
        else:
            uris = [self.uri]
        gfiles = [Gio.File.new_for_uri(u) for u in uris]
        file_list = Gdk.FileList.new_from_list(gfiles)
        cp_file_list = Gdk.ContentProvider.new_for_value(file_list)
        cp_uri_text = Gdk.ContentProvider.new_for_value("\r\n".join(uris) + "\r\n")
        return Gdk.ContentProvider.new_union([cp_file_list, cp_uri_text])

    def _on_drag_begin(self, _source, drag) -> None:
        col = self.get_ancestor(MyComputerColumn)
        selected_rows = col.selected_rows() if col is not None else []
        count = len(selected_rows) if self in selected_rows and len(selected_rows) > 1 else 1

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.add_css_class("mc-column-row")
        box.add_css_class("navigation-sidebar")

        icon = Gtk.Image()
        if count > 1:
            icon.set_from_icon_name("emblem-documents-symbolic")
        elif self.is_dir:
            icon.set_from_icon_name("folder")
        else:
            icon.set_from_icon_name("text-x-generic")
        box.append(icon)

        label_text = (
            _n("{n} item", "{n} items", count).format(n=count) if count > 1 else self.display_name
        )
        label = Gtk.Label(label=label_text)
        box.append(label)

        Gtk.DragIcon.get_for_drag(drag).set_child(box)

    def _on_drop_enter(self, target, _x, _y):
        set_row_active(self, True)
        return _negotiated_file_drop_action(target)

    def _on_drop_leave(self, _target):
        set_row_active(self, False)

    def _on_drop(self, drop_target, value, _x, _y) -> bool:
        set_row_active(self, False)
        if not isinstance(value, Gdk.FileList):
            return False
        files = value.get_files()
        if not files:
            return False
        uris = [f.get_uri() for f in files if f is not None]
        if not uris:
            return False

        is_move = _negotiated_file_drop_action(drop_target) == Gdk.DragAction.MOVE

        uris = _droppable_uris(uris, self.uri, is_move=is_move)
        if not uris:
            return False

        col = self.get_ancestor(MyComputerColumn)
        if col is not None and callable(getattr(col, "_on_drop_files", None)):
            col._on_drop_files(uris, destination_uri=self.uri, cut=is_move)
            return True
        return False

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        """Draw the complete cut treatment in the padded row's bounds."""
        if not self._is_cut:
            Gtk.ListBoxRow.do_snapshot(self, snapshot)
            return

        width = float(self.get_width())
        height = float(self.get_height())
        # A 2px stroke centered 1px from the allocation edge lands flush
        # with the row surface instead of floating inside it.
        inset = 1.0
        if width <= inset * 2 or height <= inset * 2:
            Gtk.ListBoxRow.do_snapshot(self, snapshot)
            return

        bounds = Graphene.Rect()
        bounds.init(0.0, 0.0, width, height)
        color = self.get_color()

        # The background is below GTK's normal row snapshot so native hover
        # and selection layers retain their expected stacking order.
        cr = snapshot.append_cairo(bounds)
        cr.set_source_rgba(color.red, color.green, color.blue, color.alpha * 0.07)
        # Libadwaita's .navigation-sidebar > row rule supplies the visible
        # row radius (9px). Keep this snapshot path in sync with that native
        # rule because GTK does not expose a computed CSS radius to Python.
        self._append_rounded_rectangle(cr, 0.0, 0.0, width, height, 9.0)
        cr.fill()
        del cr

        Gtk.ListBoxRow.do_snapshot(self, snapshot)

        cr = snapshot.append_cairo(bounds)
        opacity = 0.5 if Adw.StyleManager.get_default().get_high_contrast() else 0.15
        cr.set_source_rgba(color.red, color.green, color.blue, color.alpha * opacity)
        cr.set_line_width(2.0)
        cr.set_dash([4.0, 3.0])
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        self._append_rounded_rectangle(
            cr, inset, inset, width - inset * 2.0, height - inset * 2.0, 9.0
        )
        cr.stroke()
        del cr

    @staticmethod
    def _append_rounded_rectangle(
        cr: cairo.Context, x: float, y: float, width: float, height: float, radius: float
    ) -> None:
        """Append a rounded rectangle path to a Cairo context."""
        left = x
        top = y
        right = x + width
        bottom = y + height
        radius = min(radius, width / 2.0, height / 2.0)
        cr.move_to(left + radius, top)
        cr.line_to(right - radius, top)
        cr.arc(right - radius, top + radius, radius, -math.pi / 2.0, 0.0)
        cr.line_to(right, bottom - radius)
        cr.arc(right - radius, bottom - radius, radius, 0.0, math.pi / 2.0)
        cr.line_to(left + radius, bottom)
        cr.arc(left + radius, bottom - radius, radius, math.pi / 2.0, math.pi)
        cr.line_to(left, top + radius)
        cr.arc(left + radius, top + radius, radius, math.pi, math.pi * 1.5)
        cr.close_path()

    def set_cut(self, cut: bool) -> None:
        """Switch between the row-owned regular and cut visual pages."""
        if self._is_cut == cut:
            return
        self._is_cut = cut
        if cut:
            self.add_css_class("mc-row-cut")
        else:
            self.remove_css_class("mc-row-cut")
        self._icon_stack.set_visible_child_name("cut" if cut else "regular")
        self.queue_draw()

    def set_thumbnail(self, texture: Gdk.Texture) -> None:
        """Replace the regular icon page with a finished thumbnail."""
        self._thumbnail.set_paintable(texture)
        self._regular_stack.set_visible_child_name("thumbnail")

    def _load_thumbnail_texture(self, path: str) -> Gdk.Texture | None:
        """Decode one cached thumbnail at this row's fixed visual size."""
        try:
            # Scale during decode: gdk-pixbuf preserves the aspect ratio and
            # never enlarges a smaller source, so the row receives an already
            # fitted paintable.
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                path, self._row_icon_size, self._row_icon_size, True
            )
        except GLib.Error:
            return None
        return Gdk.Texture.new_for_pixbuf(pixbuf)

    def _load_row_thumbnail(
        self, content_type: str, mtime: int, cancellable: Gio.Cancellable
    ) -> None:
        # Nautilus-style icon-then-thumbnail: the row already shows its plain
        # icon (set just above, in __init__) the instant it's built. Both the
        # cache lookup AND the fallback generation run entirely on a daemon
        # thread -- previously the lookup ran inline on the main thread during
        # row construction, and with many files in a folder that serial chain
        # of stat/hash checks delayed the whole column's first paint (it
        # looked like "blank, then all rows appear at once"). Now every row
        # renders immediately and its thumbnail (if any) pops in on its own,
        # independent thread as soon as that one file's lookup/generation
        # finishes -- one by one, never blocking the others.
        self._thumbnail_future = _ROW_THUMBNAIL_EXECUTOR.submit(
            self._row_thumbnail_worker, self.uri, content_type, mtime, cancellable
        )

    def cancel_thumbnail(self) -> None:
        """Drop queued thumbnail work when this row leaves its column."""
        if self._thumbnail_cancellable is not None:
            self._thumbnail_cancellable.cancel()
            self._thumbnail_cancellable = None
        if self._thumbnail_future is not None:
            self._thumbnail_future.cancel()
            self._thumbnail_future = None

    def _row_thumbnail_worker(
        self, uri: str, content_type: str, mtime: int, cancellable: Gio.Cancellable
    ) -> None:
        if cancellable.is_cancelled():
            return
        cached = _thumb_factory.lookup(uri, mtime)
        if cached:
            texture = self._load_thumbnail_texture(cached)
            if texture is None:
                return
            if cancellable.is_cancelled():
                return
            GLib.idle_add(self._set_row_thumbnail_texture, texture, cancellable)
            return
        if _thumb_factory.has_valid_failed_thumbnail(uri, mtime):
            return
        if not _thumb_factory.can_thumbnail(uri, content_type, mtime):
            return
        # Generation shells out to a system thumbnailer subprocess per file --
        # bounded so a folder with hundreds of un-cached images doesn't spawn
        # hundreds of them at once. Cache lookups above are unthrottled (cheap
        # stat/read), so repeat folder visits stay instant regardless.
        with _ROW_THUMBNAIL_SEMAPHORE:
            if cancellable.is_cancelled():
                return
            try:
                pixbuf = _thumb_factory.generate_thumbnail(uri, content_type, cancellable)
            except GLib.Error:
                pixbuf = None
        if cancellable.is_cancelled():
            return
        if pixbuf is None:
            try:
                _thumb_factory.create_failed_thumbnail(uri, mtime, cancellable)
            except GLib.Error:
                pass
            return
        try:
            _thumb_factory.save_thumbnail(pixbuf, uri, mtime, cancellable)
        except GLib.Error:
            return
        cached = _thumb_factory.lookup(uri, mtime)
        if not cached or cancellable.is_cancelled():
            return
        texture = self._load_thumbnail_texture(cached)
        if texture is None:
            return
        GLib.idle_add(self._set_row_thumbnail_texture, texture, cancellable)

    def _set_row_thumbnail_texture(self, texture: Gdk.Texture, cancellable: Gio.Cancellable) -> int:
        if cancellable.is_cancelled():
            return GLib.SOURCE_REMOVE
        # The thumbnail page was fully constructed with the row. Swapping its
        # paintable through set_thumbnail() preserves a fixed, predictable
        # widget tree while retaining the same SCALE_DOWN centering behavior.
        self.set_thumbnail(texture)
        return GLib.SOURCE_REMOVE


@dataclasses.dataclass(frozen=True, slots=True)
class _ColumnEntry:
    """One row's immutable sort data, built from an enumeration batch.

    `size` holds byte size for files and a neutral zero for directories.
    Counting every directory's children is intentionally avoided because it
    turns one listing into N extra enumerations on large/remote folders."""

    is_dir: bool
    sort_key: str
    sort_last: bool
    name: str
    display_name: str
    icon: Gio.Icon | None
    content_type: str | None
    is_hidden: bool
    size: int
    mtime: int
    btime: int
    atime: int
    can_execute: bool = False


def _name_tiebreak(e: _ColumnEntry) -> tuple:
    """Nautilus's compare_by_full_path is the tiebreak for every criterion
    (and *is* the entire comparison for name sort), and resolves through
    compare_by_display_name: names starting with "." or "#" sort last
    (SORT_LAST_CHAR1/2, nautilus-file.c), otherwise by filename collation
    key -- g_utf8_collate_key_for_filename, natural-numeric and locale
    aware, not a plain lowercase compare (img2.png vs img10.png sorted
    "wrong" under str.lower(), right here)."""
    return (e.sort_last, e.sort_key)


# Transcribed from nautilus-file.c's mime_type_map -- the coarse basic-type
# string Nautilus's Type sort groups files by (Program, Audio, Image, ...).
_BASIC_TYPE_BY_GENERIC_ICON = {
    "application-x-executable": N_("Program"),
    "audio-x-generic": N_("Audio"),
    "font-x-generic": N_("Font"),
    "image-x-generic": N_("Image"),
    "package-x-generic": N_("Archive"),
    "text-html": N_("Markup"),
    "text-x-generic": N_("Text"),
    "text-x-generic-template": N_("Text"),
    "text-x-script": N_("Program"),
    "video-x-generic": N_("Video"),
    "x-office-address-book": N_("Contacts"),
    "x-office-calendar": N_("Calendar"),
    "x-office-document": N_("Document"),
    "x-office-presentation": N_("Presentation"),
    "x-office-spreadsheet": N_("Spreadsheet"),
}

# Rows a column builds synchronously before yielding to the main loop --
# enough to fill a tall column so the first frame carries real content.
_COLUMN_FIRST_CHUNK_ROWS = 60
# Rows per idle turn after that. Keep the batch small enough that scrolling and
# clicks stay responsive while a large folder finishes filling in.
_COLUMN_CHUNK_ROWS = 50
# Preserve native sorting for ordinary folders. Once this many visible
# entries have arrived, latency matters more than a global ordering pass:
# publish enumeration batches immediately and skip the all-items sort.
_COLUMN_STREAMING_THRESHOLD = 400
_COLUMN_ROW_ZOOM_SIZE = {"small": 18, "medium": 24, "large": 40}


def _basic_type_string(content_type: str | None) -> str:
    # get_basic_type_for_mime_type: map the content type to its generic
    # themed-icon name, then to Nautilus's coarse type label. These strings
    # are Nautilus's own and already in its gettext catalog (14/14 locales),
    # so _native() rather than _() per the #120 convention -- and native
    # collates the *translated* string, so comparing translated strings here
    # is what reproduces native's actual on-screen order.
    if content_type is None:
        return _native("Other")
    generic_icon = Gio.content_type_get_generic_icon_name(content_type)
    key = _BASIC_TYPE_BY_GENERIC_ICON.get(generic_icon or "")
    return _native(key) if key else _native("Other")


def _type_key(e: _ColumnEntry) -> tuple:
    # compare_by_type: directories always first and tied with each other (a
    # folder carries no type string) -- unconditional on the dirs-first pref,
    # same as size (see _size_key in the next commit). Among files: coarse
    # basic-type string, collated, then the raw mime type, then the same
    # tiebreak as every other criterion. No hidden bucket here either -- see
    # _name_tiebreak; the old is-hidden-based 4-bucket model was never what
    # nautilus-file.c's compare_by_type actually does.
    if e.is_dir:
        return (0, "", "", *_name_tiebreak(e))
    basic_type = _basic_type_string(e.content_type)
    mime_type = e.content_type or ""
    return (
        1,
        GLib.utf8_collate_key(basic_type, -1),
        GLib.utf8_collate_key(mime_type, -1),
        *_name_tiebreak(e),
    )


def _size_key(e: _ColumnEntry) -> tuple:
    # compare_by_size: directories always first (using the neutral value),
    # files after (compared by byte size) -- baked into the criterion
    # itself, unconditional on the "Sort Folders Before Files" pref, same as
    # type. Because this bucketing is part of the criterion's own result
    # (not a separately-pinned pass), reverse= DOES flip it, same as native:
    # "if (reversed) result = -result;" negates the whole per-criterion
    # result, dir/file split included -- there is no fixed-bucket exception
    # here despite what an earlier version of this code assumed.
    return (0 if e.is_dir else 1, e.size, *_name_tiebreak(e))


# Each criterion's key, derived directly from nautilus_file_compare_for_sort
# in nautilus-file.c. A plain `entries.sort(key=..., reverse=)` reproduces
# native's "if (reversed) result = -result" exactly, because that negates
# the *whole* per-criterion result, tiebreak and any criterion-local
# bucketing (size/type's dir-first split) included -- which is also why the
# separate "Sort Folders Before Files" pref is handled before these keys by
# _compare_column_entries: unlike them, that pinned bucket is applied before
# reversed is ever considered.
_SORT_KEY_BUILDERS = {
    # compare_by_display_name IS the whole comparison for name sort: no
    # is-hidden bucket, only the sort-last (. or #) rule inside the tiebreak.
    "name": _name_tiebreak,
    # Flat pool, no buckets -- folders/files/hidden all mixed, sorted purely
    # by timestamp, tiebreak for equal timestamps (matches native's
    # compare_by_time -> compare_by_full_path chain; btime especially needs
    # this, since time::created is 0/equal for many files).
    "mtime": lambda e: (e.mtime, *_name_tiebreak(e)),
    "btime": lambda e: (e.btime, *_name_tiebreak(e)),
    "atime": lambda e: (e.atime, *_name_tiebreak(e)),
    "size": _size_key,
    "type": _type_key,
}


def _compare_column_entries(
    left: _ColumnEntry,
    right: _ColumnEntry,
    sort: tuple[str, bool],
    directories_first: bool,
) -> int:
    """Three-way comparison for targeted inserts into an already-sorted list."""
    if directories_first and left.is_dir != right.is_dir:
        return -1 if left.is_dir else 1
    column, reverse = sort
    key_fn = _SORT_KEY_BUILDERS.get(column, _SORT_KEY_BUILDERS["name"])
    left_key, right_key = key_fn(left), key_fn(right)
    result = (left_key > right_key) - (left_key < right_key)
    return -result if reverse else result


class MyComputerColumn(Gtk.ScrolledWindow):
    """One fixed-width Miller column: lists the contents of a single folder.

    Enumeration is always async (never Gio.File.enumerate_children /
    query_info sync variants) per this project's async-only rule for anything
    that can touch the filesystem/network from an event handler.
    """

    __gtype_name__ = "MyComputerColumn"

    def __init__(
        self,
        ext,
        folder_uri: str,
        on_row_activated,
        on_loaded=None,
        on_row_created=None,
        on_files_dropped=None,
        on_open_error=None,
        on_file_open=None,
        on_child_renamed=None,
        on_child_changed=None,
        on_folder_moved=None,
        on_folder_unavailable=None,
        sort: tuple[str, bool] = ("name", False),
    ) -> None:
        super().__init__()
        self._ext = ext
        self.folder_uri = folder_uri
        self._on_row_activated = on_row_activated
        self._on_loaded = on_loaded
        self._on_row_created = on_row_created
        self._on_files_dropped = on_files_dropped
        self._on_open_error = on_open_error
        self._on_file_open = on_file_open
        self._on_child_renamed = on_child_renamed
        self._on_child_changed = on_child_changed
        self._on_folder_moved = on_folder_moved
        self._on_folder_unavailable = on_folder_unavailable
        self._sort = sort
        self._cancellable = Gio.Cancellable()
        # Keyboard navigation is a cursor, not a change to the committed
        # Gtk.ListBox selection. It is rendered with GTK's :active state so
        # the selected path and the arrow-key target can coexist.
        self._keyboard_active_row: MyComputerColumnRow | None = None
        # Manual repeat-click detection for opening the already-previewed file row
        # (see _on_row_activated_internal): a raw GestureClick on the row can't be
        # used for this because every activation rebuilds the paned chain
        # (column_view.py's _rebuild_chain), which resets GTK's own press-count
        # tracking on the row before a second click can land.
        self._last_activated_uri: str | None = None
        self._last_activated_time: int = 0
        # Selection a modifier click already committed, held while GtkListBox
        # finishes release, reparenting, and focus work (see pin_selection and
        # _on_row_activated_internal). None means no modifier transaction is
        # in flight and row-activated is a genuine activation.
        self._pinned_selection: list[MyComputerColumnRow] | None = None
        # Re-selecting rows emits selected-rows-changed synchronously.  Keep
        # the repair callback from recursing while it restores a pinned
        # modifier selection after GTK focus/reparent settling.
        self._repairing_pinned_selection = False
        # Idle source still building the tail of a large folder's rows (see
        # _append_rows_in_chunks), or 0.
        self._fill_id = 0
        self._pending_row_entries: list[_ColumnEntry] = []
        self._load_entries: list[_ColumnEntry] = []
        self._rows_by_name: dict[str, MyComputerColumnRow] = {}
        self._row_order: list[MyComputerColumnRow] = []
        self._enumeration_finished = False
        self._streaming_large_folder = False
        # A remove event can arrive while enumerate_children_async still has
        # the old child in a batch that has not become a row yet. Tombstones
        # keep that stale entry from being appended after the event.
        self._removed_child_names: set[str] = set()
        self._child_refresh_ids: dict[str, int] = {}
        # A monotonically increasing revision per changed child makes async
        # query results safe across create-delete-recreate races. A global
        # serial (rather than per-name counters) means completed revisions
        # can be discarded without ever reusing an old value.
        self._child_revision_serial = 0
        self._child_revisions: dict[str, int] = {}
        self._load_generation = 0
        self._directories_first = self._ext._nautilus_prefs.sort_directories_first()
        # Extension-owned operations can announce a specific child before
        # creating it. Suppressing just that monitor event lets them insert
        # the finished row without blanking and re-enumerating this column.
        self._expected_child_uris: set[str] = set()
        self._load_complete = False
        self._load_error: GLib.Error | None = None
        self._reload_selection_uris: set[str] | None = None
        self._reload_cursor_uri: str | None = None
        self._reload_anchor_uri: str | None = None
        self._reload_scroll_position: float | None = None
        self._thumbnail_rows_enabled = True
        self.width: float = _COLUMN_WIDTH

        self._file_monitor = None
        self._install_file_monitor()

        self.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        # size_request is only this column's enforced floor (_COLUMN_MIN_WIDTH).
        # shrink_start_child(False) on the enclosing paned is what actually
        # enforces this floor at drag time.
        self.set_size_request(_COLUMN_MIN_WIDTH, -1)
        self.set_vexpand(True)
        # Clip contents to the column's own bounds so a mid-drag width change
        # can't paint list rows past the column edge into the neighbour.
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self.add_css_class("mc-column")

        self.list_box = Gtk.ListBox()
        # Same style class the native Nautilus sidebar (a Gtk.ListBox) carries:
        # gives rows the sidebar's rounded-corner selection shape and
        # theme-aware hover highlight. Its native :selected fill is a neutral
        # grey (sidebar convention, not accent) -- .mc-column-list below
        # re-tints just the selected state to accent (see _CSS in main.py),
        # keeping the sidebar's shape/hover but with content-view-style
        # selection color, since this is a Miller *browsing* view, not a
        # places sidebar.
        self.list_box.add_css_class("navigation-sidebar")
        self.list_box.add_css_class("mc-column-list")
        self.list_box.set_selection_mode(Gtk.SelectionMode.MULTIPLE)
        # Column view always activates on single click, regardless of the
        # Nautilus double-click setting (ext._nautilus_prefs.click_policy) that the cards use
        # -- Miller columns read naturally as single-click-to-drill-down. A
        # future Column View settings tab may make this configurable; for now
        # it's fixed.
        self.list_box.set_activate_on_single_click(True)
        self.list_box.connect("row-activated", self._on_row_activated_internal)
        self.list_box.connect("selected-rows-changed", self._on_selected_rows_changed)
        # Matches Nautilus's own empty-folder state (nautilus-files-view.c
        # update_empty_view -- AdwStatusPage, "folder-symbolic" icon, "Folder
        # is Empty" title, no description). .compact keeps it readable at
        # column width. It replaces the loading placeholder only after the
        # async enumeration has confirmed that the directory is really empty,
        # avoiding both a blank column and a misleading "Folder is Empty"
        # flash while a slow backend is still responding.
        self._empty_page = Adw.StatusPage()
        self._empty_page.set_icon_name("folder-symbolic")
        self._empty_page.set_title(_native("Folder is Empty"))
        self._empty_page.add_css_class("compact")
        self._error_page = Adw.StatusPage()
        self._error_page.set_icon_name("dialog-error-symbolic")
        self._error_page.set_title(_("Unable to Display Folder"))
        self._error_page.add_css_class("compact")
        retry_button = Gtk.Button(label=_native("Retry"))
        retry_button.set_halign(Gtk.Align.CENTER)
        retry_button.add_css_class("suggested-action")
        retry_button.connect("clicked", lambda *_args: self.reload())
        self._error_page.set_child(retry_button)
        self.set_child(self.list_box)
        self._loading_page = Adw.StatusPage()
        self._loading_page.set_title(_("Loading…"))
        self._loading_page.add_css_class("compact")
        loading_spinner = Gtk.Spinner()
        loading_spinner.set_spinning(True)
        loading_spinner.set_halign(Gtk.Align.CENTER)
        self._loading_page.set_child(loading_spinner)
        self.list_box.set_placeholder(self._loading_page)
        self._wire_column_drop()

        self._load()

    def _wire_column_drop(self) -> None:
        drop = Gtk.DropTarget.new(Gdk.FileList.__gtype__, Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        drop.set_preload(True)
        drop.connect("enter", self._on_column_drop_enter)
        drop.connect("leave", self._on_column_drop_leave)
        drop.connect("drop", self._on_column_drop)
        self.add_controller(drop)

    def _on_column_drop_enter(self, target, _x, _y):
        return _negotiated_file_drop_action(target)

    def _on_column_drop_leave(self, _target):
        pass

    def _on_column_drop(self, drop_target, value, _x, _y) -> bool:
        if not isinstance(value, Gdk.FileList):
            return False
        files = value.get_files()
        if not files:
            return False
        uris = [f.get_uri() for f in files if f is not None]
        if not uris:
            return False

        is_move = _negotiated_file_drop_action(drop_target) == Gdk.DragAction.MOVE

        uris = _droppable_uris(uris, self.folder_uri, is_move=is_move)
        if not uris:
            return False

        self._on_drop_files(uris, destination_uri=self.folder_uri, cut=is_move)
        return True

    def _on_drop_files(self, uris: list[str], destination_uri: str, cut: bool) -> None:
        if callable(self._on_files_dropped):
            self._on_files_dropped(uris, destination_uri, cut=cut)

    def set_sort(self, sort: tuple[str, bool]) -> None:
        """Update this column's sort and reload. Always reloads regardless of
        whether sort actually changed: this is only ever called from
        refresh_column_view, which also needs it to pick up other prefs
        changes (hidden-files) that reload() alone would apply on its own --
        an early-return here when sort is unchanged would skip that too."""
        self._sort = sort
        self.reload()

    def _install_file_monitor(self) -> None:
        if self._file_monitor is not None:
            self._file_monitor.cancel()
            self._file_monitor = None
        if not self.folder_uri:
            return
        try:
            gfile = Gio.File.new_for_uri(self.folder_uri)
            self._file_monitor = gfile.monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)
            self._file_monitor.connect("changed", self._on_dir_changed)
        except GLib.Error as error:
            _log(f"Could not create directory monitor for {self.folder_uri}: {error.message}")

    def set_folder_uri(self, folder_uri: str) -> None:
        """Update a renamed open folder and move its live monitor with it."""
        if Gio.File.new_for_uri(self.folder_uri).equal(Gio.File.new_for_uri(folder_uri)):
            self.folder_uri = folder_uri
            return
        self.folder_uri = folder_uri
        self._install_file_monitor()

    def has_pending_selection_restore(self) -> bool:
        return self._reload_selection_uris is not None

    def load_complete(self) -> bool:
        return self._load_complete

    def load_succeeded(self) -> bool:
        return self._load_complete and self._load_error is None

    def contains_uri(self, uri: str) -> bool:
        target = Gio.File.new_for_uri(uri)
        return any(Gio.File.new_for_uri(row.uri).equal(target) for row in self.rows())

    def expect_child_creation(self, uri: str) -> None:
        """Keep an operation's partial output hidden until it completes."""
        self._expected_child_uris.add(uri)

    def finish_expected_child_creation(self, uri: str, *, created: bool) -> None:
        """Release creation suppression and query the operation's final state.

        The query is intentional even for failure/cancellation: an extractor
        may leave useful partial output. It also handles deleting and then
        recreating the same output name because refresh_child_uri owns a new
        per-child revision and clears any old enumeration tombstone only
        after the new object has actually been observed.
        """
        target = Gio.File.new_for_uri(uri)
        matching = next(
            (
                expected
                for expected in self._expected_child_uris
                if Gio.File.new_for_uri(expected).equal(target)
            ),
            None,
        )
        if matching is not None:
            # Suppression is needed only while the operation may expose
            # partial output. Once its terminal signal arrives, release it
            # immediately: monitor updates are targeted/revisioned now, so a
            # late duplicate CREATED is harmless, while a grace period could
            # hide a fast delete-and-recreate or Undo after completion.
            self._expected_child_uris.discard(matching)
        self.refresh_child_uri(uri)

    def reload(self) -> None:
        """Re-enumerate this column's own folder in place (e.g. after the
        hidden-files setting changes), without touching sibling columns or
        collapsing the Miller chain."""
        if self._reload_selection_uris is None:
            self._reload_selection_uris = set(self.selected_uris())
            cursor = getattr(self, "_cursor_row", None)
            anchor = getattr(self, "_anchor_row", None)
            self._reload_cursor_uri = cursor.uri if cursor in self.rows() else None
            self._reload_anchor_uri = anchor.uri if anchor in self.rows() else None
            self._reload_scroll_position = self.scroll_position()
        if self._file_monitor is None:
            self._install_file_monitor()
        self._cancellable.cancel()
        self._cancellable = Gio.Cancellable()
        self._load_complete = False
        self._stop_fill()
        self.clear_active_row()
        # Every row object below is about to be dropped, so a pinned
        # selection can only name dead rows from here on.
        self.clear_pinned_selection()
        self.list_box.set_placeholder(self._loading_page)
        child = self.list_box.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            if isinstance(child, MyComputerColumnRow):
                child.cancel_thumbnail()
            self.list_box.remove(child)
            child = next_child
        self._rows_by_name.clear()
        self._row_order.clear()
        self._load()

    def _load(self) -> None:
        self._load_generation += 1
        generation = self._load_generation
        self._load_complete = False
        self._load_error = None
        self._enumeration_finished = False
        self._streaming_large_folder = False
        self._pending_row_entries.clear()
        self._load_entries.clear()
        self._removed_child_names.clear()
        self._child_revisions.clear()
        self._directories_first = self._ext._nautilus_prefs.sort_directories_first()
        gfile = Gio.File.new_for_uri(self.folder_uri)
        gfile.enumerate_children_async(
            _COLUMN_FILE_ATTRIBUTES,
            Gio.FileQueryInfoFlags.NONE,
            GLib.PRIORITY_DEFAULT,
            self._cancellable,
            self._on_enumerator_ready,
            generation,
        )

    def _on_enumerator_ready(
        self, gfile: Gio.File, result: Gio.AsyncResult, generation: int
    ) -> None:
        if generation != self._load_generation or self._cancellable.is_cancelled():
            return
        try:
            enumerator = gfile.enumerate_children_finish(result)
        except GLib.Error as error:
            if not self._cancellable.is_cancelled():
                self._show_load_error(error)
            return
        enumerator.next_files_async(
            200,
            GLib.PRIORITY_DEFAULT,
            self._cancellable,
            self._on_next_files_ready,
            generation,
        )

    def _on_next_files_ready(
        self, enumerator: Gio.FileEnumerator, result: Gio.AsyncResult, generation: int
    ) -> None:
        if generation != self._load_generation or self._cancellable.is_cancelled():
            return
        try:
            infos = enumerator.next_files_finish(result)
        except GLib.Error as error:
            enumerator.close_async(GLib.PRIORITY_DEFAULT, self._cancellable, lambda *_args: None)
            if not self._cancellable.is_cancelled():
                self._show_load_error(error)
            return
        if infos:
            batch = self._entries_from_infos(infos)
            if self._streaming_large_folder:
                self._queue_row_entries(
                    [
                        entry
                        for entry in batch
                        if entry.name not in self._removed_child_names
                        and entry.name not in self._rows_by_name
                    ]
                )
            elif len(self._load_entries) + len(batch) >= _COLUMN_STREAMING_THRESHOLD:
                # A full-directory sort makes the first frame wait for the
                # final remote/local enumeration batch. Large folders instead
                # retain the backend's stable enumeration order and begin
                # painting now. No row thumbnails are queued in this mode.
                self._streaming_large_folder = True
                self._thumbnail_rows_enabled = False
                pending = [*self._load_entries, *batch]
                self._load_entries.clear()
                self._queue_row_entries(
                    [
                        entry
                        for entry in pending
                        if entry.name not in self._removed_child_names
                        and entry.name not in self._rows_by_name
                    ]
                )
            else:
                # Small/ordinary folder: one Python sort after enumeration is
                # cheaper than maintaining a live GTK comparator.
                self._load_entries.extend(batch)
            enumerator.next_files_async(
                200,
                GLib.PRIORITY_DEFAULT,
                self._cancellable,
                self._on_next_files_ready,
                generation,
            )
            return
        enumerator.close_async(GLib.PRIORITY_DEFAULT, self._cancellable, lambda *_args: None)
        self._enumeration_finished = True
        if self._streaming_large_folder:
            self._load_entries.clear()
            self._removed_child_names.clear()
            self._maybe_finish_population()
            return
        entries = [
            entry
            for entry in self._load_entries
            if entry.name not in self._removed_child_names and entry.name not in self._rows_by_name
        ]
        self._load_entries.clear()
        # The completed enumeration and _pending_row_entries now contain no
        # stale deleted names. Future async child queries are protected by
        # revisions, so retaining every historical tombstone would only grow
        # memory and could hide a same-name object created without a monitor.
        self._removed_child_names.clear()
        self._sort_entries(entries)
        self._thumbnail_rows_enabled = len(entries) <= _COLUMN_ROW_THUMBNAIL_LIMIT
        self._queue_row_entries(entries)
        self._maybe_finish_population()

    def _show_load_error(self, error: GLib.Error) -> None:
        self._cancellable.cancel()
        self._load_generation += 1
        self._stop_fill()
        child = self.list_box.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            if isinstance(child, MyComputerColumnRow):
                child.cancel_thumbnail()
            self.list_box.remove(child)
            child = next_child
        self._rows_by_name.clear()
        self._row_order.clear()
        self._load_complete = True
        self._load_error = error
        self._error_page.set_description(error.message)
        self.list_box.set_placeholder(self._error_page)
        self._finish_reload_restore()
        if callable(self._on_loaded):
            self._on_loaded(self)

    def _entries_from_infos(self, infos: list) -> list[_ColumnEntry]:
        """Convert one enumeration batch into independently sortable rows."""
        show_hidden = self._ext._nautilus_prefs.hidden_files()
        entries = []
        for info in infos:
            is_hidden = info.get_attribute_boolean(
                "standard::is-hidden"
            ) or info.get_attribute_boolean("standard::is-backup")
            if not show_hidden and is_hidden:
                continue
            name = info.get_name()
            is_dir = info.get_file_type() == Gio.FileType.DIRECTORY
            # Recursively enumerating every subfolder merely to sort a parent
            # by child count is catastrophic in large or remote directories.
            # Folders use a cheap neutral value and retain their name
            # tiebreak; regular files still sort by their real byte size.
            size = 0 if is_dir else info.get_attribute_uint64("standard::size")
            display_name = info.get_display_name() or name
            entries.append(
                _ColumnEntry(
                    is_dir=is_dir,
                    sort_key=GLib.utf8_collate_key_for_filename(display_name, -1),
                    sort_last=display_name[:1] in (".", "#"),
                    name=name,
                    display_name=display_name,
                    icon=_resolve_custom_gicon(info) or info.get_icon(),
                    content_type=(
                        info.get_content_type()
                        if info.has_attribute("standard::content-type")
                        else None
                    ),
                    is_hidden=is_hidden,
                    size=size,
                    mtime=info.get_attribute_uint64("time::modified"),
                    btime=info.get_attribute_uint64("time::created"),
                    atime=info.get_attribute_uint64("time::access"),
                    can_execute=info.get_attribute_boolean("access::can-execute"),
                )
            )
        return entries

    def _sort_entries(self, entries: list[_ColumnEntry]) -> None:
        """Sort one completed enumeration without GTK widget comparisons."""
        column, reverse = self._sort
        key_fn = _SORT_KEY_BUILDERS.get(column, _SORT_KEY_BUILDERS["name"])
        entries.sort(key=key_fn, reverse=reverse)
        if self._directories_first:
            entries.sort(key=lambda entry: not entry.is_dir)

    def _make_entry_row(
        self, entry: _ColumnEntry, base: Gio.File | None = None
    ) -> MyComputerColumnRow:
        """Build one row without asking GTK to participate in sorting."""
        if base is None:
            base = Gio.File.new_for_uri(self.folder_uri)
        row = MyComputerColumnRow(
            base.get_child(entry.name).get_uri(),
            entry.display_name,
            entry.is_dir,
            entry.icon,
            entry.is_hidden,
            content_type=entry.content_type,
            mtime=entry.mtime,
            can_execute=entry.can_execute,
            # Thumbnail generation is useful in ordinary folders, but it is
            # deliberately disabled for large listings: even bounded worker
            # pools would otherwise enqueue hundreds of stat/hash/subprocess
            # jobs that compete with enumeration and interaction.
            cancellable=(self._cancellable if self._thumbnail_rows_enabled else None),
            icon_size=_COLUMN_ROW_ZOOM_SIZE.get(
                self._ext._nautilus_prefs.zoom_level("list-view"), 24
            ),
        )
        row._column_entry = entry
        return row

    def _append_entry_row(
        self, entry: _ColumnEntry, base: Gio.File | None = None
    ) -> MyComputerColumnRow | None:
        if entry.name in self._removed_child_names or entry.name in self._rows_by_name:
            return None
        row = self._make_entry_row(entry, base)
        self.list_box.append(row)
        self._rows_by_name[entry.name] = row
        self._row_order.append(row)
        if callable(self._on_row_created):
            self._on_row_created(self, row)
        return row

    def _insert_entry_row(
        self, entry: _ColumnEntry, *, replacing_uri: str | None = None
    ) -> MyComputerColumnRow | None:
        """Insert/replace one monitor result while preserving viewport state.

        External changes are rare compared with enumeration, so one linear
        scan is cheaper than keeping GtkListBox's live comparator installed
        for every row and every layout pass.
        """
        if entry.name in self._removed_child_names:
            return None
        rows = self._row_order
        replacement = None
        replacement_position = None
        replacement_target = Gio.File.new_for_uri(replacing_uri) if replacing_uri else None
        for candidate in rows:
            candidate_entry = getattr(candidate, "_column_entry", None)
            if candidate_entry is not None and candidate_entry.name == entry.name:
                replacement = candidate
                break
            if replacement_target is not None and Gio.File.new_for_uri(candidate.uri).equal(
                replacement_target
            ):
                replacement = candidate
                break

        scroll_position = self.scroll_position()
        was_selected = replacement in self.selected_rows() if replacement else False
        was_active = replacement is self._keyboard_active_row
        was_cursor = replacement is getattr(self, "_cursor_row", None)
        was_anchor = replacement is getattr(self, "_anchor_row", None)
        was_pinned = (
            replacement is not None
            and self._pinned_selection is not None
            and replacement in self._pinned_selection
        )
        if replacement is not None:
            replacement_position = rows.index(replacement)
            old_entry = getattr(replacement, "_column_entry", None)
            if old_entry is not None and self._rows_by_name.get(old_entry.name) is replacement:
                self._rows_by_name.pop(old_entry.name, None)
            replacement.cancel_thumbnail()
            if was_active:
                self._keyboard_active_row = None
            self.list_box.remove(replacement)
            rows.remove(replacement)

        position = replacement_position if replacement_position is not None else len(rows)
        if not self._streaming_large_folder:
            position = len(rows)
            for index, candidate in enumerate(rows):
                candidate_entry = getattr(candidate, "_column_entry", None)
                if (
                    candidate_entry is not None
                    and _compare_column_entries(
                        entry, candidate_entry, self._sort, self._directories_first
                    )
                    < 0
                ):
                    position = index
                    break

        row = self._make_entry_row(entry)
        self.list_box.insert(row, position)
        self._rows_by_name[entry.name] = row
        self._row_order.insert(position, row)
        if callable(self._on_row_created):
            self._on_row_created(self, row)
        if was_selected:
            set_row_selected(row, True)
        if was_active:
            set_row_active(row, True)
            self._keyboard_active_row = row
        if was_cursor:
            self._cursor_row = row
        if was_anchor:
            self._anchor_row = row
        if was_pinned and self._pinned_selection is not None:
            self._pinned_selection = [
                row if pinned is replacement else pinned for pinned in self._pinned_selection
            ]
        if replacing_uri and self._last_activated_uri == replacing_uri:
            self._last_activated_uri = row.uri
        self.list_box.set_placeholder(None if self.rows() else self._empty_page)
        self._restore_scroll_position(scroll_position)
        return row

    def _restore_scroll_position(self, scroll_position: float) -> None:
        def restore_scroll() -> bool:
            adjustment = self.get_vadjustment()
            upper = max(
                adjustment.get_lower(),
                adjustment.get_upper() - adjustment.get_page_size(),
            )
            adjustment.set_value(max(adjustment.get_lower(), min(upper, scroll_position)))
            return GLib.SOURCE_REMOVE

        GLib.idle_add(restore_scroll)

    def _queue_row_entries(self, entries: list[_ColumnEntry]) -> None:
        if not entries:
            return
        self._pending_row_entries.extend(entries)
        if self._fill_id != 0:
            return
        # Put a screenful on screen in the enumeration callback itself; the
        # remaining construction is bounded to one idle-sized batch per turn.
        first_count = _COLUMN_FIRST_CHUNK_ROWS if not self._rows_by_name else _COLUMN_CHUNK_ROWS
        self._append_pending_row_batch(first_count)
        if self._pending_row_entries:
            self._fill_id = GLib.idle_add(self._fill_more_rows, self._load_generation)

    def _append_pending_row_batch(self, count: int) -> None:
        base = Gio.File.new_for_uri(self.folder_uri)
        entries = self._pending_row_entries[:count]
        del self._pending_row_entries[:count]
        for entry in entries:
            self._append_entry_row(entry, base)
        self._restore_reload_rows()
        if entries and callable(self._on_loaded):
            self._on_loaded(self)

    def _fill_more_rows(self, generation: int) -> bool:
        if generation != self._load_generation or self._cancellable.is_cancelled():
            return GLib.SOURCE_REMOVE
        self._append_pending_row_batch(_COLUMN_CHUNK_ROWS)
        if self._pending_row_entries:
            return GLib.SOURCE_CONTINUE
        self._fill_id = 0
        self._maybe_finish_population()
        return GLib.SOURCE_REMOVE

    def _maybe_finish_population(self) -> None:
        if not self._enumeration_finished or self._fill_id != 0 or self._pending_row_entries:
            return
        self._load_complete = True
        if not self.rows():
            self.list_box.set_placeholder(self._empty_page)
        self._finish_reload_restore()
        if callable(self._on_loaded):
            self._on_loaded(self)

    def _restore_reload_rows(self) -> None:
        wanted = self._reload_selection_uris
        if wanted is None:
            return
        for row in self.rows():
            if row.uri in wanted and row not in self.selected_rows():
                set_row_selected(row, True)
            if row.uri == self._reload_cursor_uri:
                self._cursor_row = row
            if row.uri == self._reload_anchor_uri:
                self._anchor_row = row

    def _finish_reload_restore(self) -> None:
        if self._reload_selection_uris is None:
            return
        self._restore_reload_rows()
        scroll_position = self._reload_scroll_position
        cancellable = self._cancellable
        self._reload_selection_uris = None
        self._reload_cursor_uri = None
        self._reload_anchor_uri = None
        self._reload_scroll_position = None
        if scroll_position is None:
            return

        def restore_scroll() -> bool:
            if cancellable is not self._cancellable or cancellable.is_cancelled():
                return GLib.SOURCE_REMOVE
            adjustment = self.get_vadjustment()
            upper = max(adjustment.get_lower(), adjustment.get_upper() - adjustment.get_page_size())
            adjustment.set_value(max(adjustment.get_lower(), min(upper, scroll_position)))
            return GLib.SOURCE_REMOVE

        GLib.idle_add(restore_scroll)

    def select_child_for_uri(self, uri: str) -> bool:
        """Pre-select (highlight, without activating) the row whose child URI
        matches uri -- used to show which entry leads to the next column when
        seeding the view from the current location's ancestor chain.

        Exclusive: the list box is in MULTIPLE selection mode, where
        select_row() only *adds* to the selection, so any previously
        highlighted row is dropped first. Without that, a column whose
        selection no longer matches the open chain (e.g. backing up into it
        from a deeper folder, see column_view.py's sync_to_uri truncation)
        ends up with two rows highlighted at once."""
        target = Gio.File.new_for_uri(uri)
        row = self.list_box.get_first_child()
        while row is not None:
            if isinstance(row, MyComputerColumnRow) and Gio.File.new_for_uri(row.uri).equal(target):
                selected = self.selected_rows()
                if selected != [row]:
                    self.list_box.unselect_all()
                    set_row_selected(row, True)
                return True
            row = row.get_next_sibling()
        return False

    def clear_selection(self) -> None:
        """Drop all row selections in this column."""
        self.clear_pinned_selection()
        self.list_box.unselect_all()

    def active_index(self) -> int | None:
        """Return the arrow-key cursor's row index, if this column has one."""
        active = self._keyboard_active_row
        if active is None:
            return None
        for i, row in enumerate(self.rows()):
            if row is active:
                return i
        self._keyboard_active_row = None
        return None

    def active_row(self) -> "MyComputerColumnRow | None":
        """Return the temporary :active row used by arrow-key navigation."""
        return self._keyboard_active_row

    def clear_active_row(self) -> None:
        """Clear this column's temporary arrow-key cursor."""
        if self._keyboard_active_row is not None:
            set_row_active(self._keyboard_active_row, False)
            self._keyboard_active_row = None

    def set_active_index(self, index: int) -> None:
        """Move the temporary :active cursor without changing :selected."""
        rows = self.rows()
        if not rows:
            return
        clamped = max(0, min(index, len(rows) - 1))
        target = rows[clamped]
        if target is self._keyboard_active_row:
            return
        self.clear_active_row()
        set_row_active(target, True)
        self._keyboard_active_row = target

    def set_current_column(self, is_current: bool) -> None:
        """Mark whether this column is the one whose selected row should read
        as *the* accent-highlighted selection (see the .mc-current-column CSS
        rule in main.py). Driven by column_view.py's own tracked
        focused_index -- i.e. whichever column was last clicked (or, when
        arrow-key nav is enabled, last focused) -- rather than by actual GTK
        keyboard focus, so it works independent of any real focus-grabbing."""
        if is_current:
            self.list_box.add_css_class("mc-current-column")
        else:
            self.list_box.remove_css_class("mc-current-column")

    def rows(self) -> list["MyComputerColumnRow"]:
        """This column's rows in display order -- the keyboard-cursor helpers
        below index into this rather than tracking a separate list."""
        return self._row_order

    def selected_index(self) -> int | None:
        """Index of the currently highlighted row, or None if none is
        selected -- e.g. a freshly drilled-into column before any cursor
        movement has happened in it. With several rows selected, the first
        one in display order."""
        selected = self.selected_row()
        if selected is None:
            return None
        for i, row in enumerate(self.rows()):
            if row is selected:
                return i
        return None

    def selected_row(self) -> "MyComputerColumnRow | None":
        """The single selected row, or the first one in display order when
        several are selected.

        Derived from get_selected_rows() rather than get_selected_row():
        that one returns GTK's internal last-selected pointer, which in
        MULTIPLE selection mode is not cleared by unselect_row() and so can
        name a row that is no longer selected at all."""
        selected = self.selected_rows()
        return selected[0] if selected else None

    def selected_rows(self) -> list["MyComputerColumnRow"]:
        """This column's selected rows, in display order -- get_selected_rows()
        itself returns them in selection order, which callers that index into
        rows() (range selection, arrow-key cursor) must not depend on."""
        selected = {
            row for row in self.list_box.get_selected_rows() if isinstance(row, MyComputerColumnRow)
        }
        return [row for row in self.rows() if row in selected]

    def selected_uris(self) -> list[str]:
        return [row.uri for row in self.selected_rows()]

    def pin_selection(self) -> None:
        """Hold a modifier selection through GTK's remaining event work.

        Rebuilding the Miller paned chain reparents this list box.  GTK can
        then transfer focus to the clicked row on a later main-loop turn and
        collapse MULTIPLE selection to that row, even when Miller claimed
        the pointer gesture and GtkListBox never receives its release.  Keep
        the committed rows pinned until the native activation echo or the
        next explicit Miller input clears them.
        """
        self._pinned_selection = self.selected_rows()

    def clear_pinned_selection(self) -> None:
        """Forget any pinned selection, so the next row-activated counts as a
        real activation again."""
        self._pinned_selection = None

    def _enforce_pinned_selection(self) -> None:
        """Restore rows GTK changed while a modifier selection is pinned."""
        pinned = self._pinned_selection
        if pinned is None or self._repairing_pinned_selection:
            return
        live = [row for row in self.rows() if row in pinned]
        if self.selected_rows() == live:
            return
        self._repairing_pinned_selection = True
        try:
            self.list_box.unselect_all()
            for row in live:
                self.list_box.select_row(row)
        finally:
            self._repairing_pinned_selection = False

    def _on_selected_rows_changed(self, _list_box: Gtk.ListBox) -> None:
        # This is intentionally synchronous: waiting for another idle lets a
        # frame paint GTK's collapsed one-row state, which is the missing
        # accent background the user sees.
        self._enforce_pinned_selection()

    def _restore_pinned_selection(self) -> bool:
        """Re-apply a pinned selection and report whether one was pending.
        Rows dropped by a reload in the meantime are skipped rather than
        re-selected through a dead parent."""
        pinned = self._pinned_selection
        self._pinned_selection = None
        if pinned is None:
            return False
        live = [row for row in self.rows() if row in pinned]
        if self.selected_rows() != live:
            self.list_box.unselect_all()
            for row in live:
                self.list_box.select_row(row)
        return True

    def scroll_position(self) -> float:
        """This column's own vertical scroll offset, read live off the
        native Gtk.ScrolledWindow adjustment -- same pattern as
        selected_row(), not a value tracked separately on the object."""
        return self.get_vadjustment().get_value()

    @staticmethod
    def _child_revision_key(uri: str) -> str:
        return Gio.File.new_for_uri(uri).get_uri()

    def _next_child_revision(self, uri: str) -> int:
        self._child_revision_serial += 1
        revision = self._child_revision_serial
        self._child_revisions[self._child_revision_key(uri)] = revision
        return revision

    def _child_revision_is_current(self, uri: str, revision: int) -> bool:
        return self._child_revisions.get(self._child_revision_key(uri)) == revision

    def _finish_child_revision(self, uri: str, revision: int) -> None:
        key = self._child_revision_key(uri)
        if self._child_revisions.get(key) == revision:
            self._child_revisions.pop(key, None)

    def remove_child_uri(self, uri: str, *, revision: int | None = None) -> bool:
        """Remove one vanished child without rebuilding the whole column.

        Directory monitors report trash and permanent deletion one child at
        a time. Re-enumerating for those events blanks every row and resets
        GTK's viewport before the async scroll restoration can run. Removing
        the matching live row keeps the column and its scroll position
        stable while still notifying the host so an open descendant or
        preview of the vanished item can be collapsed.
        """
        if revision is None:
            revision = self._next_child_revision(uri)
        elif not self._child_revision_is_current(uri, revision):
            return False
        target = Gio.File.new_for_uri(uri)
        parent = target.get_parent()
        folder = Gio.File.new_for_uri(self.folder_uri)
        name = target.get_basename()
        if parent is None or not parent.equal(folder) or name is None:
            self._finish_child_revision(uri, revision)
            return False
        self._removed_child_names.add(name)
        refresh_id = self._child_refresh_ids.pop(uri, 0)
        if refresh_id:
            GLib.source_remove(refresh_id)
        self._pending_row_entries[:] = [
            entry for entry in self._pending_row_entries if entry.name != name
        ]
        self._load_entries[:] = [entry for entry in self._load_entries if entry.name != name]
        row = next(
            (
                candidate
                for candidate in self.rows()
                if Gio.File.new_for_uri(candidate.uri).equal(target)
            ),
            None,
        )
        if row is None:
            self._finish_child_revision(uri, revision)
            return False

        scroll_position = self.scroll_position()
        entry = getattr(row, "_column_entry", None)
        name = entry.name if entry is not None else name
        if name is not None and self._rows_by_name.get(name) is row:
            self._rows_by_name.pop(name, None)
        else:
            for indexed_name, indexed_row in tuple(self._rows_by_name.items()):
                if indexed_row is row:
                    self._rows_by_name.pop(indexed_name, None)
                    break

        if self._keyboard_active_row is row:
            self.clear_active_row()
        if getattr(self, "_cursor_row", None) is row:
            self._cursor_row = None
        if getattr(self, "_anchor_row", None) is row:
            self._anchor_row = None
        if self._pinned_selection is not None:
            self._pinned_selection = [
                pinned for pinned in self._pinned_selection if pinned is not row
            ]
        if self._last_activated_uri == row.uri:
            self._last_activated_uri = None
            self._last_activated_time = 0

        row.cancel_thumbnail()
        self.list_box.remove(row)
        if row in self._row_order:
            self._row_order.remove(row)
        if self._load_complete and not self.rows():
            self.list_box.set_placeholder(self._empty_page)

        self._restore_scroll_position(scroll_position)
        if callable(self._on_loaded):
            self._on_loaded(self)
        self._finish_child_revision(uri, revision)
        return True

    def refresh_child_uri(
        self,
        uri: str,
        *,
        replacing_uri: str | None = None,
        revision: int | None = None,
        notify_change: bool = False,
    ) -> None:
        """Refresh one direct child after a monitor notification."""
        if revision is None:
            revision = self._next_child_revision(uri)
        elif not self._child_revision_is_current(uri, revision):
            return
        target = Gio.File.new_for_uri(uri)
        parent = target.get_parent()
        if parent is None or not parent.equal(Gio.File.new_for_uri(self.folder_uri)):
            self._finish_child_revision(uri, revision)
            return
        generation = self._load_generation

        def on_info_ready(file: Gio.File, result: Gio.AsyncResult) -> None:
            if (
                generation != self._load_generation
                or self._cancellable.is_cancelled()
                or not self._child_revision_is_current(uri, revision)
            ):
                return
            try:
                info = file.query_info_finish(result)
            except GLib.Error as error:
                if error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.NOT_FOUND):
                    if replacing_uri:
                        self.remove_child_uri(replacing_uri)
                    self.remove_child_uri(uri, revision=revision)
                else:
                    self._finish_child_revision(uri, revision)
                return
            entries = self._entries_from_infos([info])
            if not entries:
                if replacing_uri:
                    self.remove_child_uri(replacing_uri)
                self.remove_child_uri(uri, revision=revision)
                return
            name = entries[0].name
            self._removed_child_names.discard(name)
            row = self._insert_entry_row(entries[0], replacing_uri=replacing_uri)
            self._finish_child_revision(uri, revision)
            if notify_change and callable(self._on_child_changed):
                self._on_child_changed(self, uri)
            if row is not None and callable(self._on_loaded):
                self._on_loaded(self)

        target.query_info_async(
            _COLUMN_FILE_ATTRIBUTES,
            Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS,
            GLib.PRIORITY_DEFAULT,
            self._cancellable,
            on_info_ready,
        )

    def schedule_child_refresh(self, uri: str) -> None:
        """Coalesce noisy CHANGED/HINT/ATTRIBUTE events for one child."""
        revision = self._next_child_revision(uri)
        existing = self._child_refresh_ids.pop(uri, 0)
        if existing:
            GLib.source_remove(existing)

        def refresh() -> bool:
            self._child_refresh_ids.pop(uri, None)
            self.refresh_child_uri(uri, revision=revision, notify_change=True)
            return GLib.SOURCE_REMOVE

        self._child_refresh_ids[uri] = GLib.timeout_add(_COLUMN_CHILD_REFRESH_DEBOUNCE_MS, refresh)

    def rename_child_uri(self, old_uri: str, new_uri: str, *, notify_host: bool = True) -> None:
        old_file = Gio.File.new_for_uri(old_uri)
        new_file = Gio.File.new_for_uri(new_uri)
        new_name = new_file.get_basename()
        old_name = old_file.get_basename()
        old_revision = self._next_child_revision(old_uri)
        new_revision = self._next_child_revision(new_uri)
        if old_name:
            self._removed_child_names.add(old_name)
        if new_name:
            self._removed_child_names.discard(new_name)

        # Move the live row's identity immediately. If the renamed object is
        # deleted before query_info_async returns, the deletion event can now
        # find and remove it by its new URI instead of leaving the old row
        # stranded forever. The final info query replaces its label/icon and
        # correct sort position without rebuilding the column.
        target = next(
            (row for row in self.rows() if Gio.File.new_for_uri(row.uri).equal(old_file)),
            None,
        )
        if target is not None:
            entry = getattr(target, "_column_entry", None)
            if entry is not None and new_name:
                if self._rows_by_name.get(entry.name) is target:
                    self._rows_by_name.pop(entry.name, None)
                target._column_entry = dataclasses.replace(entry, name=new_name)
                self._rows_by_name[new_name] = target
            target.uri = new_uri
            if self._last_activated_uri == old_uri:
                self._last_activated_uri = new_uri
        self._finish_child_revision(old_uri, old_revision)
        if notify_host and callable(self._on_child_renamed):
            self._on_child_renamed(self, old_uri, new_uri)
        self.refresh_child_uri(new_uri, revision=new_revision)

    def grab_list_focus(self) -> bool:
        return self.list_box.grab_focus()

    def _stop_fill(self) -> None:
        """Drop streamed row construction that belongs to an old load."""
        if self._fill_id != 0:
            GLib.source_remove(self._fill_id)
            self._fill_id = 0
        self._pending_row_entries.clear()

    def _on_row_activated_internal(self, _list_box: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        if not isinstance(row, MyComputerColumnRow):
            return

        # Miller browsing itself is always single-click, for every policy: one
        # click drills into a folder or previews a file, exactly like the
        # folder-column drill-down (set_activate_on_single_click above). Once a
        # file is already the active preview (the "last" column), a further
        # click on that same row is an *open* action rather than navigation,
        # and Nautilus' click-policy governs it there -- same contract as the
        # preview pane (_on_preview_area_pressed/_released), just re-derived by
        # timing instead of n_press since the chain rebuild below destroys
        # GTK's own press-count tracking on the row before a second click can
        # land (see the field comment above).
        now = GLib.get_monotonic_time()
        double_click_us = Gtk.Settings.get_default().get_property("gtk-double-click-time") * 1000
        is_same_file = not row.is_dir and row.uri == self._last_activated_uri
        is_repeat_click = is_same_file and (now - self._last_activated_time) <= double_click_us
        self._last_activated_uri = row.uri
        self._last_activated_time = now

        single_click = self._ext._nautilus_prefs.click_policy == "single"
        if is_same_file and (is_repeat_click or single_click):
            # Single policy: every further click on the already-active file
            # opens it, no timing needed -- it already selected/previewed on
            # the click before this one. Double policy: only a genuine repeat
            # click within the double-click window opens it.
            if callable(self._on_file_open):
                self._on_file_open(row.uri, row.content_type)
            else:
                _open_file_with_default_app(row.uri, self._cancellable, self._on_open_error)
            return

        self._on_row_activated(self, row)

    def _on_dir_changed(
        self,
        _monitor: Gio.FileMonitor,
        _file: Gio.File,
        _other_file: Gio.File | None,
        event_type: Gio.FileMonitorEvent,
    ) -> None:
        watched_folder = Gio.File.new_for_uri(self.folder_uri)
        if _file.equal(watched_folder):
            if (
                event_type
                in (
                    Gio.FileMonitorEvent.RENAMED,
                    Gio.FileMonitorEvent.MOVED,
                )
                and _other_file is not None
            ):
                if callable(self._on_folder_moved):
                    self._on_folder_moved(self, self.folder_uri, _other_file.get_uri())
                else:
                    self.set_folder_uri(_other_file.get_uri())
                return
            if event_type in (
                Gio.FileMonitorEvent.DELETED,
                Gio.FileMonitorEvent.MOVED_OUT,
                Gio.FileMonitorEvent.PRE_UNMOUNT,
                Gio.FileMonitorEvent.UNMOUNTED,
            ):
                if callable(self._on_folder_unavailable):
                    self._on_folder_unavailable(self, self.folder_uri)
                else:
                    self.reload()
                return

        if event_type in (
            Gio.FileMonitorEvent.DELETED,
            Gio.FileMonitorEvent.MOVED_OUT,
        ):
            self.remove_child_uri(_file.get_uri())
            return

        expected_file = any(
            Gio.File.new_for_uri(expected).equal(_file) for expected in self._expected_child_uris
        )
        expected_destination = _other_file is not None and any(
            Gio.File.new_for_uri(expected).equal(_other_file)
            for expected in self._expected_child_uris
        )
        if expected_file and event_type in (
            Gio.FileMonitorEvent.CREATED,
            Gio.FileMonitorEvent.MOVED_IN,
            Gio.FileMonitorEvent.CHANGED,
            Gio.FileMonitorEvent.CHANGES_DONE_HINT,
            Gio.FileMonitorEvent.ATTRIBUTE_CHANGED,
        ):
            return
        if expected_destination and event_type in (
            Gio.FileMonitorEvent.RENAMED,
            Gio.FileMonitorEvent.MOVED,
        ):
            # A compressor may publish a temporary file by renaming it onto
            # the expected destination. Keep that destination hidden until
            # the operation's completion callback queries its final state.
            self.remove_child_uri(_file.get_uri())
            return
        if event_type in (Gio.FileMonitorEvent.RENAMED, Gio.FileMonitorEvent.MOVED):
            if _other_file is not None:
                old_parent = _file.get_parent()
                new_parent = _other_file.get_parent()
                old_here = old_parent is not None and old_parent.equal(watched_folder)
                new_here = new_parent is not None and new_parent.equal(watched_folder)
                if old_here and new_here:
                    self.rename_child_uri(_file.get_uri(), _other_file.get_uri())
                elif old_here:
                    self.remove_child_uri(_file.get_uri())
                elif new_here:
                    self.refresh_child_uri(_other_file.get_uri())
            return
        if event_type in (Gio.FileMonitorEvent.CREATED, Gio.FileMonitorEvent.MOVED_IN):
            self.refresh_child_uri(_file.get_uri())
            return
        if event_type in (
            Gio.FileMonitorEvent.CHANGED,
            Gio.FileMonitorEvent.CHANGES_DONE_HINT,
            Gio.FileMonitorEvent.ATTRIBUTE_CHANGED,
        ):
            self.schedule_child_refresh(_file.get_uri())

    def destroy_enumeration(self) -> None:
        if self._file_monitor is not None:
            self._file_monitor.cancel()
            self._file_monitor = None
        self._expected_child_uris.clear()
        for source_id in self._child_refresh_ids.values():
            GLib.source_remove(source_id)
        self._child_refresh_ids.clear()
        self._child_revisions.clear()
        self._load_generation += 1
        self._cancellable.cancel()
        self._stop_fill()
        self._load_entries.clear()
        self._removed_child_names.clear()
        for row in self.rows():
            row.cancel_thumbnail()


def _is_media_content_type(content_type: str) -> bool:
    return content_type.startswith("image/") or content_type.startswith("video/")


def _format_datetime(unix_time: int) -> str:
    if not unix_time:
        return ""
    return GLib.DateTime.new_from_unix_local(unix_time).format("%x %X")


def _open_file_with_default_app(file_uri: str, cancellable: Gio.Cancellable, on_error=None) -> None:
    """Launch file_uri with its default app. Shared by the preview column's
    click handlers and file-row fallback, so both surfaces report launch
    failures through the same optional callback."""

    def on_done(_source, result: Gio.AsyncResult) -> None:
        try:
            Gio.AppInfo.launch_default_for_uri_finish(result)
        except GLib.Error as error:
            if error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                return
            _log(f"Open-with-default failed for {file_uri!r}: {error.message}")
            if callable(on_error):
                on_error(error.message)

    Gio.AppInfo.launch_default_for_uri_async(file_uri, None, cancellable, on_done)


def _make_kv_row(title: str, size_group: Gtk.SizeGroup | None = None) -> tuple[Gtk.Box, Gtk.Label]:
    """A label/value row for the preview column's details area (e.g.
    "Modified" on the left, the timestamp just after it).

    Pass the same size_group to every row so all the titles share one width
    and the values line up in a column. Without it each value would start at
    a different x, since "Created", "Modified" and "Dimensions" are visibly
    different lengths."""
    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
    row.set_halign(Gtk.Align.FILL)
    row.set_hexpand(True)

    title_lbl = Gtk.Label(label=title)
    title_lbl.get_style_context().add_class("dim-label")
    title_lbl.get_style_context().add_class("caption")
    title_lbl.set_halign(Gtk.Align.START)
    title_lbl.set_xalign(0.0)
    # The title is a fixed word; it takes only the width the size group
    # settles on, so every remaining pixel is available to the value.
    title_lbl.set_hexpand(False)
    if size_group is not None:
        size_group.add_widget(title_lbl)
    row.append(title_lbl)

    value_lbl = Gtk.Label(label="")
    value_lbl.get_style_context().add_class("caption")
    # Starts immediately after the title rather than being pushed to the
    # opposite edge: the details area is as wide as the preview column, so
    # right-aligning the value (an earlier version did) left a growing gap
    # between a label and the value it belongs to.
    value_lbl.set_halign(Gtk.Align.START)
    value_lbl.set_xalign(0.0)
    # Still takes the row's slack, so a long value -- a full date *and* time,
    # which in some locales runs past a narrow column -- has somewhere to
    # ellipsize into instead of overflowing. The column clips with
    # overflow:hidden and would otherwise cut the text mid-character with no
    # indication anything was lost.
    value_lbl.set_hexpand(True)
    value_lbl.set_ellipsize(Pango.EllipsizeMode.END)
    row.append(value_lbl)

    return row, value_lbl


class MyComputerPreviewColumn(Gtk.Box):
    """A preview column split between a responsive image area and bottom-pinned
    file details. Always the permanent rightmost column in the chain (see
    column_view.py's populate_column_view / _on_row_activated, which always
    (re)append one after any truncate). file_uri is None for its empty state.
    Real file preview (text, ...) is a later iteration."""

    __gtype_name__ = "MyComputerPreviewColumn"

    def __init__(self, ext, file_uri: str | list[str] | None, on_open_error=None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._ext = ext
        self._on_open_error = on_open_error
        self.file_uris: list[str] = (
            file_uri if isinstance(file_uri, list) else ([file_uri] if file_uri else [])
        )
        self.file_uri: str | None = file_uri if isinstance(file_uri, str) else None
        self._cancellable = Gio.Cancellable()
        self._discoverer = None
        # Deferred single-click-policy activation, set on "pressed" and consumed on
        # "released" -- see _on_preview_area_pressed/_released/_stopped. Initialized above
        # the file_uri is None early-return below so it exists on empty-state instances too.
        self._activate_on_release = False
        # Video decoding lives in WebKit's disposable web process. Never put
        # Gtk.MediaFile/GStreamer in Nautilus: a native decoder fault corrupts
        # or terminates the whole file manager before Python can handle it.
        self._video_web_view = None
        self._video_stream_process: Gio.Subprocess | None = None
        self._video_stream_stdin: Gio.OutputStream | None = None
        self._video_stream_stdout: Gio.DataInputStream | None = None
        self._video_stream_generation = 0
        self._video_poll_id = 0
        self._video_poll_inflight = False
        self._video_playing = False
        self._video_muted = False
        self._video_volume = 1.0
        self._video_position = 0.0
        self._video_duration = 0.0
        self._audio_process: Gio.Subprocess | None = None
        self._audio_stdin: Gio.OutputStream | None = None
        self._audio_stdout: Gio.DataInputStream | None = None
        self._audio_command_queue: list[bytes] = []
        self._audio_write_pending = False
        self._audio_generation = 0
        self._audio_process_exited = False
        self._audio_exit_successful = False
        self._audio_stdout_eof = False
        self._audio_playing = False
        self._audio_muted = False
        self._audio_volume = 1.0
        self._audio_position = 0.0
        self._audio_duration = 0.0
        self._staged_preview_path: str | None = None
        self._spreadsheet_tmpdir: str | None = None
        self._spreadsheet_generation = 0
        self._spreadsheet_view = None
        self._spreadsheet_scroll_delta = 0.0
        self._spreadsheet_scroll_idle_id = 0
        self._document_tmpdir: str | None = None
        self._document_generation = 0
        self._archive_listing: _ArchiveListing | None = None
        self._archive_folder = ""
        self._archive_generation = 0
        self._worker_futures: list[concurrent.futures.Future] = []
        self._image_ocr_words: list[_PdfWord] = []
        self._image_ocr_sections: list[_OcrSection] = []
        self._image_ocr_lines: list[tuple[float, float, float, float, int, int]] = []
        self._image_ocr_width = 0
        self._image_ocr_height = 0
        self._image_sel_anchor: int | None = None
        self._image_sel_focus: int | None = None
        self._image_drag_moved = False
        self._image_text_cursor_shown = False
        self._image_tooltip_section: str | None = None
        self._image_zoom_pct = 100
        self._image_aspect_ratio = 1.0
        self._pdf_tooltip_section: str | None = None

        self.set_size_request(_COLUMN_PREVIEW_WIDTH, -1)
        self.set_vexpand(True)
        self.set_valign(Gtk.Align.FILL)
        self.set_hexpand(True)
        self.set_halign(Gtk.Align.FILL)
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self.add_css_class("mc-column")
        self.add_css_class("mc-preview-column")

        if not self.file_uris:
            return

        preview_area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        preview_area.set_halign(Gtk.Align.FILL)
        preview_area.set_valign(Gtk.Align.FILL)
        preview_area.set_vexpand(True)
        preview_area.set_hexpand(True)
        # Open the file with its default app on click, honoring Nautilus' own
        # single-click/double-click setting -- unlike the folder columns to its
        # left, which are always single-click (Miller drill-down, see
        # MyComputerColumn). Mirrors the native item-cell press/release state
        # machine (nautilus-list-base.c on_item_click_pressed/released/stopped):
        # double-click policy activates on the second press; single-click policy
        # defers to release so a press that turns into a drag doesn't activate.
        # The gesture is left with GTK's default button (1, primary-only) --
        # middle/secondary never reach these handlers.
        if self.file_uri:
            click = Gtk.GestureClick()
            click.connect("pressed", self._on_preview_area_pressed)
            click.connect("released", self._on_preview_area_released)
            click.connect("stopped", self._on_preview_area_stopped)
            preview_area.add_controller(click)
        self._preview_area = preview_area
        self.append(preview_area)

        self._icon = Gtk.Image()
        self._icon.set_pixel_size(128)
        self._icon.set_from_icon_name(
            _MULTI_SELECTION_ICON_NAME if len(self.file_uris) > 1 else "text-x-generic"
        )
        self._icon.set_halign(Gtk.Align.CENTER)
        self._icon.set_valign(Gtk.Align.CENTER)
        self._icon.set_vexpand(True)

        self._thumb_frame = Gtk.AspectFrame.new(0.5, 0.5, 1.0, False)
        self._thumb_frame.set_halign(Gtk.Align.FILL)
        self._thumb_frame.set_valign(Gtk.Align.FILL)
        self._thumb_frame.set_hexpand(True)
        self._thumb_frame.set_vexpand(True)

        self._thumb = Gtk.Picture()
        self._thumb.set_halign(Gtk.Align.FILL)
        self._thumb.set_valign(Gtk.Align.FILL)
        self._thumb.set_hexpand(True)
        self._thumb.set_vexpand(True)
        self._thumb.set_can_shrink(True)
        self._thumb.set_content_fit(Gtk.ContentFit.FILL)
        self._thumb.set_overflow(Gtk.Overflow.HIDDEN)
        self._thumb.add_css_class("mc-preview-image")

        # Transparent OCR interaction layer over the image. It is not a
        # target until PaddleOCR has found real words, so ordinary photos and
        # systems without the private runtime behave like plain Gtk.Picture.
        self._image_overlay = Gtk.Overlay()
        self._image_overlay.set_hexpand(True)
        self._image_overlay.set_vexpand(True)
        self._image_overlay.set_child(self._thumb)
        self._image_text_layer = Gtk.DrawingArea()
        self._image_text_layer.set_hexpand(True)
        self._image_text_layer.set_vexpand(True)
        self._image_text_layer.set_halign(Gtk.Align.FILL)
        self._image_text_layer.set_valign(Gtk.Align.FILL)
        self._image_text_layer.set_focusable(True)
        self._image_text_layer.set_can_target(False)
        self._image_text_layer.set_draw_func(self._draw_image_ocr_selection)
        self._image_caret_cursor = Gdk.Cursor.new_from_name("text", None)

        image_drag = Gtk.GestureDrag()
        image_drag.set_button(1)
        image_drag.connect("drag-begin", self._on_image_drag_begin)
        image_drag.connect("drag-update", self._on_image_drag_update)
        image_drag.connect("drag-end", self._on_image_drag_end)
        self._image_text_layer.add_controller(image_drag)
        image_click = Gtk.GestureClick(button=1)
        image_click.connect("pressed", self._on_image_click_pressed)
        image_click.connect("released", self._on_image_click_released)
        self._image_text_layer.add_controller(image_click)
        image_motion = Gtk.EventControllerMotion()
        image_motion.connect("motion", self._on_image_motion)
        image_motion.connect("leave", self._on_image_motion_leave)
        self._image_text_layer.add_controller(image_motion)
        image_context = Gtk.GestureClick(button=3)
        image_context.connect("pressed", self._on_image_context_pressed)
        # Keep the image menu available even when OCR found no text and the
        # transparent text layer is therefore non-targetable. Events from an
        # active text layer bubble to this same overlay too.
        self._image_overlay.add_controller(image_context)
        image_keys = Gtk.EventControllerKey()
        image_keys.connect("key-pressed", self._on_image_key_pressed)
        self._image_text_layer.add_controller(image_keys)
        image_scroll = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.BOTH_AXES)
        image_scroll.connect("scroll", self._on_image_scroll)
        self._image_text_layer.add_controller(image_scroll)

        self._image_overlay.add_overlay(self._image_text_layer)
        self._image_overlay.set_measure_overlay(self._image_text_layer, False)
        self._thumb_frame.set_child(self._image_overlay)

        self._thumb_clamp = Adw.Clamp(maximum_size=_COLUMN_PREVIEW_IMAGE_MAX_WIDTH)
        self._thumb_clamp.set_halign(Gtk.Align.FILL)
        self._thumb_clamp.set_valign(Gtk.Align.FILL)
        self._thumb_clamp.set_hexpand(True)
        self._thumb_clamp.set_vexpand(True)
        self._thumb_clamp.set_child(self._thumb_frame)

        self._image_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._image_box.set_hexpand(True)
        self._image_box.set_vexpand(True)
        image_toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        image_toolbar.set_halign(Gtk.Align.CENTER)
        image_zoom_out = Gtk.Button.new_from_icon_name("zoom-out-symbolic")
        image_zoom_out.add_css_class("flat")
        image_zoom_out.connect("clicked", lambda *_args: self._change_image_zoom(-25))
        self._image_zoom_label = Gtk.Button(label="100%")
        self._image_zoom_label.add_css_class("flat")
        self._image_zoom_label.set_tooltip_text(_("Fit image to preview"))
        self._image_zoom_label.connect("clicked", lambda *_args: self._set_image_zoom(100))
        image_zoom_in = Gtk.Button.new_from_icon_name("zoom-in-symbolic")
        image_zoom_in.add_css_class("flat")
        image_zoom_in.connect("clicked", lambda *_args: self._change_image_zoom(25))
        image_toolbar.append(image_zoom_out)
        image_toolbar.append(self._image_zoom_label)
        image_toolbar.append(image_zoom_in)
        self._image_zoom_out = image_zoom_out
        self._image_zoom_in = image_zoom_in
        self._image_scroller = Gtk.ScrolledWindow()
        self._image_scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self._image_scroller.set_hexpand(True)
        self._image_scroller.set_vexpand(True)
        self._image_scroller.set_child(self._thumb_clamp)
        self._image_box.append(image_toolbar)
        self._image_box.append(self._image_scroller)

        self._preview_stack = Gtk.Stack()
        self._preview_stack.set_halign(Gtk.Align.FILL)
        self._preview_stack.set_valign(Gtk.Align.FILL)
        self._preview_stack.set_hexpand(True)
        self._preview_stack.set_vexpand(True)
        self._preview_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._preview_stack.set_transition_duration(100)
        loading = Gtk.Spinner()
        loading.set_spinning(True)
        loading.set_halign(Gtk.Align.CENTER)
        loading.set_valign(Gtk.Align.CENTER)
        loading.set_size_request(32, 32)
        self._text_buffer = Gtk.TextBuffer()
        self._text_view = Gtk.TextView.new_with_buffer(self._text_buffer)
        self._text_view.set_editable(False)
        self._text_view.set_cursor_visible(False)
        self._text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._text_view.add_css_class("monospace")
        self._text_view.add_css_class("mc-preview-text")

        self._text_scroll = Gtk.ScrolledWindow()
        self._text_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._text_scroll.set_child(self._text_view)
        self._text_scroll.set_vexpand(True)
        self._text_scroll.set_hexpand(True)

        self._video_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._video_box.set_halign(Gtk.Align.FILL)
        self._video_box.set_valign(Gtk.Align.FILL)
        self._video_box.set_hexpand(True)
        self._video_box.set_vexpand(True)

        self._video_picture = Gtk.Picture()
        self._video_picture.set_halign(Gtk.Align.FILL)
        self._video_picture.set_valign(Gtk.Align.FILL)
        self._video_picture.set_hexpand(True)
        self._video_picture.set_vexpand(True)
        self._video_picture.set_can_shrink(True)
        self._video_picture.set_content_fit(Gtk.ContentFit.CONTAIN)

        self._audio_visual = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._audio_visual.set_halign(Gtk.Align.FILL)
        self._audio_visual.set_valign(Gtk.Align.FILL)
        self._audio_visual.set_hexpand(True)
        self._audio_visual.set_vexpand(True)
        audio_icon = Gtk.Image.new_from_icon_name("audio-x-generic-symbolic")
        audio_icon.set_pixel_size(128)
        audio_icon.set_halign(Gtk.Align.CENTER)
        audio_icon.set_valign(Gtk.Align.CENTER)
        audio_icon.set_vexpand(True)
        self._audio_visual.append(audio_icon)

        self._media_visual_stack = Gtk.Stack()
        self._media_visual_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._media_visual_stack.set_hexpand(True)
        self._media_visual_stack.set_vexpand(True)
        self._media_visual_stack.add_named(self._video_picture, "video")
        self._media_visual_stack.add_named(self._audio_visual, "audio")
        self._video_box.append(self._media_visual_stack)

        self._video_controls = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self._video_controls.set_halign(Gtk.Align.FILL)
        self._video_controls.add_css_class("mc-video-controls")

        timeline_controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        timeline_controls.set_halign(Gtk.Align.FILL)
        volume_controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        volume_controls.set_halign(Gtk.Align.CENTER)

        self._btn_play = Gtk.Button.new_from_icon_name("media-playback-start-symbolic")
        self._btn_play.add_css_class("flat")
        self._btn_play.set_tooltip_text(_("Play or pause"))
        self._btn_play.connect("clicked", self._on_video_play_toggled)

        self._btn_mute = Gtk.Button.new_from_icon_name("audio-volume-high-symbolic")
        self._btn_mute.add_css_class("flat")
        self._btn_mute.set_tooltip_text(_("Mute or unmute"))
        self._btn_mute.connect("clicked", self._on_video_mute_toggled)

        self._video_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self._video_scale.set_draw_value(False)
        self._video_scale.set_hexpand(True)
        self._video_scale.set_tooltip_text(_("Playback position"))
        self._video_scale.connect("change-value", self._on_video_seek_changed)

        self._video_time_lbl = Gtk.Label(label="0:00 / 0:00")
        self._video_time_lbl.add_css_class("caption")
        self._video_time_lbl.add_css_class("dim-label")

        self._volume_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self._volume_scale.set_draw_value(False)
        self._volume_scale.set_value(100)
        self._volume_scale.set_size_request(120, -1)
        self._volume_scale.set_tooltip_text(_("Volume"))
        self._volume_scale.connect("value-changed", self._on_media_volume_changed)
        self._setting_volume_scale = False

        timeline_controls.append(self._btn_play)
        timeline_controls.append(self._video_scale)
        timeline_controls.append(self._video_time_lbl)
        volume_controls.append(self._btn_mute)
        volume_controls.append(self._volume_scale)
        self._video_controls.append(timeline_controls)
        self._video_controls.append(volume_controls)
        self._video_box.append(self._video_controls)

        self._archive_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._archive_box.set_hexpand(True)
        self._archive_box.set_vexpand(True)

        archive_toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        archive_toolbar.add_css_class("toolbar")
        self._archive_up_button = Gtk.Button.new_from_icon_name("go-up-symbolic")
        self._archive_up_button.add_css_class("flat")
        self._archive_up_button.set_tooltip_text(_("Parent folder in archive"))
        self._archive_up_button.connect("clicked", self._on_archive_up_clicked)
        self._archive_path_label = Gtk.Label(label="/")
        self._archive_path_label.set_xalign(0)
        self._archive_path_label.set_hexpand(True)
        self._archive_path_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self._archive_path_label.add_css_class("heading")
        self._archive_count_label = Gtk.Label(label="")
        self._archive_count_label.add_css_class("caption")
        self._archive_count_label.add_css_class("dim-label")
        archive_toolbar.append(self._archive_up_button)
        archive_toolbar.append(self._archive_path_label)
        archive_toolbar.append(self._archive_count_label)

        archive_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        archive_header.set_margin_start(10)
        archive_header.set_margin_end(10)
        archive_name_header = Gtk.Label(label=_("Name"))
        archive_name_header.set_xalign(0)
        archive_name_header.set_hexpand(True)
        archive_name_header.add_css_class("caption")
        archive_name_header.add_css_class("dim-label")
        archive_size_header = Gtk.Label(label=_("Size"))
        archive_size_header.set_xalign(1)
        archive_size_header.set_width_chars(10)
        archive_size_header.add_css_class("caption")
        archive_size_header.add_css_class("dim-label")
        archive_header.append(archive_name_header)
        archive_header.append(archive_size_header)

        self._archive_store = Gio.ListStore.new(_ArchiveListItem)
        self._archive_selection = Gtk.SingleSelection.new(self._archive_store)
        self._archive_selection.set_autoselect(False)
        self._archive_selection.set_can_unselect(True)
        archive_factory = Gtk.SignalListItemFactory()
        archive_factory.connect("setup", self._setup_archive_list_item)
        archive_factory.connect("bind", self._bind_archive_list_item)
        self._archive_list = Gtk.ListView.new(self._archive_selection, archive_factory)
        self._archive_list.set_single_click_activate(False)
        self._archive_list.connect("activate", self._on_archive_item_activated)
        archive_keys = Gtk.EventControllerKey()
        archive_keys.connect("key-pressed", self._on_archive_key_pressed)
        self._archive_list.add_controller(archive_keys)

        archive_scroll = Gtk.ScrolledWindow()
        archive_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        archive_scroll.set_hexpand(True)
        archive_scroll.set_vexpand(True)
        archive_scroll.set_child(self._archive_list)

        archive_empty = Gtk.Label(label=_("This folder is empty"))
        archive_empty.set_halign(Gtk.Align.CENTER)
        archive_empty.set_valign(Gtk.Align.CENTER)
        archive_empty.set_hexpand(True)
        archive_empty.set_vexpand(True)
        archive_empty.add_css_class("dim-label")
        self._archive_content_stack = Gtk.Stack()
        self._archive_content_stack.set_hexpand(True)
        self._archive_content_stack.set_vexpand(True)
        self._archive_content_stack.add_named(archive_scroll, "list")
        self._archive_content_stack.add_named(archive_empty, "empty")

        self._archive_box.append(archive_toolbar)
        self._archive_box.append(Gtk.Separator())
        self._archive_box.append(archive_header)
        self._archive_box.append(Gtk.Separator())
        self._archive_box.append(self._archive_content_stack)

        self._pdf_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._pdf_box.set_halign(Gtk.Align.FILL)
        self._pdf_box.set_valign(Gtk.Align.FILL)
        self._pdf_box.set_hexpand(True)
        self._pdf_box.set_vexpand(True)

        self._pdf_toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        # Compact and centered -- a small slider is easier to nudge precisely
        # than one stretched across the whole column, and this row already
        # has plenty of fixed-size buttons either side of it.
        self._pdf_toolbar.set_halign(Gtk.Align.CENTER)
        self._pdf_toolbar.add_css_class("mc-pdf-toolbar")

        self._btn_pdf_prev = Gtk.Button.new_from_icon_name("go-previous-symbolic")
        self._btn_pdf_prev.add_css_class("flat")
        self._btn_pdf_prev.set_tooltip_text(_("Previous page"))
        self._btn_pdf_prev.connect("clicked", self._on_pdf_prev_page)

        self._lbl_pdf_page = Gtk.Label(label="1 / 1")
        self._lbl_pdf_page.add_css_class("caption")

        self._btn_pdf_next = Gtk.Button.new_from_icon_name("go-next-symbolic")
        self._btn_pdf_next.add_css_class("flat")
        self._btn_pdf_next.set_tooltip_text(_("Next page"))
        self._btn_pdf_next.connect("clicked", self._on_pdf_next_page)

        self._btn_pdf_zoom_out = Gtk.Button.new_from_icon_name("zoom-out-symbolic")
        self._btn_pdf_zoom_out.add_css_class("flat")
        self._btn_pdf_zoom_out.connect("clicked", self._on_pdf_zoom_out)

        self._pdf_zoom_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, _PDF_ZOOM_PCT_MIN, _PDF_ZOOM_PCT_MAX, 1
        )
        self._pdf_zoom_scale.set_draw_value(False)
        self._pdf_zoom_scale.set_value(100)
        # Fixed, not hexpand -- this is a compact centered toolbar (see
        # above), not a full-width control. 60px (an earlier value here) was
        # too short to drag precisely with the buttons and reset label
        # already crowding the row; 120px gives the track actual room
        # without stretching the toolbar edge-to-edge again.
        self._pdf_zoom_scale.set_size_request(120, -1)
        self._pdf_zoom_scale.connect("value-changed", self._on_pdf_zoom_scale_changed)
        # Guards set_value() calls below (new file, new page, +/-, reset)
        # from being mistaken for a user drag by that same handler.
        self._pdf_setting_zoom_scale = False

        self._btn_pdf_zoom_in = Gtk.Button.new_from_icon_name("zoom-in-symbolic")
        self._btn_pdf_zoom_in.add_css_class("flat")
        self._btn_pdf_zoom_in.connect("clicked", self._on_pdf_zoom_in)

        # A button, not a bare label: doubles as the "back to 100%" reset the
        # slider itself has no notch for (see _on_pdf_zoom_reset) -- there was
        # previously no way back to the default size at all once zoomed.
        self._btn_pdf_zoom = Gtk.Button(label="100%")
        self._btn_pdf_zoom.add_css_class("flat")
        self._btn_pdf_zoom.add_css_class("caption")
        self._btn_pdf_zoom.set_tooltip_text(_("Reset zoom"))
        self._btn_pdf_zoom.connect("clicked", self._on_pdf_zoom_reset)

        self._pdf_toolbar.append(self._btn_pdf_prev)
        self._pdf_toolbar.append(self._lbl_pdf_page)
        self._pdf_toolbar.append(self._btn_pdf_next)
        self._pdf_toolbar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        self._pdf_toolbar.append(self._btn_pdf_zoom_out)
        self._pdf_toolbar.append(self._pdf_zoom_scale)
        self._pdf_toolbar.append(self._btn_pdf_zoom_in)
        self._pdf_toolbar.append(self._btn_pdf_zoom)

        # One drawing surface represents the full document. Its requested
        # height reserves every page from pdfinfo geometry, while the draw
        # callback paints only the few loaded pages around the viewport. This
        # keeps a 1,000-page PDF at one widget instead of 3,000 page widgets.
        self._pdf_canvas = Gtk.DrawingArea()
        self._pdf_canvas.set_halign(Gtk.Align.CENTER)
        self._pdf_canvas.set_valign(Gtk.Align.START)
        self._pdf_canvas.set_focusable(True)
        self._pdf_canvas.set_draw_func(self._draw_pdf_document)
        # One drag gesture for the whole canvas rather than one per page, so a
        # selection can start on one page and continue onto the next -- the
        # coordinates it reports are in the canvas's space, which spans
        # the entire document.
        pdf_drag = Gtk.GestureDrag()
        pdf_drag.set_button(1)
        pdf_drag.connect("drag-begin", self._on_pdf_drag_begin)
        pdf_drag.connect("drag-update", self._on_pdf_drag_update)
        pdf_drag.connect("drag-end", self._on_pdf_drag_end)
        self._pdf_canvas.add_controller(pdf_drag)
        # Tracks the pointer purely to swap the cursor over text.
        pdf_motion = Gtk.EventControllerMotion()
        pdf_motion.connect("motion", self._on_pdf_motion)
        pdf_motion.connect("leave", self._on_pdf_motion_leave)
        self._pdf_canvas.add_controller(pdf_motion)
        pdf_context = Gtk.GestureClick(button=3)
        pdf_context.connect("pressed", self._on_pdf_context_pressed)
        self._pdf_canvas.add_controller(pdf_context)

        self._pdf_scroll = Gtk.ScrolledWindow()
        self._pdf_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self._pdf_scroll.set_child(self._pdf_canvas)
        self._pdf_scroll.set_vexpand(True)
        self._pdf_scroll.set_hexpand(True)
        # Which pages need pixels is purely a function of where the scroll
        # position is, so it is recomputed from the adjustment rather than
        # from scroll events -- that covers the scrollbar, keyboard, and
        # touchpad kinetics identically, and needs no event interception.
        self._pdf_scroll.get_vadjustment().connect("value-changed", self._on_pdf_scrolled)

        self._pdf_box.append(self._pdf_toolbar)
        self._pdf_box.append(self._pdf_scroll)

        self._pdf_path: str | None = None
        self._pdf_current_page: int = 1
        self._pdf_total_pages: int = 1
        self._pdf_zoom_pct: int = 100
        self._pdf_mtime: int = 0
        # First page's size in PostScript points, read from pdfinfo along with
        # the page count. Width drives the render resolution (_pdf_target_dpi)
        # and the pair gives every page its pre-render aspect ratio.
        # Every page's size and rotation, index 0 == page 1. Drives both the
        # layout and the coordinate maths for text selection.
        self._pdf_geometry: list[_PdfPageGeometry] = []
        # Display size and document y-offset per page, recomputed on zoom or
        # resize (see _recompute_pdf_layout).
        self._pdf_page_sizes: list[tuple[int, int]] = []
        self._pdf_page_offsets: list[float] = []
        self._pdf_content_height: float = 0.0
        # Word boxes per page, and the selection as (page index, word index)
        # pairs -- ordered, so a selection can span pages.
        self._pdf_words: dict[int, list[_PdfWord]] = {}
        # Per-page line bands derived from those words, for hit testing.
        self._pdf_lines: dict[int, list[tuple[float, float, float, float, int, int]]] = {}
        self._pdf_words_pending: set[int] = set()
        self._pdf_sel_anchor: tuple[int, int] | None = None
        self._pdf_sel_focus: tuple[int, int] | None = None
        # Whether the current drag has actually moved between words, which is
        # what separates a selection from a plain click (see _on_pdf_drag_end).
        self._pdf_drag_moved: bool = False
        # Built once; swapping cursors is per-motion-event, so this must not
        # allocate a new one each time.
        self._pdf_caret_cursor = Gdk.Cursor.new_from_name("text", None)
        self._pdf_text_cursor_shown: bool = False
        # Raw (unscaled) pixbuf currently backing each loaded page, and the
        # DPI it was rendered at. Pages away from the viewport are dropped
        # from both and their picture blanked, so a long document costs
        # widgets but not bitmaps.
        self._pdf_page_pixbufs: dict[int, GdkPixbuf.Pixbuf] = {}
        self._pdf_page_dpi: dict[int, int] = {}
        # (page, dpi) renders already in flight, so a scroll that re-enters
        # the same range doesn't queue the same work twice.
        self._pdf_pending_renders: set[tuple[int, int]] = set()
        self._pdf_render_futures: dict[tuple[int, int], concurrent.futures.Future] = {}
        self._pdf_word_futures: dict[int, concurrent.futures.Future] = {}
        # Previously rendered pages, keyed (page, dpi) -- see
        # _cache_pdf_page for the budget. Insertion-ordered so the
        # oldest entry is the one evicted; hits are re-inserted to keep it
        # ordered by last use rather than first render.
        self._pdf_page_cache: dict[tuple[int, int], GdkPixbuf.Pixbuf] = {}
        self._pdf_display_base_width: int = 0
        self._pdf_viewport_refit_id: int = 0
        self._pdf_viewport_tick_id: int = 0
        self._pdf_last_viewport_width: int = 0
        # Bumped for each new file. Worker threads capture it and drop their
        # result if it no longer matches, so a render for the previous
        # document can never paint over the current one.
        self._pdf_generation: int = 0
        self._pdf_visible_debounce_id: int = 0
        # See _arm_pdf_quality_upgrade/_cancel_pdf_quality_upgrade.
        self._pdf_quality_debounce_id: int = 0

        # EPUB reader. All of this stays None until an EPUB is actually
        # previewed (see _ensure_epub_view) -- building a WebKit.WebView per
        # preview column, when a column is rebuilt on every file click, would
        # spawn a web process for every text file and image the user selects.
        self._epub_view = None
        self._epub_box = None
        self._epub_tmpdir: str | None = None
        # file: URI of the single concatenated document every chapter lives
        # in (see _build_epub_document) -- chapter navigation is a fragment
        # jump within this, never a fresh load.
        self._epub_doc_uri: str | None = None
        self._epub_chapters: list[str] = []
        self._epub_index: int = 0
        self._epub_zoom_pct: int = 100
        self._epub_generation: int = 0

        self._preview_stack.add_named(loading, PREVIEW_SLOT_LOADING)
        self._preview_stack.add_named(self._icon, PREVIEW_SLOT_ICON)
        self._preview_stack.add_named(self._image_box, PREVIEW_SLOT_IMAGE)
        self._preview_stack.add_named(self._video_box, PREVIEW_SLOT_VIDEO)
        self._preview_stack.add_named(self._text_scroll, PREVIEW_SLOT_DOCUMENT)
        self._preview_stack.add_named(self._pdf_box, PREVIEW_SLOT_PDF)
        self._preview_stack.add_named(self._archive_box, PREVIEW_SLOT_ARCHIVE)
        self.set_preview_slot(PREVIEW_SLOT_IMAGE)

        self._thumb_revealer = Gtk.Revealer()
        self._thumb_revealer.set_halign(Gtk.Align.FILL)
        self._thumb_revealer.set_valign(Gtk.Align.FILL)
        self._thumb_revealer.set_hexpand(True)
        self._thumb_revealer.set_vexpand(True)
        self._thumb_revealer.set_transition_type(Gtk.RevealerTransitionType.CROSSFADE)
        self._thumb_revealer.set_transition_duration(100)
        self._thumb_revealer.set_reveal_child(True)
        self._thumb_revealer.set_child(self._preview_stack)
        preview_area.append(self._thumb_revealer)

        details_area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        details_area.set_halign(Gtk.Align.FILL)
        details_area.set_valign(Gtk.Align.END)
        details_area.set_vexpand(False)
        self.append(details_area)

        if len(self.file_uris) > 1:
            count = len(self.file_uris)
            title_text = _n("{n} item selected", "{n} items selected", count).format(n=count)
        else:
            gfile = Gio.File.new_for_uri(self.file_uris[0])
            title_text = gfile.get_basename() or self.file_uris[0]

        self._name_lbl = Gtk.Label(label=title_text)
        self._name_lbl.set_justify(Gtk.Justification.CENTER)
        self._name_lbl.set_wrap(True)
        self._name_lbl.set_max_width_chars(20)
        self._name_lbl.set_halign(Gtk.Align.FILL)
        self._name_lbl.set_hexpand(True)
        self._name_lbl.get_style_context().add_class("heading")
        details_area.append(self._name_lbl)

        self._detail_lbl = Gtk.Label(label="")
        self._detail_lbl.set_halign(Gtk.Align.FILL)
        self._detail_lbl.set_hexpand(True)
        self._detail_lbl.get_style_context().add_class("dim-label")
        self._detail_lbl.get_style_context().add_class("caption")
        details_area.append(self._detail_lbl)

        # Shared by every detail row so the titles agree on one width and the
        # values start at the same x -- see _make_kv_row.
        detail_titles = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)

        created_row, self._created_val = _make_kv_row(_native("Created"), detail_titles)
        details_area.append(created_row)

        modified_row, self._modified_val = _make_kv_row(_native("Modified"), detail_titles)
        details_area.append(modified_row)

        self._dim_row, self._dim_val = _make_kv_row(_("Dimensions"), detail_titles)
        details_area.append(self._dim_row)

        if len(self.file_uris) > 1:
            self._dim_row.set_visible(False)
            self._preview_stack.set_visible_child_name(PREVIEW_SLOT_ICON)
            self._load_multi(self.file_uris)
        else:
            gfile = Gio.File.new_for_uri(self.file_uris[0])
            guessed_type, _uncertain = Gio.content_type_guess(gfile.get_basename(), None)
            self._dim_row.set_visible(bool(guessed_type) and _is_media_content_type(guessed_type))
            self._load()

    def _load_multi(self, uris: list[str]) -> None:
        """Summarize a multi-item selection: how many files/folders, and the
        files' total size.

        One async query_info per item, never the sync variant: this runs on
        every selection change, the count is unbounded (Ctrl+A in a large
        folder), and on a gvfs mount each sync call would block the main loop
        for a full network round trip. Same async-only rule the folder
        columns follow (see MyComputerColumn)."""
        # Neither timestamp is meaningful for a set of items.
        self._created_val.get_parent().set_visible(False)
        self._modified_val.get_parent().set_visible(False)

        totals = {"dirs": 0, "files": 0, "size": 0, "pending": len(uris)}

        def on_info_ready(gfile: Gio.File, result: Gio.AsyncResult, _data=None) -> None:
            try:
                info = gfile.query_info_finish(result)
            except GLib.Error:
                info = None
            if info is not None:
                if info.get_file_type() == Gio.FileType.DIRECTORY:
                    totals["dirs"] += 1
                else:
                    totals["files"] += 1
                    totals["size"] += info.get_size()
            totals["pending"] -= 1
            if totals["pending"] == 0 and not self._cancellable.is_cancelled():
                self._apply_multi_summary(totals["dirs"], totals["files"], totals["size"])
            elif totals["pending"] > 0 and not self._cancellable.is_cancelled():
                start_one()

        queued = iter(uris)

        def start_one() -> None:
            uri = next(queued, None)
            if uri is None:
                return
            Gio.File.new_for_uri(uri).query_info_async(
                "standard::type,standard::size,standard::content-type",
                Gio.FileQueryInfoFlags.NONE,
                GLib.PRIORITY_DEFAULT,
                self._cancellable,
                on_info_ready,
            )

        for _index in range(min(_MULTI_INFO_CONCURRENCY, len(uris))):
            start_one()

    def _apply_multi_summary(self, n_dirs: int, n_files: int, total_size: int) -> None:
        # Each count carries its own plural form (a single ngettext string
        # cannot agree with two different numbers at once), and the pieces
        # are joined through their own translatable format so a translator
        # owns the separator and the order.
        files_text = _n("{n} file", "{n} files", n_files).format(n=n_files)
        folders_text = _n("{n} folder", "{n} folders", n_dirs).format(n=n_dirs)
        if n_dirs > 0 and n_files > 0:
            sub = _("{files}, {folders}").format(files=files_text, folders=folders_text)
        elif n_dirs > 0:
            sub = folders_text
        else:
            sub = files_text

        if total_size > 0:
            sub = _("{summary} ({size})").format(summary=sub, size=GLib.format_size(total_size))

        self._detail_lbl.set_label(sub)

    def _on_preview_area_pressed(
        self, gesture: Gtk.GestureClick, n_press: int, _x: float, _y: float
    ) -> None:
        modifiers = gesture.get_current_event_state() & Gtk.accelerator_get_default_mod_mask()
        selection_mode = bool(
            modifiers & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK)
        )
        single_click = self._ext._nautilus_prefs.click_policy == "single"
        self._activate_on_release = single_click and n_press == 1 and not selection_mode
        if not single_click and n_press == 2 and not selection_mode:
            _open_file_with_default_app(self.file_uri, self._cancellable)
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)

    def _on_preview_area_released(
        self, _gesture: Gtk.GestureClick, _n_press: int, _x: float, _y: float
    ) -> None:
        if self._activate_on_release:
            _open_file_with_default_app(self.file_uri, self._cancellable)
        self._activate_on_release = False

    def _on_preview_area_stopped(self, _gesture: Gtk.GestureClick) -> None:
        self._activate_on_release = False

    def _load(self) -> None:
        gfile = Gio.File.new_for_uri(self.file_uri)
        gfile.query_info_async(
            "standard::display-name,standard::icon,standard::content-type,standard::size,"
            "time::modified,time::created",
            Gio.FileQueryInfoFlags.NONE,
            GLib.PRIORITY_DEFAULT,
            self._cancellable,
            self._on_info_ready,
        )

    def _on_info_ready(self, gfile: Gio.File, result: Gio.AsyncResult) -> None:
        try:
            info = gfile.query_info_finish(result)
        except GLib.Error:
            self._show_icon()
            return
        display_name = info.get_display_name()
        if display_name:
            self._name_lbl.set_label(display_name)
        # has_attribute-guarded -- see the matching code in _entries_from_infos.
        content_type = (
            info.get_content_type() if info.has_attribute("standard::content-type") else None
        )
        size = info.get_size()
        parts = []
        if content_type:
            parts.append(Gio.content_type_get_description(content_type) or content_type)
        parts.append(_format_size(size))
        self._detail_lbl.set_label(" · ".join(parts))
        mtime = info.get_attribute_uint64("time::modified")
        self._created_val.set_label(_format_datetime(info.get_attribute_uint64("time::created")))
        self._modified_val.set_label(_format_datetime(mtime))
        gio_icon = info.get_icon()
        if _gicon_renders(gio_icon):
            self._icon.set_from_gicon(gio_icon)
        basename = gfile.get_basename() or ""
        is_text = _is_text_preview_file(content_type, basename)
        is_pdf = bool(content_type == "application/pdf" or basename.lower().endswith(".pdf"))
        is_epub = bool(content_type == "application/epub+zip" or basename.lower().endswith(".epub"))
        is_spreadsheet = _is_spreadsheet_file(content_type, basename)
        is_document = _is_document_file(content_type, basename)
        is_archive = _is_archive_file(content_type, basename)
        is_audio = _is_audio_file(content_type, basename)
        is_video = bool(content_type and content_type.startswith("video/"))
        needs_local_preview = bool(
            gfile.get_path() is None
            and (
                (content_type and content_type.startswith("image/"))
                or is_pdf
                or is_epub
                or is_spreadsheet
                or is_document
                or is_archive
                or is_video
            )
        )
        if needs_local_preview:
            self._stage_remote_preview(gfile, content_type, size, mtime)
        elif content_type and content_type.startswith("image/"):
            # The image page is already visible and fills in as soon as its
            # decoded paintable arrives, with no interim icon or spinner.
            self._load_preview_image(gfile)
        elif is_audio:
            self._load_preview_audio(gfile)
        elif is_video:
            self._load_preview_video(gfile)
        elif is_pdf:
            self._load_preview_pdf(gfile, mtime)
        elif is_epub:
            # Checked before the text branch: an EPUB is a zip, but some
            # systems report it with a generic type that the extension list
            # below would otherwise claim as plain text.
            self._load_preview_epub(gfile, mtime)
        elif is_spreadsheet:
            self._load_preview_spreadsheet(gfile, content_type, size=size, mtime=mtime)
        elif is_document:
            self._load_preview_document(gfile, content_type, size=size, mtime=mtime)
        elif is_archive:
            self._load_preview_archive(gfile, content_type, mtime=mtime)
        elif is_text:
            self._load_preview_text(gfile)
        else:
            self.set_preview_slot(PREVIEW_SLOT_ICON)
            self._maybe_load_thumbnail(content_type, mtime)
        self._maybe_load_dimensions(content_type)

    def _stage_remote_preview(
        self,
        source: Gio.File,
        content_type: str | None,
        size: int,
        mtime: int,
    ) -> None:
        """Copy a remote path-only preview to a cancellable temporary file."""
        basename = source.get_basename() or "preview"
        if _is_spreadsheet_file(content_type, basename):
            size_limit = _SPREADSHEET_PREVIEW_MAX_BYTES
        elif _is_document_file(content_type, basename):
            size_limit = _DOCUMENT_PREVIEW_MAX_BYTES
        else:
            size_limit = _REMOTE_PREVIEW_STAGE_MAX_BYTES
        if size > size_limit:
            self.set_preview_slot(PREVIEW_SLOT_ICON)
            return

        archive_suffix = next(
            (
                extension
                for extension in _ARCHIVE_EXTENSIONS
                if basename.casefold().endswith(extension)
            ),
            None,
        )
        suffix = (archive_suffix or os.path.splitext(basename)[1])[:32]
        fd, path = tempfile.mkstemp(prefix="nautilus-mc-preview-", suffix=suffix)
        os.close(fd)
        self._cleanup_staged_preview()
        self._staged_preview_path = path
        destination = Gio.File.new_for_path(path)
        self.set_preview_slot(PREVIEW_SLOT_LOADING)

        def on_copied(gfile: Gio.File, result: Gio.AsyncResult) -> None:
            try:
                gfile.copy_finish(result)
            except GLib.Error as error:
                if not self._cancellable.is_cancelled():
                    _log(f"Could not stage remote preview {gfile.get_uri()!r}: {error.message}")
                    self._show_icon()
                self._cleanup_staged_preview(path)
                return
            if self._cancellable.is_cancelled() or self._staged_preview_path != path:
                self._cleanup_staged_preview(path)
                return
            local = Gio.File.new_for_path(path)
            if content_type and content_type.startswith("image/"):
                self._load_preview_image(local)
                self._maybe_load_dimensions(content_type, local)
            elif content_type and content_type.startswith("video/"):
                self._load_preview_video(local)
                self._maybe_load_dimensions(content_type, local)
            elif content_type == "application/epub+zip" or basename.lower().endswith(".epub"):
                self._load_preview_epub(local, mtime)
            elif _is_spreadsheet_file(content_type, basename):
                self._load_preview_spreadsheet(local, content_type, size=size, mtime=mtime)
            elif _is_document_file(content_type, basename):
                self._load_preview_document(local, content_type, size=size, mtime=mtime)
            elif _is_archive_file(content_type, basename):
                self._load_preview_archive(local, content_type, mtime=mtime)
            else:
                self._load_preview_pdf(local, mtime)

        try:
            source.copy_async(
                destination,
                Gio.FileCopyFlags.OVERWRITE,
                GLib.PRIORITY_DEFAULT,
                self._cancellable,
                None,
                on_copied,
            )
        except GLib.Error as error:
            _log(f"Could not start remote preview staging for {source.get_uri()!r}: {error}")
            self._cleanup_staged_preview(path)
            self._show_icon()

    def _cleanup_staged_preview(self, expected_path: str | None = None) -> None:
        path = self._staged_preview_path
        if path is None or (expected_path is not None and path != expected_path):
            return
        self._staged_preview_path = None
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError as error:
            _log(f"Could not remove staged preview {path!r}: {error}")

    def _load_preview_image(self, gfile: Gio.File | None = None) -> None:
        """Decode a real local image on a worker thread for the large preview.

        The thumbnail factory remains the correct source for list rows and
        non-image previews. A preview can justify decoding the source file,
        but the decode limit avoids allocating a full-resolution texture for
        multi-megapixel photos in a 400px column.
        """
        path = (gfile or Gio.File.new_for_uri(self.file_uri)).get_path()
        if path is None:
            # Non-local image (no path to decode from, e.g. a GVfs/network
            # location) -- use its file icon as the preview fallback.
            self._show_icon()
            return
        self._worker_futures.append(
            _PREVIEW_WORKER_EXECUTOR.submit(self._preview_image_worker, path)
        )
        if _image_ocr_available():
            self._worker_futures.append(
                _PREVIEW_WORKER_EXECUTOR.submit(self._image_ocr_worker, path)
            )

    def _preview_image_worker(self, path: str) -> None:
        if self._cancellable.is_cancelled():
            return
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                path,
                _COLUMN_PREVIEW_IMAGE_SIZE,
                _COLUMN_PREVIEW_IMAGE_SIZE,
                True,
            )
        except GLib.Error:
            # Decode failures fall back from loading to the file icon.
            GLib.idle_add(self._show_icon)
            return
        if self._cancellable.is_cancelled():
            return
        GLib.idle_add(self._apply_thumbnail, Gdk.Texture.new_for_pixbuf(pixbuf))

    def _image_ocr_worker(self, path: str) -> None:
        result = _ocr_image_words(path, self._cancellable)
        if self._cancellable.is_cancelled():
            return
        GLib.idle_add(self._on_image_ocr_ready, result)

    def _on_image_ocr_ready(self, result: _ImageOcrResult | None) -> int:
        if self._cancellable.is_cancelled():
            return GLib.SOURCE_REMOVE
        self._image_ocr_words = list(result.words) if result is not None else []
        self._image_ocr_sections = list(result.sections) if result is not None else []
        self._image_ocr_width = result.width if result is not None else 0
        self._image_ocr_height = result.height if result is not None else 0
        self._image_ocr_lines = self._pdf_build_lines(self._image_ocr_words)
        has_text = bool(self._image_ocr_words)
        self._image_text_layer.set_can_target(has_text)
        self._image_text_layer.set_tooltip_text(
            _("Drag over detected text to select it") if has_text else None
        )
        self._image_text_layer.queue_draw()
        return GLib.SOURCE_REMOVE

    def _image_word_at(self, x: float, y: float, *, exact: bool = False) -> int | None:
        if not self._image_ocr_words or self._image_ocr_width <= 0 or self._image_ocr_height <= 0:
            return None
        source_x = x * self._image_ocr_width / max(1, self._image_text_layer.get_width())
        source_y = y * self._image_ocr_height / max(1, self._image_text_layer.get_height())
        if exact:
            for y0, y1, x0, x1, first, last in self._image_ocr_lines:
                if (
                    y0 - _PDF_LINE_HIT_SLACK <= source_y <= y1 + _PDF_LINE_HIT_SLACK
                    and x0 <= source_x <= x1
                ):
                    return min(
                        range(first, last + 1),
                        key=lambda position: (
                            0.0
                            if self._image_ocr_words[position].x0
                            <= source_x
                            <= self._image_ocr_words[position].x1
                            else min(
                                abs(source_x - self._image_ocr_words[position].x0),
                                abs(source_x - self._image_ocr_words[position].x1),
                            )
                        ),
                    )
            return None

        best = None
        best_distance = None
        for position, word in enumerate(self._image_ocr_words):
            if word.x0 <= source_x <= word.x1 and word.y0 <= source_y <= word.y1:
                return position
            dy = (
                0.0
                if word.y0 <= source_y <= word.y1
                else min(abs(source_y - word.y0), abs(source_y - word.y1))
            )
            dx = (
                0.0
                if word.x0 <= source_x <= word.x1
                else min(abs(source_x - word.x0), abs(source_x - word.x1))
            )
            distance = dy * 4.0 + dx
            if best_distance is None or distance < best_distance:
                best_distance = distance
                best = position
        return best

    def _on_image_motion(self, _controller: Gtk.EventControllerMotion, x: float, y: float) -> None:
        position = self._image_word_at(x, y, exact=True)
        over_text = position is not None
        section = (
            _ocr_section_name(self._image_ocr_words[position].section_label)
            if position is not None
            else None
        )
        if section != self._image_tooltip_section:
            self._image_tooltip_section = section
            help_text = _("Drag over detected text to select it")
            self._image_text_layer.set_tooltip_text(
                f"{section} — {help_text}" if section else help_text
            )
        if over_text == self._image_text_cursor_shown:
            return
        self._image_text_cursor_shown = over_text
        self._image_text_layer.set_cursor(self._image_caret_cursor if over_text else None)

    def _on_image_motion_leave(self, _controller: Gtk.EventControllerMotion) -> None:
        self._image_text_cursor_shown = False
        self._image_tooltip_section = None
        self._image_text_layer.set_cursor(None)
        self._image_text_layer.set_tooltip_text(
            _("Drag over detected text to select it") if self._image_ocr_words else None
        )

    def _set_image_zoom(self, zoom_pct: int) -> None:
        zoom_pct = max(50, min(300, zoom_pct))
        self._image_zoom_pct = zoom_pct
        self._image_zoom_label.set_label(f"{zoom_pct}%")
        self._image_zoom_out.set_sensitive(zoom_pct > 50)
        self._image_zoom_in.set_sensitive(zoom_pct < 300)
        if zoom_pct == 100:
            self._thumb_clamp.set_size_request(-1, -1)
            self._thumb_clamp.set_hexpand(True)
            self._thumb_clamp.set_vexpand(True)
        else:
            viewport_width = max(
                1,
                self._image_scroller.get_width()
                or self._thumb_frame.get_width()
                or _COLUMN_PREVIEW_IMAGE_MAX_WIDTH,
            )
            viewport_height = max(
                1,
                self._image_scroller.get_height()
                or self._thumb_frame.get_height()
                or round(viewport_width / max(0.01, self._image_aspect_ratio)),
            )
            base_width = min(viewport_width, round(viewport_height * self._image_aspect_ratio))
            width = max(1, round(base_width * zoom_pct / 100))
            height = max(1, round(width / max(0.01, self._image_aspect_ratio)))
            self._thumb_clamp.set_hexpand(False)
            self._thumb_clamp.set_vexpand(False)
            self._thumb_clamp.set_size_request(width, height)

    def _change_image_zoom(self, delta: int) -> None:
        self._set_image_zoom(self._image_zoom_pct + delta)

    def _on_image_scroll(
        self, controller: Gtk.EventControllerScroll, _dx: float, dy: float
    ) -> bool:
        event = controller.get_current_event()
        state = event.get_modifier_state() if event is not None else 0
        if not (state & Gdk.ModifierType.CONTROL_MASK) or dy == 0:
            return Gdk.EVENT_PROPAGATE
        self._change_image_zoom(-25 if dy > 0 else 25)
        return Gdk.EVENT_STOP

    def _on_image_drag_begin(self, gesture: Gtk.GestureDrag, x: float, y: float) -> None:
        self._image_text_layer.grab_focus()
        position = self._image_word_at(x, y, exact=True)
        if position is None:
            self._clear_image_selection()
            gesture.set_state(Gtk.EventSequenceState.DENIED)
            return
        self._image_sel_anchor = position
        self._image_sel_focus = position
        self._image_drag_moved = False
        self._image_text_layer.queue_draw()

    def _on_image_drag_update(
        self, gesture: Gtk.GestureDrag, offset_x: float, offset_y: float
    ) -> None:
        if self._image_sel_anchor is None:
            return
        ok, start_x, start_y = gesture.get_start_point()
        if not ok:
            return
        position = self._image_word_at(start_x + offset_x, start_y + offset_y)
        if position is not None and position != self._image_sel_focus:
            self._image_sel_focus = position
            self._image_drag_moved = True
            self._image_text_layer.queue_draw()

    def _on_image_drag_end(
        self, _gesture: Gtk.GestureDrag, _offset_x: float, _offset_y: float
    ) -> None:
        if self._image_sel_anchor is not None and not self._image_drag_moved:
            self._clear_image_selection()

    def _on_image_click_pressed(
        self, gesture: Gtk.GestureClick, n_press: int, _x: float, _y: float
    ) -> None:
        if n_press in (2, 3):
            # A preview is a reading surface: multi-click selects OCR text
            # and must never bubble out as a request to open the image.
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)

    def _on_image_click_released(
        self, gesture: Gtk.GestureClick, n_press: int, x: float, y: float
    ) -> None:
        if n_press not in (2, 3):
            return
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        # GestureDrag sees the same release and may finish after GestureClick.
        # Apply on idle so its single-click cleanup cannot erase the word or
        # line selection we are committing here.
        GLib.idle_add(self._apply_image_multi_click_selection, n_press, x, y)

    def _apply_image_multi_click_selection(self, n_press: int, x: float, y: float) -> int:
        if self._cancellable.is_cancelled():
            return GLib.SOURCE_REMOVE
        position = self._image_word_at(x, y, exact=True)
        if position is None:
            return GLib.SOURCE_REMOVE
        self._image_text_layer.grab_focus()
        if n_press == 3:
            bounds = self._image_line_bounds(position)
            self._image_sel_anchor, self._image_sel_focus = bounds
        else:
            self._image_sel_anchor = position
            self._image_sel_focus = position
        self._image_text_layer.queue_draw()
        return GLib.SOURCE_REMOVE

    def _image_line_bounds(self, position: int) -> tuple[int, int]:
        for _y0, _y1, _x0, _x1, first, last in self._image_ocr_lines:
            if first <= position <= last:
                return first, last
        return position, position

    def _image_vertical_target(self, position: int, direction: int) -> int:
        """Move to the visually nearest word on the adjacent OCR line."""
        line_index = next(
            (
                index
                for index, (*_geometry, first, last) in enumerate(self._image_ocr_lines)
                if first <= position <= last
            ),
            None,
        )
        if line_index is None:
            return position
        target_index = max(
            0,
            min(len(self._image_ocr_lines) - 1, line_index + direction),
        )
        if target_index == line_index:
            return position
        *_geometry, first, last = self._image_ocr_lines[target_index]
        current = self._image_ocr_words[position]
        current_x = (current.x0 + current.x1) / 2
        return min(
            range(first, last + 1),
            key=lambda candidate: abs(
                (self._image_ocr_words[candidate].x0 + self._image_ocr_words[candidate].x1) / 2
                - current_x
            ),
        )

    def _on_image_key_pressed(
        self,
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        state: Gdk.ModifierType,
    ) -> bool:
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        if ctrl and keyval in (Gdk.KEY_a, Gdk.KEY_A) and self._image_ocr_words:
            self._image_sel_anchor = 0
            self._image_sel_focus = len(self._image_ocr_words) - 1
            self._image_text_layer.queue_draw()
            return True
        if keyval == Gdk.KEY_Escape and self._image_selection_bounds() is not None:
            self._clear_image_selection()
            return True
        arrow_keys = (
            Gdk.KEY_Left,
            Gdk.KEY_KP_Left,
            Gdk.KEY_Right,
            Gdk.KEY_KP_Right,
            Gdk.KEY_Up,
            Gdk.KEY_KP_Up,
            Gdk.KEY_Down,
            Gdk.KEY_KP_Down,
        )
        if keyval in arrow_keys:
            if not self._image_ocr_words:
                return False
            backward = keyval in (
                Gdk.KEY_Left,
                Gdk.KEY_KP_Left,
                Gdk.KEY_Up,
                Gdk.KEY_KP_Up,
            )
            direction = -1 if backward else 1
            current = self._image_sel_focus
            if current is None:
                current = 0 if direction > 0 else len(self._image_ocr_words) - 1
            if keyval in (
                Gdk.KEY_Up,
                Gdk.KEY_KP_Up,
                Gdk.KEY_Down,
                Gdk.KEY_KP_Down,
            ):
                target = self._image_vertical_target(current, direction)
            else:
                target = max(
                    0,
                    min(len(self._image_ocr_words) - 1, current + direction),
                )
            if shift:
                if self._image_sel_anchor is None:
                    self._image_sel_anchor = current
                self._image_sel_focus = target
            else:
                self._image_sel_anchor = target
                self._image_sel_focus = target
            self._image_text_layer.queue_draw()
            return True
        if keyval in (Gdk.KEY_Menu, Gdk.KEY_F10) and (keyval == Gdk.KEY_Menu or shift):
            return self.show_text_context_menu()
        return False

    def _clear_image_selection(self) -> None:
        had_selection = self._image_sel_anchor is not None
        self._image_sel_anchor = None
        self._image_sel_focus = None
        if had_selection:
            self._image_text_layer.queue_draw()

    def _image_selection_bounds(self) -> tuple[int, int] | None:
        if self._image_sel_anchor is None or self._image_sel_focus is None:
            return None
        return tuple(sorted((self._image_sel_anchor, self._image_sel_focus)))

    def _draw_image_ocr_selection(
        self, _area: Gtk.DrawingArea, cr, width: int, height: int
    ) -> None:
        bounds = self._image_selection_bounds()
        if bounds is None or self._image_ocr_width <= 0 or self._image_ocr_height <= 0:
            return
        scale_x = width / self._image_ocr_width
        scale_y = height / self._image_ocr_height
        cr.set_source_rgba(0.21, 0.52, 0.89, 0.35)
        first, last = bounds
        for word in self._image_ocr_words[first : last + 1]:
            cr.rectangle(
                word.x0 * scale_x,
                word.y0 * scale_y,
                (word.x1 - word.x0) * scale_x,
                (word.y1 - word.y0) * scale_y,
            )
        cr.fill()

    def _image_selected_text(self) -> str:
        bounds = self._image_selection_bounds()
        if bounds is None:
            return ""
        first, last = bounds
        pieces: list[str] = []
        line = None
        for word in self._image_ocr_words[first : last + 1]:
            if line is not None and word.line != line:
                pieces.append("\n")
            elif pieces and not pieces[-1].endswith("\n"):
                pieces.append(" ")
            pieces.append(word.text)
            line = word.line
        return "".join(pieces).strip()

    def _image_section_text(self, position: int) -> str:
        word = self._image_ocr_words[position]
        if word.section is None:
            return ""
        section_words = [
            candidate for candidate in self._image_ocr_words if candidate.section == word.section
        ]
        pieces: list[str] = []
        line = None
        for candidate in section_words:
            if line is not None and candidate.line != line:
                pieces.append("\n")
            elif pieces and not pieces[-1].endswith("\n"):
                pieces.append(" ")
            pieces.append(candidate.text)
            line = candidate.line
        return "".join(pieces).strip()

    def _on_image_context_pressed(
        self, gesture: Gtk.GestureClick, _n_press: int, x: float, y: float
    ) -> None:
        bounds = self._image_selection_bounds()
        word = self._image_word_at(x, y, exact=True)
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        selected_word = (
            word
            if bounds is not None and word is not None and bounds[0] <= word <= bounds[1]
            else None
        )
        self._show_image_text_context(selected_word, x, y)

    def _show_image_text_context(self, word: int | None, x: float, y: float) -> None:
        self._image_text_layer.grab_focus()
        popover = Gtk.Popover()
        popover.set_has_arrow(True)
        popover.set_parent(self._image_text_layer)
        menu = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        if word is not None:
            button = Gtk.Button(label=_native("Copy"))
            button.add_css_class("flat")

            def copy_and_close(_button) -> None:
                self.copy_text_selection()
                popover.popdown()

            button.connect("clicked", copy_and_close)
            menu.append(button)

        def unparent_after_unmap(widget) -> None:
            def cleanup() -> bool:
                if widget.get_parent() is not None:
                    widget.unparent()
                return GLib.SOURCE_REMOVE

            GLib.idle_add(cleanup)

        section_text = self._image_section_text(word) if word is not None else ""
        if section_text:
            section_name = _ocr_section_name(self._image_ocr_words[word].section_label)
            section_button = Gtk.Button(
                label=_("Copy section") + (f" — {section_name}" if section_name else "")
            )
            section_button.add_css_class("flat")

            def copy_section_and_close(_button) -> None:
                self.get_clipboard().set_content(Gdk.ContentProvider.new_for_value(section_text))
                popover.popdown()

            section_button.connect("clicked", copy_section_and_close)
            menu.append(section_button)
        if self.file_uri and self.file_uri.startswith("file://"):
            background_button = Gtk.Button(label=_native("Set as Background…"))
            background_button.add_css_class("flat")

            def set_background_and_close(_button) -> None:
                self._set_preview_image_as_background()
                popover.popdown()

            background_button.connect("clicked", set_background_and_close)
            menu.append(background_button)
        popover.set_child(menu)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        popover.set_pointing_to(rect)
        popover.connect("unmap", unparent_after_unmap)
        popover.popup()

    def _set_preview_image_as_background(self) -> None:
        if not self.file_uri or not self.file_uri.startswith("file://"):
            return
        try:
            settings = Gio.Settings.new("org.gnome.desktop.background")
            settings.set_string("picture-uri", self.file_uri)
            if "picture-uri-dark" in settings.list_keys():
                settings.set_string("picture-uri-dark", self.file_uri)
            Gio.Settings.sync()
        except GLib.Error as error:
            if callable(self._on_open_error):
                self._on_open_error(error.message)

    def show_text_context_menu(self) -> bool:
        """Open the keyboard context menu for the active image OCR selection."""
        if (
            not hasattr(self, "_preview_stack")
            or self._preview_stack.get_visible_child_name() != PREVIEW_SLOT_IMAGE
        ):
            return False
        bounds = self._image_selection_bounds()
        word = bounds[1] if bounds is not None else None
        if word is not None:
            selected_word = self._image_ocr_words[word]
            x = (selected_word.x0 + selected_word.x1) / 2
            y = (selected_word.y0 + selected_word.y1) / 2
            x *= self._image_text_layer.get_width() / max(1, self._image_ocr_width)
            y *= self._image_text_layer.get_height() / max(1, self._image_ocr_height)
        else:
            x = max(1.0, self._image_text_layer.get_width() / 2)
            y = max(1.0, self._image_text_layer.get_height() / 2)
        self._show_image_text_context(word, x, y)
        return True

    def _show_icon(self) -> int:
        if not self._cancellable.is_cancelled():
            self.set_preview_slot(PREVIEW_SLOT_ICON)
        return GLib.SOURCE_REMOVE

    def _maybe_load_dimensions(
        self, content_type: str | None, gfile: Gio.File | None = None
    ) -> None:
        """Populate the Dimensions row for images and videos. Images read the
        header only with GdkPixbuf. Video metadata is read by an external
        ffprobe worker, so malformed codecs and native multimedia libraries
        are never loaded into Nautilus merely by selecting a file.

        The row's visibility was already set from a fast filename guess in
        __init__ (to avoid a layout jump -- see that comment); this is the
        reconciliation against the real, confirmed content-type, and the only
        place allowed to hide a row that guess had shown. Everything below
        this point (the async calls and their callbacks) only ever fills in
        the label text of an already-decided-visible row."""
        is_media = bool(content_type) and _is_media_content_type(content_type)
        if not is_media:
            self._dim_row.set_visible(False)
            return
        if content_type.startswith("image/"):
            path = (gfile or Gio.File.new_for_uri(self.file_uri)).get_path()
            if path is None:
                self._dim_row.set_visible(False)
                return
            self._dim_row.set_visible(True)
            GdkPixbuf.Pixbuf.get_file_info_async(path, self._cancellable, self._on_image_info_ready)
        else:
            path = (gfile or Gio.File.new_for_uri(self.file_uri)).get_path()
            if path is None or shutil.which("ffprobe") is None:
                self._dim_row.set_visible(False)
                return
            self._dim_row.set_visible(True)

            def worker() -> None:
                dimensions = _probe_video_dimensions(path, self._cancellable)
                if dimensions is not None and not self._cancellable.is_cancelled():
                    GLib.idle_add(self._apply_video_dimensions, *dimensions)

            self._worker_futures.append(_PREVIEW_WORKER_EXECUTOR.submit(worker))

    def _on_image_info_ready(self, _source, result: Gio.AsyncResult) -> None:
        try:
            _fmt, width, height = GdkPixbuf.Pixbuf.get_file_info_finish(result)
        except GLib.Error:
            return
        if width and height:
            self._dim_val.set_label(f"{width}x{height}")

    def _apply_video_dimensions(self, width: int, height: int) -> int:
        if not self._cancellable.is_cancelled():
            self._dim_val.set_label(f"{width}x{height}")
        return GLib.SOURCE_REMOVE

    def _load_preview_video(self, gfile: Gio.File) -> None:
        """Load native HTML video controls in WebKit's isolated web process."""
        path = gfile.get_path()
        if path is None or WebKit is None:
            self.set_preview_slot(PREVIEW_SLOT_ICON)
            return
        self._stop_video()
        self._ensure_video_web_view()
        if self._video_web_view is None:
            self.set_preview_slot(PREVIEW_SLOT_ICON)
            return
        self._media_visual_stack.set_visible_child_name("video-web")
        # Video uses the browser's controls. The GTK controls remain for the
        # mpv-backed audio helper, whose JSON IPC drives those widgets.
        self._video_controls.set_visible(False)
        self.set_preview_slot(PREVIEW_SLOT_LOADING)
        self._start_video_stream_helper(path)

    def _start_video_stream_helper(self, path: str) -> None:
        python = shutil.which("python3")
        helper = os.path.join(os.path.dirname(__file__), "video_stream_helper.py")
        if python is None or not os.path.isfile(helper):
            _log("Video stream helper is unavailable")
            self.set_preview_slot(PREVIEW_SLOT_ICON)
            return
        flags = (
            Gio.SubprocessFlags.STDIN_PIPE
            | Gio.SubprocessFlags.STDOUT_PIPE
            | Gio.SubprocessFlags.STDERR_SILENCE
        )
        try:
            process = Gio.Subprocess.new([python, helper, path], flags)
        except GLib.Error as error:
            _log(f"Could not start video stream helper: {error.message}")
            self.set_preview_slot(PREVIEW_SLOT_ICON)
            return

        self._video_stream_generation += 1
        generation = self._video_stream_generation
        self._video_stream_process = process
        self._video_stream_stdin = process.get_stdin_pipe()
        stream = Gio.DataInputStream.new(process.get_stdout_pipe())
        self._video_stream_stdout = stream
        stream.read_line_async(
            GLib.PRIORITY_DEFAULT,
            self._cancellable,
            self._on_video_stream_ready,
            (generation, process, stream),
        )
        process.wait_async(None, self._on_video_stream_exited, generation)

    def _on_video_stream_ready(self, _source, result: Gio.AsyncResult, state) -> None:
        generation, process, stream = state
        if (
            generation != self._video_stream_generation
            or process is not self._video_stream_process
            or stream is not self._video_stream_stdout
        ):
            return
        try:
            line, _length = stream.read_line_finish_utf8(result)
        except GLib.Error as error:
            if not self._cancellable.is_cancelled():
                _log(f"Video stream helper output failed: {error.message}")
            return
        if line is None:
            return
        try:
            message = json.loads(line)
            url = message.get("url") if message.get("event") == "ready" else None
        except (AttributeError, TypeError, json.JSONDecodeError):
            url = None
        origin = _loopback_video_origin(url) if isinstance(url, str) else None
        if origin is None:
            _log("Video stream helper returned an invalid loopback URL")
            self._stop_video_stream_helper()
            if not self._cancellable.is_cancelled():
                self.set_preview_slot(PREVIEW_SLOT_ICON)
            return

        self._video_stream_stdout = None
        stream.close_async(GLib.PRIORITY_DEFAULT, None, lambda *_args: None)
        if self._video_web_view is not None:
            self._video_web_view.load_html(_build_video_preview_html(url), origin + "/")

    def _on_video_stream_exited(
        self, process: Gio.Subprocess, result: Gio.AsyncResult, generation: int
    ) -> None:
        try:
            process.wait_finish(result)
        except GLib.Error:
            pass
        if generation != self._video_stream_generation or process is not self._video_stream_process:
            return
        self._video_stream_process = None
        self._video_stream_stdin = None
        self._video_stream_stdout = None
        if not self._cancellable.is_cancelled():
            _log("Video stream helper exited unexpectedly")
            self.set_preview_slot(PREVIEW_SLOT_ICON)

    def _ensure_video_web_view(self) -> None:
        if self._video_web_view is not None or WebKit is None:
            return
        view = WebKit.WebView()
        view.set_hexpand(True)
        view.set_vexpand(True)
        settings = view.get_settings()
        # The document is generated entirely by us and CSP blocks page-owned
        # scripts. JavaScript is enabled only so the surrounding GTK controls
        # can issue play/pause/seek commands and read playback state. WebKit's
        # native media controls can get stuck displaying a stop button in an
        # embedded view, so they are deliberately absent from the HTML.
        settings.set_enable_javascript(True)
        for setter, value in (
            ("set_enable_media", True),
            ("set_media_playback_allows_inline", True),
            # The command originates in a GTK button click, which WebKit does
            # not classify as a DOM user gesture. There is no autoplay in the
            # generated document, so disabling this gate still never starts a
            # video until the user presses our Play button.
            ("set_media_playback_requires_user_gesture", False),
            ("set_allow_file_access_from_file_urls", True),
            ("set_allow_universal_access_from_file_urls", False),
            ("set_enable_encrypted_media", False),
            ("set_enable_mediasource", False),
            ("set_enable_html5_database", False),
            ("set_enable_html5_local_storage", False),
            ("set_enable_developer_extras", False),
        ):
            try:
                getattr(settings, setter)(value)
            except (AttributeError, TypeError):
                pass
        view.connect("web-process-terminated", self._on_video_web_process_terminated)
        view.connect("load-changed", self._on_video_load_changed)
        self._media_visual_stack.add_named(view, "video-web")
        self._video_web_view = view

    def _on_video_load_changed(self, _view, load_event) -> None:
        if self._cancellable.is_cancelled() or self._video_stream_process is None:
            return
        if load_event == WebKit.LoadEvent.COMMITTED:
            self._media_visual_stack.set_visible_child_name("video-web")
            self._reset_video_controls()
            self._video_controls.set_visible(True)
            self.set_preview_slot(PREVIEW_SLOT_VIDEO)
        if load_event == WebKit.LoadEvent.FINISHED:
            self._start_video_state_poll()

    def _on_video_web_process_terminated(self, view, reason) -> None:
        if view is not self._video_web_view or self._cancellable.is_cancelled():
            return
        _log(
            "Video preview process terminated "
            f"({getattr(reason, 'value_nick', reason)}); decoder failure was isolated "
            "from Nautilus"
        )
        self._stop_video_stream_helper()
        self._stop_video_state_poll()
        self._video_controls.set_visible(False)
        self.set_preview_slot(PREVIEW_SLOT_ICON)

    def _reset_video_controls(self) -> None:
        self._video_playing = False
        self._video_muted = False
        self._video_volume = 1.0
        self._video_position = 0.0
        self._video_duration = 0.0
        self._btn_play.set_icon_name("media-playback-start-symbolic")
        self._btn_play.set_sensitive(True)
        self._btn_mute.set_icon_name("audio-volume-high-symbolic")
        self._btn_mute.set_sensitive(True)
        self._video_scale.set_range(0, 1)
        self._video_scale.set_value(0)
        self._video_scale.set_sensitive(False)
        self._video_time_lbl.set_label("0:00 / 0:00")
        self._setting_volume_scale = True
        self._volume_scale.set_value(100)
        self._setting_volume_scale = False
        self._volume_scale.set_sensitive(True)

    def _start_video_state_poll(self) -> None:
        self._stop_video_state_poll()
        self._video_poll_id = GLib.timeout_add(200, self._poll_video_state)

    def _stop_video_state_poll(self) -> None:
        if self._video_poll_id:
            GLib.source_remove(self._video_poll_id)
            self._video_poll_id = 0
        self._video_poll_inflight = False

    def _poll_video_state(self) -> int:
        view = self._video_web_view
        if self._cancellable.is_cancelled() or view is None or self._video_stream_process is None:
            self._video_poll_id = 0
            return GLib.SOURCE_REMOVE
        if self._video_poll_inflight:
            return GLib.SOURCE_CONTINUE
        self._video_poll_inflight = True
        script = (
            "JSON.stringify((v=>v?({playing:!v.paused&&!v.ended,"
            "muted:v.muted,volume:v.volume,position:v.currentTime||0,"
            "duration:Number.isFinite(v.duration)?v.duration:0,"
            "ready:v.readyState,error:v.error&&v.error.message}):null)"
            "(document.querySelector('video')))"
        )
        view.evaluate_javascript(
            script,
            -1,
            None,
            None,
            self._cancellable,
            self._on_video_state_ready,
            view,
        )
        return GLib.SOURCE_CONTINUE

    def _on_video_state_ready(self, view, result: Gio.AsyncResult, expected_view) -> None:
        self._video_poll_inflight = False
        if (
            view is not expected_view
            or view is not self._video_web_view
            or self._cancellable.is_cancelled()
        ):
            return
        try:
            value = view.evaluate_javascript_finish(result)
            message = json.loads(value.to_string())
        except (AttributeError, GLib.Error, TypeError, json.JSONDecodeError):
            return
        if not isinstance(message, dict):
            return
        if message.get("error"):
            _log(f"Video preview failed for {self.file_uri!r}: {message['error']}")
            self._stop_video_stream_helper()
            self._stop_video_state_poll()
            self.set_preview_slot(PREVIEW_SLOT_ICON)
            return
        self._video_playing = bool(message.get("playing"))
        self._video_muted = bool(message.get("muted"))
        self._video_volume = max(0.0, min(1.0, float(message.get("volume", 1.0))))
        self._video_position = max(0.0, float(message.get("position", 0.0)))
        self._video_duration = max(0.0, float(message.get("duration", 0.0)))
        self._btn_play.set_icon_name(
            "media-playback-pause-symbolic"
            if self._video_playing
            else "media-playback-start-symbolic"
        )
        self._setting_volume_scale = True
        self._volume_scale.set_value(self._video_volume * 100)
        self._setting_volume_scale = False
        self._update_video_volume_icon()
        if self._video_duration > 0:
            self._video_scale.set_range(0, self._video_duration)
            self._video_scale.set_sensitive(True)
            self._video_scale.set_value(min(self._video_position, self._video_duration))
        self._update_video_time_display()

    def _evaluate_video_command(self, script: str) -> bool:
        if self._video_web_view is None or self._video_stream_process is None:
            return False
        self._video_web_view.evaluate_javascript(
            script, -1, None, None, self._cancellable, None, None
        )
        return True

    def _load_preview_audio(self, gfile: Gio.File) -> None:
        """Initialize crash-isolated song playback and its media controls."""
        self._start_audio_helper(gfile)

    def _start_audio_helper(self, gfile: Gio.File) -> None:
        """Start audio decoding outside Nautilus so native aborts are contained."""
        self._stop_video()
        python = shutil.which("python3")
        helper = os.path.join(os.path.dirname(__file__), "media_player_helper.py")
        if python is None or not os.path.isfile(helper):
            _log("Audio preview helper is unavailable")
            self.set_preview_slot(PREVIEW_SLOT_ICON)
            return

        flags = (
            Gio.SubprocessFlags.STDIN_PIPE
            | Gio.SubprocessFlags.STDOUT_PIPE
            | Gio.SubprocessFlags.STDERR_SILENCE
        )
        try:
            process = Gio.Subprocess.new([python, helper, gfile.get_uri()], flags)
        except GLib.Error as error:
            _log(f"Could not start audio preview helper: {error.message}")
            self.set_preview_slot(PREVIEW_SLOT_ICON)
            return

        self._audio_generation += 1
        generation = self._audio_generation
        self._audio_process = process
        self._audio_stdin = process.get_stdin_pipe()
        self._audio_stdout = Gio.DataInputStream.new(process.get_stdout_pipe())
        self._audio_command_queue.clear()
        self._audio_write_pending = False
        self._audio_process_exited = False
        self._audio_exit_successful = False
        self._audio_stdout_eof = False
        self._audio_playing = False
        self._audio_muted = False
        self._audio_volume = 1.0
        self._audio_position = 0.0
        self._audio_duration = 0.0

        self._media_visual_stack.set_visible_child_name("audio")
        self._video_controls.set_visible(True)
        self._video_picture.set_paintable(None)
        self._video_scale.set_range(0, 1)
        self._video_scale.set_value(0)
        self._video_scale.set_sensitive(False)
        self._video_time_lbl.set_label("0:00 / 0:00")
        self._btn_play.set_icon_name("media-playback-start-symbolic")
        self._btn_play.set_sensitive(True)
        self._btn_mute.set_icon_name("audio-volume-high-symbolic")
        self._btn_mute.set_sensitive(True)
        self._setting_volume_scale = True
        self._volume_scale.set_value(100)
        self._setting_volume_scale = False
        self._volume_scale.set_sensitive(True)
        self.set_preview_slot(PREVIEW_SLOT_VIDEO)

        self._read_audio_helper_line(generation)
        process.wait_async(None, self._on_audio_helper_exited, generation)

    def _read_audio_helper_line(self, generation: int) -> None:
        stream = self._audio_stdout
        if stream is None or generation != self._audio_generation:
            return
        stream.read_line_async(
            GLib.PRIORITY_DEFAULT,
            self._cancellable,
            self._on_audio_helper_line_ready,
            (generation, stream),
        )

    def _on_audio_helper_line_ready(self, _source, result: Gio.AsyncResult, state) -> None:
        generation, stream = state
        if generation != self._audio_generation or stream is not self._audio_stdout:
            return
        try:
            line, _length = stream.read_line_finish_utf8(result)
        except GLib.Error as error:
            if not self._cancellable.is_cancelled():
                _log(f"Audio preview helper output failed: {error.message}")
            return
        if line is None:
            self._audio_stdout_eof = True
            if self._audio_process_exited:
                self._finalize_audio_helper_exit(generation)
            return
        try:
            message = json.loads(line)
        except (TypeError, json.JSONDecodeError) as error:
            _log(f"Audio preview helper sent invalid output: {error}")
        else:
            self._apply_audio_helper_message(message)
        self._read_audio_helper_line(generation)

    def _apply_audio_helper_message(self, message: dict) -> None:
        event = message.get("event")
        if event == "state":
            self._audio_playing = bool(message.get("playing"))
            self._audio_muted = bool(message.get("muted"))
            self._audio_volume = max(
                0.0, min(1.0, float(message.get("volume", self._audio_volume)))
            )
            self._btn_play.set_icon_name(
                "media-playback-pause-symbolic"
                if self._audio_playing
                else "media-playback-start-symbolic"
            )
            self._setting_volume_scale = True
            self._volume_scale.set_value(self._audio_volume * 100)
            self._setting_volume_scale = False
            self._update_media_volume_icon()
        elif event == "position":
            self._audio_position = max(0.0, float(message.get("position", 0.0)))
            self._audio_duration = max(0.0, float(message.get("duration", 0.0)))
            if self._audio_duration > 0:
                self._video_scale.set_range(0, self._audio_duration)
                self._video_scale.set_sensitive(True)
            self._video_scale.set_value(min(self._audio_position, max(1.0, self._audio_duration)))
            self._update_video_time_display()
        elif event == "error":
            _log(
                f"Audio preview helper failed for {self.file_uri!r}: "
                f"{message.get('message', 'unknown playback error')}"
            )
            self._stop_audio_helper()
            if not self._cancellable.is_cancelled():
                self.set_preview_slot(PREVIEW_SLOT_ICON)

    def _queue_audio_command(self, command: str, **values) -> None:
        if self._audio_process is None or self._audio_stdin is None:
            return
        payload = (
            json.dumps({"command": command, **values}, separators=(",", ":")).encode("utf-8")
            + b"\n"
        )
        # Position/volume sliders can produce updates faster than a child can
        # consume them. Keep just the newest unsent value of the same kind.
        if command in {"seek", "volume"}:
            prefix = f'{{"command":"{command}"'.encode("utf-8")
            pending = self._audio_command_queue[:1] if self._audio_write_pending else []
            queued_commands = (
                self._audio_command_queue[1:]
                if self._audio_write_pending
                else self._audio_command_queue
            )
            self._audio_command_queue = pending + [
                queued for queued in queued_commands if not queued.startswith(prefix)
            ]
        self._audio_command_queue.append(payload)
        self._write_next_audio_command()

    def _write_next_audio_command(self) -> None:
        if self._audio_write_pending or not self._audio_command_queue or self._audio_stdin is None:
            return
        self._audio_write_pending = True
        generation = self._audio_generation
        stream = self._audio_stdin
        stream.write_all_async(
            self._audio_command_queue[0],
            GLib.PRIORITY_DEFAULT,
            self._cancellable,
            self._on_audio_command_written,
            (generation, stream),
        )

    def _on_audio_command_written(self, _source, result: Gio.AsyncResult, state) -> None:
        generation, stream = state
        if generation != self._audio_generation or stream is not self._audio_stdin:
            return
        try:
            stream.write_all_finish(result)
        except GLib.Error as error:
            self._audio_write_pending = False
            if not self._cancellable.is_cancelled():
                _log(f"Could not control audio preview helper: {error.message}")
            return
        if self._audio_command_queue:
            self._audio_command_queue.pop(0)
        self._audio_write_pending = False
        self._write_next_audio_command()

    def _on_audio_helper_exited(
        self, process: Gio.Subprocess, result: Gio.AsyncResult, generation: int
    ) -> None:
        try:
            process.wait_finish(result)
        except GLib.Error:
            pass
        if generation != self._audio_generation or process is not self._audio_process:
            return
        self._audio_process_exited = True
        self._audio_exit_successful = process.get_successful()
        if self._audio_stdout_eof:
            self._finalize_audio_helper_exit(generation)

    def _finalize_audio_helper_exit(self, generation: int) -> None:
        if generation != self._audio_generation or self._audio_process is None:
            return
        successful = self._audio_exit_successful
        self._audio_process = None
        self._audio_stdin = None
        self._audio_stdout = None
        self._audio_command_queue.clear()
        self._audio_write_pending = False
        if not self._cancellable.is_cancelled():
            if not successful:
                _log(
                    "Audio preview helper exited unexpectedly; "
                    "the decoder failure was isolated from Nautilus"
                )
            else:
                _log("Audio preview helper stopped unexpectedly")
            self._disable_audio_controls()
            self.set_preview_slot(PREVIEW_SLOT_ICON)

    def _disable_audio_controls(self) -> None:
        if not hasattr(self, "_btn_play"):
            return
        self._audio_playing = False
        self._btn_play.set_icon_name("media-playback-start-symbolic")
        self._btn_play.set_sensitive(False)
        self._btn_mute.set_sensitive(False)
        self._video_scale.set_sensitive(False)
        self._volume_scale.set_sensitive(False)

    def _stop_audio_helper(self) -> None:
        self._audio_generation += 1
        process = self._audio_process
        self._audio_process = None
        self._audio_stdin = None
        self._audio_stdout = None
        self._audio_command_queue.clear()
        self._audio_write_pending = False
        self._audio_process_exited = False
        self._audio_exit_successful = False
        self._audio_stdout_eof = False
        if process is not None:
            try:
                process.send_signal(15)
            except (AttributeError, GLib.Error):
                process.force_exit()
            else:
                # Pipeline teardown normally finishes immediately. A broken
                # native backend can itself hang while going to NULL, so do
                # not let stale helpers accumulate after rapid selections.
                GLib.timeout_add(750, self._force_exit_audio_helper, process)

    @staticmethod
    def _force_exit_audio_helper(process: Gio.Subprocess) -> int:
        try:
            process.force_exit()
        except GLib.Error:
            pass
        return GLib.SOURCE_REMOVE

    def _stop_video_stream_helper(self) -> None:
        self._video_stream_generation += 1
        process = self._video_stream_process
        stdin = self._video_stream_stdin
        self._video_stream_process = None
        self._video_stream_stdin = None
        self._video_stream_stdout = None
        if stdin is not None:
            try:
                stdin.close(None)
            except GLib.Error:
                pass
        if process is not None:
            try:
                process.send_signal(15)
            except (AttributeError, GLib.Error):
                process.force_exit()
            else:
                GLib.timeout_add(750, self._force_exit_audio_helper, process)

    def _stop_video(self) -> None:
        self._stop_audio_helper()
        self._stop_video_state_poll()
        self._stop_video_stream_helper()
        view = getattr(self, "_video_web_view", None)
        if view is not None:
            try:
                view.stop_loading()
                if self._cancellable.is_cancelled():
                    view.terminate_web_process()
            except (AttributeError, GLib.Error, RuntimeError):
                pass
        if hasattr(self, "_btn_play"):
            self._btn_play.set_icon_name("media-playback-start-symbolic")

    def _on_video_play_toggled(self, _btn: Gtk.Button) -> None:
        if getattr(self, "_audio_process", None) is not None:
            self._audio_playing = not self._audio_playing
            self._btn_play.set_icon_name(
                "media-playback-pause-symbolic"
                if self._audio_playing
                else "media-playback-start-symbolic"
            )
            self._queue_audio_command("play" if self._audio_playing else "pause")
        elif self._video_stream_process is not None:
            if self._video_playing:
                self._video_playing = False
                self._btn_play.set_icon_name("media-playback-start-symbolic")
                self._evaluate_video_command("document.querySelector('video')?.pause()")
            else:
                self._video_playing = True
                self._btn_play.set_icon_name("media-playback-pause-symbolic")
                self._evaluate_video_command(
                    "(()=>{const v=document.querySelector('video');"
                    "if(!v)return;if(v.ended)v.currentTime=0;"
                    "v.play().catch(()=>{});})()"
                )

    def _on_video_mute_toggled(self, _btn: Gtk.Button) -> None:
        if getattr(self, "_audio_process", None) is not None:
            self._audio_muted = not self._audio_muted
            self._update_media_volume_icon()
            self._queue_audio_command("mute", value=self._audio_muted)
        elif self._video_stream_process is not None:
            self._video_muted = not self._video_muted
            self._update_video_volume_icon()
            value = "true" if self._video_muted else "false"
            self._evaluate_video_command(
                f"(()=>{{const v=document.querySelector('video');if(v)v.muted={value};}})()"
            )

    def _on_media_volume_changed(self, scale: Gtk.Scale) -> None:
        if self._setting_volume_scale:
            return
        volume = max(0.0, min(1.0, scale.get_value() / 100.0))
        if getattr(self, "_audio_process", None) is not None:
            self._audio_volume = volume
            if volume > 0:
                self._audio_muted = False
            self._update_media_volume_icon()
            self._queue_audio_command("volume", value=volume)
        elif self._video_stream_process is not None:
            self._video_volume = volume
            if volume > 0:
                self._video_muted = False
            self._update_video_volume_icon()
            self._evaluate_video_command(
                "(()=>{const v=document.querySelector('video');if(!v)return;"
                f"v.volume={volume:.6f};" + ("v.muted=false;" if volume > 0 else "") + "})()"
            )

    def _update_media_volume_icon(self) -> None:
        volume = self._audio_volume
        muted = self._audio_muted
        if muted or volume <= 0:
            icon = "audio-volume-muted-symbolic"
        elif volume < 0.34:
            icon = "audio-volume-low-symbolic"
        elif volume < 0.67:
            icon = "audio-volume-medium-symbolic"
        else:
            icon = "audio-volume-high-symbolic"
        self._btn_mute.set_icon_name(icon)

    def _update_video_volume_icon(self) -> None:
        if self._video_muted or self._video_volume <= 0:
            icon = "audio-volume-muted-symbolic"
        elif self._video_volume < 0.34:
            icon = "audio-volume-low-symbolic"
        elif self._video_volume < 0.67:
            icon = "audio-volume-medium-symbolic"
        else:
            icon = "audio-volume-high-symbolic"
        self._btn_mute.set_icon_name(icon)

    def _on_video_seek_changed(self, scale, scroll_type, value) -> bool:
        if getattr(self, "_audio_process", None) is not None:
            self._audio_position = max(0.0, float(value))
            self._update_video_time_display()
            self._queue_audio_command("seek", value=self._audio_position)
        elif self._video_stream_process is not None:
            self._video_position = max(0.0, float(value))
            self._update_video_time_display()
            self._evaluate_video_command(
                "(()=>{const v=document.querySelector('video');"
                f"if(v)v.currentTime={self._video_position:.6f};}})()"
            )
        return False

    def _update_video_time_display(self) -> None:
        if getattr(self, "_audio_process", None) is not None:
            ts_sec = int(self._audio_position)
            dur_sec = int(self._audio_duration)
        elif self._video_stream_process is not None:
            ts_sec = int(self._video_position)
            dur_sec = int(self._video_duration)
        else:
            return

        def fmt_time(sec: int) -> str:
            m = sec // 60
            s = sec % 60
            return f"{m}:{s:02d}"

        self._video_time_lbl.set_label(f"{fmt_time(ts_sec)} / {fmt_time(dur_sec)}")

    def _setup_archive_list_item(self, _factory, list_item: Gtk.ListItem) -> None:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        row.set_margin_start(8)
        row.set_margin_end(8)
        row.set_margin_top(4)
        row.set_margin_bottom(4)
        icon = Gtk.Image()
        icon.set_pixel_size(20)
        name = Gtk.Label(label="")
        name.set_xalign(0)
        name.set_hexpand(True)
        name.set_ellipsize(Pango.EllipsizeMode.END)
        encrypted = Gtk.Image.new_from_icon_name("changes-prevent-symbolic")
        encrypted.set_pixel_size(13)
        encrypted.set_tooltip_text(_("Encrypted"))
        size = Gtk.Label(label="")
        size.set_xalign(1)
        size.set_width_chars(10)
        size.add_css_class("caption")
        size.add_css_class("dim-label")
        row.append(icon)
        row.append(name)
        row.append(encrypted)
        row.append(size)
        row._mc_archive_icon = icon
        row._mc_archive_name = name
        row._mc_archive_encrypted = encrypted
        row._mc_archive_size = size
        list_item.set_child(row)

    def _bind_archive_list_item(self, _factory, list_item: Gtk.ListItem) -> None:
        item = list_item.get_item()
        row = list_item.get_child()
        if not isinstance(item, _ArchiveListItem) or row is None:
            return
        entry = item.entry
        row._mc_archive_name.set_label(entry.name)
        row._mc_archive_encrypted.set_visible(entry.encrypted)
        row._mc_archive_size.set_label(_format_size(entry.size))
        if entry.is_dir:
            row._mc_archive_icon.set_from_icon_name("folder-symbolic")
        else:
            content_type, _uncertain = Gio.content_type_guess(entry.name, None)
            icon = Gio.content_type_get_icon(content_type) if content_type else None
            if icon is not None:
                row._mc_archive_icon.set_from_gicon(icon)
            else:
                row._mc_archive_icon.set_from_icon_name("text-x-generic-symbolic")
        details = []
        if entry.modified:
            details.append(entry.modified)
        if entry.packed_size > 0:
            details.append(_("Packed: {size}").format(size=_format_size(entry.packed_size)))
        row.set_tooltip_text(" · ".join(details))

    def _on_archive_item_activated(self, _list_view, position: int) -> None:
        item = self._archive_selection.get_item(position)
        if isinstance(item, _ArchiveListItem) and item.entry.is_dir:
            self._show_archive_folder(item.entry.path)

    def _on_archive_up_clicked(self, _button) -> None:
        if not self._archive_folder:
            return
        parent, _separator, _name = self._archive_folder.rpartition("/")
        self._show_archive_folder(parent)

    def _on_archive_key_pressed(
        self, _controller, keyval: int, _keycode: int, state: Gdk.ModifierType
    ) -> bool:
        if keyval == Gdk.KEY_BackSpace and not (
            state
            & (
                Gdk.ModifierType.CONTROL_MASK
                | Gdk.ModifierType.ALT_MASK
                | Gdk.ModifierType.SUPER_MASK
            )
        ):
            self._on_archive_up_clicked(None)
            return True
        return False

    def _show_archive_folder(self, folder: str) -> None:
        listing = self._archive_listing
        if listing is None:
            return
        normalized = _normalize_archive_member_path(folder)
        children = _archive_children(listing, normalized)
        self._archive_folder = normalized
        self._archive_store.remove_all()
        for entry in children:
            self._archive_store.append(_ArchiveListItem(entry))
        self._archive_path_label.set_label(f"/{normalized}" if normalized else "/")
        count = len(children)
        count_text = _n("{n} item", "{n} items", count).format(n=count)
        if listing.truncated:
            count_text = _("{count} · listing truncated").format(count=count_text)
        self._archive_count_label.set_label(count_text)
        self._archive_up_button.set_sensitive(bool(normalized))
        self._archive_content_stack.set_visible_child_name("list" if children else "empty")
        self._archive_selection.set_selected(Gtk.INVALID_LIST_POSITION)

    def _load_preview_archive(
        self,
        gfile: Gio.File,
        content_type: str | None,
        *,
        mtime: int,
    ) -> None:
        path = gfile.get_path()
        lister = shutil.which("7zz") or shutil.which("7z") or shutil.which("7za")
        if path is None or lister is None:
            self.set_preview_slot(PREVIEW_SLOT_ICON)
            self._maybe_load_thumbnail(content_type, mtime)
            return
        self._archive_generation += 1
        generation = self._archive_generation
        self._archive_listing = None
        self._archive_folder = ""
        self.set_preview_slot(PREVIEW_SLOT_LOADING)

        def worker() -> None:
            listing = _list_archive_contents(path, self._cancellable, lister=lister)
            if self._cancellable.is_cancelled():
                return
            GLib.idle_add(
                self._on_archive_preview_ready,
                listing,
                generation,
                content_type,
                mtime,
            )

        self._worker_futures.append(_PREVIEW_WORKER_EXECUTOR.submit(worker))

    def _on_archive_preview_ready(
        self,
        listing: _ArchiveListing | None,
        generation: int,
        content_type: str | None,
        mtime: int,
    ) -> int:
        if self._cancellable.is_cancelled() or generation != self._archive_generation:
            return GLib.SOURCE_REMOVE
        if listing is None:
            self.set_preview_slot(PREVIEW_SLOT_ICON)
            self._maybe_load_thumbnail(content_type, mtime)
            return GLib.SOURCE_REMOVE
        self._archive_listing = listing
        self._show_archive_folder("")
        self.set_preview_slot(PREVIEW_SLOT_ARCHIVE)
        return GLib.SOURCE_REMOVE

    def _load_preview_spreadsheet(
        self,
        gfile: Gio.File,
        content_type: str | None,
        *,
        size: int,
        mtime: int,
    ) -> None:
        """Convert a workbook off-thread and display a tabbed cell grid.

        LibreOffice is optional. When it is absent, conversion fails, or the
        workbook exceeds the preview budget, the ordinary icon/thumbnail is
        shown and Enter still opens the real spreadsheet application.
        """
        path = gfile.get_path()
        converter = shutil.which("libreoffice") or shutil.which("soffice")
        if (
            path is None
            or converter is None
            or WebKit is None
            or size > _SPREADSHEET_PREVIEW_MAX_BYTES
        ):
            self.set_preview_slot(PREVIEW_SLOT_ICON)
            self._maybe_load_thumbnail(content_type, mtime)
            return

        self._cleanup_spreadsheet_preview()
        tmpdir = tempfile.mkdtemp(prefix="nautilus-mc-sheet-")
        output_dir = os.path.join(tmpdir, "output")
        profile_dir = os.path.join(tmpdir, "profile")
        self._spreadsheet_tmpdir = tmpdir
        self._spreadsheet_generation += 1
        generation = self._spreadsheet_generation
        self.set_preview_slot(PREVIEW_SLOT_LOADING)

        def worker() -> None:
            html_path = _convert_spreadsheet_to_html(
                path,
                output_dir,
                profile_dir,
                self._cancellable,
                converter=converter,
            )
            if self._cancellable.is_cancelled():
                return
            GLib.idle_add(
                self._on_spreadsheet_preview_ready,
                html_path,
                tmpdir,
                generation,
                content_type,
                mtime,
            )

        self._worker_futures.append(_PREVIEW_WORKER_EXECUTOR.submit(worker))

    def _on_spreadsheet_preview_ready(
        self,
        html_path: str | None,
        tmpdir: str,
        generation: int,
        content_type: str | None,
        mtime: int,
    ) -> int:
        if (
            self._cancellable.is_cancelled()
            or generation != self._spreadsheet_generation
            or tmpdir != self._spreadsheet_tmpdir
        ):
            shutil.rmtree(tmpdir, ignore_errors=True)
            return GLib.SOURCE_REMOVE
        if html_path is None or not os.path.isfile(html_path):
            self._cleanup_spreadsheet_preview(tmpdir)
            self.set_preview_slot(PREVIEW_SLOT_ICON)
            self._maybe_load_thumbnail(content_type, mtime)
            return GLib.SOURCE_REMOVE
        self._ensure_spreadsheet_view()
        self._spreadsheet_view.load_uri(Gio.File.new_for_path(html_path).get_uri())
        return GLib.SOURCE_REMOVE

    def _ensure_spreadsheet_view(self) -> None:
        if self._spreadsheet_view is not None or WebKit is None:
            return
        self._spreadsheet_view = WebKit.WebView()
        # The Miller host's capture-phase scroll controller recognizes this
        # class and lets horizontal trackpad deltas reach WebKit. Without it,
        # two-finger horizontal scrolling pans the whole column chain instead
        # of the wide sheet beneath the pointer.
        self._spreadsheet_view.add_css_class("mc-horizontal-scroll-owner")
        self._spreadsheet_view.set_hexpand(True)
        self._spreadsheet_view.set_vexpand(True)
        settings = self._spreadsheet_view.get_settings()
        settings.set_enable_javascript(True)
        # A spreadsheet has no useful browser history. Disable WebKit's swipe
        # navigation so the propagated gesture always remains available for
        # horizontal sheet scrolling.
        try:
            settings.set_enable_back_forward_navigation_gestures(False)
        except (AttributeError, TypeError):
            pass
        for setter in (
            "set_enable_html5_database",
            "set_enable_html5_local_storage",
            "set_enable_developer_extras",
        ):
            try:
                getattr(settings, setter)(False)
            except (AttributeError, TypeError):
                pass
        self._spreadsheet_view.connect("decide-policy", self._on_spreadsheet_decide_policy)
        self._spreadsheet_view.connect("load-changed", self._on_spreadsheet_load_changed)
        self._preview_stack.add_named(self._spreadsheet_view, PREVIEW_SLOT_SPREADSHEET)

    def _on_spreadsheet_decide_policy(self, _view, decision, decision_type) -> bool:
        if decision_type != WebKit.PolicyDecisionType.NAVIGATION_ACTION:
            return False
        try:
            uri = decision.get_navigation_action().get_request().get_uri() or ""
            path = GLib.filename_from_uri(uri.split("#", 1)[0])[0]
        except Exception:
            decision.ignore()
            return True
        if not self._spreadsheet_tmpdir or not _path_is_within(self._spreadsheet_tmpdir, path):
            decision.ignore()
            return True
        return False

    def _on_spreadsheet_load_changed(self, _view, load_event) -> None:
        if load_event != WebKit.LoadEvent.COMMITTED:
            return
        if self._cancellable.is_cancelled() or not self._spreadsheet_tmpdir:
            return
        self.set_preview_slot(PREVIEW_SLOT_SPREADSHEET)

    def scroll_horizontal_preview(self, delta: float) -> bool:
        """Consume a Miller scroll delta in the active spreadsheet viewport.

        WebKitGTK does not consistently turn GTK's smooth horizontal scroll
        events into DOM scrolling. Aggregate the high-frequency touchpad
        deltas and apply at most one JavaScript scroll per main-loop cycle,
        keeping the interaction responsive without flooding the web process.
        """
        if (
            self._spreadsheet_view is None
            or self._preview_stack.get_visible_child_name() != PREVIEW_SLOT_SPREADSHEET
        ):
            return False
        self._spreadsheet_scroll_delta += delta * 48.0
        if self._spreadsheet_scroll_idle_id == 0:
            self._spreadsheet_scroll_idle_id = GLib.idle_add(
                self._flush_spreadsheet_horizontal_scroll
            )
        return True

    def _flush_spreadsheet_horizontal_scroll(self) -> int:
        self._spreadsheet_scroll_idle_id = 0
        pixels = self._spreadsheet_scroll_delta
        self._spreadsheet_scroll_delta = 0.0
        if self._cancellable.is_cancelled() or self._spreadsheet_view is None or pixels == 0:
            return GLib.SOURCE_REMOVE
        script = (
            "const scroller=document.getElementById('mc-sheet-scroll');"
            f"if(scroller)scroller.scrollLeft+={pixels:.6f};"
        )
        self._spreadsheet_view.evaluate_javascript(
            script, -1, None, None, self._cancellable, None, None
        )
        return GLib.SOURCE_REMOVE

    def _cleanup_spreadsheet_preview(self, expected_dir: str | None = None) -> None:
        tmpdir = getattr(self, "_spreadsheet_tmpdir", None)
        if tmpdir is None or (expected_dir is not None and tmpdir != expected_dir):
            return
        spreadsheet_view = getattr(self, "_spreadsheet_view", None)
        if spreadsheet_view is not None:
            spreadsheet_view.stop_loading()
        self._spreadsheet_tmpdir = None
        shutil.rmtree(tmpdir, ignore_errors=True)

    def _load_preview_document(
        self,
        gfile: Gio.File,
        content_type: str | None,
        *,
        size: int,
        mtime: int,
    ) -> None:
        """Convert a word-processing document off-thread for the PDF reader."""
        path = gfile.get_path()
        converter = shutil.which("libreoffice") or shutil.which("soffice")
        if path is None or converter is None or size > _DOCUMENT_PREVIEW_MAX_BYTES:
            self.set_preview_slot(PREVIEW_SLOT_ICON)
            self._maybe_load_thumbnail(content_type, mtime)
            return

        self._cleanup_document_preview()
        tmpdir = tempfile.mkdtemp(prefix="nautilus-mc-document-")
        output_dir = os.path.join(tmpdir, "output")
        profile_dir = os.path.join(tmpdir, "profile")
        self._document_tmpdir = tmpdir
        self._document_generation += 1
        generation = self._document_generation
        self.set_preview_slot(PREVIEW_SLOT_LOADING)

        def worker() -> None:
            pdf_path = _convert_document_to_pdf(
                path,
                output_dir,
                profile_dir,
                self._cancellable,
                converter=converter,
            )
            if self._cancellable.is_cancelled():
                return
            GLib.idle_add(
                self._on_document_preview_ready,
                pdf_path,
                tmpdir,
                generation,
                content_type,
                mtime,
            )

        self._worker_futures.append(_PREVIEW_WORKER_EXECUTOR.submit(worker))

    def _on_document_preview_ready(
        self,
        pdf_path: str | None,
        tmpdir: str,
        generation: int,
        content_type: str | None,
        mtime: int,
    ) -> int:
        if (
            self._cancellable.is_cancelled()
            or generation != self._document_generation
            or tmpdir != self._document_tmpdir
        ):
            shutil.rmtree(tmpdir, ignore_errors=True)
            return GLib.SOURCE_REMOVE
        if pdf_path is None or not os.path.isfile(pdf_path):
            self._cleanup_document_preview(tmpdir)
            self.set_preview_slot(PREVIEW_SLOT_ICON)
            self._maybe_load_thumbnail(content_type, mtime)
            return GLib.SOURCE_REMOVE
        self._load_preview_pdf(Gio.File.new_for_path(pdf_path), mtime)
        return GLib.SOURCE_REMOVE

    def _cleanup_document_preview(self, expected_dir: str | None = None) -> None:
        tmpdir = getattr(self, "_document_tmpdir", None)
        if tmpdir is None or (expected_dir is not None and tmpdir != expected_dir):
            return
        self._document_tmpdir = None
        shutil.rmtree(tmpdir, ignore_errors=True)

    def _load_preview_pdf(self, gfile: Gio.File, mtime: int) -> None:
        """Initialize the PDF reader: continuous page view, zoom, selection."""
        path = gfile.get_path()
        if not path:
            self.set_preview_slot(PREVIEW_SLOT_ICON)
            self._maybe_load_thumbnail("application/pdf", mtime)
            return

        self._pdf_path = path
        self._pdf_current_page = 1
        self._pdf_zoom_pct = 100
        self._pdf_mtime = mtime
        if self._pdf_viewport_tick_id == 0:
            self._pdf_last_viewport_width = 0
            self._pdf_viewport_tick_id = self._pdf_scroll.add_tick_callback(
                self._poll_pdf_viewport_width
            )
        self._pdf_generation += 1
        self._pdf_page_cache.clear()
        self._pdf_page_pixbufs.clear()
        self._pdf_page_dpi.clear()
        self._pdf_pending_renders.clear()
        for future in self._pdf_render_futures.values():
            future.cancel()
        self._pdf_render_futures.clear()
        for future in self._pdf_word_futures.values():
            future.cancel()
        self._pdf_word_futures.clear()
        self._pdf_words.clear()
        self._pdf_lines.clear()
        self._pdf_words_pending.clear()
        self._clear_pdf_selection()
        self._clear_pdf_pages()
        self._cancel_pdf_quality_upgrade()
        self._cancel_pdf_visible_update()
        # Re-enabled once the page count and first render land (see
        # _build_pdf_pages) -- until then self._pdf_geometry is still the
        # *previous* file's, so the controls would act on the wrong document.
        self._pdf_zoom_scale.set_sensitive(False)
        self._btn_pdf_prev.set_sensitive(False)
        self._btn_pdf_next.set_sensitive(False)
        self._set_pdf_zoom_scale_value(100)
        self._btn_pdf_zoom.set_label("100%")
        self._btn_pdf_zoom_out.set_sensitive(False)
        self._btn_pdf_zoom_in.set_sensitive(True)
        self.set_preview_slot(PREVIEW_SLOT_LOADING)

        generation = self._pdf_generation
        cancellable = self._cancellable

        def _pdf_init_worker() -> None:
            geometry = _get_pdf_info(path, cancellable)
            if cancellable.is_cancelled():
                return
            GLib.idle_add(self._build_pdf_pages, geometry, generation)

        self._worker_futures.append(_PREVIEW_WORKER_EXECUTOR.submit(_pdf_init_worker))

    def _clear_pdf_pages(self) -> None:
        self._pdf_canvas.set_content_width(1)
        self._pdf_canvas.set_content_height(1)
        self._pdf_canvas.queue_draw()

    def _build_pdf_pages(self, geometry: list[_PdfPageGeometry], generation: int) -> int:
        """Lay the whole document out as empty, correctly-sized pages.

        Sizing every page up front from the geometry pdfinfo reported is what
        lets scrolling flow: the scrollbar spans the real document
        immediately and page positions never move, so pages can be filled in
        as they approach the viewport without anything shifting under the
        user. Only pixels arrive late, never layout."""
        if self._cancellable.is_cancelled() or generation != self._pdf_generation:
            return GLib.SOURCE_REMOVE
        self._pdf_geometry = geometry
        self._pdf_total_pages = len(geometry)

        self._clear_pdf_pages()
        self._update_pdf_page_sizes()
        self._pdf_zoom_scale.set_sensitive(True)
        self._update_pdf_page_controls()
        self._update_pdf_visible_pages()
        self._queue_pdf_viewport_refit()
        return GLib.SOURCE_REMOVE

    # ── Geometry ──────────────────────────────────────────────────────────

    def _pdf_page_display_size(self, index: int) -> tuple[int, int]:
        """On-screen size of one page at the current zoom.

        Every page is drawn at the same width and takes whatever height its
        own proportions call for, which is what a PDF reader does and what
        keeps a document with mixed page sizes or rotations coherent."""
        width = max(1, round(self._pdf_base_width() * self._pdf_zoom_pct / 100))
        if index < len(self._pdf_geometry):
            page = self._pdf_geometry[index]
            aspect = page.display_width / max(1.0, page.display_height)
        else:
            aspect = _PDF_DEFAULT_PAGE_WIDTH_PTS / _PDF_DEFAULT_PAGE_HEIGHT_PTS
        return width, max(1, round(width / max(0.01, aspect)))

    def _recompute_pdf_layout(self) -> None:
        """Per-page display sizes and the y each page starts at.

        Cumulative offsets rather than one stride: page heights differ within
        a real document (a rotated page is as wide as the others are tall),
        so a single stride would put every page after the first one in the
        wrong place."""
        self._pdf_page_sizes = [
            self._pdf_page_display_size(index) for index in range(self._pdf_total_pages)
        ]
        offsets: list[float] = []
        y = 0.0
        for _width, height in self._pdf_page_sizes:
            offsets.append(y)
            y += height + _PDF_PAGE_SPACING
        self._pdf_page_offsets = offsets
        self._pdf_content_height = max(0.0, y - _PDF_PAGE_SPACING)

    def _update_pdf_page_sizes(self) -> None:
        """Resize every page slot to the current zoom, keeping the document
        position steady across the change."""
        if not self._pdf_geometry:
            return
        vadj = self._pdf_scroll.get_vadjustment()
        previous_height = self._pdf_content_height
        # Where the viewport sits as a fraction of the document, preserved
        # across the resize so zooming keeps you where you were reading.
        fraction = vadj.get_value() / previous_height if previous_height > 0 else 0.0

        self._recompute_pdf_layout()
        content_width = max((width for width, _height in self._pdf_page_sizes), default=1)
        self._pdf_canvas.set_content_width(content_width)
        self._pdf_canvas.set_content_height(max(1, round(self._pdf_content_height)))
        self._pdf_canvas.queue_draw()

        target = fraction * self._pdf_content_height

        def _restore() -> bool:
            limit = max(0.0, vadj.get_upper() - vadj.get_page_size())
            vadj.set_value(min(target, limit))
            return GLib.SOURCE_REMOVE

        # After the relayout the new upper/page-size are settled; setting the
        # value before that would clamp against the old range.
        GLib.idle_add(_restore)

    def _pdf_base_width(self) -> int:
        """The width 100% zoom maps to, established once from the live
        viewport and thereafter only updated by a genuine external resize."""
        if not self._pdf_display_base_width:
            self._pdf_display_base_width = self._pdf_viewport_width()
        return self._pdf_display_base_width

    def _pdf_viewport_width(self) -> int:
        """Live width available to a page at 100% zoom, capped at
        _PDF_DISPLAY_MAX_WIDTH."""
        page_size = round(self._pdf_scroll.get_hadjustment().get_page_size())
        width = page_size or self._pdf_scroll.get_width() or _PDF_PREVIEW_BASE_WIDTH
        return min(width, _PDF_DISPLAY_MAX_WIDTH)

    def _on_pdf_viewport_width_changed(self, *_args) -> None:
        """Keep 100% zoom tied to the live preview width after allocation or
        resizes. This and _apply_pdf_viewport_refit are the only places that
        re-measure it -- letting the zoom path re-measure would feed its own
        layout changes back in as if they were resizes."""
        if not self._pdf_geometry:
            return
        base_width = self._pdf_viewport_width()
        if base_width != self._pdf_display_base_width:
            self._pdf_display_base_width = base_width
            self._update_pdf_page_sizes()
            self._arm_pdf_quality_upgrade()

    def _poll_pdf_viewport_width(self, _widget, _clock) -> bool:
        if self._cancellable.is_cancelled():
            self._pdf_viewport_tick_id = 0
            return GLib.SOURCE_REMOVE
        width = self._pdf_viewport_width()
        if width != self._pdf_last_viewport_width:
            self._pdf_last_viewport_width = width
            self._on_pdf_viewport_width_changed()
        return GLib.SOURCE_CONTINUE

    def _queue_pdf_viewport_refit(self) -> None:
        self._cancel_pdf_viewport_refit()
        self._pdf_viewport_refit_id = GLib.timeout_add(
            _PDF_VIEWPORT_REFIT_DELAY_MS, self._apply_pdf_viewport_refit
        )

    def _apply_pdf_viewport_refit(self) -> bool:
        self._pdf_viewport_refit_id = 0
        base_width = self._pdf_viewport_width()
        if not self._cancellable.is_cancelled() and base_width != self._pdf_display_base_width:
            self._pdf_display_base_width = base_width
            self._update_pdf_page_sizes()
            self._arm_pdf_quality_upgrade()
        return GLib.SOURCE_REMOVE

    def _cancel_pdf_viewport_refit(self) -> None:
        refit_id = getattr(self, "_pdf_viewport_refit_id", 0)
        if refit_id != 0:
            GLib.source_remove(refit_id)
            self._pdf_viewport_refit_id = 0

    # ── Which pages need pixels ───────────────────────────────────────────

    def _on_pdf_scrolled(self, _adjustment: Gtk.Adjustment) -> None:
        self._update_pdf_page_controls()
        # The canvas intentionally paints only the viewport. Scrolling moves
        # the clip but does not otherwise invalidate the child snapshot, so
        # request a clipped redraw immediately for any cached page pixels.
        self._pdf_canvas.queue_draw()
        self._schedule_pdf_visible_update()

    def _schedule_pdf_visible_update(self) -> None:
        if self._pdf_visible_debounce_id != 0:
            GLib.source_remove(self._pdf_visible_debounce_id)
        self._pdf_visible_debounce_id = GLib.timeout_add(
            _PDF_VISIBLE_DEBOUNCE_MS, self._apply_pdf_visible_update
        )

    def _cancel_pdf_visible_update(self) -> None:
        debounce_id = getattr(self, "_pdf_visible_debounce_id", 0)
        if debounce_id != 0:
            GLib.source_remove(debounce_id)
            self._pdf_visible_debounce_id = 0

    def _apply_pdf_visible_update(self) -> bool:
        self._pdf_visible_debounce_id = 0
        if not self._cancellable.is_cancelled():
            self._update_pdf_visible_pages()
        return GLib.SOURCE_REMOVE

    def _pdf_page_at_offset(self, y: float) -> int:
        """0-based index of the page containing document position `y`."""
        if not self._pdf_page_offsets:
            return 0
        index = bisect.bisect_right(self._pdf_page_offsets, y) - 1
        return max(0, min(len(self._pdf_page_offsets) - 1, index))

    def _visible_pdf_page_range(self) -> tuple[int, int]:
        """Inclusive 1-based page range in (or near) the viewport, widened by
        _PDF_PREFETCH_PAGES so the next page is already rendered by the time
        its top edge scrolls into view."""
        vadj = self._pdf_scroll.get_vadjustment()
        top = vadj.get_value()
        bottom = top + (vadj.get_page_size() or self._pdf_scroll.get_height())
        first = self._pdf_page_at_offset(top) + 1 - _PDF_PREFETCH_PAGES
        last = self._pdf_page_at_offset(bottom) + 1 + _PDF_PREFETCH_PAGES
        return max(1, first), min(self._pdf_total_pages, last)

    def _current_pdf_page(self) -> int:
        """The page the viewport is mostly showing -- whichever covers its
        vertical midpoint."""
        vadj = self._pdf_scroll.get_vadjustment()
        middle = vadj.get_value() + (vadj.get_page_size() or self._pdf_scroll.get_height()) / 2
        return self._pdf_page_at_offset(middle) + 1

    def _update_pdf_page_controls(self) -> None:
        page = self._current_pdf_page()
        self._pdf_current_page = page
        self._lbl_pdf_page.set_label(f"{page} / {self._pdf_total_pages}")
        self._btn_pdf_prev.set_sensitive(page > 1)
        self._btn_pdf_next.set_sensitive(page < self._pdf_total_pages)

    def _update_pdf_visible_pages(self) -> None:
        """Render the pages around the viewport and release the ones far from
        it, so memory tracks the window being read rather than the length of
        the document."""
        if not self._pdf_geometry or self._cancellable.is_cancelled():
            return
        first, last = self._visible_pdf_page_range()
        wanted = range(first, last + 1)
        target_dpi = {page: self._pdf_target_dpi(page - 1) for page in wanted}

        # A fast scroll/zoom can supersede work before it has started. Drop
        # those queued futures so current visible pages do not wait behind a
        # backlog of pages the user has already left.
        for key, future in list(self._pdf_render_futures.items()):
            page, dpi = key
            if (page not in target_dpi or dpi != target_dpi[page]) and future.cancel():
                self._pdf_render_futures.pop(key, None)
                self._pdf_pending_renders.discard(key)
        for page, future in list(self._pdf_word_futures.items()):
            if page not in target_dpi and future.cancel():
                self._pdf_word_futures.pop(page, None)
                self._pdf_words_pending.discard(page)

        for page in list(self._pdf_page_pixbufs):
            if page not in wanted:
                self._pdf_page_pixbufs.pop(page, None)
                self._pdf_page_dpi.pop(page, None)
        self._pdf_canvas.queue_draw()

        for page in wanted:
            self._ensure_pdf_page_rendered(page, target_dpi[page])
            self._ensure_pdf_page_words(page)

    def _ensure_pdf_page_rendered(self, page: int, dpi: int) -> None:
        if self._pdf_page_dpi.get(page) == dpi or not self._pdf_path:
            return
        cached = self._pdf_page_cache.get((page, dpi))
        if cached is not None:
            # Refresh insertion order so eviction is genuinely least-recently
            # used rather than oldest-rendered.
            self._pdf_page_cache.pop((page, dpi), None)
            self._pdf_page_cache[(page, dpi)] = cached
            self._set_pdf_page_pixbuf(page, cached, dpi)
            return
        key = (page, dpi)
        if key in self._pdf_pending_renders:
            return
        self._pdf_pending_renders.add(key)

        path = self._pdf_path
        generation = self._pdf_generation
        cancellable = self._cancellable

        def _worker() -> None:
            pixbuf = _render_pdf_page_at_zoom(path, page, dpi, cancellable)
            if cancellable.is_cancelled():
                return
            GLib.idle_add(self._on_pdf_page_ready, page, dpi, pixbuf, generation)

        self._pdf_render_futures[key] = _PREVIEW_WORKER_EXECUTOR.submit(_worker)

    def _on_pdf_page_ready(
        self, page: int, dpi: int, pixbuf: GdkPixbuf.Pixbuf | None, generation: int
    ) -> int:
        if generation != self._pdf_generation or self._cancellable.is_cancelled():
            return GLib.SOURCE_REMOVE
        self._pdf_render_futures.pop((page, dpi), None)
        self._pdf_pending_renders.discard((page, dpi))
        if pixbuf is None:
            # A page that will not render leaves its (correctly sized) slot
            # blank rather than taking the whole preview down. Only a first
            # page that fails while nothing at all is on screen falls back.
            if self._preview_stack.get_visible_child_name() != PREVIEW_SLOT_PDF:
                self.set_preview_slot(PREVIEW_SLOT_ICON)
                self._maybe_load_thumbnail("application/pdf", self._pdf_mtime)
            return GLib.SOURCE_REMOVE

        self._cache_pdf_page(page, dpi, pixbuf)
        first, last = self._visible_pdf_page_range()
        if first <= page <= last:
            self._set_pdf_page_pixbuf(page, pixbuf, dpi)
        if self._preview_stack.get_visible_child_name() != PREVIEW_SLOT_PDF:
            self.set_preview_slot(PREVIEW_SLOT_PDF)
            self._queue_pdf_viewport_refit()
        return GLib.SOURCE_REMOVE

    def _set_pdf_page_pixbuf(self, page: int, pixbuf: GdkPixbuf.Pixbuf, dpi: int) -> None:
        self._pdf_page_pixbufs[page] = pixbuf
        self._pdf_page_dpi[page] = dpi
        self._pdf_canvas.queue_draw()

    # ── Rendering resolution and cache ────────────────────────────────────

    def _pdf_target_dpi(self, index: int) -> int:
        """Render resolution for a page at its current on-screen size."""
        width = self._pdf_page_display_size(index)[0]
        page_width = (
            self._pdf_geometry[index].display_width
            if index < len(self._pdf_geometry)
            else _PDF_DEFAULT_PAGE_WIDTH_PTS
        )
        return _pdf_dpi_for_width(width, page_width)

    def _cache_pdf_page(self, page: int, dpi: int, pixbuf: GdkPixbuf.Pixbuf) -> None:
        """Remember a rendered page, evicting least-recently-used entries
        until the cache is back inside its pixel/entry budget."""
        key = (page, dpi)
        self._pdf_page_cache.pop(key, None)
        self._pdf_page_cache[key] = pixbuf

        def pixels(buf: GdkPixbuf.Pixbuf) -> int:
            return buf.get_width() * buf.get_height()

        total = sum(pixels(buf) for buf in self._pdf_page_cache.values())
        while self._pdf_page_cache and (
            total > _PDF_PAGE_CACHE_MAX_PIXELS
            or len(self._pdf_page_cache) > _PDF_PAGE_CACHE_MAX_ENTRIES
        ):
            oldest = next(iter(self._pdf_page_cache))
            if oldest == key:
                break
            total -= pixels(self._pdf_page_cache.pop(oldest))

    def _arm_pdf_quality_upgrade(self) -> None:
        self._cancel_pdf_quality_upgrade()
        self._pdf_quality_debounce_id = GLib.timeout_add(
            _PDF_QUALITY_DEBOUNCE_MS, self._maybe_upgrade_pdf_quality
        )

    def _cancel_pdf_quality_upgrade(self) -> None:
        # getattr-guarded: destroy_enumeration calls this for the empty
        # placeholder column too, whose __init__ returns before the PDF setup.
        debounce_id = getattr(self, "_pdf_quality_debounce_id", 0)
        if debounce_id != 0:
            GLib.source_remove(debounce_id)
            self._pdf_quality_debounce_id = 0

    def _maybe_upgrade_pdf_quality(self) -> bool:
        """Re-render the visible pages at the DPI the current zoom calls for,
        once the zoom has settled. The rescale in _update_pdf_page_sizes
        already showed the new zoom instantly; this only makes it sharper."""
        self._pdf_quality_debounce_id = 0
        if not self._pdf_path or self._cancellable.is_cancelled():
            return GLib.SOURCE_REMOVE
        self._update_pdf_visible_pages()
        return GLib.SOURCE_REMOVE

    # ── Selectable text over the page ─────────────────────────────────────

    def _ensure_pdf_page_words(self, page: int) -> None:
        """Fetch a page's word boxes, off the main thread.

        Kept separate from the page render so the picture never waits on it:
        the text layer is effectively instant, but a page without one falls
        back to OCR, which is seconds."""
        if page in self._pdf_words:
            words = self._pdf_words.pop(page)
            lines = self._pdf_lines.pop(page, [])
            self._pdf_words[page] = words
            self._pdf_lines[page] = lines
            return
        if page in self._pdf_words_pending or not self._pdf_path:
            return
        self._pdf_words_pending.add(page)
        path = self._pdf_path
        generation = self._pdf_generation
        cancellable = self._cancellable

        def _worker() -> None:
            words = _pdf_page_words(path, page, cancellable)
            if cancellable.is_cancelled():
                return
            GLib.idle_add(self._on_pdf_words_ready, page, words, generation)

        self._pdf_word_futures[page] = _PREVIEW_WORKER_EXECUTOR.submit(_worker)

    def _on_pdf_words_ready(self, page: int, words: list[_PdfWord], generation: int) -> int:
        if generation != self._pdf_generation or self._cancellable.is_cancelled():
            return GLib.SOURCE_REMOVE
        self._pdf_word_futures.pop(page, None)
        self._pdf_words_pending.discard(page)
        self._pdf_words[page] = words
        self._pdf_lines[page] = self._pdf_build_lines(words)
        selected_pages = set()
        bounds = self._pdf_selection_bounds()
        if bounds is not None:
            selected_pages.update(range(bounds[0][0] + 1, bounds[1][0] + 2))
        while len(self._pdf_words) > _PDF_WORD_CACHE_MAX_PAGES:
            oldest = next(iter(self._pdf_words))
            if oldest in selected_pages:
                # Never retain an incomplete selection: once its geometry
                # would exceed the hard cache budget, clear it before
                # evicting rather than letting Copy silently omit pages.
                self._clear_pdf_selection()
                selected_pages.clear()
            self._pdf_words.pop(oldest, None)
            self._pdf_lines.pop(oldest, None)
        self._pdf_canvas.queue_draw()
        return GLib.SOURCE_REMOVE

    def _pdf_point_to_page(self, x: float, y: float) -> tuple[int, float, float]:
        """Map a position in the page stack to (0-based page, x, y within it),
        both in that page's own display pixels."""
        index = self._pdf_page_at_offset(y)
        local_y = y - self._pdf_page_offsets[index] if self._pdf_page_offsets else y
        page_width = self._pdf_page_sizes[index][0] if self._pdf_page_sizes else 1
        canvas_width = self._pdf_canvas.get_width() or page_width
        local_x = x - max(0.0, (canvas_width - page_width) / 2)
        return index, local_x, local_y

    def _pdf_word_at(
        self, index: int, local_x: float, local_y: float, *, exact: bool = False
    ) -> int | None:
        """Index of the word at a point on a page.

        Two different questions share this code. `exact` asks "is the pointer
        actually on a word", which is what decides whether the caret cursor
        shows and whether a drag may start -- so pressing on a margin does
        nothing rather than silently selecting the nearest paragraph.
        Otherwise the nearest word wins, which is what an in-progress drag
        wants: sweeping through a margin or the gap between two lines should
        keep extending the selection, exactly as it does in any text view."""
        words = self._pdf_words.get(index + 1)
        if not words or index >= len(self._pdf_page_sizes):
            return None
        display_width, display_height = self._pdf_page_sizes[index]
        page = self._pdf_geometry[index]
        # Display pixels back into the points the boxes are stored in.
        x = local_x * page.display_width / max(1, display_width)
        y = local_y * page.display_height / max(1, display_height)

        if exact:
            line = self._pdf_line_at(index, x, y)
            if line is None:
                return None
            _y0, _y1, _x0, _x1, first, last = line
            # On this line: the word under the pointer, or whichever sits
            # closest to the gap it landed in.
            return min(
                range(first, last + 1),
                key=lambda position: (
                    0.0
                    if words[position].x0 <= x <= words[position].x1
                    else min(abs(x - words[position].x0), abs(x - words[position].x1))
                ),
            )

        best = None
        best_distance = None
        for position, word in enumerate(words):
            if word.x0 <= x <= word.x1 and word.y0 <= y <= word.y1:
                return position
            dy = 0.0 if word.y0 <= y <= word.y1 else min(abs(y - word.y0), abs(y - word.y1))
            dx = 0.0 if word.x0 <= x <= word.x1 else min(abs(x - word.x0), abs(x - word.x1))
            # Vertical distance dominates so the nearest word on the same
            # line wins over a closer one on the line above or below.
            distance = dy * 4.0 + dx
            if best_distance is None or distance < best_distance:
                best_distance = distance
                best = position
        return best

    def _pdf_line_at(
        self, index: int, x: float, y: float
    ) -> tuple[float, float, float, float, int, int] | None:
        """The line of text at a point on a page (points), or None.

        Horizontal containment is against the line's own span, which is what
        lets the space between two words still count as text without a margin
        ever doing so."""
        for line in self._pdf_lines.get(index + 1, ()):
            y0, y1, x0, x1, _first, _last = line
            if y0 - _PDF_LINE_HIT_SLACK <= y <= y1 + _PDF_LINE_HIT_SLACK and x0 <= x <= x1:
                return line
        return None

    @staticmethod
    def _pdf_build_lines(
        words: list[_PdfWord],
    ) -> list[tuple[float, float, float, float, int, int]]:
        """Collapse a page's words into per-line bands: (y0, y1, x0, x1,
        first word, last word). Words arrive already in reading order, so a
        line is a contiguous run and its bounds are just the extremes."""
        lines: list[tuple[float, float, float, float, int, int]] = []
        start = 0
        for position in range(len(words) + 1):
            ended = position == len(words) or words[position].line != words[start].line
            if not ended:
                continue
            if position > start:
                run = words[start:position]
                lines.append(
                    (
                        min(w.y0 for w in run),
                        max(w.y1 for w in run),
                        min(w.x0 for w in run),
                        max(w.x1 for w in run),
                        start,
                        position - 1,
                    )
                )
            start = position
        return lines

    def _on_pdf_motion(self, _controller: Gtk.EventControllerMotion, x: float, y: float) -> None:
        """Show the caret only where there is text to select, so the pointer
        says up front whether a drag will do anything here."""
        index, local_x, local_y = self._pdf_point_to_page(x, y)
        position = self._pdf_word_at(index, local_x, local_y, exact=True)
        over_text = position is not None
        words = self._pdf_words.get(index + 1) or []
        section = (
            _ocr_section_name(words[position].section_label)
            if position is not None and position < len(words)
            else None
        )
        if section != self._pdf_tooltip_section:
            self._pdf_tooltip_section = section
            help_text = _("Drag over detected text to select it")
            self._pdf_canvas.set_tooltip_text(f"{section} — {help_text}" if section else help_text)
        self._set_pdf_text_cursor(over_text)

    def _on_pdf_motion_leave(self, _controller: Gtk.EventControllerMotion) -> None:
        self._pdf_tooltip_section = None
        self._pdf_canvas.set_tooltip_text(None)
        self._set_pdf_text_cursor(False)

    def _set_pdf_text_cursor(self, over_text: bool) -> None:
        if over_text == self._pdf_text_cursor_shown:
            return
        self._pdf_text_cursor_shown = over_text
        # None restores whatever the parent would have used.
        self._pdf_canvas.set_cursor(self._pdf_caret_cursor if over_text else None)

    def _on_pdf_drag_begin(self, gesture: Gtk.GestureDrag, x: float, y: float) -> None:
        self._pdf_canvas.grab_focus()
        index, local_x, local_y = self._pdf_point_to_page(x, y)
        position = self._pdf_word_at(index, local_x, local_y, exact=True)
        if position is None:
            # Not on text: give the sequence up so the press stays available
            # to whatever else wants it, rather than starting a selection the
            # user never asked for by pressing on a margin or a picture.
            self._clear_pdf_selection()
            gesture.set_state(Gtk.EventSequenceState.DENIED)
            return
        self._pdf_sel_anchor = (index, position)
        self._pdf_sel_focus = (index, position)
        # A press alone is not yet a selection -- see _on_pdf_drag_end.
        self._pdf_drag_moved = False
        self._redraw_pdf_selection()

    def _on_pdf_drag_update(
        self, gesture: Gtk.GestureDrag, offset_x: float, offset_y: float
    ) -> None:
        if self._pdf_sel_anchor is None:
            return
        ok, start_x, start_y = gesture.get_start_point()
        if not ok:
            return
        # Nearest-word matching from here on: a drag that strays into a
        # margin should keep extending, not stop dead.
        index, local_x, local_y = self._pdf_point_to_page(start_x + offset_x, start_y + offset_y)
        position = self._pdf_word_at(index, local_x, local_y)
        if position is None:
            return
        focus = (index, position)
        if focus != self._pdf_sel_focus:
            self._pdf_drag_moved = True
            self._pdf_sel_focus = focus
            self._redraw_pdf_selection()

    def _on_pdf_drag_end(
        self, _gesture: Gtk.GestureDrag, _offset_x: float, _offset_y: float
    ) -> None:
        # A press that never moved is a click, and a click clears the
        # selection the way it does in any text view -- without this, simply
        # clicking a page would leave one word highlighted.
        if self._pdf_sel_anchor is not None and not self._pdf_drag_moved:
            self._clear_pdf_selection()

    def _clear_pdf_selection(self) -> None:
        had_selection = self._pdf_sel_anchor is not None
        self._pdf_sel_anchor = None
        self._pdf_sel_focus = None
        if had_selection:
            self._redraw_pdf_selection()

    def _redraw_pdf_selection(self) -> None:
        self._pdf_canvas.queue_draw()

    def _pdf_selection_bounds(self) -> tuple[tuple[int, int], tuple[int, int]] | None:
        """The selection as an ordered (page, word) pair, low to high."""
        if self._pdf_sel_anchor is None or self._pdf_sel_focus is None:
            return None
        anchor, focus = self._pdf_sel_anchor, self._pdf_sel_focus
        return (anchor, focus) if anchor <= focus else (focus, anchor)

    def _pdf_selected_range_on(self, index: int) -> tuple[int, int] | None:
        """Inclusive word range selected on one page, or None."""
        bounds = self._pdf_selection_bounds()
        if bounds is None:
            return None
        (start_page, start_word), (end_page, end_word) = bounds
        if index < start_page or index > end_page:
            return None
        words = self._pdf_words.get(index + 1)
        if not words:
            return None
        first = start_word if index == start_page else 0
        last = end_word if index == end_page else len(words) - 1
        return first, last

    def _draw_pdf_document(self, _area: Gtk.DrawingArea, cr, width: int, _height: int) -> None:
        """Paint only visible PDF pages on the single document canvas."""
        if not self._pdf_page_sizes:
            return
        vadj = self._pdf_scroll.get_vadjustment()
        top = max(0.0, vadj.get_value() - _PDF_PAGE_SPACING)
        bottom = (
            vadj.get_value()
            + (vadj.get_page_size() or self._pdf_scroll.get_height())
            + _PDF_PAGE_SPACING
        )
        first = self._pdf_page_at_offset(top)
        last = self._pdf_page_at_offset(bottom)

        for index in range(first, last + 1):
            page_width, page_height = self._pdf_page_sizes[index]
            page_x = max(0.0, (width - page_width) / 2)
            page_y = self._pdf_page_offsets[index]

            # A PDF page is paper, independent of the surrounding dark/light
            # theme. A subtle outline retains page boundaries in light mode.
            cr.set_source_rgb(1.0, 1.0, 1.0)
            cr.rectangle(page_x, page_y, page_width, page_height)
            cr.fill()

            pixbuf = self._pdf_page_pixbufs.get(index + 1)
            if pixbuf is not None:
                cr.save()
                cr.rectangle(page_x, page_y, page_width, page_height)
                cr.clip()
                cr.translate(page_x, page_y)
                cr.scale(
                    page_width / max(1, pixbuf.get_width()),
                    page_height / max(1, pixbuf.get_height()),
                )
                Gdk.cairo_set_source_pixbuf(cr, pixbuf, 0, 0)
                cr.paint()
                cr.restore()

            cr.set_source_rgba(0.0, 0.0, 0.0, 0.22)
            cr.set_line_width(1.0)
            cr.rectangle(page_x + 0.5, page_y + 0.5, page_width - 1, page_height - 1)
            cr.stroke()

            cr.save()
            cr.translate(page_x, page_y)
            self._draw_pdf_selection(cr, page_width, page_height, index)
            cr.restore()

    def _draw_pdf_selection(self, cr, width: int, height: int, index: int) -> None:
        selected = self._pdf_selected_range_on(index)
        if selected is None or index >= len(self._pdf_geometry):
            return
        words = self._pdf_words.get(index + 1) or []
        page = self._pdf_geometry[index]
        scale_x = width / max(1.0, page.display_width)
        scale_y = height / max(1.0, page.display_height)
        # A translucent wash, so the words stay readable through it.
        cr.set_source_rgba(0.21, 0.52, 0.89, 0.35)
        first, last = selected
        for word in words[first : last + 1]:
            cr.rectangle(
                word.x0 * scale_x,
                word.y0 * scale_y,
                (word.x1 - word.x0) * scale_x,
                (word.y1 - word.y0) * scale_y,
            )
        cr.fill()

    def selected_text(self) -> str:
        """The selected words as text, with the document's line breaks."""
        if (
            hasattr(self, "_preview_stack")
            and self._preview_stack.get_visible_child_name() == PREVIEW_SLOT_IMAGE
        ):
            return self._image_selected_text()
        bounds = self._pdf_selection_bounds()
        if bounds is None:
            return ""
        (start_page, _start_word), (end_page, _end_word) = bounds
        pieces: list[str] = []
        for index in range(start_page, end_page + 1):
            selected = self._pdf_selected_range_on(index)
            words = self._pdf_words.get(index + 1)
            if selected is None or not words:
                continue
            first, last = selected
            line = None
            for word in words[first : last + 1]:
                if line is not None and word.line != line:
                    pieces.append("\n")
                elif pieces and not pieces[-1].endswith("\n"):
                    pieces.append(" ")
                pieces.append(word.text)
                line = word.line
            pieces.append("\n")
        return "".join(pieces).strip()

    def copy_text_selection(self) -> bool:
        """Put the selected text on the clipboard. False if nothing is
        selected, so the caller can fall back to its own copy action."""
        text = self.selected_text()
        if not text:
            return False
        self.get_clipboard().set_content(Gdk.ContentProvider.new_for_value(text))
        return True

    def _pdf_section_text(self, page_index: int, position: int) -> str:
        words = self._pdf_words.get(page_index + 1) or []
        if position >= len(words) or words[position].section is None:
            return ""
        section = words[position].section
        section_words = [word for word in words if word.section == section]
        pieces: list[str] = []
        line = None
        for word in section_words:
            if line is not None and word.line != line:
                pieces.append("\n")
            elif pieces and not pieces[-1].endswith("\n"):
                pieces.append(" ")
            pieces.append(word.text)
            line = word.line
        return "".join(pieces).strip()

    def _on_pdf_context_pressed(
        self, gesture: Gtk.GestureClick, _n_press: int, x: float, y: float
    ) -> None:
        """Offer Copy only when the secondary click lands in the selection."""
        selected = self._pdf_selection_bounds()
        if selected is None:
            return
        index, local_x, local_y = self._pdf_point_to_page(x, y)
        word = self._pdf_word_at(index, local_x, local_y, exact=True)
        selected_on_page = self._pdf_selected_range_on(index)
        if word is None or selected_on_page is None:
            return
        first, last = selected_on_page
        if not first <= word <= last:
            return
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self._pdf_canvas.grab_focus()
        popover = Gtk.Popover()
        popover.set_has_arrow(True)
        popover.set_parent(self._pdf_canvas)
        menu = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        button = Gtk.Button(label=_native("Copy"))
        button.add_css_class("flat")

        def copy_and_close(_button) -> None:
            self.copy_text_selection()
            popover.popdown()

        def unparent_after_unmap(widget) -> None:
            def cleanup() -> bool:
                if widget.get_parent() is not None:
                    widget.unparent()
                return GLib.SOURCE_REMOVE

            GLib.idle_add(cleanup)

        button.connect("clicked", copy_and_close)
        menu.append(button)
        section_text = self._pdf_section_text(index, word)
        if section_text:
            words = self._pdf_words.get(index + 1) or []
            section_name = _ocr_section_name(words[word].section_label)
            section_button = Gtk.Button(
                label=_("Copy section") + (f" — {section_name}" if section_name else "")
            )
            section_button.add_css_class("flat")

            def copy_section_and_close(_button) -> None:
                self.get_clipboard().set_content(Gdk.ContentProvider.new_for_value(section_text))
                popover.popdown()

            section_button.connect("clicked", copy_section_and_close)
            menu.append(section_button)
        popover.set_child(menu)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        popover.set_pointing_to(rect)
        popover.connect("unmap", unparent_after_unmap)
        popover.popup()

    # ── Navigation and zoom controls ──────────────────────────────────────

    def _go_to_pdf_page(self, page: int) -> None:
        """Scroll so `page` starts at the top of the viewport."""
        page = max(1, min(self._pdf_total_pages, page))
        if not self._pdf_page_offsets:
            return
        vadj = self._pdf_scroll.get_vadjustment()
        target = self._pdf_page_offsets[page - 1]
        vadj.set_value(min(target, max(0.0, vadj.get_upper() - vadj.get_page_size())))

    def _on_pdf_prev_page(self, _btn: Gtk.Button) -> None:
        self._go_to_pdf_page(self._current_pdf_page() - 1)

    def _on_pdf_next_page(self, _btn: Gtk.Button) -> None:
        self._go_to_pdf_page(self._current_pdf_page() + 1)

    def _on_pdf_zoom_reset(self, _btn: Gtk.Button) -> None:
        self._set_pdf_zoom(100)

    def _on_pdf_zoom_in(self, _btn: Gtk.Button) -> None:
        self._set_pdf_zoom(self._pdf_zoom_pct + _PDF_ZOOM_PCT_STEP)

    def _on_pdf_zoom_out(self, _btn: Gtk.Button) -> None:
        self._set_pdf_zoom(self._pdf_zoom_pct - _PDF_ZOOM_PCT_STEP)

    def _apply_pdf_zoom(self, zoom_pct: int) -> None:
        """Shared tail for every way zoom can change: clamp, store, update the
        %-button label and +/- sensitivity, resize instantly from the pixbufs
        already in hand, and queue a sharper re-render behind that.

        Deliberately does not touch the slider's own value -- a drag already
        *is* the slider moving. Programmatic callers sync it via
        _set_pdf_zoom."""
        zoom_pct = max(_PDF_ZOOM_PCT_MIN, min(_PDF_ZOOM_PCT_MAX, zoom_pct))
        if zoom_pct == self._pdf_zoom_pct:
            return
        self._pdf_zoom_pct = zoom_pct
        self._btn_pdf_zoom.set_label(f"{zoom_pct}%")
        self._btn_pdf_zoom_out.set_sensitive(zoom_pct > _PDF_ZOOM_PCT_MIN)
        self._btn_pdf_zoom_in.set_sensitive(zoom_pct < _PDF_ZOOM_PCT_MAX)
        self._update_pdf_page_sizes()
        self._arm_pdf_quality_upgrade()

    def _set_pdf_zoom(self, zoom_pct: int) -> None:
        self._apply_pdf_zoom(zoom_pct)
        self._set_pdf_zoom_scale_value(self._pdf_zoom_pct)

    def _set_pdf_zoom_scale_value(self, zoom_pct: int) -> None:
        """set_value() without _on_pdf_zoom_scale_changed mistaking it for a
        user drag (it fires "value-changed" for a programmatic set too)."""
        self._pdf_setting_zoom_scale = True
        self._pdf_zoom_scale.set_value(zoom_pct)
        self._pdf_setting_zoom_scale = False

    def _on_pdf_zoom_scale_changed(self, scale: Gtk.Scale) -> None:
        if self._pdf_setting_zoom_scale:
            return
        self._apply_pdf_zoom(round(scale.get_value()))

    # ── EPUB reader ───────────────────────────────────────────────────────

    def _load_preview_epub(self, gfile: Gio.File, mtime: int) -> None:
        """Open an EPUB in the reader: chapter navigation and text zoom."""
        path = gfile.get_path()
        if not path or WebKit is None:
            # No local path (a GVfs location WebKit cannot resolve relative
            # links inside) or no HTML engine on this system -- fall back to
            # the file icon exactly as an unsupported type would.
            self.set_preview_slot(PREVIEW_SLOT_ICON)
            self._maybe_load_thumbnail("application/epub+zip", mtime)
            return

        self._epub_generation += 1
        generation = self._epub_generation
        self._clear_epub()
        self._epub_zoom_pct = 100
        self.set_preview_slot(PREVIEW_SLOT_LOADING)

        def _worker() -> None:
            result = _extract_epub(path, self._cancellable)
            built = None
            if result is not None:
                # Building the whole document up front is what makes the
                # reader scroll continuously (see _build_epub_document); it
                # is string work over already-extracted files, so it belongs
                # on this thread rather than blocking the UI.
                built = _build_epub_document(result[0], result[1], self._cancellable)
                if built is None:
                    shutil.rmtree(result[0], ignore_errors=True)
                    result = None
            if self._cancellable.is_cancelled():
                if result is not None:
                    shutil.rmtree(result[0], ignore_errors=True)
                return
            GLib.idle_add(self._on_epub_ready, result, built, mtime, generation)

        self._worker_futures.append(_PREVIEW_WORKER_EXECUTOR.submit(_worker))

    def _on_epub_ready(
        self,
        result: tuple[str, list[str], str] | None,
        built: tuple[str, list[str]] | None,
        mtime: int,
        generation: int,
    ) -> int:
        # A newer file (or a torn-down column) owns the view now; the work
        # this finished is for a document nobody is looking at.
        if generation != self._epub_generation or self._cancellable.is_cancelled():
            if result is not None:
                shutil.rmtree(result[0], ignore_errors=True)
            return GLib.SOURCE_REMOVE
        if result is None or built is None:
            self.set_preview_slot(PREVIEW_SLOT_ICON)
            self._maybe_load_thumbnail("application/epub+zip", mtime)
            return GLib.SOURCE_REMOVE

        combined, included = built
        self._epub_tmpdir = result[0]
        # The chapters the document actually contains, not every chapter the
        # spine listed -- see _build_epub_document.
        self._epub_chapters = included
        self._epub_doc_uri = GLib.filename_to_uri(combined, None)
        self._epub_index = 0
        self._ensure_epub_view()
        # _set_pdf_zoom's counterpart, so the slider returns to 100% with the
        # label; _apply_epub_zoom alone would leave it wherever the previous
        # book left it.
        self._set_epub_zoom(100)
        self._update_epub_chapter_controls()
        # The whole book is one document, so this is the only real load; the
        # chapter buttons only move within it from here on. The preview stays
        # on the spinner until WebKit commits the load (see
        # _on_epub_load_changed) -- switching here would show an empty view
        # for as long as parsing takes, which the PDF reader never does.
        self._epub_view.load_uri(self._epub_doc_uri)
        return GLib.SOURCE_REMOVE

    def _ensure_epub_view(self) -> None:
        """Build the reader on first use and register it with the preview
        stack. Deliberately lazy -- see the _epub_view field in __init__."""
        if self._epub_box is not None:
            return

        self._epub_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._epub_box.set_hexpand(True)
        self._epub_box.set_vexpand(True)

        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        toolbar.set_halign(Gtk.Align.CENTER)
        toolbar.add_css_class("mc-pdf-toolbar")

        self._btn_epub_prev = Gtk.Button.new_from_icon_name("go-previous-symbolic")
        self._btn_epub_prev.add_css_class("flat")
        self._btn_epub_prev.set_tooltip_text(_("Previous chapter"))
        self._btn_epub_prev.connect("clicked", self._on_epub_prev_chapter)

        self._lbl_epub_chapter = Gtk.Label(label="1 / 1")
        self._lbl_epub_chapter.add_css_class("caption")

        self._btn_epub_next = Gtk.Button.new_from_icon_name("go-next-symbolic")
        self._btn_epub_next.add_css_class("flat")
        self._btn_epub_next.set_tooltip_text(_("Next chapter"))
        self._btn_epub_next.connect("clicked", self._on_epub_next_chapter)

        self._btn_epub_zoom_out = Gtk.Button.new_from_icon_name("zoom-out-symbolic")
        self._btn_epub_zoom_out.add_css_class("flat")
        self._btn_epub_zoom_out.connect("clicked", self._on_epub_zoom_out)

        self._epub_zoom_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, _EPUB_ZOOM_PCT_MIN, _EPUB_ZOOM_PCT_MAX, 1
        )
        self._epub_zoom_scale.set_draw_value(False)
        self._epub_zoom_scale.set_value(100)
        self._epub_zoom_scale.set_size_request(120, -1)
        self._epub_zoom_scale.connect("value-changed", self._on_epub_zoom_scale_changed)
        self._epub_setting_zoom_scale = False

        self._btn_epub_zoom_in = Gtk.Button.new_from_icon_name("zoom-in-symbolic")
        self._btn_epub_zoom_in.add_css_class("flat")
        self._btn_epub_zoom_in.connect("clicked", self._on_epub_zoom_in)

        self._btn_epub_zoom = Gtk.Button(label="100%")
        self._btn_epub_zoom.add_css_class("flat")
        self._btn_epub_zoom.add_css_class("caption")
        self._btn_epub_zoom.set_tooltip_text(_("Reset zoom"))
        self._btn_epub_zoom.connect("clicked", self._on_epub_zoom_reset)

        toolbar.append(self._btn_epub_prev)
        toolbar.append(self._lbl_epub_chapter)
        toolbar.append(self._btn_epub_next)
        toolbar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        toolbar.append(self._btn_epub_zoom_out)
        toolbar.append(self._epub_zoom_scale)
        toolbar.append(self._btn_epub_zoom_in)
        toolbar.append(self._btn_epub_zoom)

        # The reader script posts the visible chapter back through this
        # channel (see _EPUB_READER_JS / _on_epub_script_message).
        content_manager = WebKit.UserContentManager()
        content_manager.register_script_message_handler("mcReader", None)
        content_manager.connect("script-message-received::mcReader", self._on_epub_script_message)

        self._epub_view = WebKit.WebView(user_content_manager=content_manager)
        self._epub_view.set_hexpand(True)
        self._epub_view.set_vexpand(True)
        settings = self._epub_view.get_settings()
        # An EPUB is untrusted third-party content, and JavaScript is on only
        # because the chapter counter cannot follow the scroll position
        # without it. What makes that safe is not this switch but the
        # Content-Security-Policy the generated document carries: scripts run
        # only with a per-document nonce the book's own markup never has, and
        # connect-src is denied outright, so nothing can reach the network.
        # See _build_epub_document. Navigation is separately confined to the
        # extracted files by _on_epub_decide_policy below.
        settings.set_enable_javascript(True)
        for setter in (
            "set_enable_html5_database",
            "set_enable_html5_local_storage",
            "set_enable_developer_extras",
        ):
            # Property coverage varies across WebKitGTK versions; each is a
            # hardening extra, never required for the preview to work.
            try:
                getattr(settings, setter)(False)
            except (AttributeError, TypeError):
                pass
        self._epub_view.connect("decide-policy", self._on_epub_decide_policy)
        self._epub_view.connect("load-changed", self._on_epub_load_changed)

        self._epub_box.append(toolbar)
        self._epub_box.append(self._epub_view)
        self._preview_stack.add_named(self._epub_box, PREVIEW_SLOT_EPUB)

    def _on_epub_decide_policy(self, _view, decision, decision_type) -> bool:
        """Keep the reader inside the unpacked book.

        A chapter can link anywhere -- to the web, or to a mail client. In a
        preview pane nothing should follow those: a click that quietly
        fetched a remote URL would both leak that the file was opened and
        hand an untrusted document a network request. Only file:// loads
        from our own temporary directory proceed."""
        if decision_type != WebKit.PolicyDecisionType.NAVIGATION_ACTION:
            return False
        try:
            uri = decision.get_navigation_action().get_request().get_uri() or ""
        except Exception:
            return False
        allowed = False
        if uri.startswith("file://") and self._epub_tmpdir:
            path = GLib.filename_from_uri(uri)[0]
            allowed = _path_is_within(self._epub_tmpdir, path)
        if not allowed:
            decision.ignore()
            return True
        return False

    def _on_epub_load_changed(self, _view, load_event) -> None:
        """Reveal the reader only once WebKit has content to draw.

        Committed rather than finished: the document is parsed and painting
        from here, whereas waiting for finished would hold the spinner until
        every image in a long book had loaded."""
        if load_event != WebKit.LoadEvent.COMMITTED:
            return
        if self._cancellable.is_cancelled() or not self._epub_chapters:
            return
        self.set_preview_slot(PREVIEW_SLOT_EPUB)

    def _on_epub_script_message(self, _manager, message) -> None:
        """Chapter reported by the in-document reader script.

        This is what keeps the counter honest while scrolling, and it is also
        what makes the chapter buttons step from where the reader actually is
        -- exactly like the PDF's page buttons, which read the scroll
        position rather than the last jump."""
        try:
            # WebKit 6 hands over a JSCValue; older bindings wrap it in a
            # JavascriptResult first.
            value = message.get_js_value() if hasattr(message, "get_js_value") else message
            index = int(value.to_int32())
        except Exception:
            return
        if not self._epub_chapters:
            return
        index = max(0, min(len(self._epub_chapters) - 1, index))
        if index == self._epub_index:
            return
        self._epub_index = index
        self._update_epub_chapter_controls()

    def _show_epub_chapter(self, index: int) -> None:
        """Jump to a chapter *within* the combined document.

        A fragment load, not a fresh page load: the book is already all in
        one document, so this only moves the scroll position, leaving the
        reader free to keep scrolling straight on into the next chapter."""
        if not self._epub_chapters or self._epub_view is None or not self._epub_doc_uri:
            return
        index = max(0, min(len(self._epub_chapters) - 1, index))
        self._epub_index = index
        self._epub_view.load_uri(f"{self._epub_doc_uri}#mc-ch-{index}")
        self._update_epub_chapter_controls()

    def _update_epub_chapter_controls(self) -> None:
        total = len(self._epub_chapters)
        self._lbl_epub_chapter.set_label(f"{self._epub_index + 1} / {total}")
        self._btn_epub_prev.set_sensitive(self._epub_index > 0)
        self._btn_epub_next.set_sensitive(self._epub_index < total - 1)

    def _on_epub_prev_chapter(self, _btn: Gtk.Button) -> None:
        self._show_epub_chapter(self._epub_index - 1)

    def _on_epub_next_chapter(self, _btn: Gtk.Button) -> None:
        self._show_epub_chapter(self._epub_index + 1)

    def _apply_epub_zoom(self, zoom_pct: int) -> None:
        """WebKit reflows the text itself, so unlike the PDF viewer this needs
        no re-render and no debounce -- it is already instant."""
        zoom_pct = max(_EPUB_ZOOM_PCT_MIN, min(_EPUB_ZOOM_PCT_MAX, zoom_pct))
        self._epub_zoom_pct = zoom_pct
        if self._epub_view is not None:
            self._epub_view.set_zoom_level(zoom_pct / 100.0)
        self._btn_epub_zoom.set_label(f"{zoom_pct}%")
        self._btn_epub_zoom_out.set_sensitive(zoom_pct > _EPUB_ZOOM_PCT_MIN)
        self._btn_epub_zoom_in.set_sensitive(zoom_pct < _EPUB_ZOOM_PCT_MAX)

    def _set_epub_zoom(self, zoom_pct: int) -> None:
        self._apply_epub_zoom(zoom_pct)
        self._epub_setting_zoom_scale = True
        self._epub_zoom_scale.set_value(self._epub_zoom_pct)
        self._epub_setting_zoom_scale = False

    def _on_epub_zoom_scale_changed(self, scale: Gtk.Scale) -> None:
        if self._epub_setting_zoom_scale:
            return
        self._apply_epub_zoom(round(scale.get_value()))

    def _on_epub_zoom_in(self, _btn: Gtk.Button) -> None:
        self._set_epub_zoom(self._epub_zoom_pct + _EPUB_ZOOM_PCT_STEP)

    def _on_epub_zoom_out(self, _btn: Gtk.Button) -> None:
        self._set_epub_zoom(self._epub_zoom_pct - _EPUB_ZOOM_PCT_STEP)

    def _on_epub_zoom_reset(self, _btn: Gtk.Button) -> None:
        self._set_epub_zoom(100)

    def _clear_epub(self) -> None:
        """Drop the unpacked book. The temporary directory is ours alone, so
        nothing else can be relying on it once the reader moves on.

        getattr-guarded throughout: destroy_enumeration calls this for the
        empty placeholder column as well, which never ran the preview setup
        that defines these fields."""
        tmpdir = getattr(self, "_epub_tmpdir", None)
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
        if hasattr(self, "_epub_tmpdir"):
            self._epub_tmpdir = None
            self._epub_doc_uri = None
            self._epub_chapters = []
            self._epub_index = 0

    def _load_preview_text(self, gfile: Gio.File) -> None:
        """Stream only enough of a text file to build the 100-line preview."""
        chunks: list[bytes] = []
        byte_count = 0
        line_count = 0

        def apply_chunks() -> None:
            text = b"".join(chunks).decode("utf-8", errors="replace")
            self._apply_text_preview("\n".join(text.splitlines()[:100]))

        def on_chunk_ready(stream: Gio.InputStream, result: Gio.AsyncResult, _data=None) -> None:
            nonlocal byte_count, line_count
            try:
                data = bytes(stream.read_bytes_finish(result).get_data())
            except GLib.Error:
                stream.close_async(GLib.PRIORITY_DEFAULT, None, lambda *_args: None)
                if not self._cancellable.is_cancelled():
                    self._show_icon()
                return
            if self._cancellable.is_cancelled():
                stream.close_async(GLib.PRIORITY_DEFAULT, None, lambda *_args: None)
                return
            if data:
                chunks.append(data)
                byte_count += len(data)
                line_count += data.count(b"\n")
            if not data or line_count >= 100 or byte_count >= _TEXT_PREVIEW_MAX_BYTES:
                stream.close_async(GLib.PRIORITY_DEFAULT, self._cancellable, lambda *_args: None)
                apply_chunks()
                return
            stream.read_bytes_async(
                min(64 * 1024, _TEXT_PREVIEW_MAX_BYTES - byte_count),
                GLib.PRIORITY_DEFAULT,
                self._cancellable,
                on_chunk_ready,
                None,
            )

        def on_stream_ready(source: Gio.File, result: Gio.AsyncResult, _data=None) -> None:
            try:
                stream = source.read_finish(result)
            except GLib.Error:
                self._show_icon()
                return
            stream.read_bytes_async(
                64 * 1024,
                GLib.PRIORITY_DEFAULT,
                self._cancellable,
                on_chunk_ready,
                None,
            )

        gfile.read_async(GLib.PRIORITY_DEFAULT, self._cancellable, on_stream_ready, None)

    def _apply_text_preview(self, text: str) -> None:
        if self._cancellable.is_cancelled():
            return
        self._text_buffer.set_text(text)
        self.set_preview_slot(PREVIEW_SLOT_DOCUMENT)

    def _maybe_load_thumbnail(self, content_type: str | None, mtime: int) -> None:
        """Show a native thumbnail for the file when GNOME or video fallback can make one."""
        if not content_type:
            return
        uri = self.file_uri
        if _thumb_factory is not None:
            cached = _thumb_factory.lookup(uri, mtime)
            if cached:
                self._show_thumbnail_from_file(cached)
                return
            if _thumb_factory.has_valid_failed_thumbnail(
                uri, mtime
            ) and not content_type.startswith("video/"):
                return
            if not _thumb_factory.can_thumbnail(
                uri, content_type, mtime
            ) and not content_type.startswith("video/"):
                return
        elif not content_type.startswith("video/"):
            return

        self._worker_futures.append(
            _PREVIEW_WORKER_EXECUTOR.submit(self._thumbnail_worker, uri, content_type, mtime)
        )

    def _thumbnail_worker(self, uri: str, content_type: str, mtime: int) -> None:
        pixbuf = None
        if _thumb_factory is not None:
            try:
                pixbuf = _thumb_factory.generate_thumbnail(uri, content_type, self._cancellable)
            except GLib.Error:
                pixbuf = None

        if pixbuf is None and content_type and content_type.startswith("video/"):
            path = Gio.File.new_for_uri(uri).get_path()
            if path:
                pixbuf = _generate_video_thumbnail_fallback(path, self._cancellable)
        elif pixbuf is None and content_type == "application/pdf":
            path = Gio.File.new_for_uri(uri).get_path()
            if path:
                pixbuf = _render_pdf_page_at_zoom(path, 1, 300, self._cancellable)

        if self._cancellable.is_cancelled():
            return

        if pixbuf is None:
            if _thumb_factory is not None:
                try:
                    _thumb_factory.create_failed_thumbnail(uri, mtime, self._cancellable)
                except GLib.Error:
                    pass
            return

        if _thumb_factory is not None:
            try:
                _thumb_factory.save_thumbnail(pixbuf, uri, mtime, self._cancellable)
            except GLib.Error:
                pass
        GLib.idle_add(self._apply_thumbnail, Gdk.Texture.new_for_pixbuf(pixbuf))

    def _show_thumbnail_from_file(self, path: str) -> None:
        try:
            texture = Gdk.Texture.new_from_filename(path)
        except GLib.Error:
            return
        self._apply_thumbnail(texture)

    def _apply_thumbnail(self, texture: Gdk.Texture) -> int:
        if self._cancellable.is_cancelled():
            return GLib.SOURCE_REMOVE
        self._image_aspect_ratio = texture.get_width() / texture.get_height()
        self._thumb_frame.set_ratio(self._image_aspect_ratio)
        self._set_image_zoom(self._image_zoom_pct)
        self._thumb.set_paintable(texture)
        self.set_preview_slot(PREVIEW_SLOT_IMAGE)
        return GLib.SOURCE_REMOVE

    def set_preview_slot(self, slot: str) -> None:
        """Show a named preview surface from the stable preview stack.

        Keeping this state transition on the widget mirrors
        ``MyComputerColumnRow.set_cut()`` / ``set_thumbnail()``: callers
        request a semantic state without depending on its Gtk.Stack layout.
        """
        if slot not in {
            PREVIEW_SLOT_LOADING,
            PREVIEW_SLOT_ICON,
            PREVIEW_SLOT_IMAGE,
            PREVIEW_SLOT_VIDEO,
            PREVIEW_SLOT_DOCUMENT,
            PREVIEW_SLOT_PDF,
            PREVIEW_SLOT_EPUB,
            PREVIEW_SLOT_SPREADSHEET,
            PREVIEW_SLOT_ARCHIVE,
        }:
            raise ValueError(f"unknown preview slot: {slot}")
        self._preview_stack.set_visible_child_name(slot)
        # Put the ownership marker on the whole preview column, rather than
        # relying on WebKit's internal picked child hierarchy. WebKit may use
        # a separate native surface, but the column's event bounds remain
        # stable and the Miller capture controller can recognize it reliably.
        if slot == PREVIEW_SLOT_SPREADSHEET:
            self.add_css_class("mc-horizontal-scroll-owner")
        else:
            self.remove_css_class("mc-horizontal-scroll-owner")

    def destroy_enumeration(self) -> None:
        self._cancellable.cancel()
        if self._spreadsheet_scroll_idle_id:
            GLib.source_remove(self._spreadsheet_scroll_idle_id)
            self._spreadsheet_scroll_idle_id = 0
        self._spreadsheet_scroll_delta = 0.0
        for future in self._worker_futures:
            future.cancel()
        self._worker_futures.clear()
        for future in getattr(self, "_pdf_render_futures", {}).values():
            future.cancel()
        for future in getattr(self, "_pdf_word_futures", {}).values():
            future.cancel()
        self._stop_video()
        self._cancel_pdf_quality_upgrade()
        self._cancel_pdf_viewport_refit()
        self._cancel_pdf_visible_update()
        viewport_tick_id = getattr(self, "_pdf_viewport_tick_id", 0)
        if viewport_tick_id:
            self._pdf_scroll.remove_tick_callback(viewport_tick_id)
            self._pdf_viewport_tick_id = 0
        # Stops the web process and releases the unpacked book -- a column is
        # discarded on every file click, so leaking either would accumulate
        # fast. getattr-guarded like _cancel_pdf_quality_upgrade: this runs
        # for the empty placeholder column too, whose __init__ returns before
        # any of the preview state exists.
        epub_view = getattr(self, "_epub_view", None)
        if epub_view is not None:
            epub_view.stop_loading()
        self._clear_epub()
        self._archive_generation += 1
        self._archive_listing = None
        if hasattr(self, "_archive_store"):
            self._archive_store.remove_all()
        self._cleanup_spreadsheet_preview()
        self._cleanup_document_preview()
        self._cleanup_staged_preview()
