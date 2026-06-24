"""A Drawer panel that slides in from a screen edge, shown as a Panel layer.

``show_drawer`` pushes a ``Drawer`` as a layer anchored to one of the four
edges — ``left`` / ``right`` / ``top`` / ``bottom``. The drawer hosts an
arbitrary content widget, slides in from the edge it is anchored to, and
(when modal) dims the rest of the screen. It is the *same* ``push_layer``
intent the dialogs use, plus a ``slide`` transition: GUI composites the slide
(a sub-pixel transform) over a dimmed page with a drop shadow; TUI plays the
same slide as a 2-frame whole-cell move (the terminal's "2-frame policy") and
leans on the surface-background contrast for separation — one intent, every
backend.

Escape closes the drawer; when modal, a click on the dimmed area outside it
closes it too. Closing plays the opening slide in reverse — the drawer slides
back off its edge, then the layer pops (GUI composites the slide-out, TUI plays
the 2-frame whole-cell move, a still backend pops at once). Tab / Shift+Tab
cycle the focusable widgets inside the content
(the drawer is the modal focus root, exactly like the Panel is for the page),
so the content is fully usable without the app branching on the backend.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..backend import Style, TextAttribute
from ..event import Event, EventType
from ..focus import FocusContainer, focus_on_click, move_focus
from ..panel import DrawContext, Rect
from ..theme import DEFAULT_THEME
from .base import Widget

_BOLD = Style(attr=TextAttribute.BOLD)

#: The four edges a drawer can anchor to.
SIDES = ("left", "right", "top", "bottom")

#: Default corner radius (device pixels) on the drawer's inner edge.
DEFAULT_RADIUS = 12.0

#: Which corners each side rounds — the inner ones (the edge facing the page);
#: the corners flush to the screen edge stay square. Corner names are
#: screen-oriented: "tl"/"tr"/"br"/"bl".
ROUNDED_CORNERS = {
    "left": ("tr", "br"),
    "right": ("tl", "bl"),
    "top": ("bl", "br"),
    "bottom": ("tl", "tr"),
}


class Drawer(FocusContainer, Widget):
    """Modal layer content for an edge drawer. Construct via ``show_drawer``
    rather than directly; it sizes, anchors, and pushes the layer.

    A drawer hosts one ``content`` widget (commonly a Container / ScrollView /
    ListView holding the real controls). The drawer is the focus root for that
    subtree while it is the top layer, so Tab traversal cycles within it and
    wraps — focus never escapes to the dimmed page underneath."""

    focusable = True
    # The drawer always responds to keys on its own (escape closes it), so it
    # is a focus stop even when its content holds nothing focusable.
    focus_stop_when_empty = True

    def __init__(
        self,
        content: Widget,
        side: str = "left",
        title: str = "",
        on_close: Callable[[], None] | None = None,
        modal: bool = True,
        surface: str = "sidebar",
        radius: float = DEFAULT_RADIUS,
        duration_ms: int = 200,
    ):
        if side not in SIDES:
            raise ValueError(f"side must be one of {SIDES}, got {side!r}")
        self.content = content
        self.side = side
        self.title = title
        self.on_close = on_close
        self.modal = modal
        # Slide duration (ms) reused by the closing animation so it mirrors the
        # opening one. ``_closing`` guards against a second close (e.g. a repeated
        # escape) firing while the slide-out is already playing.
        self.duration_ms = duration_ms
        self._closing = False
        # Surface role resolved to a fill color by the theme, and the inner-edge
        # corner radius (device pixels) for the rounded face on GUI.
        self.surface = surface
        self.radius = radius
        self._panel: Any = None
        # The drawer's own extent and the content sub-rect, captured at draw
        # time so event handling can tell a content click from a scrim click.
        self._size: tuple[float, float] = (0.0, 0.0)
        self._content_rect = Rect(0.0, 0.0, 0.0, 0.0)
        self._focused: Any | None = (
            content if getattr(content, "focusable", False) else None
        )

    # --- focus ---------------------------------------------------------------

    def focus_children(self) -> list[Any]:
        return [self.content] if getattr(self.content, "focusable", False) else []

    # --- drawing -------------------------------------------------------------

    def draw(self, ctx: DrawContext) -> None:
        self._panel = ctx.panel
        self._size = ctx.size_units
        wu, hu = ctx.size_units
        w, h = ctx.width, ctx.height

        # Paint the drawer's own face. On vector backends it is a rounded
        # rectangle whose *inner* corners are rounded, separated from the page
        # by the drop shadow the Panel casts (radius/corners match the rounding).
        # On a grid the round_rect fallback fills the whole rect flat and the
        # drawer draws no edge line — the surface-role background contrast alone
        # separates it from the page (a box-drawing line cannot sit on a colored
        # surface without inter-line gaps; see CLAUDE.md → Rendering).
        theme = ctx.theme or DEFAULT_THEME
        bg = theme.surface_bg(self.surface)
        if bg is not None:
            ctx.round_rect(
                0, 0, wu, hu, Style(bg=bg),
                radius=self.radius,
                hints={"fill": True, "corners": ROUNDED_CORNERS[self.side]},
            )

        # One base unit of padding inside the drawer. A title, when given, takes
        # the first padded row and the content starts below it.
        pad = 1
        cx, cy = pad, pad
        cw, ch = max(0, w - 2 * pad), max(0, h - 2 * pad)
        if self.title:
            ctx.draw_text(pad, 0, self.title[: max(0, w - 2 * pad)], _BOLD)
            cy += 1
            ch = max(0, ch - 1)

        self._content_rect = Rect(cx, cy, cw, ch)
        focused = self._focused is self.content
        ctx.draw_child(self.content, cx, cy, cw, ch, hints={"focused": focused})

    # --- events --------------------------------------------------------------

    def _slide_offset(self) -> tuple[float, float]:
        """The off-edge offset (base units) the drawer slides *to* when closing —
        the mirror of the opening slide, derived from the drawer's current size so
        it stays correct after a window resize."""
        wu, hu = self._size
        if self.side == "left":
            return (-wu, 0.0)
        if self.side == "right":
            return (wu, 0.0)
        if self.side == "top":
            return (0.0, -hu)
        return (0.0, hu)  # bottom

    def close(self) -> None:
        """Slide the drawer back off its edge, then pop the layer and notify
        ``on_close``. On a compositing backend the slide-out is composited; on a
        terminal it plays as the 2-frame whole-cell move; on a still backend the
        layer pops at once — one intent, the Panel resolves it (mirroring the
        opening slide in ``show_drawer``). The drawer is the top layer while open,
        so it pops unconditionally, like ``MessageBox``."""
        if self._closing:
            return
        self._closing = True
        from_dx, from_dy = self._slide_offset()
        started = False
        if self._panel is not None and (from_dx or from_dy):
            started = self._panel.animate(
                self,
                hints={
                    "transition": "slide",
                    "out": True,
                    "duration_ms": self.duration_ms,
                    "from_dx": from_dx,
                    "from_dy": from_dy,
                    "on_complete": self._finish_close,
                },
            )
        if not started:
            self._finish_close()

    def _finish_close(self) -> None:
        """Pop the drawer layer once the slide-out has played and repaint the page
        without it, then notify ``on_close``."""
        if self._panel is not None:
            self._panel.pop_layer()
            self._panel.render()
        if self.on_close is not None:
            self.on_close()

    def handle_event(self, event: Event) -> bool:
        if self._closing:
            return True  # sliding out: swallow input, the layer is leaving
        if event.type is EventType.KEY:
            if event.key == "escape":
                self.close()
                return True
            if event.key == "tab":
                # The drawer is the modal focus root: cycle within the content,
                # wrapping at the ends (focus never falls through to the page).
                direction = -1 if "shift" in event.modifiers else 1
                move_focus(self, direction, wrap=True)
                return True
            if self._focused is not None:
                return bool(self._focused.handle_event(event))
            return True  # modal: swallow keys even with nothing focusable

        if event.type in (
            EventType.MOUSE_DOWN, EventType.MOUSE_UP,
            EventType.MOUSE_CLICK, EventType.MOUSE_DRAG, EventType.MOUSE_SCROLL
        ):
            inside = event.x is not None and self._content_rect.contains(event.x, event.y)
            if inside:
                if event.type is EventType.MOUSE_DOWN:
                    focus_on_click(self, self.content)
                local = event.translated(-self._content_rect.x, -self._content_rect.y)
                self.content.handle_event(local)
                return True
            # A click on the dimmed scrim (outside the drawer rect, delivered to
            # us because we are the top layer) dismisses a modal drawer.
            if (
                self.modal
                and event.type is EventType.MOUSE_CLICK
                and event.x is not None
                and not (0 <= event.x < self._size[0] and 0 <= event.y < self._size[1])
            ):
                self.close()
            return True
        return True  # modal: swallow everything else


def show_drawer(
    panel: Any,
    content: Widget,
    side: str = "left",
    size: float | None = None,
    title: str = "",
    on_close: Callable[[], None] | None = None,
    modal: bool = True,
    dim: bool = True,
    shadow: bool = True,
    surface: str = "sidebar",
    radius: float = DEFAULT_RADIUS,
    z: int = 80,
    duration_ms: int = 200,
) -> Drawer:
    """Push a ``Drawer`` anchored to ``side`` over ``panel`` and return it.

    ``size`` is the drawer's thickness in base units — its width for
    ``left`` / ``right``, its height for ``top`` / ``bottom`` — defaulting to a
    sensible fraction of the window. The drawer fills the whole cross-axis
    (full height for a side drawer, full width for a top/bottom drawer).

    GUI slides it in from the anchored edge over a dimmed page with a drop
    shadow; TUI shows it at once and separates it with the ``surface`` role's
    background. The chosen edge, the dim, and the shadow are all intent — the
    Panel resolves them per backend, so the caller never branches."""
    if side not in SIDES:
        raise ValueError(f"side must be one of {SIDES}, got {side!r}")
    drawer = Drawer(
        content, side=side, title=title, on_close=on_close, modal=modal,
        surface=surface, radius=radius, duration_ms=duration_ms,
    )
    snap = not panel.backend.capabilities.supports("pixel_layout")

    def geometry(sw: float, sh: float) -> tuple[float, float, float, float, float, float]:
        """Anchor rect (x, y, w, h) plus the slide's start offset (from_dx,
        from_dy) for a window of (sw, sh) base units. Recomputed from the live
        window size on every render (see ``reflow`` below) so the drawer keeps
        filling its cross-axis and hugging its edge as the window resizes; a
        default thickness tracks the window the same way."""
        if side in ("left", "right"):
            w = float(size) if size is not None else max(20.0, min(sw * 0.33, 44.0))
            w = min(w, sw)
            h = sh
            x = 0.0 if side == "left" else sw - w
            y = 0.0
            from_dx = -w if side == "left" else w
            from_dy = 0.0
        else:
            h = float(size) if size is not None else max(6.0, min(sh * 0.4, 16.0))
            h = min(h, sh)
            w = sw
            x = 0.0
            y = 0.0 if side == "top" else sh - h
            from_dx = 0.0
            from_dy = -h if side == "top" else h
        return x, y, w, h, from_dx, from_dy

    def reflow(sw: float, sh: float) -> Rect:
        x, y, w, h, _, _ = geometry(sw, sh)
        if snap:
            # Whole-unit backends keep the layer on the base unit grid, exactly
            # like Panel._layer_rect does for a freshly pushed layer.
            x, y, w, h = (round(v) for v in (x, y, w, h))
        return Rect(x, y, w, h)

    sw, sh = panel.backend.size_units
    x, y, w, h, from_dx, from_dy = geometry(sw, sh)

    # The drawer paints its own (rounded) face, so "self_paint" tells the Panel
    # to skip the square background fill while still passing the surface color
    # down for content inheritance. "radius"/"corners" let the drop shadow match
    # the rounded inner edge.
    hints: dict[str, Any] = {
        "x": x, "y": y, "w": w, "h": h,
        "surface": surface, "self_paint": True,
        "radius": radius, "corners": ROUNDED_CORNERS[side],
    }
    if dim:
        hints["dim_below"] = True
    if shadow:
        hints["shadow"] = True

    drawer._panel = panel
    panel.push_layer(drawer, z=z, hints=hints, reflow=reflow)
    # Same slide intent on every backend: GUI composites a sub-pixel transform,
    # TUI plays a 2-frame whole-cell move (the Panel resolves which).
    panel.animate(
        drawer,
        hints={
            "transition": "slide",
            "duration_ms": duration_ms,
            "from_dx": from_dx,
            "from_dy": from_dy,
        },
    )
    return drawer
