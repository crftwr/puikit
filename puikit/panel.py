"""Panel / Layout / Layer management.

The Panel is the only API widgets talk to. It places widgets in cell
coordinates, resolves backend capabilities, and contains all fallback
chains so widget code never branches on TUI/GUI.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any

from .backend import Backend, DEFAULT_STYLE, Style
from .capability import CapabilityProfile
from .event import Event, EventType
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
    # Cell coordinates. Fractional values are produced by the layout system
    # on pixel_layout-capable backends; cell-grid backends only see integers.
    x: float
    y: float
    w: float
    h: float

    def contains(self, x: float, y: float) -> bool:
        return self.x <= x < self.x + self.w and self.y <= y < self.y + self.h


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
    ):
        self._backend = backend
        self._rect = rect
        self._caps = capabilities
        self._clip = clip if clip is not None else rect
        self._panel = panel
        self._background = background

    def _resolve(self, style: Style) -> Style:
        """Styles without an explicit background inherit the pane's."""
        if self._background is not None and style.bg is None:
            return Style(style.fg, self._background, style.attr)
        return style

    @property
    def width(self) -> int:
        return int(self._rect.w)

    @property
    def height(self) -> int:
        return int(self._rect.h)

    @property
    def size_cells(self) -> tuple[float, float]:
        """Exact extent in cells; fractional on pixel-aware backends."""
        return (self._rect.w, self._rect.h)

    @property
    def cell_size(self) -> tuple[int, int]:
        """Pixel size of one cell, as declared by the backend."""
        return self._backend.cell_size

    def draw_text(self, x: int, y: int, text: str, style: Style = DEFAULT_STYLE) -> None:
        # Gate on the exact (possibly fractional) extent and let the
        # backend's clip rect cut the overflow: a pane squeezed to 0.97
        # cells by pixel rounding must still render its row 0, clipped at
        # the pane edge, not drop it.
        if not 0 <= y < self._rect.h:
            return
        if x < 0:
            text = text[-x:]
            x = 0
        text = text[: max(0, math.ceil(self._rect.w) - x)]
        if not text:
            return
        self._backend.draw_text(self._rect.x + x, self._rect.y + y, text, self._resolve(style))

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
        """Box around the widget's exact extent. Unlike draw_box with
        width/height (whole cells), this covers fractional edges on
        pixel-layout backends, so adjacent widgets meet without gaps."""
        self._backend.draw_box(
            self._rect.x, self._rect.y, self._rect.w, self._rect.h, self._resolve(style), hints
        )

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
        # TUI fallback: no-op

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
        widget.draw(
            DrawContext(
                self._backend, rect, self._caps,
                clip=clip, panel=self._panel, background=background,
            )
        )
        self._backend.pop_clip()
        self._backend.end_group(widget)


