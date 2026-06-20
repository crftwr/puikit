"""A Drawer panel that slides in from a screen edge, shown as a Panel layer.

``show_drawer`` pushes a ``Drawer`` as a layer anchored to one of the four
edges — ``left`` / ``right`` / ``top`` / ``bottom``. The drawer hosts an
arbitrary content widget, slides in from the edge it is anchored to, and
(when modal) dims the rest of the screen. It is the *same* ``push_layer``
intent the dialogs use, plus a ``slide`` transition: GUI animates the slide
over a dimmed page with a drop shadow, TUI shows it immediately and leans on
the surface-background contrast for separation — one intent, every backend.

Escape closes the drawer; when modal, a click on the dimmed area outside it
closes it too. Tab / Shift+Tab cycle the focusable widgets inside the content
(the drawer is the modal focus root, exactly like the Panel is for the page),
so the content is fully usable without the app branching on the backend.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..backend import Style, TextAttribute
from ..event import Event, EventType
from ..focus import FocusContainer, focus_on_click, move_focus
from ..layout import Divider
from ..panel import DrawContext, Rect
from .base import Widget

_BOLD = Style(attr=TextAttribute.BOLD)

#: The four edges a drawer can anchor to.
SIDES = ("left", "right", "top", "bottom")


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
    ):
        if side not in SIDES:
            raise ValueError(f"side must be one of {SIDES}, got {side!r}")
        self.content = content
        self.side = side
        self.title = title
        self.on_close = on_close
        self.modal = modal
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

    def _inner_divider(self, ctx: DrawContext) -> Divider:
        """The boundary line on the drawer's *inner* edge (the one facing the
        page): a vertical column for left/right drawers, a horizontal row for
        top/bottom. Kept one device pixel thin on hairline backends and one
        box-drawing line on whole-unit backends — ``ctx.draw_divider`` picks
        which, so the drawer never branches on the capability."""
        w, h = ctx.size_units
        bw, bh = ctx.base_size
        tx = 1.0 / bw if bw else 1.0
        ty = 1.0 / bh if bh else 1.0
        if self.side == "left":
            return Divider(Rect(w - tx, 0.0, tx, h), vertical=True, level="subtle")
        if self.side == "right":
            return Divider(Rect(0.0, 0.0, tx, h), vertical=True, level="subtle")
        if self.side == "top":
            return Divider(Rect(0.0, h - ty, w, ty), vertical=False, level="subtle")
        return Divider(Rect(0.0, 0.0, w, ty), vertical=False, level="subtle")

    def draw(self, ctx: DrawContext) -> None:
        self._panel = ctx.panel
        self._size = ctx.size_units
        w, h = ctx.width, ctx.height
        ctx.draw_divider(self._inner_divider(ctx))

        # One base unit of padding inside the drawer, with the divider edge kept
        # clear (its line is already drawn at the very edge). A title, when
        # given, takes the first padded row and the content starts below it.
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

    def close(self) -> None:
        """Pop the drawer layer and notify ``on_close``. Pops unconditionally
        (the drawer is the top layer while open), mirroring ``MessageBox``."""
        if self._panel is not None:
            self._panel.pop_layer()
        if self.on_close is not None:
            self.on_close()

    def handle_event(self, event: Event) -> bool:
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
            EventType.MOUSE_CLICK, EventType.MOUSE_DRAG, EventType.MOUSE_SCROLL
        ):
            inside = event.x is not None and self._content_rect.contains(event.x, event.y)
            if inside:
                if event.type is EventType.MOUSE_CLICK:
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
    drawer = Drawer(content, side=side, title=title, on_close=on_close, modal=modal)
    sw, sh = panel.backend.size_units

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

    hints: dict[str, Any] = {"x": x, "y": y, "w": w, "h": h, "surface": surface}
    if dim:
        hints["dim_below"] = True
    if shadow:
        hints["shadow"] = True

    drawer._panel = panel
    panel.push_layer(drawer, z=z, hints=hints)
    # GUI slides it in from the edge; TUI shows it immediately (no animation cap).
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
