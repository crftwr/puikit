"""Web backend — renders into a browser tab launched with ``webbrowser``.

This is the pixel/vector GUI backend for a web browser (the ``CanvasBackend``
slot in the roadmap). The Python process runs a local HTTP + WebSocket server
(``_web_server``), opens the user's browser at it with the stdlib ``webbrowser``
module, and streams one serialized display list per frame to a ``<canvas>`` that
replays it; the page streams input events back, which the event loop turns into
PuiKit ``Event`` objects. It advertises the web GUI capability profile
(``PROFILE_GUI_WEB``): pixel layout, vector control faces, proportional fonts,
layering, transparency, shadows, images, and hover.

**Text is measured in Python.** The layout/measurement seam runs synchronously
inside ``panel.render()``, before anything reaches the browser, so the backend
cannot ask the canvas how wide a run is. Instead it predicts the browser's
rendering: the page draws with the *same* bundled Noto faces and
``fontKerning: "none"``, so a run's width is the plain sum of its glyphs' advance
widths — which ``_ttf`` reads straight from the font files. (Glyphs absent from
the bundled fonts — CJK, astral emoji — fall to a browser font-fallback whose
advances Python cannot predict, so their fit/wrap is approximate; see
``docs/web_backend.md``.)

**Deferred for v1** (advertised off, so the Panel substitutes its fallbacks):
composited ``animate`` transitions (kept immediate; geometry/blink still animate
through ``request_animation_ticks``), IME composition, and drop-*in* drag &
drop. Everything the demo catalog exercises otherwise runs.
"""

from __future__ import annotations

import base64
import json
import os
import queue
import threading
import time
import webbrowser
from typing import Any

from ..backend import (
    Backend,
    DEFAULT_STYLE,
    EventHandler,
    Style,
    TextAttribute,
    is_transparent,
)
from ..capability import PROFILE_GUI_WEB, CapabilityProfile
from ..event import Event, EventType, char_key_event
from ..font import Font, FontMetrics
from . import _ttf
from ._web_server import WebServer

_DEFAULT_FG = (230, 230, 230)
_DEFAULT_BG = (24, 24, 24)
_SCROLLBAR_THUMB = (150, 150, 150)
_SCROLLBAR_TRACK = (60, 60, 60)

_ASSET_DIR = os.path.join(os.path.dirname(__file__), "web")
_FONT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "fonts")

# The web GUI profile, minus the axes v1 does not implement yet. Each override
# turns a capability *off* so the Panel layer substitutes its documented
# fallback (an immediate transition, a plain command key, a clipboard copy),
# never calling a primitive this backend does not serve.
PROFILE_WEB = CapabilityProfile(
    {
        **PROFILE_GUI_WEB,
        # Geometry/color transitions and self-driven motion (caret blink, busy
        # spinner) run through timed re-render ticks; composited fade/scale stay
        # immediate until a real animate() lands.
        "animation_ticks": True,
        "animation": False,
        "ime": False,
        "drag_and_drop": False,
        # draw_icon falls back to a text/emoji glyph — no icon set is bundled.
        "icons": False,
    }
)

# DOM KeyboardEvent.key -> the canonical PuiKit key name (the same vocabulary the
# curses/macOS backends use). Printable keys are not here — they route through
# char_key_event so the shared keyboard contract owns the glyph rules.
_DOM_KEYS = {
    "ArrowUp": "up",
    "ArrowDown": "down",
    "ArrowLeft": "left",
    "ArrowRight": "right",
    "Enter": "enter",
    "Tab": "tab",
    "Escape": "escape",
    "Backspace": "backspace",
    "Delete": "delete",
    "Home": "home",
    "End": "end",
    "PageUp": "pageup",
    "PageDown": "pagedown",
    "Insert": "insert",
    **{f"F{i}": f"f{i}" for i in range(1, 13)},
}

