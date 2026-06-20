"""A drop-down (combo) selector with a real overlay popup.

Closed, it shows the current choice as a flat field with a chevron. Opening it
pushes a popup *layer* onto the Panel — positioned under the field via
``ctx.screen_rect`` — instead of expanding in place, so the list floats above
the page and the surrounding layout never reflows. On pixel backends the popup
draws a framed, padded menu (a thin border line, not a drop shadow, sets it
apart from the page); on a character grid the popup_bg contrast does that. The
popup is modal: it commits on click/enter, and cancels on escape or an outside
click. This is the same ``push_layer`` intent the demo dialogs use; the Panel
resolves the compositing per backend.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from ..backend import DEFAULT_STYLE, Style
from ..event import Event, EventType
from ..layout import LayoutContext, SizeRequest
from ..panel import DrawContext
from ..theme import DEFAULT_THEME
from ._input import is_activate
from .base import CONTROL_HEIGHT, Widget

_POPUP_Z = 50

# Corner radius of the closed field, in device pixels (dropped on a grid).
_FIELD_RADIUS = 4.0
# Popup geometry, in base units, used only on pixel backends (a grid keeps one
# cell per row, flush to the frame). Rows match the control box height so the
# list and the field read consistently; the centered text, not a gap between
# the highlight and the frame, supplies the padding (so the selection bar is
# even on every side).
_POPUP_ROW_H = CONTROL_HEIGHT
# Left inset of the option text, matching the closed field's text column, so the
# field and the list line up horizontally on every backend.
_POPUP_TEXT_X = 1
_POPUP_RADIUS = 5.0  # frame corner radius, device pixels


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
        self._pixel = False  # backend places content at device-pixel precision
        self._field_w = float("inf")  # field width; set at draw (permissive until then)
        self._popup: _DropDownPopup | None = None

    # --- geometry -------------------------------------------------------------

    def view_height(self) -> float:
        # The popup floats, so only the field is sized here: one cell on a grid,
        # a little taller (centered text + padding) on pixel backends.
        return CONTROL_HEIGHT if self._pixel else 1.0

    def measure(self, ctx: LayoutContext, axis: str, available: float) -> SizeRequest:
        if axis == "y":
            # One cell on a grid, a little taller (centered text + padding) on
            # pixel backends — matching the other single-line controls.
            h = 1.0 if ctx.snap else CONTROL_HEIGHT
            return SizeRequest(min=1.0, preferred=h, max=h)
        w = float(self.width)
        return SizeRequest(min=w, preferred=w, max=w)

    # --- drawing -------------------------------------------------------------

    def draw(self, ctx: DrawContext) -> None:
        self._panel = ctx.panel
        self._screen_rect = ctx.screen_rect
        self._pixel = ctx.vector_shapes
        if self.options:
            self.selected = max(0, min(self.selected, len(self.options) - 1))
        theme = ctx.theme or DEFAULT_THEME
        w = min(self.width, ctx.width)
        if w < 5:
            return
        field_w = min(float(self.width), ctx.size_units[0])
        self._field_w = field_w  # captured for hit-testing
        bg = theme.hover_bg if ctx.hovered_in(field_w) else theme.control_bg
        field_h = ctx.size_units[1]
        ty = (field_h - 1.0) / 2.0  # center the text line within the field box
        # A flat, rounded field on vector backends, a plain fill on a character
        # grid. The fill goes down first and the border is stroked *last* (after
        # the text), so the field text's own background cannot paint over the
        # border line at the box's top/bottom edges.
        ctx.round_rect(0, 0, field_w, field_h, Style(bg=bg), radius=_FIELD_RADIUS, hints={"fill": True})

        label = self.options[self.selected] if self.options else ""
        field = label[: w - 4].ljust(w - 4)
        ctx.draw_text(1, ty, field, Style(fg=theme.text, bg=bg))
        # The accent-colored chevron is the focus cue that reads on every
        # backend (the vector border below adds to it on capable ones).
        arrow = "▴" if self.open else "▾"
        arrow_fg = theme.accent if ctx.focused else theme.text
        ctx.draw_text(w - 2, ty, arrow, Style(fg=arrow_fg, bg=bg))

        border = theme.accent if ctx.focused else theme.control_border
        ctx.round_rect(0, 0, field_w, field_h, Style(fg=border), radius=_FIELD_RADIUS)

    # --- events --------------------------------------------------------------

    def handle_event(self, event: Event) -> bool:
        # While the popup is up it is modal and receives events directly; the
        # field only needs to handle the closed state here.
        if event.type is EventType.MOUSE_CLICK:
            # Only the field is clickable, not the empty slot to its right.
            if event.x is not None and event.x >= self._field_w:
                return False
            self._open_popup()
            return True
        if is_activate(event) or (event.type is EventType.KEY and event.key == "down"):
            self._open_popup()
            return True
        return False

    def _open_popup(self) -> None:
        if self.open or not self.options or self._panel is None or self._screen_rect is None:
            return
        x, y, _w, fh = self._screen_rect
        self.open = True
        self._popup = _DropDownPopup(
            self.options, self.selected,
            on_commit=self._commit, on_cancel=self._cancel,
        )
        # Rows are taller than one cell on pixel backends; the popup recomputes
        # the same row height from its own context when drawing. The list opens
        # just below the field (whatever its height), not always one row down.
        row_h = _POPUP_ROW_H if self._pixel else 1.0
        self._panel.push_layer(
            self._popup, z=_POPUP_Z,
            hints={
                "x": x, "y": y + fh,
                "w": float(self.width), "h": float(len(self.options)) * row_h,
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
        self._row_h = 1.0  # row height in base units, captured at draw

    def _hover_row(self, ctx: DrawContext) -> int | None:
        panel = ctx.panel
        if panel is None or panel.pointer is None:
            return None
        px, py = panel.pointer
        rx, ry, rw, rh = ctx.screen_rect
        if not (rx <= px < rx + rw and ry <= py < ry + rh):
            return None
        row = int((py - ry) / self._row_h)
        return row if 0 <= row < len(self.options) else None

    def draw(self, ctx: DrawContext) -> None:
        theme = ctx.theme or DEFAULT_THEME
        wu, hu = ctx.size_units
        self._width = ctx.width
        row_h = _POPUP_ROW_H if ctx.vector_shapes else 1.0
        self._row_h = row_h
        text_dy = (row_h - 1.0) / 2.0  # center the text line within the taller row
        ctx.fill_rect(0, 0, wu, hu, Style(bg=theme.popup_bg))
        hover_row = self._hover_row(ctx)
        avail = max(0, ctx.width - _POPUP_TEXT_X - 1)
        for i, option in enumerate(self.options):
            top = i * row_h
            if top >= hu:
                break
            if i == self.cursor:
                row_bg = theme.selection_bg
            elif i == hover_row:
                row_bg = theme.hover_bg
            else:
                row_bg = theme.popup_bg
            # The highlight fills the whole row, edge to edge, so the selection
            # bar's padding is even on every side (the centered text is the
            # breathing room — no top-only gap). No bullet: the bar marks it.
            if row_bg != theme.popup_bg:
                ctx.fill_rect(0, top, wu, row_h, Style(bg=row_bg))
            ctx.draw_text(_POPUP_TEXT_X, top + text_dy, option[:avail], Style(fg=theme.text, bg=row_bg))
        # A visible frame line replaces the drop shadow as the separation cue
        # (pixel backends only; on a character grid the popup_bg contrast already
        # sets the list apart, and a box frame would overwrite the row edges).
        if ctx.vector_shapes:
            ctx.round_rect(0, 0, wu, hu, Style(fg=theme.popup_border), radius=_POPUP_RADIUS)

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
            row = int(event.y / self._row_h) if event.y is not None else -1
            inside_x = event.x is not None and 0 <= event.x < self._width
            if inside_x and 0 <= row < len(self.options):
                self.on_commit(row)
            else:
                self.on_cancel()  # click outside the list dismisses it
            return True
        return True  # modal: swallow everything else
