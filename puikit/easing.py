"""Easing curves — pure ``progress -> progress`` functions.

An easing reshapes *linear* time into the curve a motion actually plays on. It
takes a normalized progress ``0..1`` and returns a normalized progress ``0..1``,
with ``f(0) == 0`` and ``f(1) == 1`` fixed so a transition always starts at its
"from" state and lands exactly on its target. Nothing here knows about widgets,
backends, or pixels — that is the point: the same curve shapes a composited GPU
zoom on macOS, a Direct2D one on Windows, and a Panel-driven rect interpolation
on a terminal.

An animation names one by string (``hints={"easing": "ease_out_expo"}``); the
Panel and the GUI backends both resolve it through :func:`resolve`, so a curve
added here is immediately available to every backend and every transition kind.

Which curve to reach for:

* :func:`linear` — constant velocity. Right for a *continuous* motion with no
  start or end (a marquee, a scrolling ticker), and for a color tween where an
  eased midpoint reads as a wrong color rather than as timing.
* :func:`ease_out_quad` — a gentle deceleration; PuiKit's historical default and
  still the default for every transition that does not name one.
* :func:`ease_out_expo` — a *hard* deceleration: most of the distance is covered
  in the first fifth of the duration, then it glides in. This is the curve that
  reads as "the UI snapped to attention and settled", and it is what a
  holographic/tactical-HUD look (TFM's Sci-Fi theme) is built on.
* :func:`ease_in_out_quad` — symmetric; for a motion that both starts and ends at
  rest, e.g. something moving between two resting positions.
* :func:`ease_out_back` — overshoots past the target and settles back. Use
  sparingly and only on *scale*: on a position slide it reads as a mistake.

Easing and the 2-frame policy
-----------------------------
Easing applies only to a *continuously* interpolated animation. On a stepped
(terminal) backend the Panel plays every transition as exactly two frames — one
intermediate state, then the target — and that intermediate frame's whole job is
to be **visibly** intermediate. Running it through a sharp curve destroys it:
``ease_out_expo(0.5)`` is ``0.9990``, so the "intermediate" frame would be
pixel-for-pixel the target and the user would see one abrupt jump instead of the
intended beat. So the Panel deliberately keeps stepped progress linear and
applies easing only when it is interpolating in wall-clock time. See
``_anim_progress`` in ``puikit/panel.py``.
"""

from __future__ import annotations

from typing import Callable

#: An easing is any pure ``float -> float`` mapping progress to progress.
Easing = Callable[[float], float]


def _clamp01(p: float) -> float:
    return 0.0 if p < 0.0 else 1.0 if p > 1.0 else float(p)


def linear(p: float) -> float:
    """Constant velocity — progress passes straight through."""
    return _clamp01(p)


def ease_in_quad(p: float) -> float:
    """Accelerate from rest (slow start, fast finish)."""
    p = _clamp01(p)
    return p * p


def ease_out_quad(p: float) -> float:
    """Decelerate to rest — the gentle, general-purpose deceleration.

    PuiKit's historical curve: both GUI backends hardcoded exactly this as
    ``1 - (1 - p) ** 2`` before easing became selectable, so it stays the default
    and existing transitions are unchanged by the easing work.
    """
    p = _clamp01(p)
    return 1.0 - (1.0 - p) ** 2


def ease_in_out_quad(p: float) -> float:
    """Accelerate, then decelerate — symmetric, for motion at rest on both ends."""
    p = _clamp01(p)
    if p < 0.5:
        return 2.0 * p * p
    return 1.0 - ((-2.0 * p + 2.0) ** 2) / 2.0


def ease_out_cubic(p: float) -> float:
    """A firmer deceleration than :func:`ease_out_quad`, short of expo's snap."""
    p = _clamp01(p)
    return 1.0 - (1.0 - p) ** 3


def ease_out_expo(p: float) -> float:
    """Hard deceleration: ~90% of the distance in the first 30% of the duration,
    then a long glide into the target.

    Special-cased at ``p == 1`` because ``1 - 2 ** -10`` is ``0.99902``, not
    ``1`` — without the exact landing a transition would stop a hair short of its
    target and leave a widget permanently offset by a sub-unit amount.
    """
    p = _clamp01(p)
    if p >= 1.0:
        return 1.0
    return 1.0 - 2.0 ** (-10.0 * p)


def ease_in_expo(p: float) -> float:
    """The mirror of :func:`ease_out_expo` — a slow creep that then rushes out.
    Suits an element *leaving* (``out=True``), where expo-out on the way in is
    matched by expo-in on the way back."""
    p = _clamp01(p)
    if p <= 0.0:
        return 0.0
    return 2.0 ** (10.0 * p - 10.0)


def ease_out_back(p: float) -> float:
    """Overshoot past the target, then settle back onto it.

    Peaks at about 1.10 near ``p == 0.7``, so a consumer must tolerate a progress
    value **greater than 1** — a scale reaching 1.1x is the intended effect, but a
    rect interpolation that clamps or a color tween that indexes a palette will
    misbehave. Scale only.
    """
    p = _clamp01(p)
    c1 = 1.70158
    c3 = c1 + 1.0
    return 1.0 + c3 * (p - 1.0) ** 3 + c1 * (p - 1.0) ** 2


#: Name → curve. A transition names a key here through its ``easing`` hint; an
#: app may register its own curve by assigning into this dict before use.
EASINGS: dict[str, Easing] = {
    "linear": linear,
    "ease_in_quad": ease_in_quad,
    "ease_out_quad": ease_out_quad,
    "ease_in_out_quad": ease_in_out_quad,
    "ease_out_cubic": ease_out_cubic,
    "ease_out_expo": ease_out_expo,
    "ease_in_expo": ease_in_expo,
    "ease_out_back": ease_out_back,
}

#: The curve used when a transition names none — PuiKit's historical behavior.
DEFAULT_EASING = "ease_out_quad"


def resolve(easing: str | Easing | None, default: str | Easing = DEFAULT_EASING) -> Easing:
    """The curve for ``easing``: a registered name, a callable passed through
    as-is, or ``None`` for ``default``.

    An unknown *name* falls back to ``default`` rather than raising. A theme or a
    user config file is a normal source of these strings, and a typo'd curve name
    should cost the intended timing, not take down the app mid-transition.
    """
    if easing is None:
        easing = default
    if callable(easing):
        return easing
    curve = EASINGS.get(easing) if isinstance(easing, str) else None
    if curve is not None:
        return curve
    # Unknown name: the named default, else the identity curve.
    if isinstance(default, str) and default != easing:
        return EASINGS.get(default, linear)
    return linear
