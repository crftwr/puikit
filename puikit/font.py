"""Font descriptor — the one value used to name a font everywhere.

The same ``Font`` is used two ways:

- as a text widget's font, carried on ``Style.font`` (per-widget, GUI may
  render it proportionally; see docs/font_system.md);
- as a backend's **base font**, passed to the backend constructor. The base
  font is the monospaced grid font: the **base unit** (the layout's length
  unit) is derived from its glyph box (advance x line-height). A monospaced
  font has a canonical advance and line height, so this grounding is
  well-defined — and per-``Style`` *proportional* fonts never affect the base
  unit, only the base font does.

Every field has a "use the backend default" sentinel, so a ``Font`` only
overrides what it names.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntEnum


class FontWeight(IntEnum):
    """CSS 100..900 weight scale."""

    THIN = 100
    EXTRA_LIGHT = 200
    LIGHT = 300
    REGULAR = 400
    MEDIUM = 500
    SEMI_BOLD = 600
    BOLD = 700
    EXTRA_BOLD = 800
    BLACK = 900


class FontSlant(Enum):
    ROMAN = "roman"
    ITALIC = "italic"


@dataclass(frozen=True)
class Font:
    family: str | None = None        # installed family; None = backend default
    size: float | None = None        # points; None = backend base size
    weight: FontWeight = FontWeight.REGULAR
    slant: FontSlant = FontSlant.ROMAN
    monospace: bool = False           # request a fixed-advance face

    @property
    def bold(self) -> bool:
        return self.weight >= FontWeight.SEMI_BOLD

    @property
    def italic(self) -> bool:
        return self.slant is FontSlant.ITALIC


@dataclass(frozen=True)
class FontMetrics:
    """Vertical metrics of a resolved font, in **base units** (like
    ``measure_text`` / ``measure_line_height``, so the whole coordinate system
    stays in one unit). ``ascent`` is the distance from the top of the line box
    down to the baseline; ``descent`` is baseline down to the bottom. Their sum
    is the line height. A widget mixing several fonts on one row sizes the row
    to ``max(ascent) + max(descent)`` across them and places each run's baseline
    at ``row_top + max(ascent)`` (see ``Backend.draw_text_baseline``)."""

    ascent: float
    descent: float

    @property
    def line_height(self) -> float:
        return self.ascent + self.descent
