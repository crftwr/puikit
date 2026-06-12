"""Declarative layout: weighted splits resolved by the Panel layer.

Apps describe layout intent in cell units (fixed sizes, weights, and hints
like min_px); resolution depends on the backend's capabilities:

- pixel_layout backends keep fractional cell coordinates, snapped to the
  device-pixel grid (a 1:2 split lands on a real, whole-pixel boundary)
- cell-grid backends (TUI) snap every boundary to whole cells

Widgets never see the difference — they just get their DrawContext.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .panel import Rect


@dataclass(frozen=True)
class LayoutContext:
    cell_w: int
    cell_h: int
    snap: bool  # True: round all boundaries to whole cells (TUI)
    hairline: bool = False  # backend can draw sub-cell divider lines
    # Divider rects emitted during resolve, for the Panel to draw.
    dividers: list[Divider] = field(default_factory=list)


@dataclass(frozen=True)
class Divider:
    """A region boundary the Panel must make visible: a hairline on
    hairline-capable backends, box-drawing line cells otherwise."""

    rect: Rect
    vertical: bool  # True: column between side-by-side items
    level: str  # "subtle" | "strong"


class Item:
    """One slot in a split: a widget or a nested split, plus sizing intent.

    size    fixed length in cells along the split axis (weight is ignored)
    weight  share of the remaining space (flex)
    hints   "min_cells": minimum length in cells
            "min_px": minimum length in pixels, converted via the backend's
            cell size; applies only on pixel-layout backends — cell-grid
            backends (TUI) ignore it and use min_cells
    """

    def __init__(
        self,
        content: Any,
        size: float | None = None,
        weight: float = 1.0,
        hints: dict[str, Any] | None = None,
    ):
        self.content = content
        self.size = size
        self.weight = weight if size is None else 0.0
        self.hints = hints or {}

    def min_size(self, cell_px: int, px_aware: bool) -> float:
        minimum = float(self.hints.get("min_cells", 0.0))
        if px_aware and "min_px" in self.hints and cell_px > 0:
            minimum = max(minimum, self.hints["min_px"] / cell_px)
        return minimum


def _normalize(item: Any) -> Item:
    return item if isinstance(item, Item) else Item(item)


class Split:
    """Divides its rectangle among items along one axis. Subclassed as
    HSplit (side by side) and VSplit (stacked).

    divider declares separation intent between adjacent items, never
    geometry — each backend maps it to its own idiom:
      "subtle"  hairline backends: a 1-device-pixel line (zero cell cost);
                cell-grid backends: nothing is drawn or reserved — adjacent
                panes are told apart by background contrast (surface roles)
      "strong"  hairline backends: same hairline; cell-grid backends spend
                one whole cell on a box-drawing line, because the app said
                the separation is worth the space
    """

    _axis = "x"

    def __init__(self, *items: Any, gap: float = 0.0, divider: str | None = None):
        self.items = [_normalize(item) for item in items]
        self.gap = gap
        self.divider = divider

    def resolve(
        self, x: float, y: float, w: float, h: float, ctx: LayoutContext
    ) -> list[tuple[Any, Rect, dict[str, Any]]]:
        """Compute placements as (widget, rect, hints) tuples, recursing into
        nested splits. Divider rects are accumulated on ctx.dividers."""
        if not self.items:
            return []
        horizontal = self._axis == "x"
        total = w if horizontal else h
        cell_px = ctx.cell_w if horizontal else ctx.cell_h
        thickness = self._divider_thickness(cell_px, ctx)
        spacing = self.gap + thickness
        sizes = self._sizes(total, cell_px, px_aware=not ctx.snap, spacing=spacing)

        # Each item's start is anchored to the previous item's rounded end
        # plus the rounded spacing (not re-rounded from the accumulated
        # position): rounding ties must never swallow a 1px divider. Ends
        # keep accumulating unrounded so rounding error does not drift.
        bounds: list[tuple[float, float]] = []
        cursor = 0.0
        prev_end: float | None = None
        for size in sizes:
            end = cursor + size
            cursor = end + spacing
            if ctx.snap:
                end = round(end)
                start = 0 if prev_end is None else prev_end + round(spacing)
            elif cell_px > 0:
                # Pixel granularity means whole device pixels: fractional
                # cells are fine, fractional pixels are not.
                end = round(end * cell_px) / cell_px
                start = (
                    0.0
                    if prev_end is None
                    else prev_end + round(spacing * cell_px) / cell_px
                )
            else:
                start = 0.0 if prev_end is None else prev_end + spacing
            end = max(end, start)
            bounds.append((start, end))
            prev_end = end

        placements: list[tuple[Any, Rect, dict[str, Any]]] = []
        for item, (start, end) in zip(self.items, bounds):
            if horizontal:
                rect = (x + start, y, end - start, h)
            else:
                rect = (x, y + start, w, end - start)
            if ctx.snap:
                # Cell-grid backends must see true integers, not whole floats.
                rect = tuple(round(v) for v in rect)
            if isinstance(item.content, Split):
                placements.extend(item.content.resolve(*rect, ctx))
            else:
                placements.append((item.content, Rect(*rect), item.hints))

        if self.divider is not None and thickness > 0:
            for (_, end), _next in zip(bounds, bounds[1:]):
                if horizontal:
                    rect = Rect(x + end, y, thickness, h)
                else:
                    rect = Rect(x, y + end, w, thickness)
                if ctx.snap:
                    rect = Rect(*(round(v) for v in (rect.x, rect.y, rect.w, rect.h)))
                ctx.dividers.append(Divider(rect, vertical=horizontal, level=self.divider))
        return placements

    def _divider_thickness(self, cell_px: int, ctx: LayoutContext) -> float:
        """Space reserved between items for the divider, in cells."""
        if self.divider is None:
            return 0.0
        if ctx.hairline and not ctx.snap and cell_px > 0:
            return 1.0 / cell_px  # one device pixel: zero cell cost
        # Cell-grid: "subtle" costs nothing (background contrast separates);
        # "strong" explicitly spends one cell on a drawn line.
        return 1.0 if self.divider == "strong" else 0.0

    def _sizes(
        self, total: float, cell_px: int, px_aware: bool, spacing: float | None = None
    ) -> list[float]:
        if spacing is None:
            spacing = self.gap
        gaps = spacing * (len(self.items) - 1)
        avail = max(0.0, total - gaps)
        mins = [item.min_size(cell_px, px_aware) for item in self.items]

        fixed_sum = sum(
            max(item.size, minimum)
            for item, minimum in zip(self.items, mins)
            if item.size is not None
        )
        weight_sum = sum(item.weight for item in self.items if item.size is None)
        flex_space = max(0.0, avail - fixed_sum)

        sizes = []
        for item, minimum in zip(self.items, mins):
            if item.size is not None:
                size = max(item.size, minimum)
            elif weight_sum > 0:
                size = max(flex_space * item.weight / weight_sum, minimum)
            else:
                size = minimum
            sizes.append(size)

        # Minimums can overflow the available space; shrink items that still
        # have slack above their minimum, proportionally (single pass).
        overflow = sum(sizes) - avail
        if overflow > 0:
            slack = [size - minimum for size, minimum in zip(sizes, mins)]
            slack_sum = sum(slack)
            if slack_sum > 0:
                factor = min(1.0, overflow / slack_sum)
                sizes = [size - s * factor for size, s in zip(sizes, slack)]
        return sizes


class HSplit(Split):
    """Items side by side (split along the x axis)."""

    _axis = "x"


class VSplit(Split):
    """Items stacked top to bottom (split along the y axis)."""

    _axis = "y"
