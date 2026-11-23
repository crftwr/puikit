"""A multi-line editable text field: :class:`TextEdit`'s counterpart for text
that runs over several lines - a message to send, a note, a commit message.

The buffer is one string with ``\\n`` between lines. Each line wraps softly to
the field's width (at a space where one fits, else at the character that
would cross the edge), so the visual rows are a view of the buffer, never part
of it: copying, submitting and ``on_change`` see the text as typed.

Editing follows :class:`TextEdit` - the same caret, selection, word motion,
clipboard and IME composition, drawn the same flat way - and adds what more
than one line needs:

- **Up / Down** move by visual row, keeping the column the caret had when the
  vertical run began. On the first row Up (and on the last row Down) is left
  unhandled, so a host can give it a meaning of its own - recalling the
  previous message in a chat input, say. Page Up / Page Down move a field's
  height; Ctrl+Home / Ctrl+End (Cmd+Up / Cmd+Down on macOS) go to either end.
- **Home / End** go to the start and end of the visual row.
- **Enter** inserts a line break, or, with ``submit_on_enter``, submits
  (``on_submit``) - then a line break is Shift+Enter, Alt+Enter, Ctrl+Enter
  or Ctrl+J. A terminal cannot report Shift with Enter, but it sends Alt+Enter
  (ESC CR) and Ctrl+J (a line feed, which arrives as an Enter whose ``char``
  is ``"\\n"``), so every backend has a key for both. Without
  ``submit_on_enter``, Ctrl/Cmd+Enter submits.
- **Height.** The field measures as tall as its wrapped text, between
  ``min_rows`` and ``max_rows``; past that it scrolls (the wheel, or following
  the caret) and shows a scrollbar.
- **Paste** keeps line breaks (``\\r\\n`` and ``\\r`` become ``\\n``).
"""

from __future__ import annotations

from collections.abc import Callable

from ..backend import DEFAULT_STYLE, Style, TextAttribute
from ..event import Event, EventType
from ..layout import LayoutContext, SizeRequest
from ..panel import DrawContext
from ..text import is_word_char, word_bounds
from ..theme import DEFAULT_THEME
from ._input import MultiClickTracker, typed_char
from .base import CONTROL_HEIGHT, Widget

_FIELD_RADIUS = 4.0
_WORD_KEYS = frozenset({"left", "right", "backspace", "delete"})
#: Columns a tab advances, measured in spaces.
_TAB_SPACES = 4
#: Modifiers that turn Enter into a line break in a ``submit_on_enter`` field.
_NEWLINE_MODS = frozenset({"shift", "alt", "ctrl"})


