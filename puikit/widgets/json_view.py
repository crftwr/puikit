"""A scrollable, collapsible tree view of parsed JSON data.

``JsonView`` renders already-parsed Python data (the result of ``json.loads`` /
``json.JSONDecoder``) as an indented tree: objects and arrays are collapsible
branches, scalars are leaves. Each row shows an optional key (an object key or an
array index) and either the scalar value — colored by type (string / number /
bool-null) — or a ``{n}`` / ``[n]`` summary of a container's size. It behaves
like ``TreeView`` for navigation (arrow keys move / expand / collapse, page /
home / end, a wheel scrolls, a click toggles the expander) and adds two things a
plain tree lacks: **per-type coloring** and the **incremental-search protocol**
(``search_*``) a host file viewer drives from its search bar. ``Cmd/Ctrl+C``
copies the selected node's value as compact JSON.

Rows use a fixed-advance (monospace) face so a search highlight lands on the same
columns it does on a terminal. A long value is reachable two ways: unwrapped
(the default) the view pans horizontally — a horizontal wheel / trackpad swipe or
``Shift+←/→``, with a horizontal scrollbar while a row overflows — and
:meth:`JsonView.toggle_wrap` folds every row into the width instead, a long
value continuing on extra display lines aligned under where its label starts
(a host file viewer binds its line-wrap key to it, like the search protocol).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..backend import DEFAULT_STYLE, Style
from ..event import Event, EventType
from ..font import Font
from ..panel import DrawContext
from ..text import display_width, glyph_runs, truncate_to_width, wrap_text
from ._input import MultiClickTracker, is_activate
from ._scroll import search_scroll_offset
from .base import Widget, draw_list_row, selected_row_style

_INDENT = 2  # columns per depth level
_EXPANDED = "▾ "
_COLLAPSED = "▸ "
_LEAF = "  "

# GUI/vector disclosure chevron. On a vector backend a branch's mark is stroked as
# a crisp ›/⌄ chevron (``ctx.draw_chevron``, width ``_MARK_W``) in a reserved slot;
# a character grid keeps the inline ▸/▾ glyph (the constants above). The slot is a
# whole ``_MARK_SLOT`` columns wide — the same width as the inline "▸ " glyph — so
# the key/value text starts at the *same* integer column on both backends (which
# also keeps the colored segments on integer columns, off the sub-cell grid).
_MARK_W = 1.1
_MARK_SLOT = 2

#: Columns one Shift+←/→ press pans an unwrapped view (the text viewer's step).
_PAN_STEP = 4

#: Content is drawn in a fixed-advance face so a column maps to one base unit —
#: search highlights and the depth indent line up on the GUI as on the TUI.
_MONO = Font(monospace=True)

#: Type → RGB, the default value palette (VS Code Dark+), mirroring the text
#: viewer / MarkdownView code palette. A theme recolors any subset through its
#: ``extras['syntax']`` (keys ``name`` / ``string`` / ``number`` / ``keyword``);
#: the muted roles (index, summary, punctuation, indent marker) follow the
#: theme's ``muted_text``.
_DEFAULT_PALETTE = {
    "key": (156, 220, 254),      # object key            (syntax 'name')
    "string": (206, 145, 120),   # "quoted string"
    "number": (181, 206, 168),   # 42, 3.14
    "keyword": (86, 156, 214),   # true / false / null
    "punct": (212, 212, 212),    # the ": " separator
    "index": (157, 157, 157),    # array index
    "summary": (157, 157, 157),  # {n} / [n]
    "muted": (120, 120, 130),    # indent + expander marker
}

#: Search-match highlight = the content background blended toward amber, firmer
#: for the current match. Derived (not a fixed constant) so it tracks the theme.
_MATCH_HUE = (200, 175, 55)
_MATCH_TINT = 0.24
_CURRENT_MATCH_TINT = 0.46


def _mix(a, b, t):
    """Linear RGB blend a→b by ``t`` (0..1)."""
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _is_light(bg) -> bool:
    """True when ``bg`` is a light surface (Rec.601 luma). Lets the value palette
    stay exact on a dark theme (its tuned home) but be re-toned by auto-ink on a
    light one, where the dark-tuned colors would be unreadable."""
    if bg is None:
        return False
    return (0.299 * bg[0] + 0.587 * bg[1] + 0.114 * bg[2]) >= 140


def _match_bg(content, current: bool):
    """Search-highlight background for ``content``, firmer for the current match."""
    return _mix(content or (30, 30, 38), _MATCH_HUE,
                _CURRENT_MATCH_TINT if current else _MATCH_TINT)


@dataclass
class _Node:
    """One parsed JSON node: an object / array / scalar, its key (an object key,
    an array index, or ``None`` at the root), the raw value, its children (empty
    for a scalar), and whether the branch is expanded."""

    key: Any                 # str (object key) | int (array index) | None (root)
    kind: str                # "object" | "array" | "scalar"
    value: Any
    children: list["_Node"] = field(default_factory=list)
    expanded: bool = False
    label: str | None = None  # cached key + value text (values never change)

    @property
    def is_branch(self) -> bool:
        return self.kind in ("object", "array") and bool(self.children)


def _build(value: Any, key: Any) -> _Node:
    """Recursively wrap parsed ``value`` (under ``key``) into a ``_Node`` tree."""
    if isinstance(value, dict):
        node = _Node(key, "object", value)
        node.children = [_build(v, k) for k, v in value.items()]
    elif isinstance(value, list):
        node = _Node(key, "array", value)
        node.children = [_build(v, i) for i, v in enumerate(value)]
    else:
        node = _Node(key, "scalar", value)
    return node


def _slice_segs(segs: list[tuple[str, Any]], cs: int, ce: int) -> list[tuple[str, Any]]:
    """The sub-run [cs, ce) — character indices over the segments' concatenation
    — of colored ``(text, color)`` segments, for one wrapped chunk of a row."""
    out: list[tuple[str, Any]] = []
    pos = 0
    for text, color in segs:
        end = pos + len(text)
        a, b = max(pos, cs), min(end, ce)
        if b > a:
            out.append((text[a - pos:b - pos], color))
        pos = end
    return out


def _window_text(text: str, origin: float, lo: float, hi: float) -> tuple[float, str]:
    """The part of ``text`` — a flow whose first display column is ``origin`` —
    visible in the column window [lo, hi), and the window-relative x where it
    starts (column ``c`` lands at ``c - lo``). A wide glyph straddling either
    edge is dropped, its cells left blank, like the text viewer's pan."""
    if origin < lo:
        skip = lo - origin
        if len(text) == display_width(text):        # all-narrow fast path
            n = int(skip)
            text, origin = text[n:], origin + n
        else:
            w = 0.0
            runs = glyph_runs(text)
            i = 0
            while i < len(runs) and w < skip:
                w += display_width(runs[i])
                i += 1
            text, origin = "".join(runs[i:]), origin + w
    avail = hi - origin
    if avail <= 0 or not text:
        return (max(0.0, origin - lo), "")
    if display_width(text) > avail:
        text = truncate_to_width(text, int(avail))
    return (origin - lo, text)


