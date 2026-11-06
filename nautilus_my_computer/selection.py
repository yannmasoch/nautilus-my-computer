"""Stateless selection rules for one file listing (#178).

A selection is a set of URIs plus a Shift-range anchor. Identity is the URI,
never a row position, so a selection survives a reload or a re-sort. Nothing
here knows about GTK, columns, or the Miller path: the view decides which
listing is active, then asks these functions what the selection becomes.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum


class Kind(Enum):
    EMPTY = "empty"
    SINGLE = "single"
    MULTI = "multi"


@dataclass(frozen=True)
class Selection:
    uris: frozenset[str] = frozenset()
    # Where a Shift-click range starts: the last URI clicked.
    anchor: str | None = None

    @property
    def kind(self) -> Kind:
        if not self.uris:
            return Kind.EMPTY
        return Kind.SINGLE if len(self.uris) == 1 else Kind.MULTI


def click(sel: Selection, uri: str) -> Selection:
    """Plain click: select only this URI."""
    return Selection(frozenset((uri,)), uri)


def ctrl_click(sel: Selection, uri: str) -> Selection:
    """Ctrl-click: add or remove one URI, leaving the rest alone."""
    return Selection(sel.uris ^ {uri}, uri)


def shift_click(
    sel: Selection, uri: str, ordered: Sequence[str], *, extend: bool = False
) -> Selection:
    """Shift-click: select from the anchor to this URI in display order.
    Without extend (Ctrl+Shift adds to the selection) every URI outside the
    range is dropped. The anchor stays where it was."""
    if sel.anchor is None or sel.anchor not in ordered or uri not in ordered:
        return Selection(sel.uris | {uri} if extend else frozenset((uri,)), uri)
    a, b = ordered.index(sel.anchor), ordered.index(uri)
    span = frozenset(ordered[min(a, b) : max(a, b) + 1])
    return Selection(sel.uris | span if extend else span, sel.anchor)


def cursor_index(sel: Selection, ordered: Sequence[str]) -> int | None:
    """Position of the moving end of a Shift range: the selected row furthest
    from the anchor. With one row selected, or the anchor in the middle of the
    selection, that is the anchor itself. None when nothing listed is selected."""
    positions = [i for i, uri in enumerate(ordered) if uri in sel.uris]
    if not positions:
        return None
    if sel.anchor is None or sel.anchor not in ordered:
        return positions[0]
    anchor = ordered.index(sel.anchor)
    if anchor == positions[0]:
        return positions[-1]
    if anchor == positions[-1]:
        return positions[0]
    return anchor


def extend_to(sel: Selection, ordered: Sequence[str], index: int) -> Selection:
    """Shift+arrow: select from the anchor to the row at `index`, so the range
    grows or shrinks as the cursor moves. Starts from `index` alone when there
    is no anchor yet."""
    return shift_click(sel, ordered[index], ordered)


def select_all(ordered: Sequence[str]) -> Selection:
    return Selection(frozenset(ordered))


def clear(sel: Selection) -> Selection:
    """Drop the whole selection. Has no navigation side effects."""
    return Selection()


def target(sel: Selection, uri: str) -> Selection:
    """What a drag or a context menu on `uri` operates on: the whole group when
    the URI is inside it, else only that URI."""
    return sel if uri in sel.uris else click(sel, uri)


def reconcile(sel: Selection, ordered: Sequence[str]) -> Selection:
    """After a reload or a sort: keep URIs still listed, drop vanished ones."""
    present = frozenset(ordered)
    anchor = sel.anchor if sel.anchor in present else None
    return Selection(sel.uris & present, anchor)


def ordered_uris(sel: Selection, ordered: Sequence[str]) -> tuple[str, ...]:
    """The selected URIs in display order (for the stack preview and file ops)."""
    return tuple(uri for uri in ordered if uri in sel.uris)


class PendingSelection:
    """A Column View selection waiting to be written into the native model of
    one folder (the cross-view bridge). Nautilus stays the authority: the
    request is applied at most once, never partially, and dropped as soon as
    Nautilus or the user changes the native selection."""

    def __init__(self) -> None:
        self.folder: str | None = None
        self.uris: frozenset[str] = frozenset()

    @property
    def active(self) -> bool:
        return self.folder is not None

    def set(self, folder: str, uris) -> None:
        self.folder = folder
        self.uris = frozenset(uris)

    def drop(self) -> None:
        self.folder = None
        self.uris = frozenset()

    def resolve(self, available: frozenset[str], *, final: bool) -> frozenset[str] | None:
        """URIs to write now, or None. Only a complete match is ever written. A
        partial match stays pending until `final` (the folder finished loading,
        so a missing file is really gone), then is dropped unwritten."""
        if self.folder is None:
            return None
        if self.uris <= available:
            ready = self.uris
            self.drop()
            return ready
        if final:
            self.drop()
        return None


def adopt_native(*, loading: bool, writing: bool, pending: bool) -> bool:
    """Whether a native selection change should be copied into Column View: not
    while the folder loads (the model's empty/refill and Nautilus's own restore
    are transitions), not our own write, and not while our request is pending.
    An empty selection is a real selection and is adopted."""
    return not (loading or writing or pending)