# Browser modifier flags -> contract modifier names. The browser's Meta is the
# platform command key (Cmd on macOS, the Windows key elsewhere); PuiKit calls
# the command modifier "cmd", matching the macOS backend.
_MODS = (("shift", "shift"), ("ctrl", "ctrl"), ("alt", "alt"), ("meta", "cmd"))


def _modifier_names(mods: dict) -> frozenset[str]:
    return frozenset(name for flag, name in _MODS if mods.get(flag))


def translate_key(key: str, mods: dict) -> Event | None:
    """Translate a browser KeyboardEvent (its ``key`` + modifier flags) into a
    PuiKit ``Event``. Module-level so the mapping is testable without a browser.

    Returns ``None`` for a bare modifier press (Shift/Control/...) and anything
    with no PuiKit meaning."""
    modifiers = _modifier_names(mods)
    named = _DOM_KEYS.get(key)
    if named is not None:
        return Event(type=EventType.KEY, key=named, modifiers=modifiers)
    # A single produced character (letter, digit, punctuation, space): defer to
    # the shared contract helper for the lowercase-letter / shifted-glyph rules.
    if len(key) == 1 and key.isprintable():
        return char_key_event(key, modifiers)
    return None


def _css_color(color: tuple[int, ...] | None, alpha: float = 1.0) -> str | None:
    """An ``rgba(...)`` string for the client, or None to skip the paint."""
    if color is None:
        return None
    if len(color) == 4:
        r, g, b, a = color
        alpha = alpha * (a / 255.0)
    else:
        r, g, b = color
    return f"rgba({r},{g},{b},{alpha:.3f})"


class _Face:
    """A resolved font: its metrics table (Python-side measurement) and the CSS
    string the browser draws with (they name the same bundled face)."""

    __slots__ = ("table", "css", "px", "ascent_px", "line_px")

    def __init__(self, table: _ttf.TrueTypeFont, css: str, px: float):
        self.table = table
        self.css = css
        self.px = px
        self.ascent_px = table.ascent * px
        self.line_px = table.line_height * px


