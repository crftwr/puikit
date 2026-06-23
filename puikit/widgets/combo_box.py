"""An editable drop-down (combo box): a text field plus a filtered list.

A ``DropDown`` is read-only — it picks one of a fixed set. A ComboBox adds free
text and type-to-filter: the closed control is an editable ``TextEdit`` field
with a chevron; opening it floats a popup list (the same ``push_layer`` intent
``DropDown`` uses) that narrows to the rows matching what has been typed.

It is deliberately composed from parts the framework already has — the editing
machinery (cursor, IME composition, scrolling) is a real embedded ``TextEdit``,
and the floating list reuses the popup pattern. While the popup is open it is
the modal layer, so it owns events and *forwards* the editing keys back to the
field; the field keeps drawing underneath with its caret, so typing filters the
list live. Enter commits the highlighted row (or the free text when nothing
matches and ``allow_custom``); escape closes and keeps the text. One control,
every backend — the Panel layer resolves the popup compositing.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from ..backend import DEFAULT_STYLE, Style
from ..event import Event, EventType
from ..layout import LayoutContext, SizeRequest
from ..panel import DrawContext
from ..theme import DEFAULT_THEME
from .base import CONTROL_HEIGHT, Widget
from .text_edit import TextEdit

_POPUP_Z = 50
_POPUP_ROW_H = CONTROL_HEIGHT  # pixel backends; a grid keeps one cell per row
_POPUP_TEXT_X = 1
_POPUP_RADIUS = 5.0
# Columns reserved at the field's right edge for the chevron.
_CHEVRON_W = 2


class ComboBox(Widget):
    focusable = True

    def __init__(
        self,
        options: Sequence[str],
        text: str = "",
        on_change: Callable[[str], None] | None = None,
        width: int = 22,
        allow_custom: bool = True,
        style: Style = DEFAULT_STYLE,
    ):
        self.options = list(options)
        self.on_change = on_change
        self.width = width
        # Whether Enter with no matching row accepts the typed text as the value.
        self.allow_custom = allow_custom
        self.style = style
        # A real text field owns the editing — cursor, IME composition, the
        # horizontal scroll — so the combo never re-implements any of it.
        self._field = TextEdit(text, width=width, right_pad=_CHEVRON_W)
        self.open = False
        self.cursor = 0                  # index into the filtered list
        self._filtered = list(range(len(self.options)))
        self._panel = None
        self._screen_rect: tuple[float, float, float, float] | None = None
        self._pixel = False
        self._content_w = float("inf")  # field + chevron width; set at draw (permissive until then)
        self._popup: _ComboPopup | None = None

    # --- value ---------------------------------------------------------------

    @property
    def text(self) -> str:
        return self._field.text

    @text.setter
    def text(self, value: str) -> None:
        self._field.text = value
        self._field.cursor = len(value)

    # --- geometry ------------------------------------------------------------

    def view_height(self) -> float:
        return CONTROL_HEIGHT if self._pixel else 1.0

    def measure(self, ctx: LayoutContext, axis: str, available: float) -> SizeRequest:
        if axis == "y":
            h = 1.0 if ctx.snap else CONTROL_HEIGHT
            return SizeRequest(min=1.0, preferred=h, max=h)
        w = float(self.width)
        return SizeRequest(min=w, preferred=w, max=w)

    # --- drawing -------------------------------------------------------------

    def draw(self, ctx: DrawContext) -> None:
        self._panel = ctx.panel
        self._screen_rect = ctx.screen_rect
        self._pixel = ctx.vector_shapes
        theme = ctx.theme or DEFAULT_THEME
        wu, hu = ctx.size_units
        w = min(self.width, ctx.width)             # grid columns, for chevron placement
        field_units = min(float(self.width), wu)   # the field box spans the full control
        self._field.width = int(w)
        self._content_w = field_units              # the chevron lives inside the field
        # The field shows its caret while the combo holds focus or the popup is
        # open (the open popup is the modal layer, but the field is what the
        # typing flows into, so it reads as the active control).
        focused = ctx.focused or self.open
        ctx.draw_child(self._field, 0, 0, field_units, hu, hints={"focused": focused})
        ty = (hu - 1.0) / 2.0
        arrow = "▴" if self.open else "▾"
        arrow_fg = theme.accent if focused else theme.text
        # Drawn over the field box's own background so the chevron reads as part
        # of the field, not a separate box hanging off its right edge. The field
        # reserves these columns (right_pad), so it never collides with text.
        hovering = ctx.hovered_in(field_units)
        # The combo reads as a clickable control; the pointer request comes after
        # the inner field child draws, so it takes precedence over the field's
        # I-beam over the whole trigger.
        if hovering:
            ctx.set_cursor("pointer")
        field_bg = theme.hover_bg if (hovering and not focused) else theme.control_bg
        ctx.draw_text(w - 2, ty, arrow, Style(fg=arrow_fg, bg=field_bg))

    # --- filtering -----------------------------------------------------------

    def _refilter(self) -> None:
        query = self._field.text.lower()
        if query:
            self._filtered = [
                i for i, o in enumerate(self.options) if query in o.lower()
            ]
        else:
            self._filtered = list(range(len(self.options)))
        if self._filtered:
            self.cursor = max(0, min(self.cursor, len(self._filtered) - 1))
        else:
            self.cursor = 0

    # --- events (closed) -----------------------------------------------------

    def handle_event(self, event: Event) -> bool:
        if event.type is EventType.MOUSE_CLICK:
            # Only the field + chevron is clickable, not the empty slot beyond it.
            if event.x is not None and event.x >= self._content_w:
                return False
            on_chevron = event.x is not None and event.x >= self._content_w - _CHEVRON_W
            if not on_chevron:
                self._field.handle_event(event)  # place the caret at the click
            self._open_popup()
            return True
        if event.type is EventType.KEY and event.key == "down":
            self._open_popup()
            return True
        if event.type in (EventType.KEY, EventType.IME_COMPOSITION):
            consumed = self._field.handle_event(event)
            self._refilter()
            return consumed
        return False

    # --- popup lifecycle -----------------------------------------------------

    def _open_popup(self) -> None:
        if self.open or self._panel is None or self._screen_rect is None:
            return
        x, y, _w, fh = self._screen_rect
        self._refilter()
        self.open = True
        self._popup = _ComboPopup(self)
        # Sized to the full option count so the list does not have to re-push as
        # filtering changes the visible row count; the popup draws only the rows
        # that match and the rest is its own background.
        row_h = _POPUP_ROW_H if self._pixel else 1.0
        rows = max(1, len(self.options))
        self._panel.push_layer(
            self._popup, z=_POPUP_Z,
            hints={"x": x, "y": y + fh, "w": float(self.width), "h": float(rows) * row_h},
        )

    def _close_layer(self) -> None:
        self.open = False
        if self._panel is not None and self._popup is not None:
            self._panel.pop_layer()
        self._popup = None

    def _commit(self, filtered_index: int) -> None:
        self._close_layer()
        if self._filtered and 0 <= filtered_index < len(self._filtered):
            self.text = self.options[self._filtered[filtered_index]]
        self._fire()

    def _accept_text(self) -> None:
        # Enter with no matching row: keep what was typed (free-text value).
        self._close_layer()
        if self.allow_custom:
            self._fire()

    def _cancel(self) -> None:
        self._close_layer()

    def _fire(self) -> None:
        if self.on_change is not None:
            self.on_change(self._field.text)


class _ComboPopup(Widget):
    """The floating, filtered option list pushed as a Panel layer. Modal: it
    owns events while open, but forwards editing keys to the combo's field so
    typing keeps filtering. It commits the highlighted/clicked row, accepts the
    free text on Enter when nothing matches, and cancels on escape / an outside
    click."""

    def __init__(self, combo: ComboBox):
        self.combo = combo
        self._width = 0
        self._row_h = 1.0

    def _hover_row(self, ctx: DrawContext, count: int) -> int | None:
        panel = ctx.panel
        if panel is None or panel.pointer is None:
            return None
        px, py = panel.pointer
        rx, ry, rw, rh = ctx.screen_rect
        if not (rx <= px < rx + rw and ry <= py < ry + rh):
            return None
        row = int((py - ry) / self._row_h)
        return row if 0 <= row < count else None

    def draw(self, ctx: DrawContext) -> None:
        theme = ctx.theme or DEFAULT_THEME
        wu, hu = ctx.size_units
        self._width = ctx.width
        row_h = _POPUP_ROW_H if ctx.vector_shapes else 1.0
        self._row_h = row_h
        text_dy = (row_h - 1.0) / 2.0
        ctx.fill_rect(0, 0, wu, hu, Style(bg=theme.popup_bg))
        combo = self.combo
        filtered = combo._filtered
        avail = max(0, ctx.width - _POPUP_TEXT_X - 1)
        if not filtered:
            ctx.draw_text(
                _POPUP_TEXT_X, text_dy, "(no matches)"[:avail],
                Style(fg=theme.muted_text, bg=theme.popup_bg),
            )
        hover_row = self._hover_row(ctx, len(filtered))
        if hover_row is not None:
            ctx.set_cursor("pointer")  # rows select on click
        for i, option_index in enumerate(filtered):
            top = i * row_h
            if top >= hu:
                break
            if i == combo.cursor:
                row_bg = theme.selection_bg
            elif i == hover_row:
                row_bg = theme.hover_bg
            else:
                row_bg = theme.popup_bg
            if row_bg != theme.popup_bg:
                ctx.fill_rect(0, top, wu, row_h, Style(bg=row_bg))
            ctx.draw_text(
                _POPUP_TEXT_X, top + text_dy, combo.options[option_index][:avail],
                Style(fg=theme.text, bg=row_bg),
            )
        if ctx.vector_shapes:
            ctx.round_rect(0, 0, wu, hu, Style(fg=theme.popup_border), radius=_POPUP_RADIUS)

    def handle_event(self, event: Event) -> bool:
        combo = self.combo
        if event.type is EventType.KEY:
            key = event.key
            if key == "up":
                if combo._filtered:
                    combo.cursor = max(0, combo.cursor - 1)
                return True
            if key == "down":
                if combo._filtered:
                    combo.cursor = min(len(combo._filtered) - 1, combo.cursor + 1)
                return True
            if key == "escape":
                combo._cancel()
                return True
            if key == "enter":
                if combo._filtered:
                    combo._commit(combo.cursor)
                else:
                    combo._accept_text()
                return True
            # Any other key (typing, backspace, cursor moves) edits the field
            # and re-filters the list — space included, so it is never an
            # "activate" here, just a character.
            combo._field.handle_event(event)
            combo._refilter()
            return True
        if event.type is EventType.IME_COMPOSITION:
            combo._field.handle_event(event)
            combo._refilter()
            return True
        if event.type is EventType.MOUSE_CLICK:
            row = int(event.y / self._row_h) if event.y is not None else -1
            inside_x = event.x is not None and 0 <= event.x < self._width
            if inside_x and 0 <= row < len(combo._filtered):
                combo._commit(row)
            else:
                combo._cancel()  # a click outside the list dismisses it
            return True
        return True  # modal: swallow everything else
