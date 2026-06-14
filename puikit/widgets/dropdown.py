"""A drop-down (combo) selector.

Closed, it shows the current choice and a ▾ marker. Open, it expands its
option list *inline*, below the header: it reports a taller ``view_height`` so
a host that re-measures its children (e.g. ScrollView) reserves the room and
pushes the rest down. This keeps the control working without a popup-layer
mechanism — the same intent renders on every backend.

The control owns a fixed visual ``width`` so it reads as a compact field even
when the layout hands it a wide slot.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace

from ..backend import DEFAULT_STYLE, Style, TextAttribute
from ..event import Event, EventType
from ..layout import LayoutContext, SizeRequest
from ..panel import DrawContext
from ._input import is_activate
from .base import Widget


class DropDown(Widget):
    focusable = True

    def __init__(
        self,
        options: Sequence[str],
        selected: int = 0,
        on_change: Callable[[int, str], None] | None = None,
        width: int = 22,
        style: Style = DEFAULT_STYLE,
    ):
        self.options = list(options)
        self.selected = selected
        self.on_change = on_change
        self.width = width
        self.style = style
        self.open = False
        # Highlighted row while the list is open (the keyboard cursor).
        self._cursor = selected

    # --- geometry -------------------------------------------------------------

    def view_height(self) -> int:
        """Current desired height in base units: one row closed, header plus
        every option when open. Hosts that re-measure children read this so the
        open list reserves real space."""
        return 1 + len(self.options) if self.open else 1

    def measure(self, ctx: LayoutContext, axis: str, available: float) -> SizeRequest:
        if axis == "y":
            h = float(self.view_height())
            return SizeRequest(min=h, preferred=h, max=h)
        w = float(self.width)
        return SizeRequest(min=w, preferred=w, max=w)

    # --- drawing -------------------------------------------------------------

    def draw(self, ctx: DrawContext) -> None:
        if self.options:
            self.selected = max(0, min(self.selected, len(self.options) - 1))
        w = min(self.width, ctx.width)
        if w < 5:
            return
        self._draw_header(ctx, w)
        if self.open:
            self._draw_list(ctx, w)

    def _draw_header(self, ctx: DrawContext, w: int) -> None:
        label = self.options[self.selected] if self.options else ""
        arrow = "▴" if self.open else "▾"
        field = label[: w - 5].ljust(w - 5)
        line = f"[ {field} {arrow}]"
        style = self.style
        if ctx.focused and not self.open:
            style = replace(style, attr=style.attr | TextAttribute.REVERSE)
        ctx.draw_text(0, 0, line, style)

    def _draw_list(self, ctx: DrawContext, w: int) -> None:
        for i, option in enumerate(self.options):
            row = i + 1
            if row >= ctx.height:
                break
            text = option[: w - 4].ljust(w - 4)
            bullet = "•" if i == self.selected else " "
            line = f" {bullet}{text} "
            style = self.style
            if i == self._cursor:
                style = replace(style, attr=style.attr | TextAttribute.REVERSE)
            ctx.draw_text(0, row, line, style)

    # --- events --------------------------------------------------------------

    def handle_event(self, event: Event) -> bool:
        if event.type is EventType.MOUSE_CLICK:
            return self._handle_click(int(event.y or 0))
        if event.type is EventType.KEY:
            return self._handle_key(event)
        return False

    def _handle_click(self, row: int) -> bool:
        if not self.open:
            self._open()
            return True
        if row == 0:  # clicking the header again closes the list
            self.open = False
            return True
        index = row - 1
        if 0 <= index < len(self.options):
            self._commit(index)
        else:
            self.open = False
        return True

    def _handle_key(self, event: Event) -> bool:
        if not self.open:
            if is_activate(event) or event.key == "down":
                self._open()
                return True
            return False
        if event.key == "up":
            self._cursor = max(0, self._cursor - 1)
            return True
        if event.key == "down":
            self._cursor = min(len(self.options) - 1, self._cursor + 1)
            return True
        if is_activate(event):
            self._commit(self._cursor)
            return True
        if event.key == "escape":
            self.open = False
            return True
        return False

    def _open(self) -> None:
        if not self.options:
            return
        self.open = True
        self._cursor = self.selected

    def _commit(self, index: int) -> None:
        self.open = False
        index = max(0, min(index, len(self.options) - 1))
        if index != self.selected:
            self.selected = index
            if self.on_change is not None:
                self.on_change(index, self.options[index])
