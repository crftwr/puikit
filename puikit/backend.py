"""Backend interface definition.

A Backend turns drawing intents into actual output and native input into
Event objects. Core primitives must be implemented by every backend;
extended primitives are optional and the Panel layer provides fallbacks
based on the backend's CapabilityProfile.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from enum import IntFlag
from typing import Any

from .capability import CapabilityProfile
from .event import Event
from .font import Font


class TextAttribute(IntFlag):
    NORMAL = 0
    BOLD = 1
    UNDERLINE = 2
    REVERSE = 4
    DIM = 8
    BLINK = 16
    ITALIC = 32
    STRIKETHROUGH = 64


# RGB, or RGBA with a 4th alpha channel (0-255 per channel; alpha 255 = opaque,
# a 3-tuple is implicitly opaque). Transparency-capable backends composite an
# RGBA color per pixel; others have the Panel layer flatten it over the pane
# background to an opaque RGB before it reaches the backend.
Color = tuple[int, int, int] | tuple[int, int, int, int]


#: A fully-transparent background (RGBA, alpha 0). Text drawn with this as its
#: ``bg`` renders its glyphs only — no background fill — on transparency-capable
#: backends, so it composites over whatever was already drawn beneath it (e.g. a
#: cursor outline that must stay unbroken). Because it is not ``None``, the Panel
#: style resolver leaves it in place instead of inheriting the pane background;
#: backends without per-pixel transparency have that resolver flatten it back to
#: the pane background (an opaque approximation) before it reaches them.
TRANSPARENT: "Color" = (0, 0, 0, 0)


def is_transparent(color: "Color | None") -> bool:
    """True when ``color`` is a fully-transparent RGBA (alpha 0), i.e. a request
    to paint no background at all — distinct from ``None`` (inherit the pane's)."""
    return color is not None and len(color) == 4 and color[3] == 0


@dataclass(frozen=True)
class Style:
    fg: Color | None = None
    bg: Color | None = None
    attr: TextAttribute = TextAttribute.NORMAL
    # Optional font for text. None -> the backend's base monospaced grid font,
    # so every existing widget renders unchanged. A `fonts`-capable backend
    # renders a real face/size/weight/slant (proportional when monospace=False);
    # backends without `fonts` have the Panel fold weight/slant into `attr` and
    # drop the rest (see docs/font_system.md §6).
    font: Font | None = None


DEFAULT_STYLE = Style()

EventHandler = Callable[[Event], None]


class CapabilityNotSupported(Exception):
    """Raised when an extended primitive is called on a backend without it."""


class Backend(ABC):
    """Abstract base class for all PuiKit backends."""

    PROFILE: CapabilityProfile = CapabilityProfile()

    @property
    def capabilities(self) -> CapabilityProfile:
        return self.PROFILE

    # --- lifecycle ---------------------------------------------------------

    @abstractmethod
    def open(self) -> None:
        """Initialize the backend (window, terminal mode, ...)."""

    @abstractmethod
    def close(self) -> None:
        """Tear down the backend and restore the environment."""

    def __enter__(self) -> "Backend":
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- geometry ----------------------------------------------------------

    @property
    @abstractmethod
    def size(self) -> tuple[int, int]:
        """Drawable area in whole base units (width, height)."""

    @property
    def size_units(self) -> tuple[float, float]:
        """Exact drawable area in base units. Pixel-layout backends return
        fractional values when the window is not a whole number of base units,
        so layouts can fill it to the last pixel."""
        w, h = self.size
        return (float(w), float(h))

    @property
    def base_size(self) -> tuple[int, int]:
        """Size of one base unit in pixels. (1, 1) for TUI backends."""
        return (1, 1)

    @property
    def scrollbar_units(self) -> float:
        """Backend's fixed scrollbar thickness, in base units. Font-independent:
        a scrollbar that asks the layout to size it (size="content") gets this
        width. One base unit on whole-unit backends; GUI backends may override."""
        return 1.0

    def measure_text(self, text: str, style: Style = DEFAULT_STYLE) -> float:
        """Displayed width of ``text`` in base units (fractional on GUI). Whole-unit
        backends count display columns (East Asian wide characters count as two);
        GUI backends with a proportional or sized font measure natively and divide
        by the base unit width, so the result stays in the shared base unit. Used
        by widgets that size themselves to text (a label, a button) — the layout
        never calls a font directly."""
        from .text import display_width

        return float(display_width(text))

    def measure_line_height(self, style: Style = DEFAULT_STYLE) -> float:
        """Vertical advance of one text row in base units. The base grid font
        (font=None) is one base unit by definition; a taller per-Style font
        needs more, so a multi-line widget asks here for its row pitch instead
        of assuming one base unit. Whole-unit backends fold the font away and
        always answer 1.0 — like measure_text, this routes font metrics through
        the backend so the widget never reads a font."""
        return 1.0

    # Nominal point size of the base grid font, the size a Font that names no
    # size of its own resolves to. Backends with a differently sized base font
    # override measure_font_size.
    BASE_FONT_SIZE = 14.0

    def measure_font_size(self, style: Style = DEFAULT_STYLE) -> float:
        """Resolved point size of ``style``'s font, in points. A widget that
        derives one size from another (a heading scaled off the body size) reads
        the body's absolute size here and keeps only the ratio, so the absolute
        size stays the backend's. Whole-unit backends fold the font away on
        screen but still answer the nominal size, so the same relative math runs
        on every backend."""
        font = style.font
        if font is not None and font.size is not None:
            return float(font.size)
        return self.BASE_FONT_SIZE

    # --- core drawing primitives (all backends implement) -------------------

    @abstractmethod
    def clear(self) -> None: ...

    @abstractmethod
    def draw_text(self, x: int, y: int, text: str, style: Style = DEFAULT_STYLE) -> None: ...

    @abstractmethod
    def draw_box(
        self,
        x: int,
        y: int,
        w: int,
        h: int,
        style: Style = DEFAULT_STYLE,
        hints: dict[str, Any] | None = None,
    ) -> None:
        """Rectangle outline. With hints={"fill": True} the interior is
        filled (TUI: spaces with the style's background; GUI: filled rect)."""

    @abstractmethod
    def dim_rect(
        self,
        x: int,
        y: int,
        w: int,
        h: int,
        scrim: tuple[Color, Color] | None = None,
        per_cell: bool = False,
        fade: bool = False,
    ) -> None:
        """Dim already-drawn content in the region, e.g. below a dialog layer.
        GUI: translucent dark overlay; TUI: approximated with dim/dark
        attributes. ``scrim`` is an optional (fg, bg) pair a whole-cell backend
        recolors every cell with instead of its built-in modal scrim — used by
        the Panel's 2-frame ``fade`` to wash a group toward its own (possibly
        light) background rather than a fixed dark veil. ``per_cell`` asks a
        whole-cell backend to composite the veil (the scrim's bg) over each
        cell's own color instead of flattening to one pair, so the page shows
        through faintly — the TUI stand-in for the translucent overlay a
        compositing backend always draws. ``fade`` asks a whole-cell backend to
        play the 2-frame ``fade`` as opacity: blend each cell's own fg toward its
        own bg (keeping the bg), so the intermediate frame follows the actual
        grid cells rather than collapsing every surface to the ``scrim`` pair
        (the scrim is then only the fallback for untouched cells). Compositing
        backends alpha-blend and ignore all three hints."""

    def flash_rect(self, x: int, y: int, w: int, h: int, color: Color) -> None:
        """Tint already-drawn content in the region with ``color`` for one
        frame — the stepped (terminal) stand-in for a composited highlight
        overlay, used only by the Panel's 2-frame ``highlight`` effect. The
        default is a no-op: compositing backends realize highlight through their
        own alpha overlay (``animate``) and never reach this."""

    @abstractmethod
    def draw_scrollbar(
        self, x: int, y: int, h: int, pos: float, ratio: float,
        style: Style = DEFAULT_STYLE, orientation: str = "vertical",
        surface: tuple[int, int, int] | None = None,
    ) -> None:
        """A scrollbar of length ``h`` along its axis, anchored at ``(x, y)``.
        ``pos`` (0..1) is the thumb position, ``ratio`` (0..1) the visible
        fraction of the content. ``orientation`` is "vertical" (``h`` runs down)
        or "horizontal" (``h`` runs right). A vector backend draws the horizontal
        bar the same px thickness as the vertical one (a base-unit *row* would be
        too thick), centered in its row.

        ``surface`` is the client-area background behind the bar. On a character
        grid the horizontal bar is a lower-half-block glyph, so its *upper* half
        shows this color (None → the terminal default); the vector backends, which
        only paint the thin bar itself, ignore it (the row already shows the
        surface around it)."""

    @abstractmethod
    def fill_rect(self, x: float, y: float, w: float, h: float, style: Style = DEFAULT_STYLE) -> None:
        """Fill the region with the style's background color (TUI: spaces
        with the background; GUI: solid rect). Used for pane backgrounds."""

    @abstractmethod
    def push_clip(self, x: float, y: float, w: float, h: float) -> None:
        """Restrict subsequent drawing to the rect, intersected with any
        enclosing clip. On GUI backends the rect lives in the current
        (possibly animated) transform, so clips travel with transitions."""

    @abstractmethod
    def pop_clip(self) -> None:
        """Remove the most recent clip rect."""

    @abstractmethod
    def present(self) -> None:
        """Flush pending drawing to the screen."""

    # --- extended drawing primitives (GUI only; Panel handles fallback) -----

    def draw_icon(self, x: int, y: int, icon_name: str, style: Style = DEFAULT_STYLE) -> None:
        raise CapabilityNotSupported("icons")

    def draw_shadow(
        self,
        x: int,
        y: int,
        w: int,
        h: int,
        radius: float | None = None,
        corners: "tuple[str, ...] | None" = None,
        bg: "Color | None" = None,
    ) -> None:
        """Cast a drop shadow from the layer's silhouette. ``radius`` (device
        pixels) and ``corners`` (a subset of ``"tl"``/``"tr"``/``"br"``/``"bl"``)
        round the silhouette so the shadow matches a rounded panel; ``None``
        means a square rect / all four corners.

        ``bg`` is the layer's own surface color, used to fill the opaque caster
        silhouette the shadow is cast from. It must match the color the layer
        paints on top: the caster is only meant to be hidden behind the content,
        but content that fills a whole-unit-snapped rect can leave a sub-unit
        sliver of the caster exposed at the layer edge, so a mismatched (e.g.
        hardcoded window-dark) caster reads there as a hard TUI-style fringe
        rather than a soft shadow. ``None`` falls back to the window background."""
        raise CapabilityNotSupported("shadow")

    def shadow_rect(
        self, x: int, y: int, w: int, h: int, base_bg: "Color | None" = None
    ) -> None:
        """Stepped drop-shadow stand-in: darken the page cells in a one-cell halo
        around the layer rect (the TUI equivalent of a soft GUI shadow), reading
        the underlying colors so text stays rendered. The Panel calls this for a
        ``shadow`` hint on backends without real ``shadow`` compositing; a backend
        that can't darken in place ignores it. ``base_bg`` is the page background
        behind the layer, used where a cell has no recorded color."""

    def draw_round_rect(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        radius: float | None,
        style: Style = DEFAULT_STYLE,
        hints: dict[str, Any] | None = None,
    ) -> None:
        """Rounded rectangle in base-unit coordinates. ``radius`` is the corner
        radius in device pixels; ``None`` means fully rounded (a pill/circle).
        With hints={"fill": True} the interior is filled with ``style.bg``; a
        non-None ``style.fg`` strokes the outline (hints "line_width" in device
        pixels, default 1). The modern, non-character control face — the Panel
        layer falls back to ``fill_rect``/``draw_box`` on backends without the
        ``vector_shapes`` capability."""
        raise CapabilityNotSupported("vector_shapes")

    def draw_check(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        style: Style = DEFAULT_STYLE,
        hints: dict[str, Any] | None = None,
    ) -> None:
        """A check-mark glyph inscribed in the (base-unit) rect, stroked with
        ``style.fg``. Used by the checkbox's vector face; backends without
        ``vector_shapes`` never receive it (the Panel layer draws "[x]" text)."""
        raise CapabilityNotSupported("vector_shapes")

    # --- animation (capability "animation"; Panel gates the calls) -----------

    def animate(self, widget: Any, hints: dict[str, Any] | None = None) -> None:
        """Start a render-level transition for the widget's next appearance,
        e.g. hints={"transition": "fade", "duration_ms": 200}."""
        raise CapabilityNotSupported("animation")

    def request_animation_ticks(self, callback: Callable[[], bool]) -> None:
        """Register a callback invoked on each animation frame (~60fps) for
        layout-level animations driven above the backend (e.g. the Panel's
        "size" transition). The callback returns False to unregister."""
        raise CapabilityNotSupported("animation")

    def call_on_main_thread(self, callback: Callable[[], None]) -> None:
        """Schedule ``callback`` to run on the event-loop (UI) thread, waking a
        blocked loop if necessary. Thread-safe: the whole point is for a worker
        thread to hand UI work back without polling. Gated on the
        ``main_thread_dispatch`` capability — only backends that run a native
        loop on a distinct thread provide it; a single-threaded poll-loop backend
        drains its own producers each iteration and does not need it."""
        raise CapabilityNotSupported("main_thread_dispatch")

    # --- command grouping ----------------------------------------------------

    def begin_group(self, key: Any, rect: Any = None) -> None:
        """Called by the Panel before a widget draws, with the widget's rect
        in base units. Backends that render per-widget effects (animation alpha,
        transforms, ...) use the markers; the default is a no-op."""

    def end_group(self, key: Any) -> None:
        """Counterpart of begin_group."""

    def draw_image(self, x: int, y: int, path: str, hints: dict[str, Any] | None = None) -> None:
        raise CapabilityNotSupported("images")

    def image_size(self, path: str) -> tuple[int, int] | None:
        """Natural ``(width, height)`` of the image in pixels, or None if
        unknown. Lets the layout size an ImageView to its aspect ratio
        (fit="width"/"height") on every backend, even where the image cannot
        be drawn. The default parses the file header (puikit.image); a backend
        may override with a native loader."""
        from .image import image_size

        return image_size(path)

    # --- native menus (capability "native_menus"; Panel gates the calls) -----

    def set_menu_bar(self, menu: Any) -> None:
        """Install ``menu`` (a puikit.menu.Menu whose items carry submenus) as
        the OS application menu bar. ``None`` clears it. Backends without the
        ``native_menus`` capability never receive this — the Panel falls back
        to a widget-rendered MenuBar placed in the app's own layout."""
        raise CapabilityNotSupported("native_menus")

    def popup_menu(
        self, menu: Any, x: float, y: float, on_done: Callable[[], None] | None = None
    ) -> None:
        """Pop up ``menu`` as an OS context menu, its top-left near base-unit
        position (x, y). The OS owns the interaction and fires each chosen
        item's callback; ``on_done`` is invoked when the menu closes. Backends
        without ``native_menus`` never receive this — the Panel falls back to a
        widget-rendered popup layer."""
        raise CapabilityNotSupported("native_menus")

    # --- clipboard ----------------------------------------------------------

    def get_clipboard(self) -> str:
        """Current plain-text clipboard contents (empty string if none).

        The default is a process-local buffer, so widgets get working
        copy/paste on every backend (curses, headless tests, ...) even where no
        OS clipboard is reachable. Backends with a real system clipboard
        (``clipboard_rich`` or otherwise) override both accessors to bridge to
        it; the Panel/widget layer never branches on the capability."""
        return getattr(self, "_clipboard", "")

    def set_clipboard(self, text: str) -> None:
        """Replace the plain-text clipboard contents. See ``get_clipboard``."""
        self._clipboard = text

    # --- pointer shape (capability "pointer_shape"; Panel gates the calls) ----

    def set_pointer_shape(self, shape: str | None) -> None:
        """Request the pointer shape under the mouse, named with a CSS/X cursor
        name (``"text"``, ``"pointer"``, ``"crosshair"``, ``"not-allowed"``,
        ``"grabbing"``, ...); ``None`` resets to the default arrow.

        Only backends with the ``pointer_shape`` capability act on this. A GUI
        backend sets a real OS cursor (``NSCursor`` / ``SetCursor`` / CSS
        ``cursor``); a terminal backend can at most *ask* its emulator (OSC 22),
        which is emulator-gated and silently ignored where unsupported. The
        Panel resolves the hovered region's ``cursor`` hint into one intent and
        never branches; the default no-ops, so backends without the capability
        need no override."""

    # --- text input / IME activation -----------------------------------------

    def begin_text_input(self) -> None:
        """Engage the platform text-input system because a text widget took
        focus. The Panel calls this when focus lands on a widget that declares
        ``wants_text_input`` (a ``TextEdit``/``ComboBox``).

        Only after this does a GUI backend route key presses through the OS text
        services (IME composition, dead keys, layout translation) and deliver
        committed characters / ``IME_COMPOSITION`` preedit. **While inactive a
        GUI backend must deliver plain command KEY events instead**, so a
        single-letter binding (a file-manager's ``j`` / ``f``) dispatches as a
        command even when a CJK input source is selected — otherwise the IME
        would swallow it into composition. The default no-ops: a terminal has no
        IME, and a still backend nothing to engage."""

    def end_text_input(self) -> None:
        """Disengage the text-input system because focus left the text widget
        (its inverse). The default no-ops."""

    # --- open a URL / file (capability "os_open"; Panel falls back) ----------

    def open_url(self, url: str) -> bool:
        """Open ``url`` in the OS handler (a browser for ``http(s)``, the default
        app for a file path), used for a clicked hyperlink.

        Only backends with the ``os_open`` capability really launch a handler —
        it needs an OS shell a terminal app does not own. The default copies the
        URL to the clipboard so the user can paste it, and returns False; the
        Panel issues one ``open_url`` intent and never branches."""
        self.set_clipboard(url)
        return False

    # --- drag source (capability "os_drag_drop"; Panel gates the calls) ------

    def begin_file_drag(
        self,
        paths: list[str],
        event: Event | None = None,
        operations: tuple[str, ...] = ("copy",),
        on_complete: Callable[[str], None] | None = None,
    ) -> bool:
        """Begin an OS drag session exporting ``paths`` as files, so the user
        can drop them onto another application (Finder, an editor, ...).

        Only backends with the ``os_drag_drop`` capability implement this:
        being a drag *source* requires a native window/view (macOS
        ``NSDraggingSource``), which a terminal app does not own. The Panel
        gates the call on the capability and, where it is missing, falls back to
        copying the paths to the clipboard — so the app issues one intent and
        never branches. ``event`` is the originating ``MOUSE_DRAG`` event when
        available (a native session must start from the live mouse event).

        ``operations`` is the set of operations the source offers — any of
        ``"copy"``, ``"move"``, ``"link"``; the destination app chooses one.
        The bytes are always copied to the receiver: for ``"move"`` the *source*
        is responsible for removing the originals. PuiKit never deletes files —
        it reports the chosen operation through ``on_complete(op)`` (``op`` is
        ``"copy"`` / ``"move"`` / ``"link"`` / ``"none"`` if cancelled) so the
        app performs the move (and any undo bookkeeping) itself.

        Returns True if a real drag session began."""
        raise CapabilityNotSupported("os_drag_drop")

    # --- event loop ---------------------------------------------------------

    @abstractmethod
    def run_event_loop(self, handler: EventHandler) -> None:
        """Run until quit() is called, delivering events to ``handler``."""

    @abstractmethod
    def run_event_loop_iteration(self, handler: EventHandler, timeout_ms: int = 0) -> bool:
        """Process at most one pending event. Returns False once quit() was
        requested, True otherwise."""

    @abstractmethod
    def quit(self) -> None:
        """Request the event loop to stop after the current iteration."""

    # --- shell-out ----------------------------------------------------------

    @contextmanager
    def suspended(self):
        """Temporarily hand the display back to the terminal/OS so a full-screen
        child process (an editor, a subshell) can own it, then reclaim it.

        The base implementation is a no-op: on GUI backends a launched program
        opens in its own window and nothing needs releasing. Terminal backends
        override this to leave curses/raw mode on entry and restore it on exit.
        Use as ``with backend.suspended(): subprocess.run(...)``."""
        yield
