"""Backend interface definition.

A Backend turns drawing intents into actual output and native input into
Event objects. Core primitives must be implemented by every backend;
extended primitives are optional and the Panel layer provides fallbacks
based on the backend's CapabilityProfile.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
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


# RGB, or RGBA with a 4th alpha channel (0-255 per channel; alpha 255 = opaque,
# a 3-tuple is implicitly opaque). Transparency-capable backends composite an
# RGBA color per pixel; others have the Panel layer flatten it over the pane
# background to an opaque RGB before it reaches the backend.
Color = tuple[int, int, int] | tuple[int, int, int, int]


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
    def dim_rect(self, x: int, y: int, w: int, h: int) -> None:
        """Dim already-drawn content in the region, e.g. below a dialog layer.
        GUI: translucent dark overlay; TUI: approximated with dim/dark
        attributes."""

    @abstractmethod
    def draw_scrollbar(
        self, x: int, y: int, h: int, pos: float, ratio: float, style: Style = DEFAULT_STYLE
    ) -> None:
        """Vertical scrollbar. ``pos`` (0..1) is the thumb position,
        ``ratio`` (0..1) is the visible fraction of the content."""

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

    def draw_shadow(self, x: int, y: int, w: int, h: int) -> None:
        raise CapabilityNotSupported("shadow")

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