class WebBackend(Backend):
    PROFILE = PROFILE_WEB

    def __init__(
        self,
        width: int = 100,
        height: int = 30,
        title: str = "PuiKit",
        base_font: Font | None = None,
        ui_font: Font | None = None,
        port: int = 0,
        open_browser: bool = True,
    ):
        self._initial_size = (width, height)
        self._title = title
        # Base (monospaced grid) font grounds the base unit; the UI font is the
        # default proportional face an unnamed Font() resolves to.
        self._base_font = base_font or Font(size=14.0, monospace=True)
        self._ui_font = ui_font
        self._base_pt = float(self._base_font.size or 14.0)
        self._port = port
        self._open_browser = open_browser

        # Loaded metrics tables for the four bundled Noto faces.
        self._tables = {
            ("mono", False): _ttf.load(os.path.join(_FONT_DIR, "NotoSansMono-Regular.ttf")),
            ("mono", True): _ttf.load(os.path.join(_FONT_DIR, "NotoSansMono-Bold.ttf")),
            ("sans", False): _ttf.load(os.path.join(_FONT_DIR, "NotoSans-Regular.ttf")),
            ("sans", True): _ttf.load(os.path.join(_FONT_DIR, "NotoSans-Bold.ttf")),
        }
        mono = self._tables[("mono", False)]
        # One base unit in CSS pixels, kept as floats so the drawing path stays
        # crisp and measure_line_height(font=None) is exactly 1.0.
        self._base_w = mono.advance(ord("M")) * self._base_pt
        self._base_h = mono.line_height * self._base_pt
        self._face_cache: dict[tuple, _Face] = {}

        # Display list (base-unit coords + Style/Font), rebuilt each frame.
        self._back: list[tuple] = []

        # Transport + event plumbing.
        self._server: WebServer | None = None
        self._queue: "queue.Queue[Any]" = queue.Queue()
        self._quit = False
        self._connected = threading.Event()
        self._canvas_px: tuple[float, float] | None = None
        self._last_units: tuple[int, int] | None = None
        self._pressed_button: str | None = None
        self._sent_images: set[str] = set()
        self._pointer_shape: str | None = None

        # Animation ticks: callbacks driven by a repeating enqueue of _TICK.
        self._tick_callbacks: list[Any] = []
        self._tick_lock = threading.Lock()
        self._ticker: threading.Thread | None = None

    # --- capabilities ------------------------------------------------------

    @property
    def capabilities(self) -> CapabilityProfile:
        return self.PROFILE

    # --- lifecycle ---------------------------------------------------------

    def open(self) -> None:
        self._quit = False
        server = WebServer(_ASSET_DIR, _FONT_DIR, self._on_message, self._on_connect)
        self._port = server.start()
        self._server = server
        url = f"http://127.0.0.1:{self._port}/"
        if self._open_browser:
            webbrowser.open(url)
        # Wait for the tab to connect and report its canvas size, so the app's
        # first render() sizes to the real window instead of the seed size. If
        # the browser never opens (headless CI), proceed after a timeout — the
        # app still runs; it just has nowhere to draw.
        self._connected.wait(timeout=15.0)
        # Seed the resize baseline so the connect-time size does not also fire a
        # redundant RESIZE event into the loop below.
        self._last_units = self.size

    def close(self) -> None:
        self._quit = True
        self._stop_ticker()
        if self._server is not None:
            self._server.close()
            self._server = None

    @property
    def url(self) -> str:
        """The address the client page is served at (valid after open())."""
        return f"http://127.0.0.1:{self._port}/"

    # --- geometry ----------------------------------------------------------

    @property
    def size(self) -> tuple[int, int]:
        if self._canvas_px is None:
            return self._initial_size
        w, h = self._canvas_px
        return (max(1, int(w / self._base_w)), max(1, int(h / self._base_h)))

    @property
    def size_units(self) -> tuple[float, float]:
        if self._canvas_px is None:
            return (float(self._initial_size[0]), float(self._initial_size[1]))
        w, h = self._canvas_px
        return (w / self._base_w, h / self._base_h)

    @property
    def base_size(self) -> tuple[int, int]:
        return (int(self._base_w), int(self._base_h))

    @property
    def base_pixel_size(self) -> tuple[float, float]:
        return (self._base_w, self._base_h)

    # --- fonts / measurement ----------------------------------------------

    def _face(self, style: Style) -> _Face:
        """Resolve ``style`` to a drawable+measurable face, folding the Style's
        bold/italic attributes into the font (like the Panel does for a grid
        backend, but here the real weight/slant is honored)."""
        font = style.font
        attr = style.attr
        bold = (font.bold if font else False) or bool(attr & TextAttribute.BOLD)
        italic = (font.italic if font else False) or bool(attr & TextAttribute.ITALIC)
        mono = font.monospace if font else True  # font=None -> the mono base grid font
        family = font.family if font else None
        px = float((font.size if font and font.size else None) or self._base_pt)
        key = (mono, bool(bold), bool(italic), family, px)
        face = self._face_cache.get(key)
        if face is None:
            kind = "mono" if mono else "sans"
            table = self._tables[(kind, bool(bold))]
            # CSS family: our bundled @font-face names, or a named installed
            # family the browser has (metrics then approximate the bundled sans).
            if family:
                css_family = f'"{family}", "PuiSans", sans-serif'
            elif mono:
                css_family = '"PuiMono", monospace'
            else:
                css_family = '"PuiSans", sans-serif'
            weight = 700 if bold else 400
            slant = "italic " if italic else ""
            css = f"{slant}{weight} {px:.2f}px {css_family}"
            face = _Face(table, css, px)
            self._face_cache[key] = face
        return face

    def measure_text(self, text: str, style: Style = DEFAULT_STYLE) -> float:
        face = self._face(style)
        return (face.table.advance_text(text) * face.px) / self._base_w

    def measure_line_height(self, style: Style = DEFAULT_STYLE) -> float:
        face = self._face(style)
        return face.line_px / self._base_h

    def measure_font_size(self, style: Style = DEFAULT_STYLE) -> float:
        return self._face(style).px

    def font_metrics(self, style: Style = DEFAULT_STYLE) -> FontMetrics:
        face = self._face(style)
        ascent = face.ascent_px / self._base_h
        descent = (face.line_px - face.ascent_px) / self._base_h
        return FontMetrics(ascent=ascent, descent=descent)

    # --- drawing (display list, base-unit coordinates) --------------------

    def clear(self) -> None:
        self._back = []

    def draw_text(self, x: int, y: int, text: str, style: Style = DEFAULT_STYLE) -> None:
        self._back.append(("text", x, y, text, style))

    def draw_box(
        self, x: int, y: int, w: int, h: int,
        style: Style = DEFAULT_STYLE, hints: dict[str, Any] | None = None,
    ) -> None:
        self._back.append(("box", x, y, w, h, style, hints or {}))

    def fill_rect(self, x: float, y: float, w: float, h: float, style: Style = DEFAULT_STYLE) -> None:
        self._back.append(("fill", x, y, w, h, style))

    def draw_round_rect(
        self, x: float, y: float, w: float, h: float, radius: float | None,
        style: Style = DEFAULT_STYLE, hints: dict[str, Any] | None = None,
    ) -> None:
        self._back.append(("rrect", x, y, w, h, radius, style, hints or {}))

    def draw_check(
        self, x: float, y: float, w: float, h: float,
        style: Style = DEFAULT_STYLE, hints: dict[str, Any] | None = None,
    ) -> None:
        self._back.append(("check", x, y, w, h, style))

    def draw_chevron(
        self, x: float, y: float, w: float, h: float, expanded: bool,
        style: Style = DEFAULT_STYLE, hints: dict[str, Any] | None = None,
    ) -> None:
        self._back.append(("chevron", x, y, w, h, expanded, style))

    def dim_rect(
        self, x: int, y: int, w: int, h: int, scrim: Any = None,
        per_cell: bool = False, fade: bool = False,
    ) -> None:
        # Compositing backend: a real translucent overlay, so the whole-cell
        # scrim/per_cell/fade hints (the terminal stand-ins) are ignored.
        self._back.append(("dim", x, y, w, h))

    def flash_rect(self, x: int, y: int, w: int, h: int, color: Any) -> None:
        self._back.append(("flash", x, y, w, h, tuple(color)))

    def draw_shadow(
        self, x: int, y: int, w: int, h: int, radius: float | None = None,
        corners: tuple[str, ...] | None = None, bg: tuple[int, ...] | None = None,
    ) -> None:
        self._back.append(("shadow", x, y, w, h, radius, bg))

    def draw_scrollbar(
        self, x: int, y: int, h: int, pos: float, ratio: float,
        style: Style = DEFAULT_STYLE, orientation: str = "vertical",
        surface: tuple[int, int, int] | None = None,
    ) -> None:
        self._back.append(("sbar", x, y, h, pos, ratio, style, orientation))

    def draw_image(self, x: int, y: int, path: str, hints: dict[str, Any] | None = None) -> None:
        self._back.append(("image", x, y, path, hints or {}))

    def push_clip(self, x: float, y: float, w: float, h: float) -> None:
        self._back.append(("clip", x, y, w, h))

    def pop_clip(self) -> None:
        self._back.append(("unclip",))

    # --- frame serialization ----------------------------------------------

    def _px_rect(self, x, y, w, h):
        return [x * self._base_w, y * self._base_h, w * self._base_w, h * self._base_h]

    def _serialize(self, display_list: list[tuple]) -> list:
        ops: list = []
        for cmd in display_list:
            kind = cmd[0]
            handler = getattr(self, "_ser_" + kind, None)
            if handler is not None:
                handler(cmd, ops)
        return ops

    def _ser_fill(self, cmd, ops):
        _, x, y, w, h, style = cmd
        col = _css_color(style.bg)
        if col:
            ops.append(["fill", *self._px_rect(x, y, w, h), col])

    def _ser_box(self, cmd, ops):
        _, x, y, w, h, style, hints = cmd
        rect = self._px_rect(x, y, w, h)
        fill = _css_color(style.bg) if hints.get("fill") else None
        stroke = _css_color(style.fg) if style.fg else _css_color(_DEFAULT_FG)
        ops.append(["box", *rect, stroke, fill, 1.0])

    def _ser_rrect(self, cmd, ops):
        _, x, y, w, h, radius, style, hints = cmd
        rect = self._px_rect(x, y, w, h)
        fill = _css_color(style.bg) if hints.get("fill") else None
        stroke = _css_color(style.fg) if style.fg else None
        lw = float(hints.get("line_width", 1))
        ops.append(["rrect", *rect, radius, stroke, fill, lw])

    def _ser_check(self, cmd, ops):
        _, x, y, w, h, style = cmd
        ops.append(["check", *self._px_rect(x, y, w, h), _css_color(style.fg or _DEFAULT_FG)])

    def _ser_chevron(self, cmd, ops):
        _, x, y, w, h, expanded, style = cmd
        ops.append(["chevron", *self._px_rect(x, y, w, h), bool(expanded),
                    _css_color(style.fg or _DEFAULT_FG)])

    def _ser_dim(self, cmd, ops):
        _, x, y, w, h = cmd
        ops.append(["dim", *self._px_rect(x, y, w, h)])

    def _ser_flash(self, cmd, ops):
        # A one-frame highlight *tint* over the existing content (the stepped
        # stand-in the Panel paints when composited animation is off), so a
        # translucent wash — not an opaque cover that would hide the widget.
        _, x, y, w, h, color = cmd
        ops.append(["fill", *self._px_rect(x, y, w, h), _css_color(color, 0.45)])

    def _ser_shadow(self, cmd, ops):
        _, x, y, w, h, radius, bg = cmd
        ops.append(["shadow", *self._px_rect(x, y, w, h), radius,
                    _css_color(bg or _DEFAULT_BG)])

    def _ser_sbar(self, cmd, ops):
        _, x, y, length, pos, ratio, style, orientation = cmd
        thumb = _css_color(style.fg or _SCROLLBAR_THUMB)
        track = _css_color(style.bg or _SCROLLBAR_TRACK)
        if orientation == "horizontal":
            rect = self._px_rect(x, y, length, self.scrollbar_units)
        else:
            rect = self._px_rect(x, y, self.scrollbar_units, length)
        ops.append(["sbar", *rect, float(pos), float(ratio), thumb, track, orientation])

    def _ser_clip(self, cmd, ops):
        _, x, y, w, h = cmd
        ops.append(["clip", *self._px_rect(x, y, w, h)])

    def _ser_unclip(self, cmd, ops):
        ops.append(["unclip"])

    def _ser_text(self, cmd, ops):
        _, x, y, text, style = cmd
        if not text:
            return
        face = self._face(style)
        fg = style.fg or _DEFAULT_FG
        bg = style.bg
        if style.attr & TextAttribute.REVERSE:
            fg, bg = (bg or _DEFAULT_BG), (style.fg or _DEFAULT_FG)
        alpha = 0.55 if style.attr & TextAttribute.DIM else 1.0
        x_px = x * self._base_w
        top_px = y * self._base_h
        width_px = face.table.advance_text(text) * face.px
        # Background band behind the run (skip a transparent request, which asks
        # to paint glyphs only over whatever is beneath).
        if bg is not None and not is_transparent(bg):
            ops.append(["fill", x_px, top_px, width_px, face.line_px, _css_color(bg)])
        underline = bool(style.attr & TextAttribute.UNDERLINE)
        strike = bool(style.attr & TextAttribute.STRIKETHROUGH)
        ops.append(["text", x_px, top_px + face.ascent_px, text, face.css,
                    _css_color(fg, alpha), underline, strike])

    def _ser_image(self, cmd, ops):
        from ..image import CONTAIN, COVER, contain_box, cover_source

        _, x, y, path, hints = cmd
        size = self.image_size(path)
        if size is None:
            return
        iw, ih = size
        w_units = hints.get("w", max(1, round(iw / self._base_w)))
        h_units = hints.get("h", max(1, round(ih / self._base_h)))
        tx, ty, tw, th = self._px_rect(x, y, w_units, h_units)
        fit = hints.get("fit", "fill")
        src = hints.get("src")
        # Source crop (client canvas is top-left origin, so no Y flip).
        if src is not None:
            fx, fy, fw, fh = src
            sx, sy, sw, sh = fx * iw, fy * ih, fw * iw, fh * ih
        else:
            sx, sy, sw, sh = 0.0, 0.0, float(iw), float(ih)
        dx, dy, dw, dh = tx, ty, tw, th
        if fit == CONTAIN and src is None:
            ox, oy, bw, bh = contain_box(tw, th, iw, ih)
            dx, dy, dw, dh = tx + ox, ty + oy, bw, bh
        elif fit == COVER and src is None:
            sx, sy, sw, sh = cover_source(iw, ih, tw, th)
        alpha = float(hints.get("alpha", 1.0))
        ops.append(["img", path, sx, sy, sw, sh, dx, dy, dw, dh, alpha])

    # --- present ----------------------------------------------------------

    def present(self) -> None:
        frame, self._back = self._back, []
        server = self._server
        if server is None:
            return
        # Make sure the client holds the bytes of every image this frame draws
        # before the frame references it (sent once per distinct path).
        for cmd in frame:
            if cmd[0] == "image":
                self._ensure_image(cmd[3])
        ops = self._serialize(frame)
        server.send(json.dumps({"type": "frame", "ops": ops}))

    def _ensure_image(self, path: str) -> None:
        if path in self._sent_images or self._server is None:
            return
        self._sent_images.add(path)
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            return
        ext = os.path.splitext(path)[1].lstrip(".").lower() or "png"
        mime = "jpeg" if ext in ("jpg", "jpeg") else ext
        url = f"data:image/{mime};base64," + base64.b64encode(data).decode()
        self._server.send(json.dumps({"type": "asset", "id": path, "url": url}))

    # --- system integration ------------------------------------------------

    def set_pointer_shape(self, shape: str | None) -> None:
        # The Panel re-asserts the hovered region's cursor every render; only
        # push a message when it actually changes, so a steady pointer costs
        # nothing per frame.
        resolved = shape or "default"
        if resolved == self._pointer_shape:
            return
        self._pointer_shape = resolved
        if self._server is not None:
            self._server.send(json.dumps({"type": "cursor", "shape": resolved}))

    def open_url(self, url: str) -> bool:
        if self._server is not None and self._server.send(
            json.dumps({"type": "open_url", "url": url})
        ):
            return True
        return super().open_url(url)

    # --- animation ticks ---------------------------------------------------

    def request_animation_ticks(self, callback) -> None:
        with self._tick_lock:
            if callback not in self._tick_callbacks:
                self._tick_callbacks.append(callback)
        self._ensure_ticker()

    _TICK_INTERVAL = 1 / 60.0

    def _ensure_ticker(self) -> None:
        if self._ticker is not None and self._ticker.is_alive():
            return
        self._ticker = threading.Thread(target=self._tick_loop, daemon=True)
        self._ticker.start()

    def _tick_loop(self) -> None:
        # Enqueue a tick sentinel at ~60fps while callbacks remain; the loop
        # thread runs them (they re-render), so ticks never touch the UI state
        # off-thread. Exits once the list drains, and is restarted on the next
        # request_animation_ticks.
        while not self._quit:
            with self._tick_lock:
                if not self._tick_callbacks:
                    return
            self._queue.put(_TICK)
            time.sleep(self._TICK_INTERVAL)

    def _stop_ticker(self) -> None:
        with self._tick_lock:
            self._tick_callbacks = []

    def _run_ticks(self) -> None:
        with self._tick_lock:
            callbacks = list(self._tick_callbacks)
        survivors = [cb for cb in callbacks if cb()]
        with self._tick_lock:
            # Keep only callbacks that survived and were not removed meanwhile.
            self._tick_callbacks = [cb for cb in self._tick_callbacks if cb in survivors]

    # --- incoming client messages -----------------------------------------

    def _on_connect(self) -> None:
        # A reconnected tab (a reload) starts with an empty image cache and a
        # default cursor, so forget what the previous page was told.
        self._sent_images.clear()
        self._pointer_shape = None

    def _on_message(self, text: str) -> None:
        try:
            msg = json.loads(text)
        except ValueError:
            return
        kind = msg.get("type")
        if kind == "resize":
            self._handle_resize(msg)
        elif kind == "key":
            event = translate_key(msg.get("key", ""), msg.get("mods", {}))
            if event is not None:
                self._queue.put(event)
        elif kind == "mouse":
            event = self._mouse_event(msg)
            if event is not None:
                self._queue.put(event)

    def _handle_resize(self, msg: dict) -> None:
        w = float(msg.get("w", 0)) or 1.0
        h = float(msg.get("h", 0)) or 1.0
        self._canvas_px = (w, h)
        first = not self._connected.is_set()
        self._connected.set()
        units = self.size
        if not first and units != self._last_units:
            self._last_units = units
            self._queue.put(Event(type=EventType.RESIZE))

    def _mouse_event(self, msg: dict) -> Event | None:
        bx = float(msg.get("x", 0)) / self._base_w
        by = float(msg.get("y", 0)) / self._base_h
        mods = _modifier_names(msg.get("mods", {}))
        button = msg.get("button", "left")
        kind = msg.get("kind")
        if kind == "down":
            self._pressed_button = button
            return Event(EventType.MOUSE_DOWN, x=bx, y=by, button=button, modifiers=mods)
        if kind == "up":
            self._pressed_button = None
            return Event(EventType.MOUSE_UP, x=bx, y=by, button=button, modifiers=mods)
        if kind == "move":
            if self._pressed_button is not None:
                return Event(EventType.MOUSE_DRAG, x=bx, y=by,
                             button=self._pressed_button, modifiers=mods)
            return Event(EventType.MOUSE_MOVE, x=bx, y=by, modifiers=mods)
        if kind == "scroll":
            dy = float(msg.get("dy", 0.0))
            dx = float(msg.get("dx", 0.0))
            # Positive scroll = away/up; the browser's wheel deltaY is positive
            # downward, so invert. Precise (trackpad) deltas ride in hints as
            # base units for smooth scrolling.
            notch = -1 if dy > 0 else 1 if dy < 0 else 0
            return Event(
                EventType.MOUSE_SCROLL, x=bx, y=by, scroll=notch, modifiers=mods,
                hints={"scroll_units": -dy / self._base_h,
                       "scroll_units_x": -dx / self._base_w},
            )
        return None

    # --- event loop --------------------------------------------------------

    def _handle_item(self, item: Any, handler: EventHandler) -> None:
        if item is _TICK:
            self._run_ticks()
        elif isinstance(item, Event):
            handler(item)

    def run_event_loop(self, handler: EventHandler) -> None:
        self._quit = False
        while not self._quit:
            try:
                item = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if item is _WAKE:
                continue
            self._handle_item(item, handler)

    def run_event_loop_iteration(self, handler: EventHandler, timeout_ms: int = 0) -> bool:
        if self._quit:
            return False
        try:
            item = self._queue.get(timeout=max(0.0, timeout_ms / 1000.0))
        except queue.Empty:
            return not self._quit
        if item is not _WAKE:
            self._handle_item(item, handler)
        return not self._quit

    def quit(self) -> None:
        self._quit = True
        self._queue.put(_WAKE)


# Loop control sentinels (identity-compared in the loop).
_TICK = object()
_WAKE = object()