class TextArea(Widget):
    focusable = True
    wants_text_input = True

    def __init__(
        self,
        text: str = "",
        on_change: Callable[[str], None] | None = None,
        on_submit: Callable[[str], None] | None = None,
        width: int = 40,
        min_rows: int = 1,
        max_rows: int = 8,
        submit_on_enter: bool = False,
        placeholder: str = "",
        style: Style = DEFAULT_STYLE,
    ):
        self.text = _normalize(text)
        self.on_change = on_change
        self.on_submit = on_submit
        #: Preferred width in base units (the field fills what it is given).
        self.width = width
        self.min_rows = max(1, int(min_rows))
        self.max_rows = max(self.min_rows, int(max_rows))
        self.submit_on_enter = submit_on_enter
        #: Muted text drawn while the field is empty; never part of ``text``.
        self.placeholder = placeholder
        self.style = style
        self.cursor = len(self.text)
        self._anchor: int | None = None
        self._clicks: MultiClickTracker[int] = MultiClickTracker()
        self._sel_base: tuple[int, int] | None = None
        self._sel_granularity = 1
        self._pressed = False
        self._preedit = ""
        self._preedit_caret = 0
        self._target_start = 0
        self._target_end = 0
        #: First visual row shown.
        self._top = 0
        #: Bring the caret into view at the next draw (set by edits and caret
        #: moves, not by the wheel, so scrolling away from the caret sticks).
        self._follow = True
        #: The x the caret keeps over a run of Up/Down presses (None: none).
        self._goal_x: float | None = None
        self._panel = None
        self._focused_now = False
        self._blinking = False
        self._blink_phase: bool | None = None
        # Measurement from the last draw; a column count until then.
        self._measure = lambda t: float(len(t))
        self._widths: dict[str, float] = {}
        self._measure_id = None
        # Layout of the last draw: rows of (start, end) indices into the
        # displayed string, at ``_wrap_w``; and the geometry hit-testing needs.
        self._rows: list[tuple[int, int]] = [(0, 0)]
        self._layout_key: tuple | None = None
        self._wrap_w = float("inf")
        self._text_x = 1.0
        self._text_y = 0.0
        self._visible = 1

    # --- text --------------------------------------------------------------

    def set_text(self, text: str) -> None:
        """Replace the whole text (the caret goes to its end). ``on_change``
        is not called: the host set it."""
        self.text = _normalize(text)
        self.cursor = len(self.text)
        self._anchor = None
        self._clear_preedit()
        self._top = 0
        self._follow = True
        self._goal_x = None

    def insert_text(self, text: str) -> None:
        """Insert ``text`` at the caret, replacing the selection, as a paste
        would."""
        text = _normalize(text)
        if not text:
            return
        self._clear_preedit()
        self._delete_selection()
        self.text = self.text[: self.cursor] + text + self.text[self.cursor :]
        self.cursor += len(text)
        self._changed()

    def _selection(self) -> tuple[int, int] | None:
        if self._anchor is None or self._anchor == self.cursor:
            return None
        return (min(self._anchor, self.cursor), max(self._anchor, self.cursor))

    @property
    def selection_text(self) -> str:
        sel = self._selection()
        return self.text[sel[0] : sel[1]] if sel else ""

    def _delete_selection(self) -> bool:
        sel = self._selection()
        self._anchor = None
        if sel is None:
            return False
        self.text = self.text[: sel[0]] + self.text[sel[1] :]
        self.cursor = sel[0]
        return True

    def _clear_preedit(self) -> None:
        self._preedit = ""
        self._preedit_caret = 0
        self._target_start = 0
        self._target_end = 0

    # --- layout ------------------------------------------------------------

    def _display(self) -> str:
        return self.text[: self.cursor] + self._preedit + self.text[self.cursor :]

    def _char_w(self, ch: str) -> float:
        w = self._widths.get(ch)
        if w is None:
            if ch == "\t":
                w = self._measure(" ") * _TAB_SPACES
            else:
                w = self._measure(ch)
            self._widths[ch] = w
        return w

    def _bind_measure(self, measure) -> None:
        # Widths are cached per measurer: a font change brings a new one.
        if measure is not self._measure_id:
            self._measure_id = measure
            self._measure = measure
            self._widths = {}
            self._layout_key = None

    def _wrap(self, disp: str, width: float) -> list[tuple[int, int]]:
        """The visual rows of ``disp`` at ``width``: ``(start, end)`` index
        pairs, the line break itself in no row. A line breaks after its last
        space that fits, else before the character that would cross."""
        rows: list[tuple[int, int]] = []
        width = max(width, 1.0)
        start = 0
        n = len(disp)
        while True:
            nl = disp.find("\n", start)
            line_end = n if nl < 0 else nl
            if start == line_end:
                rows.append((start, start))
            i = start
            while i < line_end:
                x = 0.0
                j = i
                space = -1
                while j < line_end:
                    cw = self._char_w(disp[j])
                    if x + cw > width and j > i:
                        break
                    x += cw
                    if disp[j] == " ":
                        space = j
                    j += 1
                if j < line_end and space >= i:
                    j = space + 1  # break after the space; it ends this row
                rows.append((i, j))
                i = j
            if nl < 0:
                break
            start = nl + 1
        return rows or [(0, 0)]

    def _layout(self, width: float) -> list[tuple[int, int]]:
        disp = self._display()
        key = (disp, width)
        if key != self._layout_key:
            self._rows = self._wrap(disp, width)
            self._layout_key = key
            self._wrap_w = width
        return self._rows

    def _disp_index(self, i: int) -> int:
        """A buffer index as an index into the displayed string."""
        return i if i <= self.cursor else i + len(self._preedit)

    def _row_of(self, idx: int, rows: list[tuple[int, int]] | None = None) -> int:
        """The row a caret at display index ``idx`` sits on. At a soft break
        (the end of one row is the start of the next) it is the next row."""
        rows = rows if rows is not None else self._rows
        lo, hi = 0, len(rows) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if rows[mid][0] <= idx:
                lo = mid
            else:
                hi = mid - 1
        return lo

    def _x_in_row(self, row: int, idx: int, disp: str) -> float:
        start, _end = self._rows[row]
        return sum(self._char_w(c) for c in disp[start:idx])

    def _index_in_row(self, row: int, x: float, disp: str) -> int:
        start, end = self._rows[row]
        # A soft-wrapped row's last index is the next row's first: the caret
        # cannot sit after the row's last character there, only before it.
        last = end
        nxt = self._rows[row + 1][0] if row + 1 < len(self._rows) else None
        if nxt == end and end > start:
            last = end - 1
        pos = 0.0
        for i in range(start, last):
            cw = self._char_w(disp[i])
            if pos + cw / 2 > x:
                return i
            pos += cw
        return last

    def content_rows(self, measure, width: float) -> int:
        """How many visual rows the text takes wrapped in a field ``width``
        base units wide (the padding included, the scrollbar not)."""
        self._bind_measure(measure)
        return len(self._wrap(self._display(), max(1.0, width - 2)))

    def measure(self, ctx: LayoutContext, axis: str, available: float) -> SizeRequest:
        """``x``: the preferred ``width``. ``y``: the wrapped text's height
        at ``available`` width (as :class:`MarkdownView` takes it), between
        ``min_rows`` and ``max_rows``, plus the padding on a pixel backend."""
        if axis == "x":
            w = float(self.width)
            return SizeRequest(min=4.0, preferred=w, max=None)
        width = available if available and available != float("inf") else float(self.width)
        rows = self.content_rows(lambda t: ctx.measure_text(t), width)
        pad = 0.0 if ctx.snap else CONTROL_HEIGHT - 1.0
        lo = self.min_rows + pad
        h = max(self.min_rows, min(self.max_rows, rows)) + pad
        return SizeRequest(min=lo, preferred=h, max=self.max_rows + pad)

    # --- drawing -----------------------------------------------------------

    def draw(self, ctx: DrawContext) -> None:
        self._panel = ctx.panel
        self._bind_measure(ctx.measure_text)
        theme = ctx.theme or DEFAULT_THEME
        fw, fh = ctx.size_units
        if fw < 4 or fh < 1:
            return
        self.cursor = max(0, min(self.cursor, len(self.text)))
        if self._anchor is not None:
            self._anchor = max(0, min(self._anchor, len(self.text)))
        vpad = 0.0 if not ctx.pixel_layout else min((CONTROL_HEIGHT - 1.0) / 2, max(0.0, (fh - 1) / 2))
        visible = max(1, int((fh - 2 * vpad) + 1e-6))
        self._visible = visible
        self._text_x, self._text_y = 1.0, vpad
        rows = self._layout(fw - 2)
        bar = len(rows) > visible
        if bar:
            rows = self._layout(fw - 3)
        disp = self._display()
        caret = self.cursor + (self._preedit_caret if self._preedit else 0)
        caret_row = self._row_of(caret, rows)
        if self._follow:
            if caret_row < self._top:
                self._top = caret_row
            elif caret_row >= self._top + visible:
                self._top = caret_row - visible + 1
            self._follow = False
        self._top = max(0, min(self._top, max(0, len(rows) - visible)))

        hovering = ctx.hovered_in(fw)
        if hovering:
            ctx.set_cursor("text")
        bg = theme.control_hover_bg if (hovering and not ctx.focused) else theme.control_bg
        ctx.round_rect(0, 0, fw, fh, Style(bg=bg), radius=_FIELD_RADIUS, hints={"fill": True})

        pre_start, pre_end = self.cursor, self.cursor + len(self._preedit)
        target_lo = pre_start + min(self._target_start, len(self._preedit))
        target_hi = pre_start + min(self._target_end, len(self._preedit))
        sel = self._selection() if not self._preedit else None
        sel_bg = theme.text_selection_bg if ctx.focused else theme.text_selection_inactive_bg

        caret_xy = None
        anchor_xy = None
        for vis in range(visible):
            r = self._top + vis
            if r >= len(rows):
                break
            start, end = rows[r]
            y = vpad + vis
            x = 0.0
            run, run_x, run_key = "", 0.0, None
            for i in range(start, end + 1):
                if i == caret and caret_row == r:
                    caret_xy = (x, y)
                if i == target_lo and pre_start <= i <= pre_end and self._preedit:
                    anchor_xy = (x, y)
                if i == end:
                    break
                ch = disp[i]
                marked = pre_start <= i < pre_end
                key = (marked, marked and target_lo <= i < target_hi,
                       sel is not None and sel[0] <= i < sel[1])
                if key != run_key and run:
                    self._draw_run(ctx, theme, run, run_x, y, run_key, bg, sel_bg)
                    run = ""
                if not run:
                    run_x, run_key = x, key
                run += " " * _TAB_SPACES if ch == "\t" else ch
                x += self._char_w(ch)
            if run:
                self._draw_run(ctx, theme, run, run_x, y, run_key, bg, sel_bg)
        if not disp and self.placeholder:
            text = self.placeholder
            limit = fw - 2
            while text and ctx.measure_text(text) > limit:
                text = text[:-1]
            if text:
                ctx.draw_text(1, vpad, text,
                              Style(fg=theme.muted_text, bg=bg, attr=TextAttribute.DIM))
        if bar:
            ratio = visible / len(rows)
            span = max(1, len(rows) - visible)
            ctx.draw_scrollbar(int(fw - 1), int(vpad), visible, self._top / span, ratio)

        self._focused_now = ctx.focused
        if ctx.focused:
            if ctx.vector_shapes and ctx.animated and not self._blinking and ctx.panel is not None:
                self._blink_phase = None
                self._blinking = ctx.panel.request_animation_ticks(self._blink_tick)
            if caret_xy is not None:
                cx, cy = caret_xy
                glyph = disp[caret] if caret < len(disp) and disp[caret] not in "\n\t" else " "
                ctx.draw_caret(1 + cx, cy, height=1.0, theme=theme, glyph=glyph,
                               visible=ctx.caret_visible, on=bg)
            self._notify_input_position(ctx, anchor_xy or caret_xy, disp)

        if ctx.vector_shapes:
            border = theme.accent if ctx.focused else theme.control_border
            ctx.round_rect(0, 0, fw, fh, Style(fg=border), radius=_FIELD_RADIUS)

    def _draw_run(self, ctx, theme, text, x, y, key, bg, sel_bg) -> None:
        marked, target, selected = key
        attr = TextAttribute.UNDERLINE if marked else TextAttribute.NORMAL
        if target:
            attr |= TextAttribute.UNDERLINE_THICK
        fg = theme.accent if marked else theme.text
        if selected:
            ctx.fill_mark(1 + x, y, ctx.measure_text(text), 1.0, sel_bg)
        ctx.draw_text(1 + x, y, text, Style(fg=fg, bg=sel_bg if selected else bg, attr=attr))

    def _notify_input_position(self, ctx: DrawContext, xy, disp: str) -> None:
        if ctx.panel is None or xy is None:
            return
        sx, sy, _sw, _sh = ctx.screen_rect
        x, y = xy
        start = self.cursor
        row = self._row_of(start)
        base = self._x_in_row(row, start, disp) if row < len(self._rows) else 0.0
        char_xs = []
        acc = base
        for k in range(len(self._preedit) + 1):
            char_xs.append(sx + 1 + acc)
            if k < len(self._preedit):
                acc += self._char_w(self._preedit[k])
        ctx.panel.request_text_input(sx + 1 + x, sy + y, {"ime_char_xs": char_xs})

    def _blink_tick(self) -> bool:
        # The same retirement rules as TextEdit's blink (see there): stop once
        # focus has left, the field has left the tree, or the window closed.
        if not self._focused_now or self._panel is None:
            self._blinking = False
            return False
        if self._panel.focused_leaf() is not self or self._panel.window_closed:
            self._focused_now = False
            self._blinking = False
            return False
        phase = self._panel.caret_visible
        if phase == self._blink_phase:
            return True
        if self._blink_phase is None:
            self._blink_phase = phase
            return True
        self._blink_phase = phase
        self._focused_now = False
        self._panel.render()
        if not self._focused_now:
            self._blinking = False
            return False
        return True

    # --- events ------------------------------------------------------------

    def handle_event(self, event: Event) -> bool:
        handled = self._handle_event(event)
        if handled and event.type in (EventType.KEY, EventType.MOUSE_DOWN,
                                      EventType.MOUSE_DRAG, EventType.IME_COMPOSITION):
            if self._panel is not None:
                self._panel.reset_caret_blink()
        return handled

    def _handle_event(self, event: Event) -> bool:
        if event.type is EventType.IME_COMPOSITION:
            if self._preedit == "":
                self._delete_selection()
            self._preedit = event.hints.get("preedit", "")
            self._preedit_caret = event.hints.get("caret", len(self._preedit))
            self._target_start = event.hints.get("target_start", 0)
            self._target_end = event.hints.get("target_end", 0)
            self._follow = True
            return True
        if event.type is EventType.MOUSE_SCROLL:
            amount = event.hints.get("scroll_units")
            if amount is None:
                amount = float(event.scroll or 0)
            step = -int(round(amount)) or (-1 if amount > 0 else 1 if amount < 0 else 0)
            if not step:
                return False
            limit = max(0, len(self._rows) - self._visible)
            top = max(0, min(limit, self._top + step))
            if top == self._top:
                return False  # at the end: let an enclosing view scroll
            self._top = top
            return True
        if event.type is EventType.MOUSE_DOWN:
            self._pressed = True
            idx = self._index_at(event.x or 0.0, event.y or 0.0)
            self._goal_x = None
            if "shift" in event.modifiers:
                if self._anchor is None:
                    self._anchor = self.cursor
                self.cursor = idx
                self._sel_base = None
                self._sel_granularity = 1
                self._clicks.reset()
                return True
            count = self._clicks.press(idx)
            self._sel_granularity = (count - 1) % 3 + 1
            if self._sel_granularity == 2:
                self._sel_base = word_bounds(list(self.text), idx)
            elif self._sel_granularity == 3:
                self._sel_base = self._line_bounds(idx)
            else:
                self._sel_base = None
                self._anchor = idx
                self.cursor = idx
                return True
            self._anchor, self.cursor = self._sel_base
            return True
        if event.type is EventType.MOUSE_UP:
            self._pressed = False
            return False
        if event.type is EventType.MOUSE_DRAG:
            if not self._pressed:
                return False
            self._clicks.note_drag()
            self._extend_drag(self._index_at(event.x or 0.0, event.y or 0.0))
            self._follow = True
            return True
        if event.type is not EventType.KEY:
            return False
        handled = self._handle_key(event)
        if handled:
            self._follow = True
        return handled

    def _handle_key(self, event: Event) -> bool:
        key = event.key
        mods = event.modifiers or frozenset()
        extend = "shift" in mods
        if key not in ("up", "down", "pageup", "pagedown"):
            self._goal_x = None
        if key == "enter":
            return self._enter(event)
        if key == "j" and "ctrl" in mods and not mods & {"cmd", "alt"}:
            self.insert_text("\n")  # a GUI's Ctrl+J; a terminal sends it as Enter
            return True
        if mods & {"ctrl", "alt"} and key in _WORD_KEYS and "cmd" not in mods:
            if key in ("left", "right"):
                return self._move_to(self._word_boundary(key == "right"), extend)
            return self._delete(forward=key == "delete", word=True)
        if "cmd" in mods and key in ("left", "right"):
            return self._move_to(self._row_edge(key == "right"), extend)
        if ("cmd" in mods and key in ("up", "down")) or \
                ("ctrl" in mods and key in ("home", "end")):
            return self._move_to(0 if key in ("up", "home") else len(self.text), extend)
        if "cmd" in mods and key == "backspace":
            return self._delete_to_line_start()
        if mods & {"ctrl", "cmd"}:
            return self._command(key)
        ch = typed_char(event)
        if ch is not None:
            self.insert_text(ch)
            return True
        if key in ("left", "right"):
            sel = self._selection()
            if sel is not None and not extend:
                self.cursor = sel[0] if key == "left" else sel[1]
                self._anchor = None
                return True
            step = -1 if key == "left" else 1
            return self._move_to(max(0, min(len(self.text), self.cursor + step)), extend)
        if key in ("home", "end"):
            return self._move_to(self._row_edge(key == "end"), extend)
        if key in ("up", "down"):
            return self._vertical(-1 if key == "up" else 1, extend, edge_unhandled=True)
        if key in ("pageup", "pagedown"):
            return self._vertical((-1 if key == "pageup" else 1) * max(1, self._visible - 1),
                                  extend, edge_unhandled=False)
        if key == "backspace":
            return self._delete(forward=False, word=False)
        if key == "delete":
            return self._delete(forward=True, word=False)
        return False

    def _enter(self, event: Event) -> bool:
        mods = event.modifiers or frozenset()
        newline_key = bool(mods & _NEWLINE_MODS) or event.char == "\n"
        if self.submit_on_enter:
            if newline_key:
                self.insert_text("\n")
                return True
            return self._submit()
        if mods & {"ctrl", "cmd"} and event.char != "\n":
            return self._submit()
        self.insert_text("\n")
        return True

    def _submit(self) -> bool:
        if self.on_submit is None:
            return False
        self._clear_preedit()
        self.on_submit(self.text)
        return True

    def _command(self, key: str | None) -> bool:
        if key == "a":
            self._anchor = 0
            self.cursor = len(self.text)
            return True
        if key == "c":
            self._copy()
            return True
        if key == "x":
            if self._copy():
                self._delete_selection()
                self._changed()
            return True
        if key == "v":
            if self._panel is not None:
                self.insert_text(self._panel.get_clipboard() or "")
            return True
        return False

    def _copy(self) -> bool:
        text = self.selection_text
        if not text or self._panel is None:
            return False
        self._panel.set_clipboard(text)
        return True

    # --- motion ------------------------------------------------------------

    def _move_to(self, idx: int, extend: bool) -> bool:
        if extend:
            if self._anchor is None:
                self._anchor = self.cursor
        else:
            self._anchor = None
        self.cursor = max(0, min(len(self.text), idx))
        return True

    def _row_edge(self, end: bool) -> int:
        """The start (or end) of the caret's visual row, as a buffer index.
        Without a layout yet (no draw), the logical line's."""
        if self._layout_key is None or self._preedit:
            return self._line_bounds(self.cursor)[1 if end else 0]
        row = self._row_of(self.cursor)
        start, stop = self._rows[row]
        if not end:
            return start
        nxt = self._rows[row + 1][0] if row + 1 < len(self._rows) else None
        # A soft-wrapped row ends where the next begins: stop before its
        # trailing space so the caret stays on this row.
        if nxt == stop and stop > start:
            return stop - 1 if self.text[stop - 1] == " " else stop
        return stop

    def _vertical(self, delta: int, extend: bool, *, edge_unhandled: bool) -> bool:
        if self._layout_key is None:
            self._layout(self._wrap_w if self._wrap_w != float("inf")
                         else max(1.0, float(self.width) - 2))
        disp = self._display()
        row = self._row_of(self.cursor)
        target = row + delta
        if target < 0 or target >= len(self._rows):
            if edge_unhandled and not extend and abs(delta) == 1 \
                    and row in (0, len(self._rows) - 1):
                return False  # the host's: history recall, focus moving on
            # Past either end: to the start (end) of the text.
            return self._move_to(0 if delta < 0 else len(self.text), extend)
        if self._goal_x is None:
            self._goal_x = self._x_in_row(row, self.cursor, disp)
        idx = self._index_in_row(target, self._goal_x, disp)
        goal = self._goal_x
        self._move_to(idx, extend)
        self._goal_x = goal
        return True

    def _index_at(self, x: float, y: float) -> int:
        """The buffer index nearest widget-local ``(x, y)``."""
        if self._layout_key is None:
            return len(self.text)
        row = self._top + int(max(0.0, y - self._text_y))
        row = max(0, min(len(self._rows) - 1, row))
        return min(len(self.text), self._index_in_row(row, x - self._text_x, self._display()))

    def _line_bounds(self, idx: int) -> tuple[int, int]:
        start = self.text.rfind("\n", 0, idx) + 1
        end = self.text.find("\n", idx)
        return start, len(self.text) if end < 0 else end

    def _extend_drag(self, idx: int) -> None:
        if self._sel_base is None:
            if self._anchor is None:
                self._anchor = self.cursor
            self.cursor = idx
            return
        b0, b1 = self._sel_base
        if self._sel_granularity == 2:
            p0, p1 = word_bounds(list(self.text), idx)
        else:
            p0, p1 = self._line_bounds(idx)
        self._anchor, self.cursor = min(b0, p0), max(b1, p1)

    def _word_boundary(self, forward: bool) -> int:
        text, i = self.text, self.cursor
        if forward:
            n = len(text)
            while i < n and not is_word_char(text[i]):
                i += 1
            while i < n and is_word_char(text[i]):
                i += 1
        else:
            while i > 0 and not is_word_char(text[i - 1]):
                i -= 1
            while i > 0 and is_word_char(text[i - 1]):
                i -= 1
        return i

    # --- deletion ----------------------------------------------------------

    def _delete(self, forward: bool, word: bool) -> bool:
        if self._delete_selection():
            self._changed()
            return True
        if word:
            target = self._word_boundary(forward)
        else:
            target = self.cursor + (1 if forward else -1)
        target = max(0, min(len(self.text), target))
        if target != self.cursor:
            lo, hi = min(self.cursor, target), max(self.cursor, target)
            self.text = self.text[:lo] + self.text[hi:]
            self.cursor = lo
            self._changed()
        return True

    def _delete_to_line_start(self) -> bool:
        sel = self._selection()
        hi = sel[1] if sel is not None else self.cursor
        lo = self._line_bounds(sel[0] if sel is not None else self.cursor)[0]
        if hi == lo and lo > 0 and sel is None:
            lo -= 1  # at a line's start: join it to the line above
        self._anchor = None
        if hi > lo:
            self.text = self.text[:lo] + self.text[hi:]
            self.cursor = lo
            self._changed()
        return True

    def _changed(self) -> None:
        self._follow = True
        if self.on_change is not None:
            self.on_change(self.text)


def _normalize(text: str) -> str:
    return (text or "").replace("\r\n", "\n").replace("\r", "\n")
