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
from ..text import display_width, is_word_char, word_bounds
from ..theme import DEFAULT_THEME
from ._input import MultiClickTracker, typed_char
from .base import CONTROL_HEIGHT, Widget

# Corner radius of the field, in device pixels (dropped on a character grid).
_FIELD_RADIUS = 4.0

# Keys whose meaning changes to a whole-word operation when a word modifier
# (Ctrl / Alt-Option) is held: caret motion and forward/backward deletion.
_WORD_KEYS = frozenset({"left", "right", "backspace", "delete"})


class TextEdit(Widget):
    focusable = True
    wants_text_input = True  # engages IME / text-input while focused

    def __init__(
        self,
        text: str = "",
        on_change: Callable[[str], None] | None = None,
        on_submit: Callable[[str], None] | None = None,
        width: int = 24,
        right_pad: int = 0,
        style: Style = DEFAULT_STYLE,
        mask: str | None = None,
    ):
        self.text = text
        self.on_change = on_change
        self.on_submit = on_submit
        self.width = width
        # When set to a single glyph (e.g. "•"), the field is a password prompt:
        # every character is *displayed* as this glyph while ``self.text`` keeps
        # the real value. ``_display`` renders it and copy/cut are disabled so the
        # plaintext never leaves the field. None = an ordinary visible field.
        self._mask = mask
        # Columns reserved at the field's right edge for an external adornment
        # (e.g. a combo box chevron) that draws over the field box. The box still
        # spans the full width; only the text/caret region shrinks, so the
        # adornment never overlaps typed text.
        self.right_pad = right_pad
        self.style = style
        self.cursor = len(text)
        self._anchor: int | None = None  # selection start; None = no selection
        # Double/triple-click selection, keyed on the buffer index pressed: a
        # double-click takes the word, a triple-click the whole line (the field
        # is single-line, so that is select-all). ``_sel_base`` is the span the
        # multi-click fixed, so a following drag grows by that same unit; the
        # granularity is 1 (caret) / 2 (word) / 3 (line).
        self._clicks: MultiClickTracker[int] = MultiClickTracker()
        self._sel_base: tuple[int, int] | None = None
        self._sel_granularity = 1
        # True between a press inside the field and its release, so a drag that
        # began outside (empty space or another widget) and wandered in is
        # ignored rather than hijacking the selection.
        self._pressed = False
        self._view = 0          # first visible index into the displayed string
        self._preedit = ""      # IME marked (composition) text, not yet committed
        self._preedit_caret = 0  # caret offset within the preedit
        self._target_start = 0  # offset of the clause selected for conversion (0 = none)
        self._target_end = 0    # end of that clause (== start when none is selected)
        self._panel = None
        self._field_w = float("inf")  # field width; set at draw (permissive until then)
        self._focused_now = False  # last-drawn focus state, read by the blink tick
        self._blinking = False     # whether a caret-blink tick is registered
        # Blink phase the caret was last *rendered* at, so the tick can re-render
        # on the phase flip alone rather than on every frame (see _blink_tick).
        # None until the tick adopts the current phase.
        self._blink_phase: bool | None = None
        # Text measurement bound at draw time (proportional on GUI, columns on a
        # grid), so hit-testing between frames maps clicks the same way the glyphs
        # were laid out. A column-count fallback covers events before the first draw.
        self._measure = lambda t: float(display_width(t))

    # --- masking -------------------------------------------------------------

    def _display(self, s: str) -> str:
        """The on-screen form of a buffer run: ``s`` itself, or one mask glyph
        per character when the field is masked. Length-preserving (1 glyph per
        character), so cursor/selection/view indices into ``self.text`` still map
        one-to-one onto the returned string."""
        return self._mask * len(s) if self._mask else s

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
        self._measure = ctx.measure_text
        theme = ctx.theme or DEFAULT_THEME
        w = min(self.width, ctx.width)
        if w - self.right_pad < 3:
            return
        # One column of padding on each side, plus any right reserve for an
        # external adornment drawn over the field box (the box still spans w).
        field_w = w - 2 - self.right_pad
        self.cursor = max(0, min(self.cursor, len(self.text)))
        if self._anchor is not None:
            self._anchor = max(0, min(self._anchor, len(self.text)))

        # The displayed string has the preedit spliced in at the cursor. Masked
        # fields substitute a mask glyph for every character; ``_display`` keeps
        # the length, so the index math below (which addresses ``self.text``)
        # still lines up with this display string.
        mtext = self._display(self.text)
        disp = mtext[: self.cursor] + self._display(self._preedit) + mtext[self.cursor :]
        pre_start, pre_end = self.cursor, self.cursor + len(self._preedit)
        caret = self.cursor + (self._preedit_caret if self._preedit else 0)
        # Display range of the clause currently selected for conversion (the
        # composition's start while there's no such clause yet, i.e. raw kana
        # input). Its start is where the IME candidate window anchors (see
        # _notify_input_position); the whole range is drawn with a heavier
        # underline so the user can see which segment left/right is cycling
        # (empty while nothing is selected, so nothing is thickened).
        target_lo = pre_start + min(self._target_start, len(self._preedit))
        target_hi = pre_start + min(self._target_end, len(self._preedit))
        anchor_idx = target_lo
        # Selection indices address self.text directly, so they only line up
        # with the display string while no preedit is spliced in.
        sel = self._selection() if not self._preedit else None
        self._scroll_into_view(ctx, caret, field_w)

        field_full_w = min(float(self.width), ctx.size_units[0])
        self._field_w = field_full_w  # captured for hit-testing
        hovering = ctx.hovered_in(field_full_w)
        if hovering:
            # An I-beam over the editable area; one intent, resolved per backend.
            ctx.set_cursor("text")
        bg = theme.control_hover_bg if (hovering and not ctx.focused) else theme.control_bg
        field_h = ctx.size_units[1]
        ty = (field_h - 1.0) / 2.0  # center the text line within the field box
        # A flat, rounded field on vector backends, a plain fill on a character
        # grid. The fill goes first; the border is stroked last (end of draw),
        # so the text/caret backgrounds cannot paint over the border line.
        ctx.round_rect(0, 0, field_full_w, field_h, Style(bg=bg), radius=_FIELD_RADIUS, hints={"fill": True})

        # Place each glyph at its *measured* x offset (proportional on GUI, whole
        # columns on a grid), so a proportional font neither overlaps nor gaps and
        # a wide CJK glyph still reserves its full width. The offset is the
        # measured width of the prefix drawn so far, so positions are cumulative
        # and never drift. Per-glyph background keeps the selection highlight a
        # property of the cells it covers (the grid stores one style per cell).
        view = self._view
        sel_bg = theme.text_selection_bg if ctx.focused else theme.text_selection_inactive_bg
        col = 0.0          # measured offset of the prefix already drawn
        caret_col = None
        anchor_col = None
        prefix = ""        # disp[view:idx], the run measured for the next offset
        for idx in range(view, len(disp)):
            if idx == caret:
                caret_col = col
            if idx == anchor_idx:
                anchor_col = col
            ch = disp[idx]
            nxt = ctx.measure_text(prefix + ch)
            if nxt > field_w:  # this glyph would cross the field edge
                break
            marked = pre_start <= idx < pre_end
            selected = sel is not None and sel[0] <= idx < sel[1]
            attr = TextAttribute.UNDERLINE if marked else TextAttribute.NORMAL
            # The selected conversion clause gets a thick underline on top of the
            # composition's thin one (backends without a thick rule ignore the
            # extra bit and still draw the plain underline).
            if marked and target_lo <= idx < target_hi:
                attr |= TextAttribute.UNDERLINE_THICK
            # The selection reads as active only while the field holds focus: a
            # visible blue when focused, a muted neutral otherwise
            # (docs/interaction_states.md §5).
            fg = theme.accent if marked else theme.text
            cell_bg = sel_bg if selected else bg
            ctx.draw_text(1 + col, ty, ch, Style(fg=fg, bg=cell_bg, attr=attr))
            prefix += ch
            col = nxt
        if caret_col is None:  # caret sits at/after the last visible glyph
            caret_col = col
        if anchor_col is None:  # anchor sits at/after the last visible glyph
            anchor_col = col

        self._focused_now = ctx.focused
        if ctx.focused:
            # Drive the blink: register one tick the first time we draw focused;
            # it re-renders each frame so caret_visible toggles, and unregisters
            # itself once focus leaves (the tick reads _focused_now). Only the
            # vector caret is ours to blink — on a grid the terminal blinks its
            # own hardware cursor, so re-rendering for a blink we don't draw would
            # be wasted work.
            if ctx.vector_shapes and ctx.animated and not self._blinking and ctx.panel is not None:
                self._blink_phase = None  # re-adopt the phase for this focus run
                self._blinking = ctx.panel.request_animation_ticks(self._blink_tick)
            self._draw_caret(ctx, theme, disp, caret, caret_col, field_w, bg, ty)
            self._notify_input_position(ctx, anchor_col, field_h, disp, view, pre_start)

        # Border stroked last so the glyph/caret backgrounds above cannot paint
        # over it; accent while focused, a subtle outline otherwise.
        border = theme.accent if ctx.focused else theme.control_border
        ctx.round_rect(0, 0, field_full_w, field_h, Style(fg=border), radius=_FIELD_RADIUS)
        # Grid backends get no box frame on a one-row field, so the accent focus
        # ring resolves to bracket markers in the padding columns instead. The
        # right_pad reserve (e.g. a combo chevron) keeps the ``]`` clear of it.
        if ctx.focused:
            ctx.draw_focus_brackets(field_full_w, field_h, theme, bg=bg)

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
        # Unregister once focus has left, the panel is gone, or the field has left
        # the widget tree; otherwise re-render so the caret's blink phase advances
        # on screen — the tick is the only thing that rebuilds the display list.
        #
        # Tree-exit is the important case: when the field's dialog closes, draw
        # (which sets _focused_now) stops running, so _focused_now would stay a
        # stale True and this tick would re-render *forever* — a permanent
        # CPU-burning render loop leaked on every dialog open/close.
        if not self._focused_now or self._panel is None:
            self._blinking = False
            return False
        # Retire the moment the field stops being the focus leaf, which covers
        # both tree-exit and a plain focus move. Asking the focus chain directly
        # costs a short walk; the previous form inferred it from whether a full
        # render re-set _focused_now, which is why this tick used to have to
        # render every frame to stay correct.
        if self._panel.focused_leaf() is not self:
            self._focused_now = False
            self._blinking = False
            return False
        # The caret is a ~_CARET_BLINK second square wave, but this tick fires
        # every frame. Re-rendering on frames where the phase did not change
        # rebuilds the whole display list (and invalidates the backend's recorded
        # copy of it) ~30x per visible blink for no visible difference — which is
        # what made a focused field the most expensive thing on screen. Render on
        # the flip only; the first tick adopts the current phase silently.
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

    def _reset_blink(self) -> None:
        """Show the caret now by restarting its blink cycle — called whenever the
        caret moves or the text changes."""
        if self._panel is not None:
            self._panel.reset_caret_blink()

    def _notify_input_position(
        self, ctx: DrawContext, anchor_col: float, field_h: float,
        disp: str, view: int, pre_start: int,
    ) -> None:
        if ctx.panel is None:
            return
        sx, sy, _sw, _sh = ctx.screen_rect
        # Anchor the IME UI (candidate list, input-mode indicator) at the field's
        # bottom row, so the backend's caret rect bottom-edge lands on the field
        # bottom and the UI opens just *under* the field — not on top of the
        # composed text, which a tall (padded) field would otherwise overlap.
        #
        # `anchor_col` is the column of the currently-selected clause (see
        # `_target_start`) — the composition's start while there's no such
        # clause yet, i.e. raw kana input with nothing converted. It is
        # deliberately NOT the in-progress preedit caret, which moves right as
        # more characters are typed: native IME candidate windows (Notepad,
        # VS Code, ...) stay put while typing and only jump when the target
        # clause changes (converting, or cycling clauses with left/right);
        # feeding them the moving caret instead makes the window visibly
        # jitter rightward as a word gets longer.
        #
        # Pass the FRACTIONAL position through (no int() truncation): a field laid
        # out at a fractional base-unit origin — a dialog nudges its field by a
        # fraction of a row for vertical centering — would otherwise round to the
        # row above, so the candidate window opens misaligned with the field's
        # bottom edge. The backend maps these base-unit coordinates to pixels.
        y = sy + field_h - 1
        # Also report the x of every composition-character boundary (base units,
        # absolute). A platform whose IME positions its candidate window per
        # character range (macOS firstRectForCharacterRange:) can then answer for
        # the *exact* clause it is converting — which is what makes the window
        # follow left/right clause changes. The composition's glyph layout does
        # not move while cycling clauses (only the highlight does), so this list
        # stays valid even between the keystroke and the platform's next query.
        char_xs = [
            sx + 1 + (ctx.measure_text(disp[view:pre_start + k]) if pre_start + k >= view else 0.0)
            for k in range(len(self._preedit) + 1)
        ]
        ctx.panel.request_text_input(sx + 1 + anchor_col, y, {"ime_char_xs": char_xs})

    def _scroll_into_view(self, ctx: DrawContext, caret: int, field_w: int) -> None:
        # Keep the start (a character index) such that the caret stays inside the
        # field, measured in base units (proportional on GUI, columns on a grid)
        # so the visible window matches how the run is laid out in draw.
        mtext = self._display(self.text)
        disp = mtext[: self.cursor] + self._display(self._preedit) + mtext[self.cursor :]
        if caret < self._view:
            self._view = caret
        while self._view < caret and ctx.measure_text(disp[self._view : caret]) > field_w - 1:
            self._view += 1
        self._view = max(0, min(self._view, len(disp)))

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
            self._target_start = event.hints.get("target_start", 0)
            self._target_end = event.hints.get("target_end", 0)
            return True
        if event.type is EventType.MOUSE_DOWN:
            # Only the field is clickable, not the empty slot to its right.
            if event.x is not None and event.x >= self._field_w:
                return False
            self._pressed = True
            idx = self._index_at_column((event.x or 0) - 1)
            if "shift" in event.modifiers:
                # Shift+click extends from the existing cursor (or selection); it
                # is a fresh drag, not part of a double-click run.
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
                self._sel_base = self._word_range(idx)  # double-click: the word
            elif self._sel_granularity == 3:
                self._sel_base = (0, len(self.text))    # triple-click: whole line
            else:
                # A plain press collapses the cursor and seeds the anchor a drag
                # will pivot around.
                self._sel_base = None
                self._anchor = idx
                self.cursor = idx
                return True
            self._anchor, self.cursor = self._sel_base
            return True
        if event.type is EventType.MOUSE_UP:
            self._pressed = False  # gesture ends; a later stray drag won't extend
            return False
        if event.type is EventType.MOUSE_DRAG:
            # A drag only extends a selection the press began in this field; one
            # that wandered in from an outside press leaves the field alone.
            if not self._pressed:
                return False
            self._clicks.note_drag()
            self._extend_drag(self._index_at_column((event.x or 0) - 1))
            return True
        if event.type is not EventType.KEY:
            return False

        # Word-granularity caret motion / deletion: Ctrl (Windows, terminals) or
        # Alt/Option (macOS, terminals) turns an arrow or delete key into a
        # whole-word operation. Handled before the Cmd/Ctrl command branch so a
        # chord like Ctrl+Left is a word move, not an unhandled command chord.
        if event.modifiers & {"ctrl", "alt"} and event.key in _WORD_KEYS:
            return self._handle_key(event)

        # Command shortcuts (Cmd/Ctrl) are consumed before text insertion, so a
        # chord like Cmd+A never types its letter into the field.
        if event.modifiers & {"ctrl", "cmd"}:
            return self._handle_command(event.key)

        ch = typed_char(event)
        if ch is not None:
            self._insert(ch)
            return True
        return self._handle_key(event)

    def _index_at_column(self, target_x: float) -> int:
        """The buffer index nearest field-local x ``target_x`` (padding already
        removed), measured in base units so a proportional font hit-tests where
        its glyphs actually fall (columns on a grid)."""
        target = max(0.0, target_x)
        view = self._view
        idx, prev = view, 0.0
        while idx < len(self.text):
            cur = self._measure(self._display(self.text[view : idx + 1]))
            if cur >= target:
                # Snap to whichever glyph boundary (before / after) is nearer.
                return idx + 1 if (target - prev) > (cur - target) else idx
            prev = cur
            idx += 1
        return min(len(self.text), idx)

    def _word_range(self, idx: int) -> tuple[int, int]:
        """The half-open buffer range of the word at index ``idx`` — the run of
        one character class (:func:`~puikit.text.word_bounds`). Indices address
        the buffer character-by-character, matching the rest of the field."""
        return word_bounds(list(self.text), idx)

    def _extend_drag(self, idx: int) -> None:
        """Extend the active drag to buffer index ``idx``. At caret granularity
        the cursor simply moves there; after a double/triple click the selection
        grows to the union of the fixed base span and the word/line at ``idx``,
        keeping whole-word/line edges."""
        if self._sel_base is None:
            if self._anchor is None:
                self._anchor = self.cursor
            self.cursor = idx
            return
        b0, b1 = self._sel_base
        p0, p1 = self._word_range(idx) if self._sel_granularity == 2 else (0, len(self.text))
        # _selection() orders the endpoints, so cover both spans without tracking
        # drag direction.
        self._anchor, self.cursor = min(b0, p0), max(b1, p1)

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
        # A masked (password) field never surrenders its plaintext to the
        # clipboard; returning False here disables both copy and cut.
        if self._mask:
            return False
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
        self._target_start = 0
        self._target_end = 0
        self._delete_selection()
        self.text = self.text[: self.cursor] + text + self.text[self.cursor :]
        self.cursor += len(text)
        self._changed()

    def _handle_key(self, event: Event) -> bool:
        key = event.key
        extend = "shift" in event.modifiers
        # A held Ctrl (Windows/terminals) or Alt-Option (macOS/terminals) makes
        # caret motion and deletion operate on whole words.
        word = bool(event.modifiers & {"ctrl", "alt"})
        if key in ("left", "right", "home", "end"):
            return self._move(key, extend, word)
        if key == "backspace":
            return self._delete(forward=False, word=word)
        if key == "delete":
            return self._delete(forward=True, word=word)
        if key == "enter":
            if self.on_submit is not None:
                self.on_submit(self.text)
            return self.on_submit is not None
        return False

    def _move(self, key: str | None, extend: bool, word: bool = False) -> bool:
        sel = self._selection()
        if extend:
            if self._anchor is None:  # begin a keyboard selection from the cursor
                self._anchor = self.cursor
        elif sel is not None and key in ("left", "right") and not word:
            # Plain left/right collapse a selection onto the matching edge.
            self.cursor = sel[0] if key == "left" else sel[1]
            self._anchor = None
            return True
        else:
            self._anchor = None
        if key == "left":
            self.cursor = self._word_boundary(False) if word else max(0, self.cursor - 1)
        elif key == "right":
            self.cursor = self._word_boundary(True) if word else min(len(self.text), self.cursor + 1)
        elif key == "home":
            self.cursor = 0
        elif key == "end":
            self.cursor = len(self.text)
        return True

    def _delete(self, forward: bool, word: bool) -> bool:
        """Delete a character (or, with ``word``, to the next word boundary) in
        the given direction, or the selection if there is one. Always consumes
        the key, even when there is nothing left to remove at the edge."""
        if self._delete_selection():
            self._changed()
            return True
        if word:
            target = self._word_boundary(forward)
            if target != self.cursor:
                lo, hi = min(self.cursor, target), max(self.cursor, target)
                self.text = self.text[:lo] + self.text[hi:]
                self.cursor = lo
                self._changed()
            return True
        if forward:
            if self.cursor < len(self.text):
                self.text = self.text[: self.cursor] + self.text[self.cursor + 1 :]
                self._changed()
        elif self.cursor > 0:
            self.text = self.text[: self.cursor - 1] + self.text[self.cursor :]
            self.cursor -= 1
            self._changed()
        return True

    def _word_boundary(self, forward: bool) -> int:
        """The index of the next word boundary from the cursor in ``forward``
        direction. Moving forward skips a run of separators (whitespace /
        punctuation) then the word that follows; backward is the mirror. A word
        is an alphanumeric/underscore run (:func:`~puikit.text.is_word_char`),
        so this steps between words the way native fields do."""
        text = self.text
        i = self.cursor
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

    def _insert(self, ch: str) -> None:
        # A committed character ends any composition and replaces any selection.
        self._preedit = ""
        self._preedit_caret = 0
        self._target_start = 0
        self._target_end = 0
        self._delete_selection()
        self.text = self.text[: self.cursor] + ch + self.text[self.cursor :]
        self.cursor += 1
        self._changed()

    def _changed(self) -> None:
        if self.on_change is not None:
            self.on_change(self.text)
