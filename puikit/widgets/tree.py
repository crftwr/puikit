"""A scrollable hierarchical tree of expandable nodes.

A ``TreeNode`` carries a label, optional children, and its expanded state. The
``TreeView`` flattens the currently-visible nodes (respecting each node's
``expanded`` flag), draws them indented by depth with an expander marker, and
behaves like ``ListView`` otherwise: a selection highlight, a scroll bar when
the rows overflow, and full keyboard navigation. Right/left expand and collapse
(or move into a child / out to the parent); enter activates a leaf or toggles a
branch; clicking the expander toggles a branch, clicking a row selects it.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..backend import DEFAULT_STYLE, Style, TextAttribute
from ..event import Event, EventType
from ..panel import DrawContext
from ..text import display_width, truncate_to_width
from .base import Widget

_INDENT = 2  # columns per depth level
_EXPANDED = "▾ "
_COLLAPSED = "▸ "
_LEAF = "  "


@dataclass
class TreeNode:
    """One node in a tree: a label, optional children, an expanded flag, and an
    opaque ``data`` payload the app can attach (e.g. a file path)."""

    label: str
    children: list["TreeNode"] = field(default_factory=list)
    expanded: bool = False
    data: Any = None

    @property
    def is_leaf(self) -> bool:
        return not self.children


class TreeView(Widget):
    focusable = True

    def __init__(
        self,
        roots: Sequence[TreeNode],
        on_select: Callable[[TreeNode], None] | None = None,
        on_activate: Callable[[TreeNode], None] | None = None,
        style: Style = DEFAULT_STYLE,
    ):
        self.roots = list(roots)
        self.on_select = on_select      # selection moved
        self.on_activate = on_activate  # enter / double-purpose activate
        self.style = style
        self.selected = 0
        self.offset: float = 0.0
        self._viewport_h = 1
        self._view_h = 1.0

    # --- flattening -----------------------------------------------------------

    def _visible(self) -> list[tuple[TreeNode, int]]:
        """(node, depth) for every currently-visible node, in display order."""
        out: list[tuple[TreeNode, int]] = []

        def walk(nodes: list[TreeNode], depth: int) -> None:
            for node in nodes:
                out.append((node, depth))
                if node.expanded and node.children:
                    walk(node.children, depth + 1)

        walk(self.roots, 0)
        return out

    # --- drawing -------------------------------------------------------------

    def draw(self, ctx: DrawContext) -> None:
        view_h = ctx.size_units[1]
        self._view_h = view_h
        self._viewport_h = max(1, int(view_h))
        rows = self._visible()
        if rows:
            self.selected = max(0, min(self.selected, len(rows) - 1))
        else:
            self.selected = 0
        self._clamp_offset(len(rows), view_h)

        show_bar = len(rows) > view_h
        text_w = ctx.width - (1 if show_bar else 0)
        theme = ctx.theme
        first = int(self.offset)
        frac = self.offset - first
        row = 0
        while True:
            index = first + row
            y = row - frac
            if y >= view_h or index >= len(rows):
                break
            if index >= 0:
                node, depth = rows[index]
                marker = _LEAF if node.is_leaf else (_EXPANDED if node.expanded else _COLLAPSED)
                raw = " " * (depth * _INDENT) + marker + node.label
                clipped = truncate_to_width(raw, text_w)
                text = clipped + " " * (text_w - display_width(clipped))
                style = self.style
                if index == self.selected:
                    style = Style(style.fg, style.bg, style.attr | TextAttribute.REVERSE)
                ctx.draw_text(0, y, text, style)
            row += 1

        if show_bar:
            ratio = view_h / len(rows)
            denom = len(rows) - view_h
            pos = self.offset / denom if denom > 0 else 0.0
            ctx.draw_scrollbar(ctx.width - 1, 0, view_h, max(0.0, min(1.0, pos)), ratio, self.style)

    def _clamp_offset(self, count: int, viewport_h: float) -> None:
        self.offset = max(0.0, min(self.offset, max(0.0, count - viewport_h)))

    def _ensure_visible(self) -> None:
        if self.selected < self.offset:
            self.offset = self.selected
        elif self.selected >= self.offset + self._viewport_h:
            self.offset = self.selected - self._viewport_h + 1

    # --- events --------------------------------------------------------------

    def handle_event(self, event: Event) -> bool:
        if event.type is EventType.KEY:
            return self._handle_key(event.key)
        if event.type is EventType.MOUSE_CLICK:
            return self._handle_click(event)
        if event.type is EventType.MOUSE_SCROLL:
            amount = event.hints.get("scroll_units")
            if amount is None:
                amount = float(event.scroll)
            self.offset -= amount
            self._clamp_offset(len(self._visible()), self._view_h)
            return True
        return False

    def _handle_key(self, key: str | None) -> bool:
        rows = self._visible()
        if not rows:
            return False
        before = self.selected
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
            if not node.is_leaf and not node.expanded:
                node.expanded = True
            elif not node.is_leaf and node.expanded:
                self.selected += 1  # step into the first child
            self._finish_move(before)
            return True
        elif key == "left":
            if not node.is_leaf and node.expanded:
                node.expanded = False
            else:
                self._select_parent(rows)
            self._finish_move(before)
            return True
        elif key == "enter":
            self._activate(node)
            return True
        else:
            return False
        self._finish_move(before)
        return True

    def _select_parent(self, rows: list[tuple[TreeNode, int]]) -> None:
        _node, depth = rows[self.selected]
        for i in range(self.selected - 1, -1, -1):
            if rows[i][1] < depth:
                self.selected = i
                return

    def _finish_move(self, before: int) -> None:
        rows = self._visible()
        self.selected = max(0, min(self.selected, max(0, len(rows) - 1)))
        self._ensure_visible()
        self._clamp_offset(len(rows), self._view_h)
        if self.selected != before and self.on_select is not None and rows:
            self.on_select(rows[self.selected][0])

    def _handle_click(self, event: Event) -> bool:
        rows = self._visible()
        index = int(self.offset + (event.y or 0))
        if not (0 <= index < len(rows)):
            return False
        before = self.selected
        node, depth = rows[index]
        self.selected = index
        # A click on the expander marker column toggles a branch.
        marker_col = depth * _INDENT
        if not node.is_leaf and event.x is not None and marker_col <= event.x < marker_col + _INDENT:
            node.expanded = not node.expanded
        self._finish_move(before)
        return True

    def _activate(self, node: TreeNode) -> None:
        if not node.is_leaf:
            node.expanded = not node.expanded
            self._clamp_offset(len(self._visible()), self._view_h)
        if self.on_activate is not None:
            self.on_activate(node)
