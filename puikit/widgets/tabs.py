"""A tabbed container: a strip of titles over a swappable content pane.

Each tab pairs a title with a content widget; the active tab's content fills
the area below the strip (drawn through ``draw_child``, so it is clipped and
gets its own focus/animation group). The strip highlights the active tab and,
when the Tabs widget holds focus, marks it with the theme accent — an accent
underline on vector backends, accent title text on a character grid. Left/right
switch tabs; other keys and mouse climbs through to the active content.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from ..backend import DEFAULT_STYLE, Style, TextAttribute
from ..event import Event, EventType
from ..focus import FocusContainer
from ..panel import DrawContext
from ..theme import DEFAULT_THEME
from .base import CONTROL_HEIGHT, Widget


def _lighten(color: tuple[int, int, int], amount: float = 0.16) -> tuple[int, int, int]:
    """Nudge a color toward white, for the hover tint of a tab fill — a clearly
    visible delta, not the near-imperceptible row-hover gray."""
    return tuple(round(c + (255 - c) * amount) for c in color)  # type: ignore[return-value]


class Tabs(FocusContainer, Widget):
    focusable = True
    # A tab strip is a focus stop even when the active content has no focusable
    # child: left / right still switch tabs, so it must be reachable.
    focus_stop_when_empty = True

    def __init__(
        self,
        tabs: Sequence[tuple[str, Any]],
        selected: int = 0,
        on_change: Callable[[int, str], None] | None = None,
        style: Style = DEFAULT_STYLE,
    ):
        self.tabs = list(tabs)
        self.selected = selected
        self.on_change = on_change
        self.style = style
        # (x0, x1) base-unit span of each title in the strip, captured at draw
        # for hit-testing.
        self._tab_x: list[tuple[int, int]] = []
        self._strip_h = 1.0

    # --- focus ----------------------------------------------------------------
    #
    # The active tab's content is the single focused child: Tab descends into it
    # (and through it, if it is itself a container) and escapes to the next pane
    # at its ends. The strip stays a focus stop in its own right — when the
    # content has no focusable, traversal lands on the Tabs widget as a leaf, so
    # left/right can still switch tabs.

    def focus_children(self) -> list[Any]:
        if not self.tabs:
            return []
        content = self.tabs[self.selected][1]
        return [content] if getattr(content, "focusable", False) else []

    def get_focused(self) -> Any | None:
        return self.tabs[self.selected][1] if self.tabs else None

    def set_focused(self, widget: Any) -> None:
        # The focused child always tracks the active tab; switching tabs
        # (left/right) moves focus with it, so there is nothing to store.
        pass

    # --- drawing -------------------------------------------------------------

    def draw(self, ctx: DrawContext) -> None:
        theme = ctx.theme or DEFAULT_THEME
        wu, hu = ctx.size_units
        if self.tabs:
            self.selected = max(0, min(self.selected, len(self.tabs) - 1))
        strip_h = CONTROL_HEIGHT if ctx.vector_shapes else 1.0
        self._strip_h = strip_h
        ty = (strip_h - 1.0) / 2.0
        ctx.fill_rect(0, 0, wu, strip_h, Style(bg=theme.popup_bg))

        # Resolve the hovered tab against last frame's positions *before* we
        # rebuild them — _tab_x is emptied below, so hit-testing the list while
        # it is still being filled would never match the current tab.
        hover = ctx.panel.pointer if ctx.panel is not None else None
        hovered_idx = self._hit_strip(ctx, hover) if hover is not None else None

        self._tab_x = []
        x = 0
        for i, (title, _content) in enumerate(self.tabs):
            label = f" {title} "
            w = max(1, int(ctx.measure_text(label)))
            active = i == self.selected
            hovered = i == hovered_idx
            # Fill channel: the active tab wears the loud selection fill (always,
            # so you can see which page is shown regardless of focus); hover
            # lightens whichever tab the pointer is over — the active one too.
            base_bg = theme.selection_active_bg if active else theme.popup_bg
            row_bg = _lighten(base_bg) if hovered else base_bg
            if row_bg != theme.popup_bg:
                ctx.fill_rect(x, 0, w, strip_h, Style(bg=row_bg))
            # Text stays high-contrast on the fill — never recolored into the
            # fill's hue (interaction_states.md §5). On a grid the focused strip
            # reverses its active label, since it has no room for an edge line.
            attr = TextAttribute.BOLD if active else TextAttribute.NORMAL
            if active and ctx.focused and not ctx.vector_shapes:
                attr |= TextAttribute.REVERSE
            ctx.draw_text(x, ty, label, Style(fg=theme.text, bg=row_bg, attr=attr))
            # Selection indicator: an accent line on the strip's OUTER edge — the
            # top, away from the content below — always on for the active tab.
            # Focus thickens it (its own channel; the text never carries focus).
            if active and ctx.vector_shapes:
                ph = (2.0 if ctx.focused else 1.0) / max(1, ctx.base_size[1])
                ctx.fill_rect(x, 0, w, ph, Style(bg=theme.accent))
            self._tab_x.append((x, x + w))
            x += w

        content_h = hu - strip_h
        if self.tabs and content_h > 0:
            content = self.tabs[self.selected][1]
            ctx.draw_child(
                content, 0, strip_h, wu, content_h, hints={"focused": ctx.focused}
            )

    def _hit_strip(self, ctx: DrawContext, point: tuple[float, float]) -> int | None:
        rx, ry, _rw, _rh = ctx.screen_rect
        px, py = point
        if not (ry <= py < ry + self._strip_h):
            return None
        local_x = px - rx
        for i, (x0, x1) in enumerate(self._tab_x):
            if x0 <= local_x < x1:
                return i
        return None

    # --- events --------------------------------------------------------------

    def _select(self, index: int) -> None:
        if not self.tabs:
            return
        index = max(0, min(index, len(self.tabs) - 1))
        if index != self.selected:
            self.selected = index
            if self.on_change is not None:
                self.on_change(index, self.tabs[index][0])

    def handle_event(self, event: Event) -> bool:
        if not self.tabs:
            return False
        if event.type is EventType.KEY:
            if event.key == "left":
                self._select(self.selected - 1)
                return True
            if event.key == "right":
                self._select(self.selected + 1)
                return True
            # Forward anything else to the active content.
            return bool(self.tabs[self.selected][1].handle_event(event))
        if event.type in (EventType.MOUSE_CLICK, EventType.MOUSE_DRAG, EventType.MOUSE_SCROLL):
            if event.y is not None and event.y < self._strip_h and event.type is EventType.MOUSE_CLICK:
                for i, (x0, x1) in enumerate(self._tab_x):
                    if x0 <= (event.x or -1) < x1:
                        self._select(i)
                        return True
                return False
            # Below the strip: forward to the active content in its coordinates.
            local = event.translated(0, -self._strip_h)
            return bool(self.tabs[self.selected][1].handle_event(local))
        return bool(self.tabs[self.selected][1].handle_event(event))
