"""Panel / Layout / Layer management.

The Panel is the only API widgets talk to. It places widgets in base unit
coordinates, resolves backend capabilities, and contains all fallback
chains so widget code never branches on TUI/GUI.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field, replace
from typing import Any

from .backend import Backend, DEFAULT_STYLE, Style, TextAttribute
from .capability import CapabilityProfile
from .event import Event, EventType
from .focus import focus_on_click, move_focus
from .font import FontSlant, FontWeight
from .theme import Theme, theme_for

# Text fallbacks used when a backend cannot draw real icons.
ICON_TEXT_FALLBACKS = {
    "folder": "📁",
    "file": "📄",
    "warning": "⚠",
    "error": "✖",
    "info": "ℹ",
    "check": "✔",
}


@dataclass(frozen=True)
class Rect:
    # Base-unit coordinates. Fractional values are produced by the layout system
    # on pixel_layout-capable backends; whole-unit backends only see integers.
    x: float
    y: float
    w: float
    h: float

    def contains(self, x: float, y: float) -> bool:
        return self.x <= x < self.x + self.w and self.y <= y < self.y + self.h


def _composite(color, base):
    """Flatten an RGBA color over an opaque base to an opaque RGB, for backends
    that cannot composite per pixel (TUI). A 3-tuple (already opaque) or None
    passes through; with no known base the alpha is simply dropped."""
    if color is None or len(color) < 4:
        return color
    r, g, b, a = color
    if base is None:
        return (r, g, b)
    f = a / 255.0
    return (
        round(r * f + base[0] * (1 - f)),
        round(g * f + base[1] * (1 - f)),
        round(b * f + base[2] * (1 - f)),
    )


def _intersect(a: Rect, b: Rect) -> Rect | None:
    x0, y0 = max(a.x, b.x), max(a.y, b.y)
    x1 = min(a.x + a.w, b.x + b.w)
    y1 = min(a.y + a.h, b.y + b.h)
    if x1 <= x0 or y1 <= y0:
        return None
    return Rect(x0, y0, x1 - x0, y1 - y0)


class DrawContext:
    """Drawing surface handed to a widget, translated to the widget's origin
    and clipped to its rectangle. Capability fallbacks live here."""

    def __init__(
        self,
        backend: Backend,
        rect: Rect,
        capabilities: CapabilityProfile,
        clip: Rect | None = None,
        panel: "Panel | None" = None,
        background: tuple[int, int, int] | None = None,
        focused: bool = False,
    ):
        self._backend = backend
        self._rect = rect
        self._caps = capabilities
        self._clip = clip if clip is not None else rect
        self._panel = panel
        self._background = background
        # Whether this widget currently holds the focus, resolved down the
        # parent chain (a widget is focused only if every container above it is
        # focused too). Interactive widgets read it to draw a focus cue; the
        # Panel layer owns the resolution so widgets never touch focus state.
        self._focused = focused
        # Backend clips this context pushed itself (e.g. draw_border's
        # interior clip); the Panel pops them when the widget's draw returns.
        self._pushed_clips = 0

    def _resolve(self, style: Style) -> Style:
        """The single seam every Style crosses before the backend sees it:
        styles without an explicit background inherit the pane's, and a font
        is folded down for backends that cannot render it (docs/font_system.md
        §6) — weight/slant become bold/italic attributes, the rest is dropped."""
        bg = self._background if (self._background is not None and style.bg is None) else style.bg
        fg = style.fg
        attr = style.attr
        font = style.font
        if font is not None and not self._caps.supports("fonts"):
            if font.weight >= FontWeight.SEMI_BOLD:
                attr |= TextAttribute.BOLD
            if font.slant is FontSlant.ITALIC:
                attr |= TextAttribute.ITALIC
            font = None
        if not self._caps.supports("transparency"):
            # No per-pixel compositing: flatten any RGBA color over the pane
            # background (bg over the pane, fg over the resolved bg) to the
            # opaque approximation the terminal can actually render.
            base = self._background
            bg = _composite(bg, base)
            fg = _composite(fg, bg or base)
        if (fg, bg, attr, font) == (style.fg, style.bg, style.attr, style.font):
            return style
        return Style(fg, bg, attr, font)

    @property
    def width(self) -> int:
        return int(self._rect.w)

    @property
    def height(self) -> int:
        return int(self._rect.h)

    @property
    def size_units(self) -> tuple[float, float]:
        """Exact extent in base units; fractional on pixel-aware backends."""
        return (self._rect.w, self._rect.h)

    @property
    def base_size(self) -> tuple[int, int]:
        """Pixel size of one base unit, as declared by the backend."""
        return self._backend.base_size

    @property
    def vector_shapes(self) -> bool:
        """True when the backend renders true device pixels (rounded rects,
        hairlines, sub-unit insets). False on a character grid — including a grid
        handed a GUI profile for a test — where sub-unit padding and frame lines
        must collapse so content stays aligned to whole cells. Widgets read this
        only to drop pixel-only ornamentation; the visible-vs-grid drawing choice
        itself still lives in the Panel layer (round_rect, draw_*_mark)."""
        return self._caps.supports("vector_shapes")

    @property
    def animated(self) -> bool:
        """True when the backend can render real transitions and per-frame
        animation ticks. A widget that drives its own motion (a busy spinner)
        reads it to decide whether to register ticks via
        ``panel.request_animation_ticks``; on a still backend it just renders a
        single frame whenever the panel re-renders. The capability is resolved
        here, not by the widget."""
        return self._caps.supports("animation")

    @property
    def native_menus(self) -> bool:
        """True when the backend owns an OS menu bar / context menus. A MenuBar
        widget reads it to know it should register the native bar and claim no
        in-window space, instead of rendering an in-window strip. The
        capability is resolved here, not by the widget."""
        return self._caps.supports("native_menus")

    @property
    def focused(self) -> bool:
        """True when this widget holds the keyboard focus. Interactive widgets
        use it to draw a focus cue (a reversed marker, a cursor); the value is
        resolved by the Panel/container chain, not by the widget."""
        return self._focused

    @property
    def hovered(self) -> bool:
        """True when the mouse pointer is over this widget's visible rect.
        Drives modern hover styling; resolved by pure geometry against the
        Panel's last pointer position, so widgets never track the mouse."""
        if self._panel is None or self._panel.pointer is None:
            return False
        px, py = self._panel.pointer
        return self._rect.contains(px, py) and self._clip.contains(px, py)

    @property
    def panel(self) -> "Panel | None":
        """The owning Panel — the one API a widget talks to for layers
        (push_layer for a popup) and text input (request_text_input for IME).
        Captured by widgets during draw and used later in event handling."""
        return self._panel

    @property
    def screen_rect(self) -> tuple[float, float, float, float]:
        """This widget's absolute rect in base units (x, y, w, h). A widget
        uses it to position a popup layer under itself or to tell the backend
        where its text cursor is for the IME candidate window."""
        return (self._rect.x, self._rect.y, self._rect.w, self._rect.h)

    @property
    def theme(self) -> "Theme | None":
        return self._panel.theme if self._panel is not None else None

    def layout_context(self) -> "Any":
        """Build a LayoutContext matching this backend's capabilities, so a
        widget can resolve a nested puikit.layout Split against its own rect
        with the same base unit-vs-pixel rules the Panel uses for the top level.
        Capability resolution stays here, not in the widget."""
        from .layout import LayoutContext

        cw, ch = self._backend.base_size
        return LayoutContext(
            cw, ch,
            snap=not self._caps.supports("pixel_layout"),
            hairline=self._caps.supports("hairline"),
            native_menus=self._caps.supports("native_menus"),
            measure=self._backend.measure_text,
            line_height=self._backend.measure_line_height,
            scrollbar_units=self._backend.scrollbar_units,
            image_size=self._backend.image_size,
        )

    def line_height(self, style: Style = DEFAULT_STYLE) -> float:
        """Row pitch of ``style``'s font in this pane's unit: one base unit for
        the grid font, more for a taller per-Style font. A stacked-text widget
        uses it to space its rows so a proportional/sized font does not overlap.
        The font is folded first, matching what draw_text will draw."""
        return self._backend.measure_line_height(self._resolve(style))

    def measure_text(self, text: str, style: Style = DEFAULT_STYLE) -> float:
        """Displayed width of ``text`` in this pane's unit (base units;
        fractional on GUI), so a widget can center, right-align, or wrap
        proportional text against its pane size. Whole-unit backends count
        columns; the font is folded first, matching what draw_text will draw."""
        return self._backend.measure_text(text, self._resolve(style))

    def draw_text(self, x: float, y: float, text: str, style: Style = DEFAULT_STYLE) -> None:
        # Gate on the exact (possibly fractional) extent and let the
        # backend's clip rect cut the overflow: a pane squeezed to 0.97
        # base units by pixel rounding must still render its row 0, clipped at
        # the pane edge, not drop it. y may be fractional and is allowed to
        # start within one unit above the pane (a row mid-scroll off the top,
        # e.g. ListView's smooth scroll): its visible part is clipped in. x may
        # be fractional too (a few pixels of sub-unit padding on a pixel backend);
        # the slice math is taken in whole cells so truncation stays grid-safe.
        if not -1 < y < self._rect.h:
            return
        resolved = self._resolve(style)
        if resolved.font is not None:
            # Proportional / sized flow text: a character is not one base unit
            # wide, so slicing by ceil(width) columns would chop trailing glyphs
            # that still fit. Hand the whole run to the backend and let the pane
            # clip rect trim it at the exact pixel edge instead.
            if text:
                self._backend.draw_text(self._rect.x + x, self._rect.y + y, text, resolved)
            return
        if x < 0:
            text = text[int(math.ceil(-x)):]
            x = 0
        text = text[: max(0, math.ceil(self._rect.w - x))]
        if not text:
            return
        self._backend.draw_text(self._rect.x + x, self._rect.y + y, text, resolved)

    def draw_box(
        self,
        x: int,
        y: int,
        w: int,
        h: int,
        style: Style = DEFAULT_STYLE,
        hints: dict[str, Any] | None = None,
    ) -> None:
        self._backend.draw_box(
            self._rect.x + x, self._rect.y + y, w, h, self._resolve(style), hints
        )

    def draw_border(
        self, style: Style = DEFAULT_STYLE, hints: dict[str, Any] | None = None
    ) -> None:
        """Box around the widget's exact extent, framing it. Unlike draw_box
        with width/height (whole base units), this covers fractional edges on
        pixel-layout backends, so adjacent widgets meet without gaps.

        The frame owns its outline, so everything drawn afterwards on this
        context (text, children) is clipped to the interior: the box's stroke
        is one device pixel on pixel-layout backends and one base unit on whole-unit
        backends, and the content clip is inset by exactly that, at pixel
        granularity. Content can fill right up to the inner edge of the frame
        but never paints over the frame line itself."""
        self._backend.draw_box(
            self._rect.x, self._rect.y, self._rect.w, self._rect.h, self._resolve(style), hints
        )
        # Inset the content region by the stroke width: one device pixel on
        # pixel-layout backends, one whole base unit on whole-unit backends.
        if self._caps.supports("pixel_layout"):
            cw, ch = self._backend.base_size
            ix = 1.0 / cw if cw else 0.0
            iy = 1.0 / ch if ch else 0.0
        else:
            ix = iy = 1.0
        interior = Rect(
            self._rect.x + ix, self._rect.y + iy,
            max(0.0, self._rect.w - 2 * ix), max(0.0, self._rect.h - 2 * iy),
        )
        clip = _intersect(self._clip, interior)
        if clip is None:
            clip = Rect(interior.x, interior.y, 0.0, 0.0)
        self._backend.push_clip(clip.x, clip.y, clip.w, clip.h)
        self._clip = clip
        self._pushed_clips += 1

    def _close(self) -> None:
        """Pop any backend clips this context pushed (see draw_border).
        Called by the Panel once the widget's draw returns."""
        for _ in range(self._pushed_clips):
            self._backend.pop_clip()
        self._pushed_clips = 0

    def draw_scrollbar(
        self, x: int, y: int, h: int, pos: float, ratio: float, style: Style = DEFAULT_STYLE
    ) -> None:
        self._backend.draw_scrollbar(
            self._rect.x + x, self._rect.y + y, h, pos, ratio, self._resolve(style)
        )

    def draw_icon(
        self,
        x: int,
        y: int,
        icon_name: str,
        style: Style = DEFAULT_STYLE,
        hints: dict[str, Any] | None = None,
    ) -> None:
        if self._caps.supports("icons"):
            self._backend.draw_icon(
                self._rect.x + x, self._rect.y + y, icon_name, self._resolve(style)
            )
            return
        hints = hints or {}
        fallback = hints.get("fallback_text") or ICON_TEXT_FALLBACKS.get(icon_name, "?")
        self.draw_text(x, y, fallback, style)

    def draw_image(
        self, x: int, y: int, path: str, hints: dict[str, Any] | None = None
    ) -> None:
        if self._caps.supports("images"):
            self._backend.draw_image(self._rect.x + x, self._rect.y + y, path, hints)
            return
        # Fallback for backends without images (TUI): the picture is replaced
        # by a single glyph — the "alt" emoji — centered in the footprint, so
        # the image still reads as a mark on the grid. Without an explicit alt,
        # a neutral "●" stands in. The widget never branches on capability.
        from .text import display_width

        hints = hints or {}
        # The glyph lives on the whole-unit grid, so snap the (possibly
        # fractional) origin and extent to cells. A caller may pass a float
        # x/y (e.g. a centered icon).
        x, y = int(x), int(y)
        w = int(hints.get("w", self.width - x))
        h = int(hints.get("h", self.height - y))
        if w <= 0 or h <= 0:
            return
        glyph = hints.get("alt") or "●"
        gx = x + max(0, (w - display_width(glyph)) // 2)
        gy = y + h // 2
        self.draw_text(gx, gy, glyph)

    def fill_rect(
        self, x: float, y: float, w: float, h: float, style: Style = DEFAULT_STYLE
    ) -> None:
        self._backend.fill_rect(
            self._rect.x + x, self._rect.y + y, w, h, self._resolve(style)
        )

    # --- modern control faces (vector on capable backends, grid otherwise) ----

    def round_rect(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        style: Style = DEFAULT_STYLE,
        radius: float | None = 4.0,
        hints: dict[str, Any] | None = None,
    ) -> None:
        """A rounded rectangle control face (button, field, mark box). On
        ``vector_shapes`` backends it draws real rounded corners; otherwise the
        rounding is dropped and the rect renders as a plain fill (hints
        ``fill``) and/or a box-drawing outline (``style.fg``), so a control
        reads correctly on a character grid too. ``radius`` is in device
        pixels; ``None`` means fully rounded (a circle/pill)."""
        style = self._resolve(style)
        hints = hints or {}
        if self._caps.supports("vector_shapes"):
            self._backend.draw_round_rect(
                self._rect.x + x, self._rect.y + y, w, h, radius, style, hints
            )
            return
        # Grid fallback: rounding is meaningless on whole cells.
        if hints.get("fill") and style.bg is not None:
            self._backend.fill_rect(self._rect.x + x, self._rect.y + y, w, h, Style(bg=style.bg))
        if style.fg is not None:
            iw, ih = round(w), round(h)
            if iw >= 2 and ih >= 2:
                self._backend.draw_box(
                    self._rect.x + x, self._rect.y + y, iw, ih, style, hints
                )

    def draw_check_mark(
        self, x: float, y: float, *, checked: bool, focused: bool, theme: "Theme",
        row_bg: tuple[int, int, int] | None = None,
    ) -> None:
        """Draw a checkbox mark whose first cell sits at (x, y). Vector backends
        get a rounded box — accent-filled with a check when on, bordered when
        off, an accent ring on focus; grid backends fall back to the ``[x]`` /
        ``[ ]`` text mark. The caller reserves the same column slot either way,
        so the label aligns identically on every backend."""
        if not self._caps.supports("vector_shapes"):
            mark = "[x]" if checked else "[ ]"
            if focused:
                style = Style(fg=theme.button_text, bg=theme.accent)
            else:
                style = Style(fg=theme.accent if checked else theme.text, bg=row_bg)
            self.draw_text(int(x), y, mark, style)
            return
        bx, by, w_u, h_u, side = self._mark_box(x, y)
        fill = theme.accent if checked else theme.control_bg
        border = theme.accent if (focused or checked) else theme.control_border
        self.round_rect(
            bx, by, w_u, h_u, Style(bg=fill, fg=border),
            radius=max(2.0, side * 0.28), hints={"fill": True},
        )
        if checked:
            self._draw_check(bx, by, w_u, h_u, Style(fg=theme.button_text))

    def draw_radio_mark(
        self, x: float, y: float, *, selected: bool, focused: bool, theme: "Theme",
        row_bg: tuple[int, int, int] | None = None,
    ) -> None:
        """Draw a radio mark whose first cell sits at (x, y). Vector backends get
        a circle — accent-ringed with a filled accent dot when selected; grid
        backends fall back to the ``(•)`` / ``( )`` text mark."""
        if not self._caps.supports("vector_shapes"):
            mark = "(•)" if selected else "( )"
            if focused and selected:
                style = Style(fg=theme.button_text, bg=theme.accent)
            else:
                style = Style(fg=theme.accent if selected else theme.text, bg=row_bg)
            self.draw_text(int(x), y, mark, style)
            return
        bx, by, w_u, h_u, side = self._mark_box(x, y)
        border = theme.accent if (focused or selected) else theme.control_border
        self.round_rect(
            bx, by, w_u, h_u, Style(bg=theme.control_bg, fg=border),
            radius=None, hints={"fill": True},
        )
        if selected:
            dw, dh = w_u * 0.46, h_u * 0.46
            self.round_rect(
                bx + (w_u - dw) / 2.0, by + (h_u - dh) / 2.0, dw, dh,
                Style(bg=theme.accent), radius=None, hints={"fill": True},
            )

    def _mark_box(self, x: float, y: float) -> tuple[float, float, float, float, float]:
        """Geometry for a checkbox/radio mark box: a pixel-square, vertically
        centered in the row, returned as (x, y, w, h) in base units plus the
        side length in device pixels. Square in pixels even though a base unit
        cell is taller than it is wide."""
        bw, bh = self.base_size
        side = min(bh, bw * 2) * 0.80  # device pixels
        w_u = side / bw if bw else 1.0
        h_u = side / bh if bh else 1.0
        return (x + 0.2, y + (1.0 - h_u) / 2.0, w_u, h_u, side)

    def _draw_check(
        self, x: float, y: float, w: float, h: float, style: Style = DEFAULT_STYLE
    ) -> None:
        if self._caps.supports("vector_shapes"):
            self._backend.draw_check(
                self._rect.x + x, self._rect.y + y, w, h, self._resolve(style)
            )

    def draw_divider(self, divider: "Any") -> None:
        """Render a layout Divider in this context's coordinates, mirroring
        the Panel's top-level divider drawing: a hairline on hairline-capable
        backends, box-drawing characters otherwise. The hairline/base unit choice
        is made here, so the hosting widget never branches on capability."""
        theme = self.theme
        color = theme.divider_color if theme is not None else (110, 110, 124)
        rect = divider.rect
        if self._caps.supports("hairline"):
            self.fill_rect(rect.x, rect.y, rect.w, rect.h, Style(bg=color))
            return
        style = Style(fg=color)
        if divider.vertical:
            for row in range(int(rect.h)):
                self.draw_text(int(rect.x), int(rect.y) + row, "│", style)
        else:
            self.draw_text(int(rect.x), int(rect.y), "─" * int(rect.w), style)

    def draw_child(
        self,
        widget: Any,
        x: float,
        y: float,
        w: float,
        h: float,
        hints: dict[str, Any] | None = None,
    ) -> None:
        """Draw a child widget at (x, y, w, h) in this context's coordinates.

        The child is clipped to the intersection of its rect with all
        enclosing clips, and gets its own animation group, so a parent's
        transition cascades to children while children can also animate
        individually. A "bg" hint (or the theme color of a "surface" role)
        fills the child's pane; otherwise the parent's background is
        inherited."""
        hints = hints or {}
        rect = Rect(self._rect.x + x, self._rect.y + y, w, h)
        if self._panel is not None:
            rect = self._panel._interpolate_rect(widget, rect)
        clip = _intersect(self._clip, rect)
        if clip is None:
            return
        self._backend.begin_group(widget, rect)
        # The clip is set inside the group so GUI transforms carry it along.
        self._backend.push_clip(clip.x, clip.y, clip.w, clip.h)
        pane_bg = hints.get("bg")
        if pane_bg is None and "surface" in hints and self._panel is not None:
            pane_bg = self._panel.theme.surface_bg(hints["surface"])
        background = pane_bg if pane_bg is not None else self._background
        if pane_bg is not None:
            self._backend.fill_rect(rect.x, rect.y, rect.w, rect.h, Style(bg=pane_bg))
        # A child is focused only if this context is focused and the parent
        # marked this child as its focused one (hints["focused"]).
        child_focused = self._focused and bool(hints.get("focused", False))
        child_ctx = DrawContext(
            self._backend, rect, self._caps,
            clip=clip, panel=self._panel, background=background,
            focused=child_focused,
        )
        widget.draw(child_ctx)
        child_ctx._close()
        self._backend.pop_clip()
        self._backend.end_group(widget)


@dataclass
class _Slot:
    widget: Any
    rect: Rect
    hints: dict[str, Any] = field(default_factory=dict)
    z: int = 0
    # Background fill extent. Differs from rect only for layout panes on the
    # window edge: their fill bleeds across the window margin so the frame
    # never shows the backend's default background.
    fill: Rect | None = None


def _bleed_to_window(
    rect: Rect, mx: float, my: float, sw: float, sh: float, snap: bool
) -> Rect:
    """Extend the sides of ``rect`` that lie on the margin bounds out to the
    window edges. Interior boundaries are untouched."""
    eps = 1e-6
    x0 = 0.0 if rect.x <= mx + eps else rect.x
    y0 = 0.0 if rect.y <= my + eps else rect.y
    x1 = sw if rect.x + rect.w >= sw - mx - eps else rect.x + rect.w
    y1 = sh if rect.y + rect.h >= sh - my - eps else rect.y + rect.h
    if snap:
        # Whole-unit backends must keep true integer coordinates.
        x0, y0, x1, y1 = (round(v) for v in (x0, y0, x1, y1))
    return Rect(x0, y0, x1 - x0, y1 - y0)


@dataclass
class _SizeAnimation:
    """Layout-level transition: the widget's rect grows from (from_w, from_h)
    to its assigned size, and the widget re-draws at each intermediate size
    (true size change, unlike the render-level "scale" zoom). A None
    dimension stays at the assigned size."""

    start: float
    duration: float
    from_w: float | None
    from_h: float | None

    def progress(self, now: float) -> float:
        if self.duration <= 0:
            return 1.0
        return min(1.0, max(0.0, (now - self.start) / self.duration))

    def eased(self, now: float) -> float:
        p = self.progress(now)
        return 1.0 - (1.0 - p) ** 2  # ease-out


class Panel:
    """Owns widget layout, layers, focus, and event routing for one screen."""

    def __init__(self, backend: Backend, theme: Theme | None = None):
        self.backend = backend
        # The theme encodes the backend's region-separation strategy: GUI
        # themes separate surfaces with hairlines, TUI themes with
        # background contrast (a line would cost a whole base unit row/column).
        self.theme = theme if theme is not None else theme_for(backend.capabilities)
        self._children: list[_Slot] = []
        self._layers: list[_Slot] = []
        self._dividers: list[Any] = []
        self._focused: Any | None = None
        self._layout: Any | None = None
        self._margin_px = 0.0
        self._margin_units = 0.0
        self._size_anims: dict[Any, _SizeAnimation] = {}
        # The app menu bar model (puikit.menu.Menu), if one was installed.
        self._menu_bar: Any | None = None
        # Last known pointer position in screen base units, fed by every mouse
        # event. DrawContext.hovered reads it to resolve hover styling.
        self._pointer: tuple[float, float] | None = None

    # --- layout management ---------------------------------------------------

    def add(
        self, widget: Any, x: int, y: int, w: int, h: int, hints: dict[str, Any] | None = None
    ) -> None:
        self._children.append(_Slot(widget, Rect(x, y, w, h), hints or {}))
        if self._focused is None and getattr(widget, "focusable", False):
            self._focused = widget

    def remove(self, widget: Any) -> None:
        self._children = [s for s in self._children if s.widget is not widget]
        self._layers = [s for s in self._layers if s.widget is not widget]
        if self._focused is widget:
            self._focused = None

    def clear(self) -> None:
        self._children.clear()
        self._layers.clear()
        self._dividers.clear()
        self._focused = None
        self._layout = None

    def set_layout(
        self, layout: Any, margin_px: float = 0.0, margin_units: float = 0.0
    ) -> None:
        """Use a declarative layout (see puikit.layout) instead of manual
        add() calls. Rects are recomputed from the backend size on every
        render, so the layout follows window resizes.

        margin_px / margin_units inset the layout from the window frame.
        They follow the min_px/min hint rules: the pixel margin
        applies only on pixel-layout backends (it would cost whole base units on
        a base unit grid), margin_units applies everywhere."""
        self._layout = layout
        self._margin_px = float(margin_px)
        self._margin_units = float(margin_units)
        self._apply_layout()

    def _resolve_margin(
        self, base_w: int, base_h: int, snap: bool
    ) -> tuple[float, float]:
        """Window margin in base units per axis, snapped to whole device pixels
        on pixel-layout backends."""
        if snap:
            margin = round(self._margin_units)
            return (margin, margin)
        mx, my = self._margin_units, self._margin_units
        if base_w > 0:
            mx = round(max(mx, self._margin_px / base_w) * base_w) / base_w
        if base_h > 0:
            my = round(max(my, self._margin_px / base_h) * base_h) / base_h
        return (mx, my)

    def _apply_layout(self) -> None:
        from .layout import LayoutContext

        # size_units is exact (fractional on pixel-layout backends), so the
        # layout tracks window resizes pixel by pixel, not base unit by base unit.
        sw, sh = self.backend.size_units
        cw, ch = self.backend.base_size
        snap = not self.backend.capabilities.supports("pixel_layout")
        mx, my = self._resolve_margin(cw, ch, snap)
        ctx = LayoutContext(
            cw, ch, snap,
            hairline=self.backend.capabilities.supports("hairline"),
            native_menus=self.backend.capabilities.supports("native_menus"),
            measure=self.backend.measure_text,
            line_height=self.backend.measure_line_height,
            scrollbar_units=self.backend.scrollbar_units,
            image_size=self.backend.image_size,
        )
        placements = self._layout.resolve(
            mx, my, max(0.0, sw - 2 * mx), max(0.0, sh - 2 * my), ctx
        )
        # Edge panes' backgrounds and the dividers bleed across the window
        # margin: the margin reads as pane padding, never as a bare frame.
        from .layout import Divider

        self._dividers = [
            Divider(_bleed_to_window(d.rect, mx, my, sw, sh, snap), d.vertical, d.level)
            for d in ctx.dividers
        ]
        focused = self._focused
        self._children = [
            _Slot(w, rect, hints, fill=_bleed_to_window(rect, mx, my, sw, sh, snap))
            for w, rect, hints in placements
        ]
        widgets = [slot.widget for slot in self._children]
        if focused not in widgets:
            focused = next(
                (w for w in widgets if getattr(w, "focusable", False)), None
            )
        self._focused = focused

    # --- layer management ------------------------------------------------------

    def push_layer(
        self, widget: Any, z: int = 0, hints: dict[str, Any] | None = None
    ) -> None:
        hints = hints or {}
        rect = self._layer_rect(hints)
        self._layers.append(_Slot(widget, rect, hints, z))
        self._layers.sort(key=lambda s: s.z)

    def pop_layer(self) -> Any | None:
        if not self._layers:
            return None
        return self._layers.pop().widget

    def _layer_rect(self, hints: dict[str, Any]) -> Rect:
        # Center within the backend's exact (fractional) base unit extent, dividing
        # by 2 — not integer // on whole base units: a layer must center at device-
        # pixel precision on GUI, not snap to a whole base unit. Whole-unit backends
        # round at draw time. (Layout-unit base units everywhere; never a pixel.)
        sw, sh = self.backend.size_units
        w = hints.get("w", sw)
        h = hints.get("h", sh)
        x = hints.get("x", (sw - w) / 2)
        y = hints.get("y", (sh - h) / 2)
        return Rect(x, y, w, h)

    # --- focus ----------------------------------------------------------------

    def focus(self, widget: Any) -> None:
        self._focused = widget

    @property
    def focused(self) -> Any | None:
        return self._focused

    # The Panel is the focus-traversal root. It exposes the same duck-typed
    # interface every container does (puikit.focus), so one walk crosses the
    # whole tree; only the root wraps, so focus cycles instead of escaping the
    # window.

    def focus_children(self) -> list[Any]:
        return [s.widget for s in self._children if getattr(s.widget, "focusable", False)]

    def get_focused(self) -> Any | None:
        return self._focused

    def set_focused(self, widget: Any) -> None:
        self._focused = widget

    def _focus_moved(self) -> None:
        """Top-level panes do not scroll, so the root has nothing to do when
        focus moves."""

    def focus_tab(self, direction: int) -> bool:
        """Advance focus to the next (direction > 0) or previous (< 0) focusable
        in the whole tree, wrapping at the ends."""
        return move_focus(self, direction, wrap=True)

    @property
    def pointer(self) -> tuple[float, float] | None:
        """Last pointer position in screen base units, or None."""
        return self._pointer

    # --- rendering --------------------------------------------------------------

    def render(self) -> None:
        if self._layout is not None:
            self._apply_layout()
        self.backend.clear()
        for slot in self._children:
            self._draw_slot(slot)
        for divider in self._dividers:
            self._draw_divider(divider)
        for slot in self._layers:
            self._render_layer(slot)
        self.backend.present()

    def _pane_background(self, hints: dict[str, Any]) -> tuple[int, int, int] | None:
        """Pane background: explicit "bg" hint, else the theme color of the
        "surface" role. Roles let each backend pick its own separation
        strategy (TUI: contrasting backgrounds; GUI: shared background plus
        hairlines)."""
        background = hints.get("bg")
        if background is None and "surface" in hints:
            background = self.theme.surface_bg(hints["surface"])
        return background

    def _draw_divider(self, divider: Any) -> None:
        rect = divider.rect
        if self.backend.capabilities.supports("hairline"):
            self.backend.fill_rect(
                rect.x, rect.y, rect.w, rect.h, Style(bg=self.theme.divider_color)
            )
            return
        # Whole-unit backends only ever get "strong" dividers (one whole
        # base unit); render them with box-drawing characters.
        style = Style(fg=self.theme.divider_color)
        if divider.vertical:
            for row in range(int(rect.h)):
                self.backend.draw_text(rect.x, rect.y + row, "│", style)
        else:
            self.backend.draw_text(rect.x, rect.y, "─" * int(rect.w), style)

    def _draw_slot(self, slot: _Slot) -> None:
        # Group markers let the backend apply per-widget effects (e.g. the
        # alpha or transform of a running transition) to exactly these
        # commands; the rect gives transforms their pivot.
        rect = self._interpolate_rect(slot.widget, slot.rect)
        self.backend.begin_group(slot.widget, rect)
        background = self._pane_background(slot.hints)
        if background is not None:
            # The fill may bleed past the widget's rect (window margin), so
            # it is drawn before the content clip is applied.
            fill = slot.fill if slot.fill is not None else rect
            self.backend.fill_rect(fill.x, fill.y, fill.w, fill.h, Style(bg=background))
        self.backend.push_clip(rect.x, rect.y, rect.w, rect.h)
        slot_ctx = DrawContext(
            self.backend, rect, self.backend.capabilities,
            panel=self, background=background,
            focused=slot.widget is self._focused,
        )
        slot.widget.draw(slot_ctx)
        slot_ctx._close()
        self.backend.pop_clip()
        self.backend.end_group(slot.widget)

    def _render_layer(self, slot: _Slot) -> None:
        if slot.hints.get("dim_below"):
            # Every backend implements dim_rect; TUI approximates with dim
            # attributes, GUI draws a translucent overlay. Dim the exact
            # (fractional) extent so the last partial base unit is covered too.
            sw, sh = self.backend.size_units
            self.backend.dim_rect(0, 0, sw, sh)
        rect = self._interpolate_rect(slot.widget, slot.rect)
        self.backend.begin_group(slot.widget, rect)
        # The shadow blurs beyond the rect, so it is drawn before clipping.
        if slot.hints.get("shadow") and self.backend.capabilities.supports("shadow"):
            self.backend.draw_shadow(rect.x, rect.y, rect.w, rect.h)
        self.backend.push_clip(rect.x, rect.y, rect.w, rect.h)
        background = self._pane_background(slot.hints)
        if background is not None:
            self.backend.fill_rect(rect.x, rect.y, rect.w, rect.h, Style(bg=background))
        layer_ctx = DrawContext(
            self.backend, rect, self.backend.capabilities,
            panel=self, background=background,
            # The top-most layer is the active modal, so it holds the focus.
            focused=bool(self._layers) and slot is self._layers[-1],
        )
        slot.widget.draw(layer_ctx)
        layer_ctx._close()
        self.backend.pop_clip()
        self.backend.end_group(slot.widget)

    # --- animation ----------------------------------------------------------------

    def animate(self, widget: Any, hints: dict[str, Any] | None = None) -> None:
        # Without animation capability the change is applied immediately on
        # the next render; capable backends render a real transition.
        hints = hints or {}
        if not self.backend.capabilities.supports("animation"):
            return
        if hints.get("transition") == "size":
            # Layout-level: the Panel animates the rect itself, the widget
            # re-draws at each intermediate size.
            self._start_size_animation(widget, hints)
        else:
            self.backend.animate(widget, hints)

    def request_animation_ticks(self, callback: Any) -> bool:
        """Register a per-frame tick ``callback`` for a widget that animates
        itself (a busy spinner). Gated on the ``animation`` capability: on a
        still backend nothing is registered — the widget simply renders one
        frame on each ordinary render — so the widget never branches on the
        backend. The callback returns False to unregister. Returns whether
        ticking started."""
        if not self.backend.capabilities.supports("animation"):
            return False
        self.backend.request_animation_ticks(callback)
        return True

    def _start_size_animation(self, widget: Any, hints: dict[str, Any]) -> None:
        # Works for any widget in the tree: the target size is whatever rect
        # the widget is given at draw time (Panel slot or Container child).
        self._size_anims[widget] = _SizeAnimation(
            start=time.monotonic(),
            duration=hints.get("duration_ms", 200) / 1000.0,
            from_w=float(hints["from_w"]) if "from_w" in hints else None,
            from_h=float(hints["from_h"]) if "from_h" in hints else None,
        )
        self.backend.request_animation_ticks(self._animation_tick)

    def _animation_tick(self) -> bool:
        now = time.monotonic()
        finished = [
            widget
            for widget, anim in self._size_anims.items()
            if anim.progress(now) >= 1.0
        ]
        for widget in finished:
            del self._size_anims[widget]
        self.render()  # the final tick renders at the assigned rect
        return bool(self._size_anims)

    def _interpolate_rect(self, widget: Any, rect: Rect) -> Rect:
        """The widget's rect with any running size animation applied."""
        anim = self._size_anims.get(widget)
        if anim is None:
            return rect
        eased = anim.eased(time.monotonic())
        from_w = anim.from_w if anim.from_w is not None else rect.w
        from_h = anim.from_h if anim.from_h is not None else rect.h
        return Rect(
            rect.x,
            rect.y,
            from_w + (rect.w - from_w) * eased,
            from_h + (rect.h - from_h) * eased,
        )

    # --- text input -----------------------------------------------------------------

    def request_text_input(self, x: int, y: int, hints: dict[str, Any] | None = None) -> None:
        request = getattr(self.backend, "request_text_input", None)
        if request is not None:
            request(x, y, hints or {})

    # --- clipboard ------------------------------------------------------------

    def get_clipboard(self) -> str:
        """Plain-text clipboard contents, delegated to the backend (a real OS
        clipboard where available, a process-local buffer otherwise). Widgets
        call this for paste without knowing which backend they run on."""
        return self.backend.get_clipboard()

    def set_clipboard(self, text: str) -> None:
        """Replace the plain-text clipboard contents. See ``get_clipboard``."""
        self.backend.set_clipboard(text)

    # --- drag source ----------------------------------------------------------

    def begin_file_drag(
        self,
        paths: Any,
        event: Event | None = None,
        operations: tuple[str, ...] = ("copy",),
        on_complete: Any | None = None,
    ) -> bool:
        """Export ``paths`` (file paths) as an OS drag, so the user can drop
        them onto another app. One intent, resolved per backend: ``os_drag_drop``
        backends start a native drag session (macOS NSDraggingSource); others —
        notably TUI, where the terminal owns the window and no app can be a drag
        source — fall back to copying the paths to the clipboard so the user can
        paste them into the target. The caller (a file list's drag handler)
        never branches.

        ``operations`` is the offered set (``"copy"`` / ``"move"`` / ``"link"``);
        the destination chooses one. PuiKit never deletes files — for a ``move``
        the app does it, prompted by ``on_complete(op)`` once the session ends
        (``op`` is the chosen operation, or ``"none"`` if cancelled). The
        clipboard fallback is copy semantics, so it reports ``"copy"``.

        Returns True if a real drag session began, False if the clipboard
        fallback was used. ``event`` is the originating MOUSE_DRAG event; a
        native session must start from it."""
        paths = [str(p) for p in paths]
        if self.backend.capabilities.supports("os_drag_drop"):
            return self.backend.begin_file_drag(paths, event, operations, on_complete)
        self.backend.set_clipboard("\n".join(paths))
        if on_complete is not None:
            on_complete("copy")
        return False

    # --- menus ----------------------------------------------------------------

    def set_menu_bar(self, menu: Any | None) -> None:
        """Install an app menu bar from a puikit.menu.Menu (its items each
        carry a submenu). On ``native_menus`` backends this becomes the real
        OS menu bar at the top of the screen; on others it is a no-op here —
        the app places a MenuBar widget that renders the bar in-window. The
        MenuBar widget calls this itself once it knows the backend, so the app
        never branches on the capability."""
        self._menu_bar = menu
        if self.backend.capabilities.supports("native_menus"):
            self.backend.set_menu_bar(menu)

    def popup_menu(
        self, menu: Any, x: float, y: float, on_done: Any | None = None
    ) -> None:
        """Open ``menu`` as a context menu with its top-left near base-unit
        (x, y). Native backends hand it to the OS; others push a widget-rendered
        popup layer. One intent, resolved per backend — the caller (a widget's
        right-click handler) never branches."""
        if self.backend.capabilities.supports("native_menus"):
            self.backend.popup_menu(menu, x, y, on_done)
            return
        from .widgets.menu import MenuPopup, popup_geometry

        vector = self.backend.capabilities.supports("vector_shapes")
        w, h, row_h = popup_geometry(menu, self.backend.measure_text, vector)
        popup = MenuPopup(menu, row_h=row_h)

        def close() -> None:
            # Pop only if our popup is still the top layer (a submenu it opened
            # pops itself first).
            if self._layers and self._layers[-1].widget is popup:
                self.pop_layer()
            if on_done is not None:
                on_done()

        popup.on_close = close
        # Keep the popup on-screen: nudge it left/up if it would overflow.
        sw, sh = self.backend.size_units
        x = max(0.0, min(x, sw - w))
        y = max(0.0, min(y, sh - h))
        self.push_layer(popup, z=60, hints={"x": x, "y": y, "w": w, "h": h})

    # --- event routing ----------------------------------------------------------------

    def dispatch_event(self, event: Event) -> bool:
        """Route an event to widgets. Returns True if it was consumed."""
        # Track the pointer for hover styling on any positioned mouse event.
        if event.x is not None and event.type in (
            EventType.MOUSE_CLICK, EventType.MOUSE_DRAG,
            EventType.MOUSE_MOVE, EventType.MOUSE_SCROLL,
        ):
            self._pointer = (event.x, event.y)
        # Plain hover movement updates the pointer but is not delivered to a
        # widget; the caller re-renders to reflect the new hover state.
        if event.type is EventType.MOUSE_MOVE:
            return False

        # The topmost layer gets events exclusively (modal behavior); it owns
        # its own focus traversal (e.g. a dialog cycles its buttons), so Tab is
        # routed in, not intercepted here.
        if self._layers:
            slot = self._layers[-1]
            return self._deliver(slot, event)

        # Tab / Shift+Tab walk the whole focus tree from the root, crossing
        # container boundaries and wrapping at the ends — one mechanism instead
        # of each container cycling on its own.
        if event.type is EventType.KEY and event.key == "tab":
            return self.focus_tab(-1 if "shift" in event.modifiers else 1)

        if event.type in (EventType.MOUSE_CLICK, EventType.MOUSE_DRAG, EventType.MOUSE_SCROLL):
            for slot in reversed(self._children):
                # Hit-test against the fill extent: a click in the bled
                # window margin belongs to the pane that visually owns it.
                hit = slot.fill if slot.fill is not None else slot.rect
                if event.x is not None and hit.contains(event.x, event.y):
                    if event.type is EventType.MOUSE_CLICK:
                        focus_on_click(self, slot.widget)
                    return self._deliver(slot, event, clamp=True)
            return False

        if self._focused is not None:
            for slot in self._children:
                if slot.widget is self._focused:
                    return self._deliver(slot, event)
        return False

    def _deliver(self, slot: _Slot, event: Event, clamp: bool = False) -> bool:
        local = event.translated(-slot.rect.x, -slot.rect.y)
        if clamp and local.x is not None:
            # Margin clicks land outside the content rect; act on the
            # nearest content base unit instead of handing widgets coordinates
            # they never drew.
            x = min(max(local.x, 0), max(0, math.ceil(slot.rect.w) - 1))
            y = min(max(local.y, 0), max(0, math.ceil(slot.rect.h) - 1))
            if (x, y) != (local.x, local.y):
                local = replace(local, x=x, y=y)
        return bool(slot.widget.handle_event(local))