class JsonView(Widget):
    focusable = True

    def __init__(self, value: Any, *, style: Style = DEFAULT_STYLE):
        self.style = style
        # A top-level container shows its entries at depth 0 (no synthetic root
        # row); a bare scalar document shows a single leaf.
        root = _build(value, None)
        self.roots: list[_Node] = root.children if root.kind in ("object", "array") else [root]

        self.selected = 0
        self.offset: float = 0.0          # first visible display line, base units
        self._row_h: float = 1.0
        self._viewport_h = 1
        self._view_h: float = 1.0
        self._panel: Any = None

        # Long values: ``wrap`` folds each row into the content width over
        # several display lines; unwrapped, ``left`` pans the view horizontally
        # (columns; float for smooth trackpad accumulation, whole columns drawn).
        self.wrap = False
        self.left: float = 0.0
        # Display-line layout, rebuilt lazily (see _ensure_layout): with wrap on,
        # ``_lines`` maps each display line to its (row, char start, char end)
        # chunk of the row's label and ``_row_line`` holds each row's first
        # display line; with wrap off, ``_max_w`` is the widest row in columns
        # (for the pan clamp + horizontal bar). ``_gen`` is bumped by every
        # expand / collapse so the cache key notices a structure change.
        self._lines: list[tuple[int, int, int]] = []
        self._row_line: list[int] = []
        self._max_w = 0
        self._gen = 0
        self._layout_key: tuple | None = None
        self._text_w = 80                 # content columns, from the last draw

        # Incremental search (a host viewer drives it through the ``search_*``
        # methods). ``_matches`` is the ordered ``(row_index, node)`` set after
        # ancestors of every hit have been expanded so the match is reachable;
        # ``_match_ids`` is the identity set the row highlight tests against;
        # ``_origin`` is the pre-search scroll, restored on cancel.
        self._pattern = ""
        self._matches: list[tuple[int, _Node]] = []
        self._match_ids: set[int] = set()
        self._search_pos = -1
        self._origin: float = 0.0
        self._origin_node: _Node | None = None  # pre-search selection (by identity)

        # Click / double-click tracking (a click toggles the expander or selects
        # a row; kept for parity with the other selectable views).
        self._clicks: MultiClickTracker[int] = MultiClickTracker()

    # --- flattening -----------------------------------------------------------

    def _visible(self) -> list[tuple[_Node, int]]:
        """(node, depth) for every currently-visible node, in display order."""
        out: list[tuple[_Node, int]] = []

        def walk(nodes: list[_Node], depth: int) -> None:
            for node in nodes:
                out.append((node, depth))
                if node.expanded and node.children:
                    walk(node.children, depth + 1)

        walk(self.roots, 0)
        return out

    # --- display-line layout --------------------------------------------------

    def _ensure_layout(self, rows: list[tuple[_Node, int]], text_w: int) -> None:
        """(Re)build the display-line layout lazily: with wrap on, each row's
        wrapped chunk spans at ``text_w`` columns; with wrap off, the widest row
        for the pan clamp + horizontal bar. Keyed on the wrap state, the width
        (which only matters while wrapping), and the expansion generation, so
        expanding / collapsing / resizing rebuilds and everything else reuses
        the cache."""
        key = (self.wrap, text_w if self.wrap else 0, self._gen)
        if key == self._layout_key:
            return
        self._layout_key = key
        self._lines = []
        self._row_line = []
        self._max_w = 0
        if not self.wrap:
            for node, depth in rows:
                w = depth * _INDENT + _MARK_SLOT + display_width(self._label_text(node))
                if w > self._max_w:
                    self._max_w = w
            return
        for i, (node, depth) in enumerate(rows):
            self._row_line.append(len(self._lines))
            avail = max(1, text_w - depth * _INDENT - _MARK_SLOT)
            start = 0
            # word=False: the hard character cut the raw text viewer wraps with.
            for seg in wrap_text(self._label_text(node), avail, display_width,
                                 word=False):
                self._lines.append((i, start, start + len(seg)))
                start += len(seg)

    def _total_lines(self, rows: list[tuple[_Node, int]]) -> int:
        """Display lines in the current layout (``_ensure_layout`` first)."""
        return len(self._lines) if self.wrap else len(rows)

    def _line_span(self, row: int) -> tuple[int, int]:
        """(first display line, line count) of visible row ``row``."""
        if not self.wrap:
            return (row, 1)
        if not (0 <= row < len(self._row_line)):
            return (0, 1)
        first = self._row_line[row]
        nxt = (self._row_line[row + 1] if row + 1 < len(self._row_line)
               else len(self._lines))
        return (first, max(1, nxt - first))

    # --- row content ----------------------------------------------------------

    def _scalar_seg(self, value: Any, palette: dict, base_fg) -> tuple[str, Any]:
        """A ``(text, color)`` segment for a scalar, formatted and colored by
        type. ``bool`` is checked before ``int`` (it is a subclass)."""
        if isinstance(value, str):
            return (json.dumps(value, ensure_ascii=False), palette["string"])
        if value is True:
            return ("true", palette["keyword"])
        if value is False:
            return ("false", palette["keyword"])
        if value is None:
            return ("null", palette["keyword"])
        if isinstance(value, (int, float)):
            return (repr(value), palette["number"])
        return (str(value), base_fg)

    def _value_segs(self, node: _Node, palette: dict, base_fg) -> list[tuple[str, Any]]:
        """The colored ``(text, color)`` segments for a row's key + value (no
        indent or expander marker)."""
        segs: list[tuple[str, Any]] = []
        if node.key is not None:
            if isinstance(node.key, int):
                segs.append((str(node.key), palette["index"]))
            else:
                segs.append((str(node.key), palette["key"]))
            segs.append((": ", palette["punct"]))
        if node.kind == "object":
            segs.append(("{%d}" % len(node.value), palette["summary"]))
        elif node.kind == "array":
            segs.append(("[%d]" % len(node.value), palette["summary"]))
        else:
            segs.append(self._scalar_seg(node.value, palette, base_fg))
        return segs

    @staticmethod
    def _marker(node: _Node) -> str:
        if not node.is_branch:
            return _LEAF
        return _EXPANDED if node.expanded else _COLLAPSED

    def _label_text(self, node: _Node) -> str:
        """The searchable key + value text of a node (no indent / marker), used
        by search and layout. Cached on the node — values never change."""
        if node.label is None:
            segs = self._value_segs(node, _DEFAULT_PALETTE, None)
            node.label = "".join(t for t, _ in segs)
        return node.label

    def _palette(self, theme) -> dict:
        p = dict(_DEFAULT_PALETTE)
        extra = theme.extras.get("syntax") if theme is not None else None
        if extra:
            if "name" in extra:
                p["key"] = extra["name"]
            for role in ("string", "number", "keyword"):
                if role in extra:
                    p[role] = extra[role]
        if theme is not None:
            for role in ("index", "summary", "punct"):
                p[role] = theme.muted_text
        return p

    # --- drawing -------------------------------------------------------------

    def draw(self, ctx: DrawContext) -> None:
        self._panel = ctx.panel
        theme = ctx.theme
        view_h = ctx.size_units[1]
        row_h = self._row_h = ctx.line_height(Style(font=_MONO))

        rows = self._visible()
        self.selected = max(0, min(self.selected, len(rows) - 1)) if rows else 0

        # Reserve the scroll bars. Wrap must know the content width before the
        # line count exists, so it always reserves the vertical bar's column
        # (like the host text viewer); unwrapped keeps the on-overflow
        # reservation and may add a horizontal bar row for overrunning rows.
        if self.wrap:
            self.left = 0.0
            text_w = max(1, ctx.width - 1)
            self._ensure_layout(rows, text_w)
            show_hbar = False
            body_h = view_h
        else:
            self._ensure_layout(rows, 0)
            show_vbar = len(rows) * row_h > view_h
            text_w = max(1, ctx.width - (1 if show_vbar else 0))
            show_hbar = self._max_w > text_w
            body_h = view_h - (row_h if show_hbar else 0.0)
            if not show_vbar and len(rows) * row_h > body_h:
                # the horizontal bar's row pushed the content into overflow
                text_w = max(1, ctx.width - 1)
        self._text_w = text_w
        self._view_h = body_h
        self._viewport_h = max(1, int(body_h / row_h))

        total = self._total_lines(rows)
        self._clamp_offset(total, body_h)
        self._clamp_left()

        content_h = total * row_h
        show_bar = content_h > body_h
        fill_w = ctx.size_units[0] - (1 if show_bar else 0)

        palette = self._palette(theme)
        base_fg = self.style.fg or (theme.text if theme is not None else (212, 212, 212))
        bg = self.style.bg
        left = int(self.left)

        index = int(self.offset / row_h)
        while index < total:
            top = index * row_h - self.offset
            if top >= body_h:
                break
            if index >= 0 and top + row_h > 0:
                if self.wrap:
                    row, cs, ce = self._lines[index]
                else:
                    row, cs, ce = index, 0, None
                node, depth = rows[row]
                self._draw_line(ctx, top, row, node, depth, cs, ce, text_w,
                                fill_w, row_h, palette, base_fg, bg, theme, left)
            index += 1

        if show_bar:
            ratio = body_h / content_h
            denom = content_h - body_h
            pos = self.offset / denom if denom > 0 else 0.0
            ctx.draw_scrollbar(ctx.size_units[0] - 1, 0, body_h,
                               max(0.0, min(1.0, pos)), ratio, self.style)
        if show_hbar:
            ratio = min(1.0, text_w / self._max_w) if self._max_w else 1.0
            denom = self._max_w - text_w
            pos = self.left / denom if denom > 0 else 0.0
            ctx.draw_scrollbar(0, view_h - row_h, text_w,
                               max(0.0, min(1.0, pos)), ratio, self.style,
                               orientation="horizontal")

    def _draw_flow(self, ctx, top, segs, origin, lo, hi, bg) -> None:
        """Draw colored ``(text, color)`` segments as one left-to-right flow
        whose first display column is ``origin``, showing only the pan window
        [lo, hi) (column ``c`` lands at screen x ``c - lo``)."""
        col = float(origin)
        for text, color in segs:
            w = display_width(text)
            if col >= hi:
                break
            if col + w > lo:
                x, piece = _window_text(text, col, lo, hi)
                if piece:
                    ctx.draw_text(x, top, piece, Style(fg=color, bg=bg, font=_MONO),
                                  ink=color is None or _is_light(bg))
            col += w

    def _draw_line(self, ctx, top, row, node, depth, cs, ce, text_w, fill_w,
                   row_h, palette, base_fg, bg, theme, left) -> None:
        """Draw one display line: the whole row when unwrapped (``cs`` 0, ``ce``
        None), or the wrapped chunk [cs, ce) of its label. The first line
        carries the indent + expander mark — an inline ▸/▾ glyph on a grid, a
        stroked chevron in a reserved ``_MARK_SLOT``-wide slot on a vector
        backend, so the label starts at the same column on both — and a
        continuation line aligns under the label. ``left`` pans the whole flow
        (unwrapped only; wrap folds into the width instead)."""
        indent = depth * _INDENT
        text_x = indent + _MARK_SLOT
        first = cs == 0
        vector = ctx.vector_shapes
        lo, hi = float(left), float(left + text_w)
        label_segs = self._value_segs(node, palette, base_fg)
        if ce is not None:
            label_segs = _slice_segs(label_segs, cs, ce)

        if row == self.selected:
            # The selected row flattens to one legible color over the selection
            # fill (per-type coloring would fight the accent). draw_list_row
            # carries the tested reverse-video grid path.
            style = selected_row_style(Style(fg=base_fg, bg=bg), theme,
                                       ctx.focused, ctx.vector_shapes)
            row_style = Style(style.fg, style.bg, style.attr, font=_MONO)
            chunk = "".join(t for t, _ in label_segs)
            if vector:
                x, piece = _window_text(chunk, text_x, lo, hi)
                draw_list_row(ctx, top, piece, text_w, row_style, x, fill_w, row_h)
            else:
                prefix = " " * indent + self._marker(node) if first else " " * text_x
                x, piece = _window_text(prefix + chunk, 0.0, lo, hi)
                # The reverse-video grid path keeps x at zero; the column a
                # straddled wide glyph left behind becomes leading pad.
                draw_list_row(ctx, top, " " * int(x) + piece, text_w, row_style,
                              0.0, fill_w, row_h)
            chevron_fg = style.fg or base_fg
        else:
            if vector:
                self._draw_flow(ctx, top, label_segs, text_x, lo, hi, bg)
            else:
                prefix = " " * indent + self._marker(node) if first else " " * text_x
                self._draw_flow(ctx, top, [(prefix, palette["muted"])] + label_segs,
                                0, lo, hi, bg)
            chevron_fg = palette["muted"]

        # The vector disclosure chevron for a branch (a no-op on a grid backend,
        # which drew the glyph inline above). Muted at rest, the row color when
        # selected so it reads over the selection fill.
        if vector and first and node.is_branch and indent >= left:
            ctx.draw_chevron(indent - left, top, _MARK_W, row_h,
                             expanded=node.expanded, style=Style(fg=chevron_fg))
        if self._pattern:
            self._draw_matches(ctx, top, row, node, cs, ce, text_x, lo, hi,
                               base_fg, bg)

    def _draw_matches(self, ctx, top, row, node, cs, ce, text_x, lo, hi,
                      base_fg, bg) -> None:
        """Repaint every occurrence of the pattern in this display line over a
        highlight background (firmer for the current match), like the text
        viewer. Occurrences are found in the row's label (what ``_recompute``
        matched); on a wrapped line only the part inside the chunk [cs, ce) is
        repainted, and the pan window [lo, hi) clips like the base text."""
        if id(node) not in self._match_ids:
            return
        label = self._label_text(node)
        end = len(label) if ce is None else ce
        low = label.lower()
        pat = self._pattern.lower()
        current = (self._search_pos >= 0 and self._matches
                   and self._matches[self._search_pos][0] == row)
        hl_bg = _match_bg(bg, current)
        start = 0
        while True:
            hit = low.find(pat, start)
            if hit < 0:
                break
            start = hit + len(pat)
            a, b = max(hit, cs), min(hit + len(pat), end)
            if b <= a:
                continue
            x, piece = _window_text(label[a:b],
                                    text_x + display_width(label[cs:a]), lo, hi)
            if piece:
                ctx.draw_text(x, top, piece, Style(fg=base_fg, bg=hl_bg, font=_MONO))

    # --- scroll helpers ------------------------------------------------------

    def _clamp_offset(self, count: int, view_h: float) -> None:
        self.offset = max(0.0, min(self.offset, max(0.0, count * self._row_h - view_h)))

    def _clamp_left(self) -> None:
        self.left = max(0.0, min(self.left, float(max(0, self._max_w - self._text_w))))

    def _ensure_visible(self) -> None:
        """Scroll the selected row into view — all of its display lines when
        they fit the viewport, else its first (``_ensure_layout`` first)."""
        first, count = self._line_span(self.selected)
        top = first * self._row_h
        bottom = (first + count) * self._row_h
        if bottom - top >= self._view_h or top < self.offset:
            self.offset = top
        elif bottom > self.offset + self._view_h:
            self.offset = bottom - self._view_h

    def toggle_wrap(self) -> bool:
        """Toggle line wrap and return the new state (a host viewer binds its
        line-wrap key to this, like the ``search_*`` protocol): wrapped, a long
        value continues on extra display lines; unwrapped, rows keep one line
        each and the view pans horizontally instead."""
        self.wrap = not self.wrap
        self.left = 0.0
        self._finish_move()
        return self.wrap

    # --- events --------------------------------------------------------------

    def handle_event(self, event: Event) -> bool:
        if event.type is EventType.MOUSE_SCROLL:
            rows = self._visible()
            self._ensure_layout(rows, self._text_w)
            amount = event.hints.get("scroll_units")
            if amount is None:
                amount = float(event.scroll)
            self.offset -= amount
            ux = event.hints.get("scroll_units_x")  # precise horizontal swipe
            if ux is not None and not self.wrap:
                self.left -= float(ux)
                self._clamp_left()
            self._clamp_offset(self._total_lines(rows), self._view_h)
            return True
        if event.type is EventType.MOUSE_CLICK:
            return self._handle_click(event)
        if event.type is EventType.KEY:
            if event.modifiers & {"ctrl", "cmd"} and event.key == "c":
                self._copy_selection()
                return True
            if "shift" in event.modifiers and event.key in ("left", "right"):
                # Shift+←/→ pans an unwrapped view (plain ←/→ stay tree
                # navigation); swallowed while wrapping, where there is
                # nothing to pan to.
                if not self.wrap:
                    self._ensure_layout(self._visible(), self._text_w)
                    self.left += _PAN_STEP if event.key == "right" else -_PAN_STEP
                    self._clamp_left()
                return True
            if is_activate(event):
                rows = self._visible()
                if rows:
                    self._toggle(rows[self.selected][0])
                return True
            return self._handle_key(event.key)
        return False

    def _handle_key(self, key: str | None) -> bool:
        rows = self._visible()
        if not rows:
            return False
        node, _depth = rows[self.selected]
        if key == "up":
            self.selected -= 1
        elif key == "down":
            self.selected += 1
        elif key == "pageup":
            self.selected -= self._viewport_h
        elif key == "pagedown":
            self.selected += self._viewport_h
        elif key == "home":
            self.selected = 0
        elif key == "end":
            self.selected = len(rows) - 1
        elif key == "right":
            if node.is_branch and not node.expanded:
                node.expanded = True
                self._gen += 1
            elif node.is_branch and node.expanded:
                self.selected += 1  # step into the first child
            self._finish_move()
            return True
        elif key == "left":
            if node.is_branch and node.expanded:
                node.expanded = False
                self._gen += 1
            else:
                self._select_parent(rows)
            self._finish_move()
            return True
        else:
            return False
        self._finish_move()
        return True

    def _select_parent(self, rows: list[tuple[_Node, int]]) -> None:
        _node, depth = rows[self.selected]
        for i in range(self.selected - 1, -1, -1):
            if rows[i][1] < depth:
                self.selected = i
                return

    def _finish_move(self) -> None:
        rows = self._visible()
        self.selected = max(0, min(self.selected, max(0, len(rows) - 1)))
        self._ensure_layout(rows, self._text_w)
        self._ensure_visible()
        self._clamp_offset(self._total_lines(rows), self._view_h)

    def _handle_click(self, event: Event) -> bool:
        rows = self._visible()
        if event.y is None:
            return False
        self._ensure_layout(rows, self._text_w)
        line = int((self.offset + event.y) / self._row_h)
        if not (0 <= line < self._total_lines(rows)):
            return False
        if self.wrap:
            row, cs, _ce = self._lines[line]
        else:
            row, cs = line, 0
        self.selected = row
        self._clicks.press(row)
        node, depth = rows[row]
        # A click on the expander column (on the row's first display line, pan
        # applied) toggles a branch; otherwise the click just selects the row.
        marker_col = depth * _INDENT - (0 if self.wrap else int(self.left))
        on_marker = (cs == 0 and event.x is not None
                     and marker_col <= event.x < marker_col + _INDENT)
        if node.is_branch and on_marker:
            self._toggle(node)
        self._finish_move()
        return True

    def _toggle(self, node: _Node) -> None:
        if node.is_branch:
            node.expanded = not node.expanded
            self._gen += 1
            self._finish_move()

    def _copy_selection(self) -> None:
        """Copy the selected node's value as compact JSON (a scalar copies its own
        JSON literal, a container its full sub-document)."""
        rows = self._visible()
        if not rows or self._panel is None:
            return
        node = rows[self.selected][0]
        try:
            text = json.dumps(node.value, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(node.value)
        self._panel.set_clipboard(text)

    # --- search protocol (driven by a host viewer's search bar) --------------

    def _recompute(self) -> None:
        """Rebuild the match set for the current pattern, expanding the ancestors
        of every hit so it is reachable, then record the matches in display
        order with their (post-expansion) row indices."""
        self._matches = []
        self._match_ids = set()
        pat = self._pattern.lower()
        if not pat:
            return

        def walk(nodes: list[_Node], ancestors: list[_Node]) -> None:
            for node in nodes:
                if pat in self._label_text(node).lower():
                    self._match_ids.add(id(node))
                    for a in ancestors:
                        if not a.expanded:
                            a.expanded = True
                            self._gen += 1
                walk(node.children, ancestors + [node])

        walk(self.roots, [])
        self._matches = [(i, n) for i, (n, _d) in enumerate(self._visible())
                         if id(n) in self._match_ids]

    def search_begin(self) -> None:
        """Remember the pre-search scroll + selected node (restored on cancel) and
        drop stale highlights. Call when opening the search bar."""
        self._origin = self.offset
        rows = self._visible()
        self._origin_node = rows[self.selected][0] if rows else None
        self.clear_search()

    def search_set(self, pattern: str) -> int:
        """Set the case-insensitive search ``pattern`` (live, per keystroke):
        expand + highlight every match and **move the selection** to the nearest
        match at/after the current one (mirroring the main file manager's
        i-search, so ``Enter`` commits the selection on the found row). With no
        match, restore the pre-search selection. Returns the match count."""
        self._pattern = pattern
        self._recompute()
        if self._matches:
            self._search_pos = next(
                (k for k, (ri, _n) in enumerate(self._matches) if ri >= self.selected), 0)
            self._select_match()
        else:
            self._search_pos = -1
            self._restore_origin()
        return len(self._matches)

    def search_navigate(self, delta: int) -> None:
        """Move the selection to the previous (``delta < 0``) / next (``delta >
        0``) match, wrapping at the ends. A no-op with no matches."""
        if not self._matches:
            return
        self._search_pos = (self._search_pos + delta) % len(self._matches)
        self._select_match()

    def search_status(self) -> tuple[int, int]:
        """``(position, total)`` for the bar's counter: the 1-based index of the
        current match (0 when off any match) and the match count."""
        n = len(self._matches)
        return (self._search_pos + 1 if (n and self._search_pos >= 0) else 0, n)

    def search_accept(self) -> None:
        """Enter: keep the selection on the current match; drop the highlights."""
        self.clear_search()

    def search_cancel(self) -> None:
        """Esc / outside click: restore the pre-search selection + scroll and clear
        (nodes expanded to reveal a match stay expanded)."""
        self._restore_origin()
        self.clear_search()

    def _select_match(self) -> None:
        """Move the selection cursor onto the current match and scroll it in —
        no scroll while it already has ~3 rows of context inside the viewport,
        else centered (the shared search-jump rule, see widgets/_scroll.py);
        plain cursor movement keeps the minimal :meth:`_ensure_visible`."""
        self.selected = self._matches[self._search_pos][0]
        rows = self._visible()
        self._ensure_layout(rows, self._text_w)
        line = self._line_span(self.selected)[0]
        self.offset = search_scroll_offset(
            self.offset, self._view_h, line * self._row_h,
            self._row_h, margin=3 * self._row_h, snap=self._row_h)
        self._clamp_offset(self._total_lines(rows), self._view_h)

    def _restore_origin(self) -> None:
        """Restore the pre-search selection (found by node identity — an expansion
        may have shifted its row index) and the pre-search scroll."""
        rows = self._visible()
        if self._origin_node is not None:
            for i, (node, _d) in enumerate(rows):
                if node is self._origin_node:
                    self.selected = i
                    break
        self.offset = self._origin
        self._ensure_layout(rows, self._text_w)
        self._clamp_offset(self._total_lines(rows), self._view_h)

    def clear_search(self) -> None:
        """Drop the search pattern, highlights, and match set."""
        self._pattern = ""
        self._matches = []
        self._match_ids = set()
        self._search_pos = -1
