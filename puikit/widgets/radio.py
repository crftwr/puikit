"""A vertical group of mutually-exclusive radio options.

The group is the natural unit of selection: it owns the option list and the
single selected index, so the "only one at a time" rule needs no shared state
between separate widgets. Up/down move the selection (a radio commits as it
moves), a click selects the clicked row.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from ..backend import DEFAULT_STYLE, Style
from ..event import Event, EventType
from ..layout import LayoutContext, SizeRequest
from ..panel import DrawContext
from ..theme import DEFAULT_THEME
from .base import Widget

_SELECTED = "(•)"   # (•)
_UNSELECTED = "( )"
_GAP = " "


class RadioGroup(Widget):
    focusable = True

    def __init__(
        self,
        options: Sequence[str],
        selected: int = 0,
        on_change: Callable[[int, str], None] | None = None,
        style: Style = DEFAULT_STYLE,
    ):
        self.options = list(options)
        self.selected = selected
        self.on_change = on_change
        self.style = style

    # --- drawing -------------------------------------------------------------

    def draw(self, ctx: DrawContext) -> None:
        if self.options:
            self.selected = max(0, min(self.selected, len(self.options) - 1))
        theme = ctx.theme or DEFAULT_THEME
        hover_row = self._hover_row(ctx)
        rows = 0
        for i, option in enumerate(self.options):
            if i >= ctx.height:
                break  # taller than the slot: clip the overflow at the edge
            rows = i + 1
            row_bg = theme.hover_bg if i == hover_row else None
            if row_bg is not None:
                ctx.fill_rect(0, i, ctx.size_units[0], 1, Style(bg=row_bg))
            # The mark is an intent: a circle with an accent dot on vector
            # backends, the "(•)"/"( )" text mark on a character grid. Focus is a
            # group-level cue (the ring below / the grid reverse), not per-row.
            selected = i == self.selected
            ctx.draw_radio_mark(
                0, i, selected=selected, focused=ctx.focused,
                theme=theme, row_bg=row_bg,
            )
            label_x = len(_UNSELECTED) + len(_GAP)
            ctx.draw_text(label_x, i, option, Style(fg=theme.text, bg=row_bg))

        # Focus is a property of the whole group, so on vector backends it draws
        # one ring around the group's content — not smuggled onto the selected
        # row's mark (interaction_states.md §4a).
        if ctx.focused and ctx.vector_shapes and rows > 0:
            cw = self._content_width(ctx)
            inset = 0.12
            ctx.round_rect(
                inset, inset, cw - 2 * inset, rows - 2 * inset,
                Style(fg=theme.accent), radius=4.0,
            )

    def _content_width(self, ctx: DrawContext) -> float:
        """Width of the widest option row (mark + label), capped at the pane, so
        the focus ring hugs the group's content rather than the full pane."""
        prefix = len(_SELECTED) + len(_GAP)
        w = max(
            (prefix + ctx.measure_text(o, self.style) for o in self.options),
            default=0.0,
        )
        return min(w, ctx.size_units[0])

    def _hover_row(self, ctx: DrawContext) -> int | None:
        panel = ctx.panel
        if panel is None or panel.pointer is None:
            return None
        px, py = panel.pointer
        rx, ry, rw, rh = ctx.screen_rect
        if rx <= px < rx + rw and ry <= py < ry + rh:
            return int(py - ry)
        return None

    def measure(self, ctx: LayoutContext, axis: str, available: float) -> SizeRequest:
        if axis == "y":
            n = float(len(self.options))
            return SizeRequest(min=1.0, preferred=n, max=n)
        prefix = len(_SELECTED) + len(_GAP)
        w = max(
            (prefix + ctx.measure_text(o, self.style) for o in self.options), default=0.0
        )
        return SizeRequest(min=w, preferred=w, max=w)

    # --- events --------------------------------------------------------------

    def handle_event(self, event: Event) -> bool:
        if not self.options:
            return False
        if event.type is EventType.MOUSE_CLICK:
            row = int(event.y or 0)
            if 0 <= row < len(self.options):
                self._select(row)
            return True
        if event.type is EventType.KEY:
            if event.key == "up":
                self._select(self.selected - 1)
                return True
            if event.key == "down":
                self._select(self.selected + 1)
                return True
        return False

    def _select(self, index: int) -> None:
        index = max(0, min(index, len(self.options) - 1))
        if index == self.selected:
            return
        self.selected = index
        if self.on_change is not None:
            self.on_change(index, self.options[index])
