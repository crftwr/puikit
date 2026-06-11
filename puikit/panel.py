"""Panel / Layout / Layer management.

The Panel is the only API widgets talk to. It places widgets in cell
coordinates, resolves backend capabilities, and contains all fallback
chains so widget code never branches on TUI/GUI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .backend import Backend, DEFAULT_STYLE, Style
from .capability import CapabilityProfile
from .event import Event, EventType

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


class DrawContext:
    """Drawing surface handed to a widget, translated to the widget's origin
    and clipped to its rectangle. Capability fallbacks live here."""

    def __init__(self, backend: Backend, rect: Rect, capabilities: CapabilityProfile):
        self._backend = backend
        self._rect = rect
        self._caps = capabilities

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
        if not 0 <= y < int(self._rect.h):
            return
        if x < 0:
            text = text[-x:]
            x = 0
        text = text[: max(0, int(self._rect.w) - x)]
        if not text:
            return
        self._backend.draw_text(self._rect.x + x, self._rect.y + y, text, style)

    def draw_box(
        self,
        x: int,
        y: int,
        w: int,
        h: int,
        style: Style = DEFAULT_STYLE,
        hints: dict[str, Any] | None = None,
    ) -> None:
        self._backend.draw_box(self._rect.x + x, self._rect.y + y, w, h, style, hints)

    def draw_border(
        self, style: Style = DEFAULT_STYLE, hints: dict[str, Any] | None = None
    ) -> None:
        """Box around the widget's exact extent. Unlike draw_box with
        width/height (whole cells), this covers fractional edges on
        pixel-layout backends, so adjacent widgets meet without gaps."""
        self._backend.draw_box(
            self._rect.x, self._rect.y, self._rect.w, self._rect.h, style, hints
        )

    def draw_scrollbar(
        self, x: int, y: int, h: int, pos: float, ratio: float, style: Style = DEFAULT_STYLE
    ) -> None:
        self._backend.draw_scrollbar(self._rect.x + x, self._rect.y + y, h, pos, ratio, style)

    def draw_icon(
        self,
        x: int,
        y: int,
        icon_name: str,
        style: Style = DEFAULT_STYLE,
        hints: dict[str, Any] | None = None,
    ) -> None:
        if self._caps.supports("icons"):
            self._backend.draw_icon(self._rect.x + x, self._rect.y + y, icon_name, style)
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


@dataclass
class _Slot:
    widget: Any
    rect: Rect
    hints: dict[str, Any] = field(default_factory=dict)
    z: int = 0


class Panel:
    """Owns widget layout, layers, focus, and event routing for one screen."""

    def __init__(self, backend: Backend):
        self.backend = backend
        self._children: list[_Slot] = []
        self._layers: list[_Slot] = []
        self._focused: Any | None = None
        self._layout: Any | None = None

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
        self._focused = None
        self._layout = None

    def set_layout(self, layout: Any) -> None:
        """Use a declarative layout (see puikit.layout) instead of manual
        add() calls. Rects are recomputed from the backend size on every
        render, so the layout follows window resizes."""
        self._layout = layout
        self._apply_layout()

    def _apply_layout(self) -> None:
        from .layout import LayoutContext

        sw, sh = self.backend.size
        cw, ch = self.backend.cell_size
        snap = not self.backend.capabilities.supports("pixel_layout")
        placements = self._layout.resolve(
            0.0, 0.0, float(sw), float(sh), LayoutContext(cw, ch, snap)
        )
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
            slot.widget.draw(self._context_for(slot))
        for slot in self._layers:
            self._render_layer(slot)
        self.backend.present()

    def _render_layer(self, slot: _Slot) -> None:
        if slot.hints.get("dim_below"):
            # Every backend implements dim_rect; TUI approximates with dim
            # attributes, GUI draws a translucent overlay.
            sw, sh = self.backend.size
            self.backend.dim_rect(0, 0, sw, sh)
        if slot.hints.get("shadow") and self.backend.capabilities.supports("shadow"):
            rect = slot.rect
            self.backend.draw_shadow(rect.x, rect.y, rect.w, rect.h)
        slot.widget.draw(self._context_for(slot))

    def _context_for(self, slot: _Slot) -> DrawContext:
        return DrawContext(self.backend, slot.rect, self.backend.capabilities)

    # --- animation ----------------------------------------------------------------

    def animate(self, widget: Any, hints: dict[str, Any] | None = None) -> None:
        # Without animation capability the change is applied immediately on
        # the next render; capable backends will get a real transition here.
        if self.backend.capabilities.supports("animation"):
            animate = getattr(self.backend, "animate", None)
            if animate is not None:
                animate(widget, hints or {})

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
