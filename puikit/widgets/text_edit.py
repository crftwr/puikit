"""A single-line editable text field with IME (e.g. Japanese) support.

The field keeps a text buffer and a cursor, inserts printable characters, and
handles the usual editing keys. It renders flat, VS Code-style: a
``control_bg`` field with an accent caret while focused.

IME composition is first-class. Committed characters arrive as ordinary KEY
events (the macOS backend routes ``insertText:`` through them); in-progress
*marked* text arrives as ``IME_COMPOSITION`` events carrying the preedit
string, which is drawn underlined at the cursor without touching the buffer
until it commits. While focused the field calls ``panel.request_text_input``
with the on-screen caret position so the backend can place the candidate
window next to it (the ttk pattern: ``firstRectForCharacterRange``).
"""

from __future__ import annotations

from collections.abc import Callable

from ..backend import DEFAULT_STYLE, Style, TextAttribute
from ..event import Event, EventType
from ..layout import LayoutContext, SizeRequest
from ..panel import DrawContext
from ..text import char_width, display_width
from ..theme import DEFAULT_THEME
from ._input import typed_char
from .base import CONTROL_HEIGHT, Widget

# Corner radius of the field, in device pixels (dropped on a character grid).
_FIELD_RADIUS = 4.0


class TextEdit(Widget):
    focusable = True

    def __init__(
        self,
        text: str = "",
        on_change: Callable[[str], None] | None = None,
        on_submit: Callable[[str], None] | None = None,
        width: int = 24,
        style: Style = DEFAULT_STYLE,
    ):
        self.text = text
        self.on_change = on_change
        self.on_submit = on_submit
        self.width = width
        self.style = style
        self.cursor = len(text)
        self._anchor: int | None = None  # selection start; None = no selection
        self._view = 0          # first visible index into the displayed string
        self._preedit = ""      # IME marked (composition) text, not yet committed
        self._preedit_caret = 0  # caret offset within the preedit
        self._panel = None
        self._focused_now = False  # last-drawn focus state, read by the blink tick
        self._blinking = False     # whether a caret-blink tick is registered

    # --- selection -----------------------------------------------------------

    def _selection(self) -> tuple[int, int] | None:
        """The selected half-open index range ``(start, end)``, or None when
        nothing is selected. The anchor is the fixed end; the cursor is the
        moving end, so either order produces the same ordered range."""
        if self._anchor is None or self._anchor == self.cursor:
            return None
        return (min(self._anchor, self.cursor), max(self._anchor, self.cursor))

    @property
    def selection_text(self) -> str:
        sel = self._selection()
        return self.text[sel[0] : sel[1]] if sel else ""

    def _delete_selection(self) -> bool:
        """Drop the selected range and collapse the cursor onto its start.
        Returns True if anything was removed."""
        sel = self._selection()
        self._anchor = None
        if sel is None:
            return False
        start, end = sel
        self.text = self.text[:start] + self.text[end:]
        self.cursor = start
        return True

    # --- geometry -------------------------------------------------------------

    def measure(self, ctx: LayoutContext, axis: str, available: float) -> SizeRequest:
        if axis == "x":
            w = float(self.width)
            return SizeRequest(min=w, preferred=w, max=w)
        # A single line: one cell on a grid, a little taller (centered text +
        # padding) on pixel backends.
        h = 1.0 if ctx.snap else CONTROL_HEIGHT
        return SizeRequest(min=1.0, preferred=h, max=h)

    # --- drawing -------------------------------------------------------------

    def draw(self, ctx: DrawContext) -> None:
        self._panel = ctx.panel
        theme = ctx.theme or DEFAULT_THEME
        w = min(self.width, ctx.width)
        if w < 3:
            return
        field_w = w - 2  # one column of padding on each side
        self.cursor = max(0, min(self.cursor, len(self.text)))
        if self._anchor is not None:
            self._anchor = max(0, min(self._anchor, len(self.text)))

        # The displayed string has the preedit spliced in at the cursor.
        disp = self.text[: self.cursor] + self._preedit + self.text[self.cursor :]
        pre_start, pre_end = self.cursor, self.cursor + len(self._preedit)
        caret = self.cursor + (self._preedit_caret if self._preedit else 0)
        # Selection indices address self.text directly, so they only line up
        # with the display string while no preedit is spliced in.
        sel = self._selection() if not self._preedit else None
        self._scroll_into_view(caret, field_w, len(disp))

        bg = theme.hover_bg if (ctx.hovered and not ctx.focused) else theme.control_bg
        field_full_w = min(float(self.width), ctx.size_units[0])
        field_h = ctx.size_units[1]
        ty = (field_h - 1.0) / 2.0  # center the text line within the field box
        # A flat, rounded field on vector backends, a plain fill on a character
        # grid. The fill goes first; the border is stroked last (end of draw),
        # so the text/caret backgrounds cannot paint over the border line.
        ctx.round_rect(0, 0, field_full_w, field_h, Style(bg=bg), radius=_FIELD_RADIUS, hints={"fill": True})

        # Lay out characters left to right in display columns (wide CJK glyphs
        # take two), stopping at the field edge. The caret column is tracked the
        # same way so it lands between the right glyphs.
        col = 0
        caret_col = None
        for idx in range(self._view, len(disp)):
            if idx == caret:
                caret_col = col
            ch = disp[idx]
            cw = char_width(ch)
            if col + cw > field_w:
                break
            marked = pre_start <= idx < pre_end
            selected = sel is not None and sel[0] <= idx < sel[1]
            attr = TextAttribute.UNDERLINE if marked else TextAttribute.NORMAL
            fg = theme.accent if marked else theme.text
            # The selection reads as active only while the field holds focus: a
            # visible blue when focused, a muted neutral when focus is elsewhere
            # (docs/interaction_states.md §5).
            sel_bg = theme.text_selection_bg if ctx.focused else theme.text_selection_inactive_bg
            cell_bg = sel_bg if selected else bg
            ctx.draw_text(1 + col, ty, ch, Style(fg=fg, bg=cell_bg, attr=attr))
            col += cw
        if caret_col is None:  # caret sits at/after the last visible glyph
            caret_col = col

        self._focused_now = ctx.focused
        if ctx.focused:
            # Drive the blink: register one tick the first time we draw focused;
            # it re-renders each frame so caret_visible toggles, and unregisters
            # itself once focus leaves (the tick reads _focused_now).
            if ctx.animated and not self._blinking and ctx.panel is not None:
                self._blinking = ctx.panel.request_animation_ticks(self._blink_tick)
            self._draw_caret(ctx, theme, disp, caret, caret_col, field_w, bg, ty)
            self._notify_input_position(ctx, caret_col)

        # Border stroked last so the glyph/caret backgrounds above cannot paint
        # over it; accent while focused, a subtle outline otherwise.
        border = theme.accent if ctx.focused else theme.control_border
        ctx.round_rect(0, 0, field_full_w, field_h, Style(fg=border), radius=_FIELD_RADIUS)

    def _draw_caret(self, ctx, theme, disp, caret, caret_col, field_w, bg, ty) -> None:
        if 0 <= caret_col < field_w:
            ch = disp[caret] if caret < len(disp) else " "
            # A thin blinking I-beam in the foreground color (vector) or a reverse
            # block (grid) — the caret marks the insertion point only; focus is
            # carried by the field border (docs/interaction_states.md §3).
            ctx.draw_caret(
                1 + caret_col, ty, height=1.0, theme=theme,
                glyph=ch, visible=ctx.caret_visible,
            )

    def _blink_tick(self) -> bool:
        # Unregister once focus has left (or the panel is gone); otherwise
        # re-render so the caret's blink phase advances on screen — the tick is
        # the only thing that rebuilds the display list.
        if not self._focused_now or self._panel is None:
            self._blinking = False
            return False
        self._panel.render()
        return True

    def _reset_blink(self) -> None:
        """Show the caret now by restarting its blink cycle — called whenever the
        caret moves or the text changes."""
        if self._panel is not None:
            self._panel.reset_caret_blink()

    def _notify_input_position(self, ctx: DrawContext, caret_col: int) -> None:
        if ctx.panel is None:
            return
        sx, sy, _sw, _sh = ctx.screen_rect
        ctx.panel.request_text_input(int(sx + 1 + caret_col), int(sy), {})

    def _scroll_into_view(self, caret: int, field_w: int, length: int) -> None:
        # Keep the start (a character index) such that the caret stays inside
        # the field, measured in display columns (wide glyphs count as two).
        disp = self.text[: self.cursor] + self._preedit + self.text[self.cursor :]
        if caret < self._view:
            self._view = caret
        while self._view < caret and display_width(disp[self._view : caret]) > field_w - 1:
            self._view += 1
        self._view = max(0, min(self._view, max(0, length)))

    # --- events --------------------------------------------------------------

    def handle_event(self, event: Event) -> bool:
        handled = self._handle_event(event)
        if handled and event.type in (
            EventType.KEY, EventType.MOUSE_DOWN, EventType.MOUSE_DRAG,
            EventType.IME_COMPOSITION,
        ):
            # Any caret move or edit shows the caret immediately (resets blink).
            self._reset_blink()
        return handled

    def _handle_event(self, event: Event) -> bool:
        if event.type is EventType.IME_COMPOSITION:
            # Starting composition replaces any selection (it occupies the cursor).
            if self._preedit == "":
                self._delete_selection()
            self._preedit = event.hints.get("preedit", "")
            self._preedit_caret = event.hints.get("caret", len(self._preedit))
            return True
        if event.type is EventType.MOUSE_DOWN:
            idx = self._index_at_column(int(event.x or 0) - 1)
            if "shift" in event.modifiers:
                # Shift+click extends from the existing cursor (or selection).
                if self._anchor is None:
                    self._anchor = self.cursor
            else:
                # A plain press collapses the cursor and seeds the anchor a drag
                # will pivot around.
                self._anchor = idx
            self.cursor = idx
            return True
        if event.type is EventType.MOUSE_DRAG:
            if self._anchor is None:
                self._anchor = self.cursor
            self.cursor = self._index_at_column(int(event.x or 0) - 1)
            return True
        if event.type is not EventType.KEY:
            return False

        # Command shortcuts (Cmd/Ctrl) are consumed before text insertion, so a
        # chord like Cmd+A never types its letter into the field.
        if event.modifiers & {"ctrl", "cmd"}:
            return self._handle_command(event.key)

        ch = typed_char(event)
        if ch is not None:
            self._insert(ch)
            return True
        return self._handle_key(event.key, "shift" in event.modifiers)

    def _index_at_column(self, target: int) -> int:
        """The buffer index under display column ``target`` (field-local, padding
        already removed), walking visible characters by display width."""
        target = max(0, target)
        idx, col = self._view, 0
        while idx < len(self.text) and col < target:
            col += char_width(self.text[idx])
            idx += 1
        return max(0, min(len(self.text), idx))

    def _handle_command(self, key: str | None) -> bool:
        if key == "a":  # select all
            self._anchor = 0
            self.cursor = len(self.text)
            return True
        if key == "c":  # copy
            self._copy()
            return True
        if key == "x":  # cut
            if self._copy():
                self._delete_selection()
                self._changed()
            return True
        if key == "v":  # paste
            self._paste()
            return True
        return False

    def _copy(self) -> bool:
        """Put the current selection on the clipboard. Returns True if there was
        a selection to copy (cut relies on this to know whether to delete)."""
        text = self.selection_text
        if not text or self._panel is None:
            return False
        self._panel.set_clipboard(text)
        return True

    def _paste(self) -> None:
        if self._panel is None:
            return
        # A single-line field flattens any newlines the clipboard carries.
        text = self._panel.get_clipboard().replace("\r", "").replace("\n", " ")
        if not text:
            return
        self._preedit = ""
        self._preedit_caret = 0
        self._delete_selection()
        self.text = self.text[: self.cursor] + text + self.text[self.cursor :]
        self.cursor += len(text)
        self._changed()

    def _handle_key(self, key: str | None, extend: bool) -> bool:
        if key in ("left", "right", "home", "end"):
            return self._move(key, extend)
        if key == "backspace":
            if self._delete_selection():
                self._changed()
            elif self.cursor > 0:
                self.text = self.text[: self.cursor - 1] + self.text[self.cursor :]
                self.cursor -= 1
                self._changed()
            return True
        if key == "delete":
            if self._delete_selection():
                self._changed()
            elif self.cursor < len(self.text):
                self.text = self.text[: self.cursor] + self.text[self.cursor + 1 :]
                self._changed()
            return True
        if key == "enter":
            if self.on_submit is not None:
                self.on_submit(self.text)
            return self.on_submit is not None
        return False

    def _move(self, key: str | None, extend: bool) -> bool:
        sel = self._selection()
        if extend:
            if self._anchor is None:  # begin a keyboard selection from the cursor
                self._anchor = self.cursor
        elif sel is not None and key in ("left", "right"):
            # Plain left/right collapse a selection onto the matching edge.
            self.cursor = sel[0] if key == "left" else sel[1]
            self._anchor = None
            return True
        else:
            self._anchor = None
        if key == "left":
            self.cursor = max(0, self.cursor - 1)
        elif key == "right":
            self.cursor = min(len(self.text), self.cursor + 1)
        elif key == "home":
            self.cursor = 0
        elif key == "end":
            self.cursor = len(self.text)
        return True

    def _insert(self, ch: str) -> None:
        # A committed character ends any composition and replaces any selection.
        self._preedit = ""
        self._preedit_caret = 0
        self._delete_selection()
        self.text = self.text[: self.cursor] + ch + self.text[self.cursor :]
        self.cursor += 1
        self._changed()

    def _changed(self) -> None:
        if self.on_change is not None:
            self.on_change(self.text)
