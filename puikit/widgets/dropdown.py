"""A drop-down (combo) selector with a real overlay popup.

Closed, it shows the current choice as a flat field with a chevron. Opening it
pushes a popup *layer* onto the Panel — positioned under the field via
``ctx.screen_rect`` — instead of expanding in place, so the list floats above
the page (with a drop shadow on capable backends) and the surrounding layout
never reflows. The popup is modal: it commits on click/enter, and cancels on
escape or an outside click. This is the same ``push_layer`` intent the demo
dialogs use; the Panel resolves the compositing per backend.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from ..backend import DEFAULT_STYLE, Style, TextAttribute
from ..event import Event, EventType
from ..layout import LayoutContext, SizeRequest
from ..panel import DrawContext
from ..theme import DEFAULT_THEME
from ._input import is_activate
from .base import Widget

_POPUP_Z = 50


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
        # Captured at draw time so event handling can reach the Panel (to push
        # the popup) and knows where the field is on screen (to place it).
        self._panel = None
        self._screen_rect: tuple[float, float, float, float] | None = None
        self._popup: _DropDownPopup | None = None

    # --- geometry -------------------------------------------------------------

    def view_height(self) -> int:
        return 1  # the popup floats; the field itself is always one row

    def measure(self, ctx: LayoutContext, axis: str, available: float) -> SizeRequest:
        if axis == "y":
            return SizeRequest(min=1.0, preferred=1.0, max=1.0)
        w = float(self.width)
        return SizeRequest(min=w, preferred=w, max=w)

    # --- drawing -------------------------------------------------------------

    def draw(self, ctx: DrawContext) -> None:
        self._panel = ctx.panel
        self._screen_rect = ctx.screen_rect
        if self.options:
            self.selected = max(0, min(self.selected, len(self.options) - 1))
        theme = ctx.theme or DEFAULT_THEME
        w = min(self.width, ctx.width)
        if w < 5:
            return
        bg = theme.hover_bg if ctx.hovered else theme.control_bg
        ctx.fill_rect(0, 0, min(float(self.width), ctx.size_units[0]), 1, Style(bg=bg))

        attr = TextAttribute.UNDERLINE if ctx.focused else TextAttribute.NORMAL
        label = self.options[self.selected] if self.options else ""
        field = label[: w - 4].ljust(w - 4)
        ctx.draw_text(1, 0, field, Style(fg=theme.text, bg=bg, attr=attr))
        arrow = "▴" if self.open else "▾"
        arrow_fg = theme.accent if ctx.focused else theme.text
        ctx.draw_text(w - 2, 0, arrow, Style(fg=arrow_fg, bg=bg, attr=attr))

    # --- events --------------------------------------------------------------

    def handle_event(self, event: Event) -> bool:
        # While the popup is up it is modal and receives events directly; the
        # field only needs to handle the closed state here.
        if event.type is EventType.MOUSE_CLICK or is_activate(event) or (
            event.type is EventType.KEY and event.key == "down"
        ):
            self._open_popup()
            return True
        return False

    def _open_popup(self) -> None:
        if self.open or not self.options or self._panel is None or self._screen_rect is None:
            return
        x, y, _w, _h = self._screen_rect
        self.open = True
        self._popup = _DropDownPopup(
            self.options, self.selected,
            on_commit=self._commit, on_cancel=self._cancel,
        )
        self._panel.push_layer(
            self._popup, z=_POPUP_Z,
            hints={
                "x": x, "y": y + 1,
                "w": float(self.width), "h": float(len(self.options)),
                "shadow": True,
            },
        )

    def _close_layer(self) -> None:
        self.open = False
        if self._panel is not None and self._popup is not None:
            # Pop only if our popup is still the top layer.
            self._panel.pop_layer()
        self._popup = None

    def _commit(self, index: int) -> None:
        self._close_layer()
        index = max(0, min(index, len(self.options) - 1))
        if index != self.selected:
            self.selected = index
            if self.on_change is not None:
                self.on_change(index, self.options[index])

    def _cancel(self) -> None:
        self._close_layer()


class _DropDownPopup(Widget):
    """The floating option list pushed as a Panel layer. Modal: it owns events
    while open, commits the clicked/entered option, and cancels on escape or an
    outside click. It highlights the keyboard cursor and the hovered row."""

    def __init__(
        self,
        options: Sequence[str],
        selected: int,
        on_commit: Callable[[int], None],
        on_cancel: Callable[[], None],
    ):
        self.options = list(options)
        self.selected = selected
        self.cursor = selected
        self.on_commit = on_commit
        self.on_cancel = on_cancel
        self._width = 0  # popup width in base units, captured at draw

    def _hover_row(self, ctx: DrawContext) -> int | None:
        panel = ctx.panel
        if panel is None or panel.pointer is None:
            return None
        px, py = panel.pointer
        rx, ry, rw, rh = ctx.screen_rect
        if rx <= px < rx + rw and ry <= py < ry + rh:
            return int(py - ry)
        return None

    def draw(self, ctx: DrawContext) -> None:
        theme = ctx.theme or DEFAULT_THEME
        wu, hu = ctx.size_units
        self._width = ctx.width
        ctx.fill_rect(0, 0, wu, hu, Style(bg=theme.popup_bg))
        hover_row = self._hover_row(ctx)
        for i, option in enumerate(self.options):
            if i >= ctx.height:
                break
            if i == self.cursor:
                row_bg = theme.selection_bg
            elif i == hover_row:
                row_bg = theme.hover_bg
            else:
                row_bg = theme.popup_bg
            ctx.fill_rect(0, i, wu, 1, Style(bg=row_bg))
            bullet = "•" if i == self.selected else " "
            text = option[: ctx.width - 3].ljust(ctx.width - 3)
            ctx.draw_text(0, i, f" {bullet}{text}", Style(fg=theme.text, bg=row_bg))

    def handle_event(self, event: Event) -> bool:
        if event.type is EventType.KEY:
            if event.key == "up":
                self.cursor = max(0, self.cursor - 1)
            elif event.key == "down":
                self.cursor = min(len(self.options) - 1, self.cursor + 1)
            elif is_activate(event):
                self.on_commit(self.cursor)
            elif event.key == "escape":
                self.on_cancel()
            return True
        if event.type is EventType.MOUSE_CLICK:
            row = int(event.y) if event.y is not None else -1
            inside_x = event.x is not None and 0 <= event.x < self._width
            if inside_x and 0 <= row < len(self.options):
                self.on_commit(row)
            else:
                self.on_cancel()  # click outside the list dismisses it
            return True
        return True  # modal: swallow everything else
