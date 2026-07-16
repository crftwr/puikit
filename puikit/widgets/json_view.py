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
columns it does on a terminal; long rows truncate with an ellipsis (there is no
horizontal scroll — a value that overflows is elided, like a file tree).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..backend import DEFAULT_STYLE, Style
from ..event import Event, EventType
from ..font import Font
from ..panel import DrawContext
from ..text import display_width, truncate_to_width
from ._input import MultiClickTracker, is_activate
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


class JsonView(Widget):
    focusable = True

    def __init__(self, value: Any, *, style: Style = DEFAULT_STYLE):
        self.style = style
        # A top-level container shows its entries at depth 0 (no synthetic root
        # row); a bare scalar document shows a single leaf.
        root = _build(value, None)
        self.roots: list[_Node] = root.children if root.kind in ("object", "array") else [root]

        self.selected = 0
        self.offset: float = 0.0          # first visible row, base units
        self._row_h: float = 1.0
        self._viewport_h = 1
        self._view_h: float = 1.0
        self._panel: Any = None

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
        by :meth:`_recompute` where the display depth is not known."""
        segs = self._value_segs(node, _DEFAULT_PALETTE, None)
        return "".join(t for t, _ in segs)

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
        self._view_h = view_h
        row_h = self._row_h = ctx.line_height(Style(font=_MONO))
        self._viewport_h = max(1, int(view_h / row_h))

        rows = self._visible()
        self.selected = max(0, min(self.selected, len(rows) - 1)) if rows else 0
        self._clamp_offset(len(rows), view_h)

        content_h = len(rows) * row_h
        show_bar = content_h > view_h
        text_w = ctx.width - (1 if show_bar else 0)
        fill_w = ctx.size_units[0] - (1 if show_bar else 0)

        palette = self._palette(theme)
        base_fg = self.style.fg or (theme.text if theme is not None else (212, 212, 212))
        bg = self.style.bg

        index = int(self.offset / row_h)
        while index < len(rows):
            top = index * row_h - self.offset
            if top >= view_h:
                break
            if index >= 0 and top + row_h > 0:
                node, depth = rows[index]
                self._draw_row(ctx, top, index, node, depth, text_w, fill_w,
                               row_h, palette, base_fg, bg, theme)
            index += 1

        if show_bar:
            ratio = view_h / content_h
            denom = content_h - view_h
            pos = self.offset / denom if denom > 0 else 0.0
            ctx.draw_scrollbar(ctx.size_units[0] - 1, 0, view_h,
                               max(0.0, min(1.0, pos)), ratio, self.style)

    def _draw_row(self, ctx, top, index, node, depth, text_w, fill_w, row_h,
                  palette, base_fg, bg, theme) -> None:
        indent = depth * _INDENT
        # A vector backend strokes the disclosure mark as a crisp chevron in a
        # reserved slot (like TreeView), with the label after it; a grid keeps the
        # ▸/▾ glyph inline. Either way the key/value text starts at ``text_x``.
        vector = ctx.vector_shapes
        if vector:
            text_x = float(indent + _MARK_SLOT)
            prefix = ""
        else:
            text_x = 0.0
            prefix = " " * indent + self._marker(node)
        value_segs = self._value_segs(node, palette, base_fg)

        if index == self.selected:
            # The selected row flattens to one legible color over the selection
            # fill (per-type coloring would fight the accent). draw_list_row
            # carries the tested reverse-video grid path.
            style = selected_row_style(Style(fg=base_fg, bg=bg), theme,
                                       ctx.focused, ctx.vector_shapes)
            plain = prefix + "".join(t for t, _ in value_segs)
            clipped = truncate_to_width(plain, max(0, text_w - int(text_x)))
            draw_list_row(ctx, top, clipped, text_w, Style(style.fg, style.bg,
                          style.attr, font=_MONO), text_x, fill_w, row_h)
            chevron_fg = style.fg or base_fg
        else:
            # Marker (grid only) then each colored value segment, truncating at the
            # content edge (no horizontal scroll — a long value is elided).
            col = text_x
            for text, color in ([(prefix, palette["muted"])] if prefix else []) + value_segs:
                if col >= text_w:
                    break
                piece = text if display_width(text) <= text_w - col \
                    else truncate_to_width(text, int(text_w - col))
                ctx.draw_text(col, top, piece, Style(fg=color, bg=bg, font=_MONO),
                              ink=color is None or _is_light(bg))
                col += display_width(piece)
            chevron_fg = palette["muted"]

        # The vector disclosure chevron for a branch (a no-op on a grid backend,
        # which drew the glyph inline above). Muted at rest, the row color when
        # selected so it reads over the selection fill.
        if vector and node.is_branch:
            ctx.draw_chevron(indent, top, _MARK_W, row_h,
                             expanded=node.expanded, style=Style(fg=chevron_fg))
        if self._pattern:
            self._draw_matches(ctx, top, index, node, text_w, base_fg, bg,
                               prefix, text_x)

    def _draw_matches(self, ctx, top, index, node, text_w, base_fg, bg,
                      prefix, text_x) -> None:
        """Repaint every occurrence of the pattern in this row over a highlight
        background (firmer for the current match), like the text viewer. Positions
        are relative to ``text_x`` — where the key/value text is drawn — so the
        highlight lands on the label whether the marker is inline (grid) or a
        vector chevron in the reserved slot."""
        if id(node) not in self._match_ids:
            return
        row_plain = prefix + "".join(
            t for t, _ in self._value_segs(node, _DEFAULT_PALETTE, None))
        low = row_plain.lower()
        pat = self._pattern.lower()
        current = (self._search_pos >= 0 and self._matches
                   and self._matches[self._search_pos][0] == index)
        hl_bg = _match_bg(bg, current)
        start = 0
        while True:
            hit = low.find(pat, start)
            if hit < 0:
                break
            end = hit + len(pat)
            start = end
            x = text_x + hit
            if x >= text_w:
                continue
            sub = truncate_to_width(row_plain[hit:end], int(text_w - x))
            if sub:
                ctx.draw_text(x, top, sub, Style(fg=base_fg, bg=hl_bg, font=_MONO))

    # --- scroll helpers ------------------------------------------------------

    def _clamp_offset(self, count: int, view_h: float) -> None:
        self.offset = max(0.0, min(self.offset, max(0.0, count * self._row_h - view_h)))

    def _ensure_visible(self) -> None:
        top = self.selected * self._row_h
        if top < self.offset:
            self.offset = top
        elif top + self._row_h > self.offset + self._view_h:
            self.offset = top + self._row_h - self._view_h

    # --- events --------------------------------------------------------------

    def handle_event(self, event: Event) -> bool:
        if event.type is EventType.MOUSE_SCROLL:
            amount = event.hints.get("scroll_units")
            if amount is None:
                amount = float(event.scroll)
            self.offset -= amount
            self._clamp_offset(len(self._visible()), self._view_h)
            return True
        if event.type is EventType.MOUSE_CLICK:
            return self._handle_click(event)
        if event.type is EventType.KEY:
            if event.modifiers & {"ctrl", "cmd"} and event.key == "c":
                self._copy_selection()
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
            elif node.is_branch and node.expanded:
                self.selected += 1  # step into the first child
            self._finish_move()
            return True
        elif key == "left":
            if node.is_branch and node.expanded:
                node.expanded = False
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
        self._ensure_visible()
        self._clamp_offset(len(rows), self._view_h)

    def _handle_click(self, event: Event) -> bool:
        rows = self._visible()
        if event.y is None:
            return False
        index = int((self.offset + event.y) / self._row_h)
        if not (0 <= index < len(rows)):
            return False
        self.selected = index
        self._clicks.press(index)
        node, depth = rows[index]
        # A click on the expander column (or a double-click anywhere on a branch)
        # toggles it; otherwise the click just selects the row.
        marker_col = depth * _INDENT
        on_marker = event.x is not None and marker_col <= event.x < marker_col + _INDENT
        if node.is_branch and on_marker:
            self._toggle(node)
        self._finish_move()
        return True

    def _toggle(self, node: _Node) -> None:
        if node.is_branch:
            node.expanded = not node.expanded
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
                        a.expanded = True
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
        """Move the selection cursor onto the current match and scroll it in."""
        self.selected = self._matches[self._search_pos][0]
        self._ensure_visible()
        self._clamp_offset(len(self._visible()), self._view_h)

    def _restore_origin(self) -> None:
        """Restore the pre-search selection (found by node identity — an expansion
        may have shifted its row index) and the pre-search scroll."""
        if self._origin_node is not None:
            for i, (node, _d) in enumerate(self._visible()):
                if node is self._origin_node:
                    self.selected = i
                    break
        self.offset = self._origin
        self._clamp_offset(len(self._visible()), self._view_h)

    def clear_search(self) -> None:
        """Drop the search pattern, highlights, and match set."""
        self._pattern = ""
        self._matches = []
        self._match_ids = set()
        self._search_pos = -1
