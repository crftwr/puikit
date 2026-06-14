"""Declarative layout: a region tree resolved by the Panel layer.

A widget tree never names a coordinate or a pixel. It describes *how regions
divide space* with three kinds of intent, and each backend resolves that
description with its own rules:

- **Unitless intent** — alignment (left/center/right, top/center/bottom),
  weight (a share of leftover space), and the split axis. These carry no
  length at all, so nothing has to ground them in base units, pixels, or fonts.
- **Length-bearing intent** — fixed sizes, minimums, gaps, and dividers.
  Each is stated in the abstract *base unit*, with an optional ``*_px``
  companion that only applies on pixel-layout backends (see below).
- **Intrinsic (measured) intent** — ``size="content"`` / ``min="content"``:
  the widget measures *itself* (a button to its label, a scrollbar to a
  backend-fixed thickness) and reports a length. The layout receives a
  number; it never reads a font or a backend constant directly.

Resolution differs per backend:

- pixel_layout backends keep fractional base-unit coordinates, snapped to the
  device-pixel grid (a 1:2 split lands on a real, whole-pixel boundary);
- whole-unit backends (TUI) snap every boundary to whole base units.

The base unit is the abstract layout unit, not a character: on TUI it grounds in
one terminal character; on GUI it grounds in a backend-configured block of
logical pixels — never in a font metric. Widgets only ever see their resolved
DrawContext; they never learn which resolution happened.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .panel import Rect


@dataclass(frozen=True)
class SizeRequest:
    """A widget's intrinsic size along one axis, in base units (fractional on GUI).

    ``preferred`` is the natural size; ``min``/``max`` bound how far the
    layout may shrink or grow it. A backend-fixed widget (a scrollbar) sets
    ``min == preferred == max`` so it has zero slack and never yields space."""

    min: float = 0.0
    preferred: float = 0.0
    max: float | None = None

    def clamped(self, fallback: float | None = None) -> float:
        """The preferred size clamped into [min, max]."""
        hi = self.max if self.max is not None else fallback
        value = self.preferred
        if hi is not None:
            value = min(value, hi)
        return max(value, self.min)


@dataclass(frozen=True)
class LayoutContext:
    base_w: int
    base_h: int
    snap: bool  # True: round all boundaries to whole base units (TUI)
    hairline: bool = False  # backend can draw sub-unit divider lines
    # How a widget measures itself, supplied by the backend. measure_text
    # returns a width in base units; scrollbar_units is the backend's fixed
    # scrollbar thickness. Both let intrinsic widgets size themselves
    # without the layout ever touching the backend.
    measure: Callable[[str, Any], float] | None = None
    scrollbar_units: float = 1.0
    # Divider rects emitted during resolve, for the Panel to draw.
    dividers: list[Divider] = field(default_factory=list)

    def measure_text(self, text: str, style: Any = None) -> float:
        """Width of ``text`` in base units. Falls back to the column count when the
        backend supplies no measurer (whole-unit backends: one column/char)."""
        if self.measure is not None:
            return self.measure(text, style)
        return float(len(text))


@dataclass(frozen=True)
class Divider:
    """A region boundary the Panel must make visible: a hairline on
    hairline-capable backends, box-drawing characters otherwise."""

    rect: Rect
    vertical: bool  # True: column between side-by-side items
    level: str  # "subtle" | "strong"


def _measure(content: Any, ctx: LayoutContext, axis: str, available: float) -> SizeRequest:
    """Ask a widget for its intrinsic size along ``axis``; widgets without a
    measure() (or nested splits) have no opinion."""
    fn = getattr(content, "measure", None)
    if fn is None:
        return SizeRequest()
    return fn(ctx, axis, available)


def _align_offset(align: str, slack: float) -> float:
    if align in ("center", "middle"):
        return slack / 2.0
    if align in ("end", "right", "bottom"):
        return slack
    return 0.0  # start / left / top


class Item:
    """One slot in a split: a widget or a nested split, plus sizing intent.

    size    main-axis length in base units. A number is a fixed length; the
            string "content" makes the item *intrinsic* — the widget measures
            itself and the layout reserves the measured length. Either way the
            item does not flex.
    size_px main-axis fixed length in pixels, used in place of ``size`` on
            pixel-layout backends (whole-unit backends keep ``size``); the same
            capability rule as ``min_px``.
    weight  share of the remaining space, after fixed and intrinsic items.
    align   cross-axis alignment of a shrink-to-content child within its slot:
            "start"/"center"/"end". Only has an effect when the widget reports
            an intrinsic cross size smaller than the slot (otherwise it fills).
    hints   "min":    minimum length on every backend — a number (base units),
                      or "content" to floor a flex item at its measured size so
                      it never shrinks below what its content needs.
            "min_px": minimum in pixels; pixel-layout backends only.
            other hints (e.g. "surface", "bg") are forwarded to the placement.
    """

    def __init__(
        self,
        content: Any,
        size: float | str | None = None,
        weight: float = 1.0,
        hints: dict[str, Any] | None = None,
        align: str | None = None,
        size_px: float | None = None,
    ):
        self.content = content
        self.size = size
        self.size_px = size_px
        # Fixed and intrinsic items do not flex.
        flexes = size is None and size_px is None
        self.weight = weight if flexes else 0.0
        self.hints = hints or {}
        self.align = align

    @property
    def category(self) -> str:
        if self.size == "content":
            return "content"
        if self.size is not None or self.size_px is not None:
            return "fixed"
        return "flex"

    def fixed_units(self, base_px: int, px_aware: bool) -> float:
        """Resolved fixed length in base units (``size`` or ``size_px``)."""
        if px_aware and self.size_px is not None and base_px > 0:
            return self.size_px / base_px
        return float(self.size) if isinstance(self.size, (int, float)) else 0.0

    def min_units(self, base_px: int, px_aware: bool, req: SizeRequest | None) -> float:
        """Minimum length in base units: a numeric ``min`` hint, a ``min_px``
        floor (pixel backends), and the widget's own measured floor — an
        intrinsic item never shrinks below ``req.min``; a flex item with
        ``min="content"`` never shrinks below its measured size."""
        m = self.hints.get("min")
        minimum = float(m) if isinstance(m, (int, float)) else 0.0
        if px_aware and "min_px" in self.hints and base_px > 0:
            minimum = max(minimum, self.hints["min_px"] / base_px)
        if req is not None:
            if self.category == "content":
                minimum = max(minimum, req.min)
            elif m == "content":
                minimum = max(minimum, req.clamped())
        return minimum


def _normalize(item: Any) -> Item:
    return item if isinstance(item, Item) else Item(item)


class Split:
    """Divides its rectangle among items along one axis. Subclassed as
    HSplit (side by side) and VSplit (stacked).

    divider declares separation intent between adjacent items, never
    geometry — each backend maps it to its own idiom:
      "subtle"  hairline backends: a 1-device-pixel line (zero base unit cost);
                whole-unit backends: nothing is drawn or reserved — adjacent
                panes are told apart by background contrast (surface roles)
      "strong"  hairline backends: same hairline; whole-unit backends spend
                one whole base unit on a box-drawing line, because the app said
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
        cross_full = h if horizontal else w
        cross_axis = "y" if horizontal else "x"
        base_px = ctx.base_w if horizontal else ctx.base_h
        thickness = self._divider_thickness(base_px, ctx)
        spacing = self.gap + thickness
        sizes = self._sizes(total, ctx, spacing=spacing, cross=cross_full)

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
            elif base_px > 0:
                # Pixel granularity means whole device pixels: fractional
                # base units are fine, fractional pixels are not.
                end = round(end * base_px) / base_px
                start = (
                    0.0
                    if prev_end is None
                    else prev_end + round(spacing * base_px) / base_px
                )
            else:
                start = 0.0 if prev_end is None else prev_end + spacing
            end = max(end, start)
            bounds.append((start, end))
            prev_end = end

        placements: list[tuple[Any, Rect, dict[str, Any]]] = []
        for item, (start, end) in zip(self.items, bounds):
            main_size = end - start
            # Cross axis: fill the slot, unless the item asks to shrink to its
            # content and align within the slack (only meaningful when the
            # measured size is smaller than the slot).
            coff, csize = 0.0, cross_full
            if item.align is not None and not isinstance(item.content, Split):
                creq = _measure(item.content, ctx, cross_axis, main_size)
                pref = creq.clamped(cross_full)
                if 0.0 < pref < cross_full:
                    csize = pref
                    coff = _align_offset(item.align, cross_full - pref)
            if horizontal:
                rect = (x + start, y + coff, main_size, csize)
            else:
                rect = (x + coff, y + start, csize, main_size)
            if ctx.snap:
                # Whole-unit backends must see true integers, not whole floats.
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

    def _divider_thickness(self, base_px: int, ctx: LayoutContext) -> float:
        """Space reserved between items for the divider, in base units."""
        if self.divider is None:
            return 0.0
        if ctx.hairline and not ctx.snap and base_px > 0:
            return 1.0 / base_px  # one device pixel: zero base unit cost
        # Whole-unit: "subtle" costs nothing (background contrast separates);
        # "strong" explicitly spends one base unit on a drawn line.
        return 1.0 if self.divider == "strong" else 0.0

    def _sizes(
        self, total: float, ctx: LayoutContext, spacing: float, cross: float
    ) -> list[float]:
        """Main-axis lengths: fixed, then intrinsic (measured), then weighted,
        then an overflow priority ladder. See the module docstring."""
        horizontal = self._axis == "x"
        base_px = ctx.base_w if horizontal else ctx.base_h
        px_aware = not ctx.snap

        gaps = spacing * (len(self.items) - 1)
        avail = max(0.0, total - gaps)

        # 1. Measure the items whose size or floor is content-driven. A widget
        #    reports a length; the layout never inspects why (font or const).
        reqs: list[SizeRequest | None] = []
        for item in self.items:
            if item.category == "content" or item.hints.get("min") == "content":
                reqs.append(_measure(item.content, ctx, self._axis, cross))
            else:
                reqs.append(None)

        mins = [
            item.min_units(base_px, px_aware, req)
            for item, req in zip(self.items, reqs)
        ]
        cats = [item.category for item in self.items]

        # 2. Reserve fixed and intrinsic items first; weight divides the rest.
        reserved = 0.0
        weight_sum = 0.0
        bases: list[float | None] = []
        for item, cat, minimum, req in zip(self.items, cats, mins, reqs):
            if cat == "fixed":
                base = max(item.fixed_units(base_px, px_aware), minimum)
                reserved += base
                bases.append(base)
            elif cat == "content":
                base = max(req.clamped(avail) if req else 0.0, minimum)
                reserved += base
                bases.append(base)
            else:
                weight_sum += item.weight
                bases.append(None)

        # 3. Distribute the remainder among flex items, each lifted to its min.
        flex_space = max(0.0, avail - reserved)
        sizes: list[float] = []
        for item, cat, base, minimum in zip(self.items, cats, bases, mins):
            if base is not None:
                sizes.append(base)
            elif weight_sum > 0:
                sizes.append(max(flex_space * item.weight / weight_sum, minimum))
            else:
                sizes.append(minimum)

        # 4. Overflow ladder: when reserved + flex minimums exceed the space,
        #    space is taken back lowest-priority first — flex surplus, then
        #    intrinsic, never fixed. Items at min==preferred==max (e.g. a
        #    backend-fixed scrollbar) have zero slack and never yield.
        overflow = sum(sizes) - avail
        for tier in ("flex", "content"):
            if overflow <= 1e-9:
                break
            slack = [
                max(0.0, sizes[i] - mins[i]) if cats[i] == tier else 0.0
                for i in range(len(sizes))
            ]
            slack_sum = sum(slack)
            if slack_sum <= 0:
                continue
            factor = min(1.0, overflow / slack_sum)
            for i, s in enumerate(slack):
                shrink = s * factor
                sizes[i] -= shrink
                overflow -= shrink
        return sizes


class HSplit(Split):
    """Items side by side (split along the x axis)."""

    _axis = "x"


class VSplit(Split):
    """Items stacked top to bottom (split along the y axis)."""

    _axis = "y"
