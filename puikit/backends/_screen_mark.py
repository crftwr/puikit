"""Geometry and timing shared by the backends' screen marks (``ScreenMarker``).

A mark is a rectangle, some text and nothing else, so what the backends have
in common is exactly that: how the text breaks, how big the box that holds it
is, and how long the arrival flash takes. Each backend keeps its own drawing
and its own colour type; everything here is pure and takes a ``measure``
callable, because measuring text is the one part that is never portable.

The ``spec`` a backend builds and passes back in is a plain dict with the keys
these functions read: ``text``, ``max_width``, ``wrapped_to``, ``lines``,
``measure`` and ``line_height``.
"""

from __future__ import annotations

from ..text import wrap_text

#: Padding inside a text mark, in device pixels, so the text is not flush
#: against the outline.
PADDING = 6.0

#: How long a mark's arrival flash takes, matching the Panel's own default
#: transition (``duration_ms`` 200).
FLASH_SECONDS = 0.2

#: How far toward white the flash starts.
FLASH_LIFT = 0.65


def lines(text: str, measure, limit: float | None) -> list[str]:
    """``text`` broken into drawn lines, wrapped to ``limit`` when there is one.

    A limit is what opts into wrapping: without one the only line breaks are
    the ones the caller wrote."""
    out: list[str] = []
    for paragraph in (text.split("\n") if text else []):
        if limit is not None:
            inner = max(1.0, float(limit) - 2 * PADDING)
            out.extend(wrap_text(paragraph, inner, measure) or [""])
        else:
            out.append(paragraph)
    return out


def rewrap(spec: dict, width: float | None) -> None:
    """Re-flow ``spec``'s text to ``width``, when that is a different width.

    A width is a width whenever it arrives: text wrapped to the one the mark
    was built with has to re-wrap to a new one, or a mark resized narrower
    keeps lines that no longer fit inside it. Unchanged widths re-flow
    nothing, since this is what an animation calls every frame."""
    limit = width if width is not None else spec["max_width"]
    if limit == spec["wrapped_to"]:
        return
    spec["wrapped_to"] = limit
    spec["lines"] = lines(spec["text"], spec["measure"], limit)


def size(spec: dict, w: float | None, h: float | None) -> tuple[float, float]:
    """The mark's size: what was asked for, or what the text needs."""
    if w is not None and h is not None:
        return float(w), float(h)
    text_w = max((spec["measure"](line) for line in spec["lines"]), default=0.0)
    text_h = spec["line_height"] * len(spec["lines"])
    pad = 2 * PADDING
    return (float(w) if w is not None else text_w + pad,
            float(h) if h is not None else text_h + pad)


def lift_rgb(colour: tuple) -> tuple:
    """An RGB(A) colour lifted toward white, for the bright end of a flash."""
    rest = tuple(colour[3:])
    return tuple(c + (255 - c) * FLASH_LIFT for c in colour[:3]) + rest


def mix_rgb(start: tuple, end: tuple | None, t: float) -> tuple:
    """``start`` blended toward ``end`` by ``t`` (0..1), channel by channel."""
    if end is None:
        return start
    t = max(0.0, min(1.0, t))
    rest = tuple(start[3:])
    return tuple(a + (b - a) * t for a, b in zip(start[:3], end[:3])) + rest
