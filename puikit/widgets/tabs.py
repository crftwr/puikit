"""A tabbed container: a strip of titles over a swappable content pane.

Each tab pairs a title with a content widget; the active tab's content fills
the area below the strip (drawn through ``draw_child``, so it is clipped and
gets its own focus/animation group). The active tab is always marked (a bold
title plus, on a vector strip, the accent indicator line), and that mark follows
the focus ordering (interaction_states.md §4b/§5): a vector strip thickens its
accent line while focused; a character grid — which has no room for the line —
fills the active tab with the loud accent selection fill while focused and the
muted inactive fill when focus is elsewhere, so an unfocused strip never shows
the loud accent. Left/right switch tabs; other keys and mouse climbs through to
the active content.
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

# Tab labels are measured bold (the active label is bold) so widths stay stable
# and never under-reserve as the selection moves.
_MEASURE_BOLD = Style(attr=TextAttribute.BOLD)

# Horizontal inset on each side of a tab label on a vector strip, in base units,
# so proportional labels are not cramped against the tab borders. The grid pads
# with literal spaces in the label instead (one column each side), since a glyph
# cell is its own unit there.
_TAB_PAD_VECTOR = 1.25


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
        # A pointing hand over a tab title, so the strip reads as clickable.
        if hovered_idx is not None:
            ctx.set_cursor("pointer")

        # Every tab is a bounded region (CLAUDE.md §2 — region separation is
        # intent, not geometry). A vector strip frames each tab on all four
        # sides with device-pixel hairlines (zero base-unit cost); a character
        # grid cannot afford top/bottom rows in a one-row strip, so it fences
        # each tab with a box-drawing column on the left and right. Adjacent
        # tabs share their inner border; the run is fenced at each end.
        border_w = 0 if ctx.vector_shapes else 1
        self._tab_x = []
        x = 0
        if not ctx.vector_shapes:
            ctx.draw_text(x, ty, "│", Style(fg=theme.popup_border, bg=theme.popup_bg))
        x += border_w
        for i, (title, _content) in enumerate(self.tabs):
            # Vector: bare label inset by a base-unit pad on each side. Grid: the
            # pad is literal spaces in the label (a cell is its own unit), so the
            # whole label — padding included — shares the active tab's highlight.
            pad = _TAB_PAD_VECTOR if ctx.vector_shapes else 0.0
            label = title if ctx.vector_shapes else f" {title} "
            # Reserve the *bold* measured width (the active tab's label is bold) so
            # widths stay stable as the selection moves and never under-reserve.
            # On a grid measure_text returns the column count, so the width and the
            # text origin stay whole-column.
            w = max(1.0, ctx.measure_text(label, _MEASURE_BOLD) + 2 * pad)
            active = i == self.selected
            hovered = i == hovered_idx
            # Fill channel: on a vector strip the active tab is marked solely by
            # its top accent line, so it keeps the plain strip background — no
            # loud fill duplicating the cue. A character grid has no room for an
            # edge line, so there the active tab wears the selection fill — and
            # that fill follows the §4b focus ordering: the loud accent fill only
            # while the Tabs widget holds focus, the muted inactive fill when
            # focus is elsewhere (the same active/inactive pair ListView rows use),
            # so an unfocused strip never shows the loud blue. Hover lightens
            # whichever tab the pointer is over, the active one included.
            if active and not ctx.vector_shapes:
                base_bg = theme.selection_active_bg if ctx.focused else theme.selection_inactive_bg
            else:
                base_bg = theme.popup_bg
            row_bg = _lighten(base_bg) if hovered else base_bg
            if row_bg != theme.popup_bg:
                ctx.fill_rect(x, 0, w, strip_h, Style(bg=row_bg))
            # Text stays high-contrast on the fill — never recolored into the
            # fill's hue (interaction_states.md §5). The active label is bold (an
            # always-visible selection hint); focus is carried by the fill
            # ordering above, matching ListView, so the label is not also reversed.
            attr = TextAttribute.BOLD if active else TextAttribute.NORMAL
            ctx.draw_text(x + pad, ty, label, Style(fg=theme.text, bg=row_bg, attr=attr))
            self._tab_x.append((x, x + w))
            x += w
            # Grid: a box-drawing column fences this tab from the next (and the
            # run's end). It rides over no fill, so drawing it inline is safe.
            if not ctx.vector_shapes:
                ctx.draw_text(x, ty, "│", Style(fg=theme.popup_border, bg=theme.popup_bg))
            x += border_w

        # Vector: stroke the box frame *after* the fills, so the loud active /
        # hover fill never paints over the border lines (which would leave the
        # gaps an inline draw produced). Then re-lay the active tab's accent over
        # the frame's top edge, since the accent is its own channel.
        if ctx.vector_shapes and self._tab_x:
            self._draw_vector_frame(ctx, strip_h, theme)

        content_h = hu - strip_h
        if self.tabs and content_h > 0:
            content = self.tabs[self.selected][1]
            ctx.draw_child(
                content, 0, strip_h, wu, content_h, hints={"focused": ctx.focused}
            )

    def _draw_vector_frame(self, ctx: DrawContext, strip_h: float, theme: Any) -> None:
        """Frame the tab run on a vector strip: a hairline top edge and tab
        boundaries, a bottom edge that divides the whole strip from the content
        below — broken open only under the active tab so it merges into the
        content — and the active tab's accent re-laid on top."""
        wu, _hu = ctx.size_units
        pw = 1.0 / max(1, ctx.base_size[0])
        ph = 1.0 / max(1, ctx.base_size[1])
        left = self._tab_x[0][0]
        right = self._tab_x[-1][1]
        ax0, ax1 = self._tab_x[self.selected]
        border = Style(bg=theme.popup_border)
        by = strip_h - ph
        # Top edge spans the tab run; the bottom edge divides the full strip
        # width from the content pane, drawn in two segments that stop at the
        # active tab's sides so its base stays open (the classic selected-tab
        # look).
        ctx.fill_rect(left, 0, right - left, ph, border)
        if ax0 > 0:
            ctx.fill_rect(0, by, ax0, ph, border)
        if wu > ax1:
            ctx.fill_rect(ax1, by, wu - ax1, ph, border)
        # Vertical edges: each tab's left plus the final tab's right.
        for x0, _x1 in self._tab_x:
            ctx.fill_rect(x0, 0, pw, strip_h, border)
        ctx.fill_rect(right - pw, 0, pw, strip_h, border)
        # Active accent on top of the frame (thickened while focused).
        aph = (2.0 if ctx.focused else 1.0) * ph
        ctx.fill_rect(ax0, 0, ax1 - ax0, aph, Style(bg=theme.accent))

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
