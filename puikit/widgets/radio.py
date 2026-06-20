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

# Padding (base units) reserved around the option rows on vector backends, so
# the group focus ring has breathing room instead of hugging the text. Grid
# backends keep tight whole-cell rows (their focus cue is the reversed mark, not
# a ring). _MARGIN is the gap between the ring and the content it surrounds.
_PAD_X = 0.5
_PAD_Y = 0.4
_MARGIN = 0.3


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
        self._pad_y = 0.0  # top inset of the rows, captured at draw for hit-testing
        self._row_x = (0.0, float("inf"))  # content x-range; set at draw (permissive until then)

    # --- drawing -------------------------------------------------------------

    def draw(self, ctx: DrawContext) -> None:
        if self.options:
            self.selected = max(0, min(self.selected, len(self.options) - 1))
        theme = ctx.theme or DEFAULT_THEME
        wu, hu = ctx.size_units
        n = len(self.options)
        # Inset the rows on vector backends so the focus ring (and hover) clears
        # the text; whole-cell grids keep tight rows. pad_y centers the rows in
        # any slack, capped so a tall slot does not float them too far.
        vector = ctx.vector_shapes
        pad_x = _PAD_X if vector else 0.0
        pad_y = min(_PAD_Y, max(0.0, (hu - n) / 2.0)) if vector else 0.0
        self._pad_y = pad_y
        cw = self._content_width(ctx)
        x0 = max(0.0, pad_x - _MARGIN) if vector else 0.0
        x1 = min(wu, pad_x + cw + _MARGIN) if vector else min(wu, cw)
        self._row_x = (x0, x1)
        label_x = len(_UNSELECTED) + len(_GAP)

        hover_row = self._hover_row(ctx)
        rows = 0
        for i, option in enumerate(self.options):
            ry = pad_y + i
            if ry + 1.0 > hu + 1e-6:
                break  # taller than the slot: clip the overflow at the edge
            rows = i + 1
            row_bg = theme.hover_bg if i == hover_row else None
            if row_bg is not None:
                ctx.fill_rect(x0, ry, x1 - x0, 1, Style(bg=row_bg))
            # The mark is an intent: a circle with an accent dot on vector
            # backends, the "(•)"/"( )" text mark on a character grid. Focus is a
            # group-level cue (the ring below / the grid reverse), not per-row.
            selected = i == self.selected
            ctx.draw_radio_mark(
                pad_x, ry, selected=selected, focused=ctx.focused,
                theme=theme, row_bg=row_bg,
            )
            ctx.draw_text(pad_x + label_x, ry, option, Style(fg=theme.text, bg=row_bg))

        # Focus is a property of the whole group, so on vector backends it draws
        # one ring around the group's content — with a margin so it never hugs
        # the text — not smuggled onto the selected row's mark
        # (interaction_states.md §4a).
        if ctx.focused and vector and rows > 0:
            y0 = max(0.0, pad_y - _MARGIN)
            y1 = min(hu, pad_y + rows + _MARGIN)
            ctx.round_rect(
                x0, y0, x1 - x0, y1 - y0, Style(fg=theme.accent), radius=4.0,
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
        rx, ry, _rw, rh = ctx.screen_rect
        x0, x1 = self._row_x
        # Limit the hover to the content's x-range, not the full (wider) slot.
        if not (rx + x0 <= px < rx + x1 and ry <= py < ry + rh):
            return None
        row = int(py - ry - self._pad_y)  # back out the rows' top inset
        return row if 0 <= row < len(self.options) else None

    def measure(self, ctx: LayoutContext, axis: str, available: float) -> SizeRequest:
        # Reserve the focus-ring padding on pixel backends so a content-sized
        # group has room for the ring; whole-unit grids keep tight cells.
        pad_x = 0.0 if ctx.snap else 2.0 * _PAD_X
        pad_y = 0.0 if ctx.snap else 2.0 * _PAD_Y
        if axis == "y":
            n = float(len(self.options)) + pad_y
            return SizeRequest(min=1.0, preferred=n, max=n)
        prefix = len(_SELECTED) + len(_GAP)
        w = pad_x + max(
            (prefix + ctx.measure_text(o, self.style) for o in self.options), default=0.0
        )
        return SizeRequest(min=w, preferred=w, max=w)

    # --- events --------------------------------------------------------------

    def handle_event(self, event: Event) -> bool:
        if not self.options:
            return False
        if event.type is EventType.MOUSE_CLICK:
            x0, x1 = self._row_x
            if event.x is not None and not (x0 <= event.x < x1):
                return False  # outside the options' x-range (the empty slot)
            row = int((event.y or 0) - self._pad_y)  # back out the rows' top inset
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
