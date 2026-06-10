"""Declarative layout: weighted splits resolved by the Panel layer.

Apps describe layout intent in cell units (fixed sizes, weights, and hints
like min_px); resolution depends on the backend's capabilities:

- pixel_layout backends keep fractional cell coordinates, which map to exact
  pixel positions (a 1:2 split lands on the real pixel boundary)
- cell-grid backends (TUI) snap every boundary to whole cells

Widgets never see the difference — they just get their DrawContext.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .panel import Rect


@dataclass(frozen=True)
class LayoutContext:
    cell_w: int
    cell_h: int
    snap: bool  # True: round all boundaries to whole cells (TUI)


class Item:
    """One slot in a split: a widget or a nested split, plus sizing intent.

    size    fixed length in cells along the split axis (weight is ignored)
    weight  share of the remaining space (flex)
    hints   "min_cells": minimum length in cells
            "min_px": minimum length in pixels, converted via the backend's
            cell size — only meaningful on pixel-aware backends
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

    def min_size(self, cell_px: int) -> float:
        minimum = float(self.hints.get("min_cells", 0.0))
        if "min_px" in self.hints and cell_px > 0:
            minimum = max(minimum, self.hints["min_px"] / cell_px)
        return minimum


def _normalize(item: Any) -> Item:
    return item if isinstance(item, Item) else Item(item)


class Split:
    """Divides its rectangle among items along one axis. Subclassed as
    HSplit (side by side) and VSplit (stacked)."""

    _axis = "x"

    def __init__(self, *items: Any, gap: float = 0.0):
        self.items = [_normalize(item) for item in items]
        self.gap = gap

    def resolve(
        self, x: float, y: float, w: float, h: float, ctx: LayoutContext
    ) -> list[tuple[Any, Rect, dict[str, Any]]]:
        """Compute placements as (widget, rect, hints) tuples, recursing into
        nested splits."""
        if not self.items:
            return []
        horizontal = self._axis == "x"
        total = w if horizontal else h
        cell_px = ctx.cell_w if horizontal else ctx.cell_h
        sizes = self._sizes(total, cell_px)

        placements: list[tuple[Any, Rect, dict[str, Any]]] = []
        cursor = 0.0
        for item, size in zip(self.items, sizes):
            start, end = cursor, cursor + size
            cursor = end + self.gap
            if ctx.snap:
                start, end = round(start), round(end)
            if horizontal:
                rect = (x + start, y, end - start, h)
            else:
                rect = (x, y + start, w, end - start)
            if isinstance(item.content, Split):
                placements.extend(item.content.resolve(*rect, ctx))
            else:
                placements.append((item.content, Rect(*rect), item.hints))
        return placements

    def _sizes(self, total: float, cell_px: int) -> list[float]:
        gaps = self.gap * (len(self.items) - 1)
        avail = max(0.0, total - gaps)
        mins = [item.min_size(cell_px) for item in self.items]

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
