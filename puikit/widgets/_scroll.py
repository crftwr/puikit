"""Shared scroll math for widgets that jump to a search match.

One rule, used by MarkdownView, JsonView and TableView (and mirrored by xefm's
raw text / diff viewers): a match already sitting comfortably inside the
viewport — at least a small margin of context from both edges — does not
scroll at all; one outside that band is centered vertically, so a far jump
lands with equal context above and below instead of pinned to an edge.
"""

from __future__ import annotations


def search_scroll_offset(offset: float, view_h: float, row_top: float,
                         row_h: float, margin: float, snap: float = 0.0) -> float:
    """Viewport ``offset`` after jumping to a search match.

    ``row_top``/``row_h`` describe the matched row in content coordinates and
    ``view_h`` the viewport height, all in the same units. Returns ``offset``
    unchanged when the row lies at least ``margin`` inside both viewport edges
    (the margin shrinks when the row nearly fills the viewport, so the row
    itself always fits); otherwise the offset that centers the row vertically.
    A row taller than the viewport aligns its top edge instead.

    ``snap`` quantizes the centering shift down to a whole number of that
    quantum (pass the widget's row pitch), so the viewport keeps resting on a
    row boundary — a character grid never lands between cells; ``0`` centers
    exactly. The caller clamps the result to its content bounds, which is what
    lets the context collapse at the document's very start and end.
    """
    if row_h >= view_h:
        return row_top
    m = min(margin, (view_h - row_h) / 2)
    if offset + m <= row_top and row_top + row_h <= offset + view_h - m:
        return offset
    shift = (view_h - row_h) / 2
    if snap > 0:
        shift = snap * int(shift / snap)
    return row_top - shift