@dataclass
class _Slot:
    widget: Any
    rect: Rect
    hints: dict[str, Any] = field(default_factory=dict)
    z: int = 0


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
        # background contrast (a line would cost a whole cell row/column).
        self.theme = theme if theme is not None else theme_for(backend.capabilities)
        self._children: list[_Slot] = []
        self._layers: list[_Slot] = []
        self._dividers: list[Any] = []
        self._focused: Any | None = None
        self._layout: Any | None = None
        self._margin_px = 0.0
        self._margin_cells = 0.0
        self._size_anims: dict[Any, _SizeAnimation] = {}

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
        self, layout: Any, margin_px: float = 0.0, margin_cells: float = 0.0
    ) -> None:
        """Use a declarative layout (see puikit.layout) instead of manual
        add() calls. Rects are recomputed from the backend size on every
        render, so the layout follows window resizes.

        margin_px / margin_cells inset the layout from the window frame.
        They follow the min_px/min_cells hint rules: the pixel margin
        applies only on pixel-layout backends (it would cost whole cells on
        a cell grid), margin_cells applies everywhere."""
        self._layout = layout
        self._margin_px = float(margin_px)
        self._margin_cells = float(margin_cells)
        self._apply_layout()

    def _resolve_margin(
        self, cell_w: int, cell_h: int, snap: bool
    ) -> tuple[float, float]:
        """Window margin in cells per axis, snapped to whole device pixels
        on pixel-layout backends."""
        if snap:
            margin = round(self._margin_cells)
            return (margin, margin)
        mx, my = self._margin_cells, self._margin_cells
        if cell_w > 0:
            mx = round(max(mx, self._margin_px / cell_w) * cell_w) / cell_w
        if cell_h > 0:
            my = round(max(my, self._margin_px / cell_h) * cell_h) / cell_h
        return (mx, my)

    def _apply_layout(self) -> None:
        from .layout import LayoutContext

        # size_cells is exact (fractional on pixel-layout backends), so the
        # layout tracks window resizes pixel by pixel, not cell by cell.
        sw, sh = self.backend.size_cells
        cw, ch = self.backend.cell_size
        snap = not self.backend.capabilities.supports("pixel_layout")
        mx, my = self._resolve_margin(cw, ch, snap)
        ctx = LayoutContext(
            cw, ch, snap, hairline=self.backend.capabilities.supports("hairline")
        )
        placements = self._layout.resolve(
            mx, my, max(0.0, sw - 2 * mx), max(0.0, sh - 2 * my), ctx
        )
        self._dividers = ctx.dividers
        focused = self._focused
        self._children = [_Slot(w, rect, hints) for w, rect, hints in placements]
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
        sw, sh = self.backend.size
        w = hints.get("w", sw)
        h = hints.get("h", sh)
        x = hints.get("x", (sw - w) // 2)
        y = hints.get("y", (sh - h) // 2)
        return Rect(x, y, w, h)

    # --- focus ----------------------------------------------------------------

    def focus(self, widget: Any) -> None:
        self._focused = widget

    @property
    def focused(self) -> Any | None:
        return self._focused

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
        # Cell-grid backends only ever get "strong" dividers (one whole
        # cell); render them with box-drawing characters.
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
        self.backend.push_clip(rect.x, rect.y, rect.w, rect.h)
        background = self._pane_background(slot.hints)
        if background is not None:
            self.backend.fill_rect(rect.x, rect.y, rect.w, rect.h, Style(bg=background))
        slot.widget.draw(
            DrawContext(
                self.backend, rect, self.backend.capabilities,
                panel=self, background=background,
            )
        )
        self.backend.pop_clip()
        self.backend.end_group(slot.widget)

    def _render_layer(self, slot: _Slot) -> None:
        if slot.hints.get("dim_below"):
            # Every backend implements dim_rect; TUI approximates with dim
            # attributes, GUI draws a translucent overlay.
            sw, sh = self.backend.size
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
        slot.widget.draw(
            DrawContext(
                self.backend, rect, self.backend.capabilities,
                panel=self, background=background,
            )
        )
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

    # --- event routing ----------------------------------------------------------------

    def dispatch_event(self, event: Event) -> bool:
        """Route an event to widgets. Returns True if it was consumed."""
        # The topmost layer gets events exclusively (modal behavior).
        if self._layers:
            slot = self._layers[-1]
            return self._deliver(slot, event)

        if event.type in (EventType.MOUSE_CLICK, EventType.MOUSE_DRAG, EventType.MOUSE_SCROLL):
            for slot in reversed(self._children):
                if event.x is not None and slot.rect.contains(event.x, event.y):
                    if event.type is EventType.MOUSE_CLICK and getattr(
                        slot.widget, "focusable", False
                    ):
                        self._focused = slot.widget
                    return self._deliver(slot, event)
            return False

        if self._focused is not None:
            for slot in self._children:
                if slot.widget is self._focused:
                    return self._deliver(slot, event)
        return False

    def _deliver(self, slot: _Slot, event: Event) -> bool:
        local = event.translated(-slot.rect.x, -slot.rect.y)
        return bool(slot.widget.handle_event(local))
