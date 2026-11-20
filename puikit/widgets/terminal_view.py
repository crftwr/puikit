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
bracketed paste through libghostty's paste encoder.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from typing import Any

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
        return _pty.UNSUPPORTED_REASON
    return G.load_error()


def default_shell() -> list[str]:
    if os.name == "nt":
        return [os.environ.get("COMSPEC") or "cmd.exe"]
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
                 on_exit: Callable[[int | None], None] | None = None,
                 on_prompt: Callable[[int, int | None, str], None] | None = None,
                 on_notification: Callable[[str, str], None] | None = None,
                 on_progress: Callable[[int, int | None], None] | None = None,
                 padding_px: float = 0.0, anchor: str = "auto",
                 term_program: tuple[str, str] | None = None) -> None:
        self.argv = argv or default_shell()
        self.cwd = cwd
        self.env = env
        #: ``(name, version)`` the programs in here see as ``TERM_PROGRAM`` /
        #: ``TERM_PROGRAM_VERSION``: the app's, as VS Code's terminal says
        #: ``vscode``. None says PuiKit. ``PUIKIT_TERMINAL`` is set either way.
        self.term_program = term_program
        self.on_title = on_title
        self.on_pwd = on_pwd
        self.on_exit = on_exit
        #: Shell integration (OSC 133): ``on_prompt(kind, exit_code, command)``
        #: for each step of a command - ``puikit._ghostty.PROMPT_START``,
        #: ``INPUT_START``, ``OUTPUT_START`` (``command`` is the command line
        #: when the shell sends it) and ``COMMAND_END`` (``exit_code`` when it
        #: reports one, else None). Only shells with integration send these.
        self.on_prompt = on_prompt
        #: ``on_notification(title, body)``: the program asked for a desktop
        #: notification (OSC 9 / OSC 777); the title may be empty.
        self.on_notification = on_notification
        #: ``on_progress(state, percent)``: the program reports progress (OSC
        #: 9;4); ``state`` is one of ``puikit._ghostty.PROGRESS_*``, ``percent``
        #: None when it gave none.
        self.on_progress = on_progress
        #: Inset of the grid from the widget's edges, in device pixels, on pixel
        #: backends (a character grid has no sub-cell and stays flush) - the
        #: breathing room a host gives its other panes.
        self.padding_px = padding_px
        #: Where the grid sits in the space it does not fill: "auto" pins a
        #: shell's screen to the bottom left (drawing the row above into the
        #: top gap) and centres a full-screen program; "top-left" keeps the
        #: plain origin. An unknown value behaves as "top-left".
        self.anchor = anchor
        #: Carry a full-screen program's outermost colours into the margins.
        self.extend_edges = True
        self._extend = False
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
        self._ox = self._oy = 0.0      # grid origin inside the widget (padding)
        self._selecting = False        # a selection drag is in progress
        self._cw = 1.0
        self._pitch = 1.0
        self._scroll_acc = 0.0          # sub-row trackpad scroll not yet sent
        self._mouse_button: int | None = None   # button the program saw pressed
        self._mouse_up_at = 0.0
        self._held_since: float | None = None   # synchronized output in progress
        self._settle_until: float | None = None  # hold kept past its end until
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
    def start(self, panel: Any) -> None:
        """Start the program now, before the view is first drawn - for a view
        pushed hidden (a session started out of view), which draws nothing
        until it is shown. The grid is guessed from the window size; the
        first draw resizes it to the view's own. Does nothing once started,
        or where the terminal is unavailable."""
        if self._term is not None or self._exited is not False or available() is not None:
            return
        self._panel = panel
        lc = panel.layout_context()
        mono = Style(font=_MONO)
        pitch = lc.measure_line_height(mono) or 1.0
        cw = (lc.measure_text("M" * 16, mono) / 16) or 1.0
        sw, sh = panel.backend.size_units
        cols = max(2, int(sw / cw + 1e-6))
        rows = max(2, int(sh / pitch + 1e-6))
        self._start_on(panel, panel.theme, cols, rows)

    def _start(self, ctx: DrawContext, cols: int, rows: int) -> None:
        self._start_on(ctx.panel, ctx.theme, cols, rows)

    def _start_on(self, panel: Any, theme: Any, cols: int, rows: int) -> None:
        fg = tuple(theme.text[:3]) if theme else None
        bg_c = theme.surfaces.get("content") if theme else None
        bg = tuple(bg_c[:3]) if bg_c else None
        self._term = G.Terminal(cols, rows, write_pty=self._write_pty,
                                on_title=self._title_changed,
                                on_pwd=self._pwd_changed, fg=fg, bg=bg,
                                on_render_hold=self._render_hold,
                                on_semantic_prompt=self._prompt_event,
                                on_notification=self._notification,
                                on_progress=self._progress)
        # Probe once, on the UI thread: a backend without main-thread dispatch
        # gets its pty output drained on the animation tick instead.
        self._use_ticks = not panel.call_on_main_thread(lambda: None)
        self._exited = False
        self._gen += 1
        gen = self._gen
        try:
            self._proc = _pty.PtyProcess(self.argv, cols=cols, rows=rows, cwd=self.cwd,
                                         env=self.env, term_program=self.term_program,
                                         on_output=self._on_output,
                                         on_exit=lambda code: self._on_child_exit(gen, code))
        except OSError as exc:
            # A program that cannot start (not found, on Windows - on POSIX the
            # forked child exits 127 instead) ends the same way: its reason on
            # screen and the exit bar under it.
            self._term.write(f"{self.argv[0]}: {exc.strerror or exc}\r\n".encode("utf-8"))
            self._on_child_exit(gen, 127)

    def close(self) -> None:
        # A child hung up here exits later; that exit is not news to whoever
        # closed it.
        self._gen += 1
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
        settling = self._settle_until is not None
        self._term.write(data)
        if settling and self._settle_until is not None:
            # Output after the hold ended: the redraw it was waiting for.
            self._end_hold()
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
            self._settle_until = None
            if self._panel is not None:
                self._panel.call_later(self.HOLD_MAX_SECONDS + 0.01, self._panel.render)
            return
        settle = getattr(self._proc, "sync_settle_seconds", 0.0)
        if settle <= 0 or self._held_since is None:
            self._end_hold()
            return
        # The pty's renderer may still owe the frame (ConPTY: see
        # PtyProcess.sync_settle_seconds) - keep the held one up until its
        # next output, or ``settle`` seconds without any.
        self._settle_until = time.monotonic() + settle
        if self._panel is not None:
            self._panel.call_later(settle + 0.005, self._panel.render)

    def _end_hold(self) -> None:
        self._held_since = None
        self._settle_until = None

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

    def _prompt_event(self, kind: int, exit_code: int | None, command: str) -> None:
        if self.on_prompt:
            self.on_prompt(kind, exit_code, command)

    def _notification(self, title: str, body: str) -> None:
        if self.on_notification:
            self.on_notification(title, body)

    def _progress(self, state: int, percent: int | None) -> None:
        if self.on_progress:
            self.on_progress(state, percent)

    @property
    def at_prompt(self) -> bool:
        """Whether the program is a shell idle at its prompt, the cursor on
        its command line - so text sent now becomes part of the next command
        rather than input to something running. Known only from shell
        integration marks (OSC 133); False without them, on the alternate
        screen, and once the child has exited."""
        if self._term is None or self._exited is not False:
            return False
        return self._term.at_prompt()

    @property
    def child_in_foreground(self) -> bool | None:
        """Whether the program it started holds the terminal's foreground: a
        shell at its prompt does, a shell running a command does not. Unlike
        :attr:`at_prompt` this needs no shell integration, but only POSIX can
        tell; None on Windows, before the program starts and after it exits."""
        if self._proc is None or self._exited is not False:
            return None
        return self._proc.child_in_foreground()

    @property
    def foreground_program(self) -> str | None:
        """The name of the program in the terminal's foreground - the shell at
        its prompt, else what it runs (``vim``) - without shell integration.
        POSIX only; None on Windows, before the program starts and after it
        exits. The command line, arguments included, comes with the OSC 133
        output-start mark instead (``on_prompt``)."""
        if self._proc is None or self._exited is not False:
            return None
        probe = getattr(self._proc, "foreground_name", None)
        return probe() if probe is not None else None

    def send(self, data: bytes | str) -> None:
        """Type ``data`` into the terminal as if the user had."""
        if isinstance(data, str):
            data = data.encode("utf-8")
        if self._proc is not None and self._exited is False:
            if self._term is not None:
                self._term.scroll(None)
                if self._term.has_selection():
                    self._term.clear_selection()
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
        bw, bh = ctx.base_size
        pad_x = pad_y = 0.0
        if ctx.pixel_layout and self.padding_px:
            pad_x = self.padding_px / bw if bw else 0.0
            pad_y = self.padding_px / bh if bh else 0.0
        # The scrollbar takes no columns: the program inside sees the same grid
        # it would in a terminal. On pixel backends it is overlaid on the right
        # edge while the view is scrolled (see _scrollbar_visible); a character
        # grid has none (the host terminal has its own scrollback).
        self._sb_w = float(ctx.panel.backend.scrollbar_units) if ctx.pixel_layout else 0.0
        self._w_units, self._h_units = w_units, h_units
        cols = max(2, int((w_units - 2 * pad_x) / cw + 1e-6))
        rows = max(2, int((h_units - 2 * pad_y) / pitch + 1e-6))
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
        # Bare pointer motion, for a program that tracks all motion (a TUI's
        # hover); the encoder sends nothing in any other mode.
        ctx.request_pointer_motion(self._pointer_motion)

        # Where the grid sits in the window. The window sizes in pixels, the
        # grid in whole cells, so up to a cell is left over each way; padding
        # is the least margin and the leftover goes where it reads best
        # (``anchor``). A shell's screen ("primary") is pinned to the bottom
        # left, so the prompt keeps its place as the window resizes, and the
        # row above the top peeks in instead of a gap; a full-screen program
        # ("alternate") is centred.
        primary = self._term.active_screen() == G.SCREEN_PRIMARY
        free_x = max(0.0, w_units - 2 * pad_x - cols * cw)
        free_y = max(0.0, h_units - 2 * pad_y - rows * pitch)
        # A full-screen program's outermost colours are carried into the
        # margins (see _extend_edges); a shell's are not - its last line is
        # whatever was printed last, not a frame.
        self._extend = ctx.pixel_layout and not primary
        if self.anchor == "auto" and primary:
            ox, oy = pad_x, pad_y + free_y
            self._term.set_overscan_above(1 if ctx.pixel_layout else 0)
        else:
            if self.anchor == "auto":
                ox, oy = pad_x + free_x / 2, pad_y + free_y / 2
            else:
                ox, oy = pad_x, pad_y
            self._term.set_overscan_above(0)
        self._ox, self._oy = ox, oy

        t0 = time.perf_counter()
        now = time.monotonic()
        if self._settle_until is not None and now >= self._settle_until:
            self._end_hold()
        held = (self._held_since is not None and self._last_frame is not None
                and now - self._held_since < self.HOLD_MAX_SECONDS)
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
        px = (1.0 / bw if bw else 0.1, 1.0 / bh if bh else 0.05)
        theme = ctx.theme
        sel_bg = None
        if theme is not None:
            sel_bg = getattr(theme, "selection_active_bg" if ctx.focused
                             else "selection_inactive_bg", None)
        # The text system leaves a run of only spaces un-underlined (and puts a
        # run's rule where its own glyphs say), so on pixel backends the
        # underline is a rule drawn here, the same for every run in the row.
        if shapes:
            fm = ctx.font_metrics(mono)
            ul_y = fm.ascent + max(px[1] * 1.5, fm.descent * 0.35)
        def draw_row(runs, ty):
            for run in runs:
                style = self._run_style(run, dfg, dbg, page_bg, sel_bg)
                x = ox + run.col * cw
                special = shapes and _cell_glyphs.SPECIAL.search(run.text)
                if run.sel or run.bg is not None or run.flags & G.STYLE_INVERSE:
                    if special and not run.wide:
                        # Block elements paint their own background (see
                        # _cell_glyphs.draw): fill only between them, so no
                        # cell is painted twice on a translucent surface.
                        start = 0
                        for i, ch in enumerate(run.text + "\u2580"):
                            if i == len(run.text) or _cell_glyphs.is_block(ch):
                                if i > start:
                                    ctx.fill_rect(x + start * cw, ty, (i - start) * cw, pitch,
                                                  Style(bg=style.bg))
                                start = i + 1
                    else:
                        ctx.fill_rect(x, ty, run.width * cw, pitch, Style(bg=style.bg))
                if run.wide:
                    # A fallback CJK face is rarely exactly two cells wide;
                    # centre the glyph in its two-cell slot.
                    gw = self._glyph_w.get(run.text)
                    if gw is None:
                        gw = self._glyph_w[run.text] = ctx.measure_text(run.text, style)
                    x += max(0.0, (2 * cw - gw) / 2)
                rule = None
                if shapes and run.flags & G.STYLE_UNDERLINE_MASK:
                    thick = (run.flags & G.STYLE_UNDERLINE_MASK) >> G.STYLE_UNDERLINE_SHIFT >= 2
                    rule = (px[1] * (2 if thick else 1),
                            Style(bg=style.underline_color or style.fg))
                    style = Style(fg=style.fg, bg=style.bg, font=style.font,
                                  attr=style.attr & ~(TextAttribute.UNDERLINE
                                                      | TextAttribute.UNDERLINE_THICK))
                if special:
                    self._draw_with_shapes(ctx, run, style, x, ty, cw, pitch, px, dbg)
                else:
                    ctx.draw_text(x, ty, run.text, style)
                if rule is not None:
                    # After the text: a run's text carries its cell background
                    # (the page's, at least), which would paint over the rule.
                    ctx.fill_rect(ox + run.col * cw, ty + ul_y, run.width * cw,
                                  rule[0], rule[1])

        if self._extend and self.extend_edges:
            self._extend_edges(ctx, ox, oy, cw, pitch, w_units, h_units,
                               dfg, dbg, page_bg, sel_bg)
        for y, runs in enumerate(self._term.row_cache):
            draw_row(runs, oy + y * pitch)
        # The rows above the viewport, into the top gap (clipped at the edge).
        above = self._term.above_rows
        for i, runs in enumerate(above):
            draw_row(runs, oy - (len(above) - i) * pitch)
        self._draw_cursor(ctx, cursor, colors, pitch, cw, dfg, dbg, ox, oy)
        if self._scrollbar_visible():
            total, offset, length = self._term.scrollbar()
            ctx.draw_scrollbar(w_units - self._sb_w, 0, h_units,
                               offset / (total - length), length / total)
        if self._exited is not False:
            msg = f" [process exited ({self._exited}) - {self.exit_hint}] "
            ctx.draw_text(ox, oy + (rows - 1) * pitch, msg[:cols],
                          Style(fg=dbg, bg=dfg, attr=TextAttribute.BOLD, font=_MONO))
        t2 = time.perf_counter()
        st = self.stats
        st["frames"] += 1
        st["rows_rebuilt"] = rebuilt
        st["last_update_ms"] = (t1 - t0) * 1000
        st["last_draw_ms"] = (t2 - t1) * 1000

    def _run_style(self, run, dfg, dbg, page_bg, sel_bg) -> Style:
        style = self._style(run, dfg, dbg, page_bg)
        if run.sel:
            # The selection: the theme's selection fill (reverse video where
            # the theme has none).
            if sel_bg is not None:
                return Style(fg=style.fg, bg=sel_bg, attr=style.attr, font=style.font,
                             underline_color=style.underline_color)
            return Style(fg=style.bg or dbg, bg=style.fg, attr=style.attr,
                         font=style.font, underline_color=style.underline_color)
        return style

    def _extend_edges(self, ctx, ox, oy, cw, pitch, w_units, h_units,
                      dfg, dbg, page_bg, sel_bg) -> None:
        """Carry the outermost cells' colours out into the margins, so a
        full-screen program's picture reaches the window edge instead of
        stopping a fraction of a cell short of it.

        Each margin strip takes, segment by segment, the colour its cell shows
        along that side (``_cell_glyphs.side_colors``): a cell's background for
        text, but for a block element the part it paints - a half-block shadow
        continues as fg on one half and bg on the other, and a page painted
        with full blocks in fg continues as fg, where copying the cell's bg
        would have broken it. A see-through default background (over a
        wallpaper) is left unpainted."""
        term = self._term
        cols, rows = term.cols, term.rows
        x1 = ox + cols * cw          # right edge of the grid
        y1 = oy + rows * pitch       # bottom edge of the grid
        right_w, bottom_h = w_units - x1, h_units - y1

        def cell(c: int, r: int):
            ch, run = self._char_at(c, r)
            if run is None:
                return " ", dfg, page_bg
            st = self._run_style(run, dfg, dbg, page_bg, sel_bg)
            return (ch if not run.wide else " "), st.fg, st.bg

        def sides(c: int, r: int, side: str):
            ch, fg, bg = cell(c, r)
            return _cell_glyphs.side_colors(ch, side, fg, bg)

        def fill(x, y, w, h, color) -> None:
            if color is not None and w > 0 and h > 0:
                ctx.fill_rect(x, y, w, h, Style(bg=color))

        for r in range(rows):
            for side, x, w in (("l", 0.0, ox), ("r", x1, right_w)):
                if w <= 0:
                    continue
                c = 0 if side == "l" else cols - 1
                for a, b, color in sides(c, r, side):
                    fill(x, oy + (r + a) * pitch, w, (b - a) * pitch, color)
        for c in range(cols):
            for side, y, h in (("t", 0.0, oy), ("b", y1, bottom_h)):
                if h <= 0:
                    continue
                r = 0 if side == "t" else rows - 1
                for a, b, color in sides(c, r, side):
                    fill(ox + (c + a) * cw, y, (b - a) * cw, h, color)
        # Corners: the colour at the corner of the corner cell.
        for (c, r, side, pick, x, y, w, h) in (
                (0, 0, "l", 0, 0.0, 0.0, ox, oy),
                (cols - 1, 0, "r", 0, x1, 0.0, right_w, oy),
                (0, rows - 1, "l", -1, 0.0, y1, ox, bottom_h),
                (cols - 1, rows - 1, "r", -1, x1, y1, right_w, bottom_h)):
            fill(x, y, w, h, sides(c, r, side)[pick][2])

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
                                     style.fg, style.bg or dbg, px, bg_fill=style.bg):
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
                if run.wide and run.col <= x <= run.col + 1:
                    return run.text, run
                if not run.wide and run.col <= x < run.col + run.width:
                    i = x - run.col
                    return (run.text[i] if i < len(run.text) else " "), run
        return " ", None

    def _draw_cursor(self, ctx: DrawContext, cursor, colors, pitch, cw, dfg, dbg,
                     ox: float = 0.0, oy: float = 0.0) -> None:
        if not cursor.viewport_has_value:
            return
        col, cy = cursor.viewport_x, cursor.viewport_y
        cx = ox + col * cw
        ty = oy + cy * pitch
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
        # Copy: Cmd-C (macOS), Ctrl+Shift+C elsewhere - only with a selection,
        # so Ctrl+C still interrupts and an unused Cmd-C stays the app's.
        if event.key == "c" and ("cmd" in mods or {"ctrl", "shift"} <= mods):
            if self.copy_selection():
                return True
        if event.key == "v" and ("cmd" in mods or {"ctrl", "shift"} <= mods) \
                and self._panel is not None:
            # Paste: Cmd-V (macOS), Ctrl+Shift+V elsewhere.
            text = self._panel.get_clipboard()
            if text:
                self.send(text.replace("\r\n", "\r").replace("\n", "\r"))
            return True
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
        # Selection: the plain drag of a program that does not track the mouse
        # (a shell), Shift+drag over one that does (the usual terminal escape),
        # Alt for a rectangle. libghostty's gesture decides cell / word / line
        # from the click count.
        select = self._selecting or not tracking or "shift" in event.modifiers
        if select and (event.button or "left") == "left":
            if event.type is EventType.MOUSE_DOWN:
                self._selecting = True
                term.select(G.GESTURE_PRESS, *self._cell_at(event))
            elif event.type is EventType.MOUSE_DRAG and self._selecting:
                term.select(G.GESTURE_DRAG, *self._cell_at(event),
                            rectangle="alt" in event.modifiers)
            elif event.type is EventType.MOUSE_UP and self._selecting:
                term.select(G.GESTURE_RELEASE, *self._cell_at(event))
                self._selecting = False
                self._mouse_up_at = time.monotonic()
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

    def _pointer_motion(self, event: Event) -> bool:
        """MOUSE_MOVE with no button held (Panel.request_pointer_motion): a
        motion report when the program asked for all-motion tracking (mode
        1003) - libghostty's encoder decides, and skips a move within the cell
        it last reported. Never a redraw of ours."""
        if self._term is None or self._exited is not False or self._selecting:
            return False
        if self._term.mouse_tracking():
            self.send_raw(self._mouse_bytes(G.MOUSE_MOTION, 0, event))
        return False

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
        col, row = self._cell_at(event)
        assert self._term is not None
        return self._term.encode_mouse(action, button, col, row, mods, pressed)

    def _cell_at(self, event: Event) -> tuple[float, float]:
        """Viewport cell (fractional) under a mouse event, padding removed."""
        col = max(0.0, ((event.x or 0.0) - self._ox) / self._cw)
        row = max(0.0, ((event.y or 0.0) - self._oy) / self._pitch)
        return col, row

    def copy_selection(self) -> bool:
        """Put the selected text on the clipboard; False when nothing is
        selected (so the key can mean something else)."""
        if self._term is None or self._panel is None or not self._term.has_selection():
            return False
        text = self._term.selection_text()
        if text:
            self._panel.set_clipboard(text)
        return True

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
        # A bare control character with no key name (a text system that turned
        # an unbound Ctrl chord into its C0 code): it already is the byte.
        if char and len(char) == 1 and ord(char) < 0x20 and not key:
            return char.encode("ascii")
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
