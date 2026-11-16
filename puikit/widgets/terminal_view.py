"""TerminalView — an embedded terminal drawn through the DrawContext
(prototype, discussion xefm#900).

libghostty-vt (:mod:`puikit._ghostty`) does the emulation, :mod:`puikit._pty`
runs the child, and this widget only draws cells and turns PuiKit key events
into the bytes the running program expects. It is a cell grid like every
other PuiKit widget, so the same code draws on a GUI window and inside a TUI
(nested, the way tmux does it).

Threading: the pty is read on a worker thread; its bytes are queued and the
UI thread feeds them to libghostty and redraws — through
``panel.call_on_main_thread`` where the backend dispatches, else on the
animation tick. libghostty is only ever touched on the UI thread.

Redraw cost: every frame draws the cached runs of every row, but a row is
rebuilt from libghostty (one ctypes walk over its cells) only when its dirty
flag says it changed. ``stats`` reports both.

Not in the prototype: selection and copy, mouse reporting to the program,
bracketed paste through libghostty's paste encoder, ConPTY (Windows).
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable

from .. import _ghostty as G
from .. import _pty
from ..backend import Style, TextAttribute
from ..event import Event, EventType
from ..font import Font
from ..panel import DrawContext
from ..text import display_width
from . import _cell_glyphs
from .base import Widget


def available() -> str | None:
    """None when an embedded terminal can run here, else why not."""
    if not _pty.SUPPORTED:
        return "no pty on this platform yet (ConPTY not implemented)"
    return G.load_error()


def default_shell() -> list[str]:
    return [os.environ.get("SHELL") or "/bin/sh", "-l"]


#: Every cell is drawn in a fixed-advance face: a GUI's base font may be
#: proportional, and then neither columns nor wide glyphs line up.
_MONO = Font(monospace=True)

_MODS = {"shift": G.MODS_SHIFT, "ctrl": G.MODS_CTRL, "alt": G.MODS_ALT}


class TerminalView(Widget):
    focusable = True
    wants_text_input = True   # IME composes into the shell's command line
    captures_tab = True       # Tab is completion here, not focus traversal

    def __init__(self, argv: list[str] | None = None, *, cwd: str | None = None,
                 env: dict[str, str] | None = None,
                 on_title: Callable[[str], None] | None = None,
                 on_pwd: Callable[[str], None] | None = None,
                 on_exit: Callable[[int | None], None] | None = None) -> None:
        self.argv = argv or default_shell()
        self.cwd = cwd
        self.env = env
        self.on_title = on_title
        self.on_pwd = on_pwd
        self.on_exit = on_exit
        self.title = ""
        self.pwd = ""
        #: What the bar shown after the child exits tells the user to do;
        #: Enter restarts the child unless a subclass handles it first.
        self.exit_hint = "press Enter to restart"
        self._term: G.Terminal | None = None
        self._proc: _pty.PtyProcess | None = None
        self._panel = None
        self._lock = threading.Lock()
        self._pending: list[bytes] = []
        self._posted = False
        self._use_ticks = False
        self._ticking = False
        self._exited: int | None | bool = False
        self._preedit = ""
        self._preedit_caret = 0
        self._glyph_w: dict[str, float] = {}
        self._w_units = self._h_units = 0.0
        self._cw = 1.0
        self._pitch = 1.0
        self._scroll_acc = 0.0          # sub-row trackpad scroll not yet sent
        self._mouse_button: int | None = None   # button the program saw pressed
        self._mouse_up_at = 0.0
        self._held_since: float | None = None   # synchronized output in progress
        self._last_frame = None                 # (cursor, colors) of the last update
        self._sb_w = 0.0                        # scrollbar width; 0 on a grid
        self._sb_drag = False
        self._sb_until = 0.0                    # keep the bar up until (monotonic)
        self._exit_pending: Callable[[], None] | None = None
        self._gen = 0  # bumps per child, so a dead child's late exit is ignored
        # Prototype instrumentation, shown by the demo page.
        self.stats = {"frames": 0, "rows_rebuilt": 0, "last_update_ms": 0.0,
                      "last_draw_ms": 0.0, "bytes": 0}

    # --- lifecycle ---------------------------------------------------------------
    def _start(self, ctx: DrawContext, cols: int, rows: int) -> None:
        theme = ctx.theme
        fg = tuple(theme.text[:3]) if theme else None
        bg_c = theme.surfaces.get("content") if theme else None
        bg = tuple(bg_c[:3]) if bg_c else None
        self._term = G.Terminal(cols, rows, write_pty=self._write_pty,
                                on_title=self._title_changed,
                                on_pwd=self._pwd_changed, fg=fg, bg=bg,
                                on_render_hold=self._render_hold)
        # Probe once, on the UI thread: a backend without main-thread dispatch
        # gets its pty output drained on the animation tick instead.
        self._use_ticks = not ctx.panel.call_on_main_thread(lambda: None)
        self._exited = False
        self._gen += 1
        gen = self._gen
        self._proc = _pty.PtyProcess(self.argv, cols=cols, rows=rows, cwd=self.cwd,
                                     env=self.env, on_output=self._on_output,
                                     on_exit=lambda code: self._on_child_exit(gen, code))

    def close(self) -> None:
        if self._proc is not None:
            self._proc.close()
            self._proc = None
        if self._term is not None:
            self._term.close()
            self._term = None

    def restart(self) -> None:
        self.close()
        self._exited = False  # the next draw starts a fresh child

    # --- worker-thread side ---------------------------------------------------------
    def _on_output(self, data: bytes) -> None:
        with self._lock:
            self._pending.append(data)
            if self._posted or self._use_ticks:
                return
            self._posted = True
        panel = self._panel
        if panel is not None:
            panel.call_on_main_thread(self._drain_and_render)

    def _on_child_exit(self, gen: int, code: int | None) -> None:
        def done() -> None:
            if gen != self._gen:
                return  # a child we already replaced
            self._drain()
            self._exited = code
            if self.on_exit:
                self.on_exit(code)
            if self._panel is not None:
                self._panel.render()
        panel = self._panel
        if panel is not None and not self._use_ticks:
            panel.call_on_main_thread(done)
        else:
            self._exit_pending = done

    # --- UI-thread side ----------------------------------------------------------------
    def _drain(self) -> bool:
        with self._lock:
            chunks, self._pending = self._pending, []
            self._posted = False
        if not chunks or self._term is None:
            return False
        data = b"".join(chunks)
        self.stats["bytes"] += len(data)
        self._term.write(data)
        return True

    def _drain_and_render(self) -> None:
        if self._drain() and self._panel is not None:
            self._panel.render()

    def _tick(self) -> bool:
        done = self._exit_pending
        if done is not None:
            self._exit_pending = None
            done()
        elif self._drain() and self._panel is not None:
            self._panel.render()
        alive = self._proc is not None and self._exited is False
        if not alive:
            self._ticking = False
        return alive

    #: Longest a synchronized-output hold keeps the last frame up: a program
    #: that sets mode 2026 and never resets it must not freeze the view.
    HOLD_MAX_SECONDS = 0.2

    def _render_hold(self, held: bool) -> None:
        """Mode 2026 began or ended (called inside a vt write, on the UI
        thread). While held, frames show the screen as it was before the
        program started its redraw — eosh clears the line and writes it
        again, and a frame in between showed it blank."""
        if held:
            self._held_since = time.monotonic()
            if self._panel is not None:
                self._panel.call_later(self.HOLD_MAX_SECONDS + 0.01, self._panel.render)
        else:
            self._held_since = None

    def _write_pty(self, data: bytes) -> None:
        if self._proc is not None:
            self._proc.write(data)

    def _title_changed(self, title: str) -> None:
        self.title = title
        if self.on_title:
            self.on_title(title)

    def _pwd_changed(self, pwd: str) -> None:
        self.pwd = pwd
        if self.on_pwd:
            self.on_pwd(pwd)

    def send(self, data: bytes | str) -> None:
        """Type ``data`` into the terminal as if the user had."""
        if isinstance(data, str):
            data = data.encode("utf-8")
        if self._proc is not None and self._exited is False:
            if self._term is not None:
                self._term.scroll(None)
            self._proc.write(data)

    # --- drawing -------------------------------------------------------------------------
    def draw(self, ctx: DrawContext) -> None:
        self._panel = ctx.panel
        why = available()
        if why is not None:
            ctx.draw_text(0, 0, "Embedded terminal unavailable:", Style(attr=TextAttribute.BOLD))
            ctx.draw_text(0, 1, why[: max(0, ctx.width)])
            return
        # A cell is the mono face's advance wide and its line tall, in base
        # units: one by one on a character grid, whatever the face gives on GUI.
        mono = Style(font=_MONO)
        pitch = ctx.line_height(mono) or 1.0
        cw = (ctx.measure_text("M" * 16, mono) / 16) or 1.0
        self._cw = cw
        self._pitch = pitch
        w_units, h_units = ctx.size_units
        # The scrollbar takes no columns: the program inside sees the same grid
        # it would in a terminal. On pixel backends it is overlaid on the right
        # edge while the view is scrolled (see _scrollbar_visible); a character
        # grid has none (the host terminal has its own scrollback).
        self._sb_w = float(ctx.panel.backend.scrollbar_units) if ctx.pixel_layout else 0.0
        self._w_units, self._h_units = w_units, h_units
        cols = max(2, int(w_units / cw + 1e-6))
        rows = max(2, int(h_units / pitch + 1e-6))
        if self._term is None and self._exited is False:
            self._start(ctx, cols, rows)
        if self._term is None:
            return
        if (cols, rows) != (self._term.cols, self._term.rows):
            bw, bh = ctx.base_pixel_size
            self._term.resize(cols, rows, int(bw * cw), int(bh * pitch))
            if self._proc is not None:
                self._proc.resize(cols, rows, int(cols * bw * cw), int(rows * bh * pitch))
        if self._use_ticks and not self._ticking and self._exited is False:
            self._ticking = ctx.panel.request_animation_ticks(self._tick)
        self._drain()

        t0 = time.perf_counter()
        held = (self._held_since is not None and self._last_frame is not None
                and time.monotonic() - self._held_since < self.HOLD_MAX_SECONDS)
        if held:
            rebuilt = 0
            cursor, colors = self._last_frame
        else:
            rebuilt, cursor, colors = self._term.update()
            self._last_frame = (cursor, colors)
        t1 = time.perf_counter()
        dfg = colors.foreground.tuple()
        dbg = colors.background.tuple()
        # Over an animated / image background (ctx.wallpaper) the terminal's
        # default background is left unpainted so the scene shows through, the
        # way the host's own panes do; cells with a colour of their own still
        # fill. Elsewhere the page is the terminal's background colour.
        reveal = ctx.wallpaper
        page_bg = None if reveal else dbg
        if not reveal:
            ctx.fill_rect(0, 0, w_units, h_units, Style(bg=dbg))
        # On pixel backends box drawing and block elements are drawn as shapes
        # filling the cell; from the font they leave gaps between rows.
        shapes = ctx.pixel_layout
        bw, bh = ctx.base_size
        px = (1.0 / bw if bw else 0.1, 1.0 / bh if bh else 0.05)
        # The text system leaves a run of only spaces un-underlined (and puts a
        # run's rule where its own glyphs say), so on pixel backends the
        # underline is a rule drawn here, the same for every run in the row.
        if shapes:
            fm = ctx.font_metrics(mono)
            ul_y = fm.ascent + max(px[1] * 1.5, fm.descent * 0.35)
        for y, runs in enumerate(self._term.row_cache):
            ty = y * pitch
            for run in runs:
                style = self._style(run, dfg, dbg, page_bg)
                x = run.col * cw
                if run.bg is not None or run.flags & G.STYLE_INVERSE:
                    ctx.fill_rect(x, ty, run.width * cw, pitch, Style(bg=style.bg))
                if run.wide:
                    # A fallback CJK face is rarely exactly two cells wide;
                    # centre the glyph in its two-cell slot.
                    gw = self._glyph_w.get(run.text)
                    if gw is None:
                        gw = self._glyph_w[run.text] = ctx.measure_text(run.text, style)
                    x += max(0.0, (2 * cw - gw) / 2)
                if shapes and run.flags & G.STYLE_UNDERLINE_MASK:
                    thick = (run.flags & G.STYLE_UNDERLINE_MASK) >> G.STYLE_UNDERLINE_SHIFT >= 2
                    ctx.fill_rect(run.col * cw, ty + ul_y, run.width * cw,
                                  px[1] * (2 if thick else 1),
                                  Style(bg=style.underline_color or style.fg))
                    style = Style(fg=style.fg, bg=style.bg, font=style.font,
                                  attr=style.attr & ~(TextAttribute.UNDERLINE
                                                      | TextAttribute.UNDERLINE_THICK))
                if shapes and _cell_glyphs.SPECIAL.search(run.text):
                    self._draw_with_shapes(ctx, run, style, x, ty, cw, pitch, px, dbg)
                else:
                    ctx.draw_text(x, ty, run.text, style)
        self._draw_cursor(ctx, cursor, colors, pitch, cw, dfg, dbg)
        if self._scrollbar_visible():
            total, offset, length = self._term.scrollbar()
            ctx.draw_scrollbar(w_units - self._sb_w, 0, h_units,
                               offset / (total - length), length / total)
        if self._exited is not False:
            msg = f" [process exited ({self._exited}) - {self.exit_hint}] "
            ctx.draw_text(0.0, (rows - 1) * pitch, msg[:cols],
                          Style(fg=dbg, bg=dfg, attr=TextAttribute.BOLD, font=_MONO))
        t2 = time.perf_counter()
        st = self.stats
        st["frames"] += 1
        st["rows_rebuilt"] = rebuilt
        st["last_update_ms"] = (t1 - t0) * 1000
        st["last_draw_ms"] = (t2 - t1) * 1000

    @staticmethod
    def _draw_with_shapes(ctx, run, style, x, ty, cw, pitch, px, dbg) -> None:
        """A run holding box-drawing / block glyphs: those drawn as shapes, the
        text between them as text, each at its own column."""
        start = 0
        text = run.text
        for m in _cell_glyphs.SPECIAL.finditer(text):
            i = m.start()
            if i > start:
                ctx.draw_text(x + start * cw, ty, text[start:i], style)
            # A shade mixes fg into the cell's background; a see-through cell
            # (over a wallpaper) mixes into the terminal's own background.
            if not _cell_glyphs.draw(ctx, text[i], x + i * cw, ty, cw, pitch,
                                     style.fg, style.bg or dbg, px):
                ctx.draw_text(x + i * cw, ty, text[i], style)
            start = i + 1
        if start < len(text):
            ctx.draw_text(x + start * cw, ty, text[start:], style)

    @staticmethod
    def _style(run: G.Cell, dfg, dbg, page_bg=None) -> Style:
        f = run.flags
        fg = run.fg or dfg
        bg = run.bg
        attr = TextAttribute.NORMAL
        if f & G.STYLE_BOLD:
            attr |= TextAttribute.BOLD
        if f & G.STYLE_ITALIC:
            attr |= TextAttribute.ITALIC
        if f & G.STYLE_FAINT:
            attr |= TextAttribute.DIM
        if f & G.STYLE_STRIKETHROUGH:
            attr |= TextAttribute.STRIKETHROUGH
        if f & G.STYLE_UNDERLINE_MASK:
            attr |= TextAttribute.UNDERLINE
            # Double / curly / dotted / dashed: the one heavier rule PuiKit has.
            if (f & G.STYLE_UNDERLINE_MASK) >> G.STYLE_UNDERLINE_SHIFT >= 2:
                attr |= TextAttribute.UNDERLINE_THICK
        if f & G.STYLE_INVERSE:
            fg, bg = (bg or dbg), fg
        if f & G.STYLE_INVISIBLE:
            fg = bg or dbg
        # An explicit bg even when it is the default: on a character grid a None
        # bg would inherit the pane surface, not the terminal's background. Over
        # a wallpaper ``page_bg`` is None and default cells stay see-through.
        return Style(fg=fg, bg=bg or page_bg, attr=attr, font=_MONO,
                     underline_color=run.ul)

    def _char_at(self, x: int, y: int) -> tuple[str, G.Cell | None]:
        rows = self._term.row_cache if self._term else []
        if 0 <= y < len(rows):
            for run in rows[y]:
                if run.wide and run.col == x:
                    return run.text, run
                if not run.wide and run.col <= x < run.col + run.width:
                    i = x - run.col
                    return (run.text[i] if i < len(run.text) else " "), run
        return " ", None

    def _draw_cursor(self, ctx: DrawContext, cursor, colors, pitch, cw, dfg, dbg) -> None:
        if not cursor.viewport_has_value:
            return
        col, cy = cursor.viewport_x, cursor.viewport_y
        cx = col * cw
        ty = cy * pitch
        sx, sy, _sw, _sh = ctx.screen_rect
        if self._preedit:
            # The composition is drawn over the cells at the cursor, underlined;
            # the shell sees nothing until it commits.
            ctx.draw_text(cx, ty, self._preedit,
                          Style(fg=dfg, bg=dbg, attr=TextAttribute.UNDERLINE, font=_MONO))
        if ctx.focused:
            xs = [sx + cx + cw * display_width(self._preedit[:k])
                  for k in range(len(self._preedit) + 1)]
            ctx.panel.request_text_input(sx + cx, sy + ty, {"ime_char_xs": xs})
        if not cursor.visible or self._preedit:
            return
        ccol = colors.cursor.tuple() if colors.cursor_has_value else dfg
        ch, run = self._char_at(col, cy)
        width = (2 if run is not None and run.wide else 1) * cw
        style = cursor.visual_style
        if not ctx.focused or style == G.CURSOR_BLOCK_HOLLOW:
            ctx.fill_rect(cx, ty + pitch - 0.12, width, 0.12, Style(bg=ccol))
        elif style == G.CURSOR_BAR:
            ctx.fill_rect(cx, ty, 0.12 * cw, pitch, Style(bg=ccol))
        elif style == G.CURSOR_UNDERLINE:
            ctx.fill_rect(cx, ty + pitch - 0.15, width, 0.15, Style(bg=ccol))
        else:
            ctx.fill_rect(cx, ty, width, pitch, Style(bg=ccol))
            ctx.draw_text(cx, ty, ch, Style(fg=dbg, bg=ccol, font=_MONO))

    # --- input ----------------------------------------------------------------------------
    def handle_event(self, event: Event) -> bool:
        if self._term is None and self._exited is False:
            return False
        if event.type is EventType.IME_COMPOSITION:
            self._preedit = event.hints.get("preedit", "")
            self._preedit_caret = event.hints.get("caret", len(self._preedit))
            return True
        if event.type in (EventType.MOUSE_SCROLL, EventType.MOUSE_DOWN, EventType.MOUSE_UP,
                          EventType.MOUSE_DRAG, EventType.MOUSE_CLICK):
            if self._term is not None and self._exited is False:
                self._mouse(event)
            return True
        if event.type is not EventType.KEY:
            return False
        if self._exited is not False:
            if event.key == "enter":
                self.restart()
                return True
            return False
        mods = event.modifiers
        if "cmd" in mods:
            if event.key == "v" and self._panel is not None:
                text = self._panel.get_clipboard()
                if text:
                    self.send(text.replace("\r\n", "\r").replace("\n", "\r"))
                return True
            return False  # Cmd chords stay the app's
        data = self._encode(event)
        if data:
            self.send(data)
        return True

    _BUTTONS = {"left": G.MOUSE_LEFT, "right": G.MOUSE_RIGHT, "middle": G.MOUSE_MIDDLE}

    def _mouse(self, event: Event) -> None:
        """A program that turned on mouse tracking gets presses, releases,
        drags and the wheel, encoded in the mode and format it asked for;
        otherwise the wheel scrolls the scrollback and clicks only focus. The
        scrollbar takes its own presses and drags before either."""
        term = self._term
        assert term is not None
        if self._scrollbar_mouse(event):
            return
        tracking = term.mouse_tracking()
        if event.type is EventType.MOUSE_SCROLL:
            if event.scroll:
                rows = 3 * event.scroll
            else:
                self._scroll_acc += float(event.hints.get("scroll_units", 0.0)) / self._pitch
                rows = int(self._scroll_acc)
                self._scroll_acc -= rows
            if not rows:
                return
            if not tracking:
                term.scroll(-rows)
                self._scrolled()
                return
            button = G.MOUSE_WHEEL_UP if rows > 0 else G.MOUSE_WHEEL_DOWN
            data = b"".join(self._mouse_bytes(G.MOUSE_PRESS, button, event)
                            for _ in range(min(abs(rows), 10)))
            if data:
                self.send_raw(data)
            return
        if not tracking:
            return
        button = self._BUTTONS.get(event.button or "left", G.MOUSE_LEFT)
        if event.type is EventType.MOUSE_DOWN:
            self._mouse_button = button
            self.send_raw(self._mouse_bytes(G.MOUSE_PRESS, button, event))
        elif event.type is EventType.MOUSE_UP:
            self.send_raw(self._mouse_bytes(G.MOUSE_RELEASE, self._mouse_button or button, event))
            self._mouse_button = None
            self._mouse_up_at = time.monotonic()
        elif event.type is EventType.MOUSE_DRAG:
            self.send_raw(self._mouse_bytes(G.MOUSE_MOTION, self._mouse_button or button,
                                            event, pressed=True))
        elif event.type is EventType.MOUSE_CLICK:
            # The Panel synthesizes a left click after the down/up already sent;
            # right / middle (and a backend without press/release) arrive only
            # as a click, so they become a press and a release here.
            if button == G.MOUSE_LEFT and time.monotonic() - self._mouse_up_at < 0.5:
                return
            self.send_raw(self._mouse_bytes(G.MOUSE_PRESS, button, event)
                          + self._mouse_bytes(G.MOUSE_RELEASE, button, event))

    #: How long the overlaid scrollbar stays up after the last scroll.
    SCROLLBAR_LINGER_SECONDS = 1.0

    def _scrollbar_visible(self) -> bool:
        """The overlay shows while it is in use: the view scrolled back from
        the bottom, a drag on it, or a scroll a moment ago."""
        if not self._sb_w or self._term is None:
            return False
        total, offset, length = self._term.scrollbar()
        if total <= length:
            return False
        return (self._sb_drag or offset + length < total
                or time.monotonic() < self._sb_until)

    def _scrolled(self) -> None:
        """Note a scroll: keep the overlay up a moment, and redraw once that
        moment is over so it goes away."""
        self._sb_until = time.monotonic() + self.SCROLLBAR_LINGER_SECONDS
        if self._panel is not None:
            self._panel.call_later(self.SCROLLBAR_LINGER_SECONDS + 0.05, self._panel.render)

    def _scrollbar_mouse(self, event: Event) -> bool:
        if not self._sb_drag and not self._scrollbar_visible():
            return False  # hidden: the right column belongs to the program
        on_bar = (event.x is not None and event.x >= self._w_units - self._sb_w)
        if event.type is EventType.MOUSE_DOWN and on_bar:
            self._sb_drag = True
        elif event.type is EventType.MOUSE_UP and self._sb_drag:
            self._sb_drag = False
            self._mouse_up_at = time.monotonic()
            self._scrolled()
            return True
        elif event.type is EventType.MOUSE_CLICK and on_bar:
            return True
        if not (self._sb_drag and event.type in (EventType.MOUSE_DOWN, EventType.MOUSE_DRAG)):
            return False
        total, _offset, length = self._term.scrollbar()
        if total > length and self._h_units > 0:
            # The thumb is centred on the pointer, the way a click on the
            # track jumps there and a drag carries it.
            y = event.hints.get("pointer_y", event.y) or 0.0
            frac = min(1.0, max(0.0, y / self._h_units))
            self._term.scroll_to_row(round(frac * total - length / 2))
        return True

    def _mouse_bytes(self, action: int, button: int, event: Event,
                     pressed: bool = False) -> bytes:
        mods = 0
        for name, bit in _MODS.items():
            if name in event.modifiers:
                mods |= bit
        col = max(0.0, (event.x or 0.0) / self._cw)
        row = max(0.0, (event.y or 0.0) / self._pitch)
        assert self._term is not None
        return self._term.encode_mouse(action, button, col, row, mods, pressed)

    def send_raw(self, data: bytes) -> None:
        """Bytes to the program without moving the viewport (mouse reports)."""
        if data and self._proc is not None and self._exited is False:
            self._proc.write(data)

    def _encode(self, event: Event) -> bytes:
        key, char = event.key, event.char
        mods = 0
        for name, bit in _MODS.items():
            if name in event.modifiers:
                mods |= bit
        # IME commit or any multi-character text: straight through as UTF-8.
        if char and (key is None or len(char) > 1) and not mods & (G.MODS_CTRL | G.MODS_ALT):
            return char.encode("utf-8")
        gkey = G.KEYS.get(key or "", 0)
        text = ""
        unshifted = 0
        if key and len(key) == 1:
            unshifted = ord(key.lower())
            if not mods & G.MODS_CTRL:
                text = char or key
        elif key == "space":
            unshifted = 0x20
            text = " " if not mods & G.MODS_CTRL else ""
        if gkey == 0 and text and not mods & G.MODS_CTRL:
            prefix = b"\x1b" if mods & G.MODS_ALT else b""
            return prefix + text.encode("utf-8")
        assert self._term is not None
        return self._term.encode_key(gkey, mods, text, unshifted)
