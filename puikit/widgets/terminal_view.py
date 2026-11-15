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
                                on_pwd=self._pwd_changed, fg=fg, bg=bg)
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
        w_units, h_units = ctx.size_units
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
        rebuilt, cursor, colors = self._term.update()
        t1 = time.perf_counter()
        dfg = colors.foreground.tuple()
        dbg = colors.background.tuple()
        ctx.fill_rect(0, 0, w_units, h_units, Style(bg=dbg))
        for y, runs in enumerate(self._term.row_cache):
            ty = y * pitch
            for run in runs:
                style = self._style(run, dfg, dbg)
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
                ctx.draw_text(x, ty, run.text, style)
        self._draw_cursor(ctx, cursor, colors, pitch, cw, dfg, dbg)
        if self._exited is not False:
            msg = f" [process exited ({self._exited}) - press Enter to restart] "
            ctx.draw_text(0.0, (rows - 1) * pitch, msg[:cols],
                          Style(fg=dbg, bg=dfg, attr=TextAttribute.BOLD, font=_MONO))
        t2 = time.perf_counter()
        st = self.stats
        st["frames"] += 1
        st["rows_rebuilt"] = rebuilt
        st["last_update_ms"] = (t1 - t0) * 1000
        st["last_draw_ms"] = (t2 - t1) * 1000

    @staticmethod
    def _style(run: G.Cell, dfg, dbg) -> Style:
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
        if f & G.STYLE_INVERSE:
            fg, bg = (bg or dbg), fg
        if f & G.STYLE_INVISIBLE:
            fg = bg or dbg
        # An explicit bg even when it is the default: on a character grid a None
        # bg would inherit the pane surface, not the terminal's background.
        return Style(fg=fg, bg=bg or dbg, attr=attr, font=_MONO)

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
        if event.type is EventType.MOUSE_SCROLL:
            if self._term is not None and not self._term.mouse_tracking():
                self._term.scroll(-3 * event.scroll)
            return True
        if event.type in (EventType.MOUSE_DOWN, EventType.MOUSE_CLICK, EventType.MOUSE_UP):
            return True  # focus is taken by the Panel; selection is not built yet
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
