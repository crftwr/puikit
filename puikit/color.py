"""Perceptual color math for legibility-guaranteeing themes.

Two independent pieces, kept separate on purpose:

- **APCA (Lc)** — the Accessible Perceptual Contrast Algorithm (SAPC / APCA
  0.1.9 constants). Unlike the WCAG 2 ratio it is polarity-aware and tuned for
  self-luminous displays, so it rates light-on-dark correctly — the case every
  dark theme lives in. We use it as the legibility *metric*.

- **OKLab / OKLCh** — a perceptually-uniform space. We adjust a color's
  *lightness* here (not in raw RGB), so lifting a color toward legibility keeps
  its hue and loses chroma only as fast as the gamut forces — a directory blue
  stays recognizably blue instead of washing to pale gray.

The headline function is :func:`legible_ink`: given the ink a designer *wants*
and the background it will actually sit on, return the closest color that meets
a target Lc, moving nothing if the wanted color already clears the bar
(floor-only). It cannot invent contrast a background can't support — see
:func:`max_achievable_lc`; that gap is the theme-recipe layer's job, not this
function's.

See ``docs/color_system.md`` for the full design — the metric, the color space,
the three layers, and how this module plugs into the draw seam and the theme.
"""

from __future__ import annotations

from functools import lru_cache
from math import sqrt

from .backend import Color

# --- APCA readability targets (Lc) --------------------------------------------
# Rounded from the APCA "bronze" font-size/weight lookup. Callers pass the level
# that matches how the text is used; these are policy defaults, not hard limits.
LC_MIN_NONTEXT = 45.0   #: spot/decorative text, icons, disabled labels
LC_LARGE = 60.0         #: large or bold UI text (headers, footers, status bars)
LC_BODY = 75.0          #: body / content text (file names, columns)
LC_PREFERRED = 90.0     #: dense or fluent-reading body text

_RGB = tuple[float, float, float]
_WHITE: Color = (255, 255, 255)
_BLACK: Color = (0, 0, 0)


def _rgb3(c: Color) -> _RGB:
    return (c[0], c[1], c[2])


# --- APCA (SAPC 0.1.9) --------------------------------------------------------
_BLK_THRS, _BLK_CLMP = 0.022, 1.414
_NORM_BG, _NORM_TXT, _REV_TXT, _REV_BG = 0.56, 0.57, 0.62, 0.65
_SCALE, _LO_OFF, _DELTA_Y_MIN, _LO_CLIP = 1.14, 0.027, 0.0005, 0.1


def _apca_y(c: Color) -> float:
    """APCA screen luminance: simple 2.4 power (not the piecewise sRGB EOTF —
    APCA deliberately uses a plain gamma here)."""
    r, g, b = _rgb3(c)
    return 0.2126729 * (r / 255) ** 2.4 + 0.7151522 * (g / 255) ** 2.4 + 0.0721750 * (b / 255) ** 2.4


def apca_lc(text: Color, background: Color) -> float:
    """Signed APCA lightness contrast Lc (roughly -108..+106).

    Positive = dark text on a light background (normal polarity); negative =
    light text on a dark background (reverse). Magnitude is what a target
    compares against; the sign only tells you the polarity.
    """
    ytxt, ybg = _apca_y(text), _apca_y(background)
    if ytxt <= _BLK_THRS:
        ytxt += (_BLK_THRS - ytxt) ** _BLK_CLMP
    if ybg <= _BLK_THRS:
        ybg += (_BLK_THRS - ybg) ** _BLK_CLMP
    if abs(ybg - ytxt) < _DELTA_Y_MIN:
        return 0.0
    if ybg > ytxt:  # normal polarity: darker text on lighter bg
        sapc = (ybg ** _NORM_BG - ytxt ** _NORM_TXT) * _SCALE
        out = 0.0 if sapc < _LO_CLIP else sapc - _LO_OFF
    else:           # reverse polarity: lighter text on darker bg
        sapc = (ybg ** _REV_BG - ytxt ** _REV_TXT) * _SCALE
        out = 0.0 if sapc > -_LO_CLIP else sapc + _LO_OFF
    return out * 100.0


def max_achievable_lc(background: Color) -> float:
    """The best |Lc| *any* ink can reach on this background (black or white,
    whichever is farther). If a target exceeds this, no foreground can satisfy
    it — the background itself must change (a theme-recipe decision)."""
    return max(abs(apca_lc(_BLACK, background)), abs(apca_lc(_WHITE, background)))


def _mix_rgb(a: Color, b: Color, t: float) -> Color:
    return tuple(max(0, min(255, round(a[i] + (b[i] - a[i]) * t))) for i in range(3))


def ensure_text_headroom(bg: Color, toward: Color, target: float, *, margin: float = 3.0) -> Color:
    """The recipe-layer complement to :func:`legible_ink`: nudge a *background*
    just far enough that *some* foreground can reach ``target`` Lc on it.

    ``legible_ink`` adjusts a foreground to a fixed background; this adjusts a
    background so a foreground is possible — for the case a background is itself
    too mid-luminance to bear legible text (a vivid accent used as a status bar,
    an accent-tinted selection fill on a light theme). ``bg`` is blended toward
    ``toward`` — normally the theme background, so the move is polarity-correct
    (a dark theme deepens the color, a light theme lightens it) — by the smallest
    amount that reaches ``target + margin``. A background that already has the
    headroom is returned unchanged (floor-only)."""
    need = target + margin
    bg3 = (bg[0], bg[1], bg[2])
    if max_achievable_lc(bg3) >= need:
        return bg3
    lo, hi = 0.0, 1.0
    for _ in range(24):
        t = (lo + hi) / 2
        if max_achievable_lc(_mix_rgb(bg3, toward, t)) >= need:
            hi = t
        else:
            lo = t
    return _mix_rgb(bg3, toward, hi)


# --- OKLab / OKLCh ------------------------------------------------------------
def _lin(c: float) -> float:      # sRGB 0..1 -> linear (standard piecewise EOTF)
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _gam(c: float) -> float:      # linear -> sRGB 0..1
    return 12.92 * c if c <= 0.0031308 else 1.055 * (c ** (1 / 2.4)) - 0.055


def _cbrt(x: float) -> float:
    return x ** (1 / 3) if x >= 0 else -((-x) ** (1 / 3))


def rgb_to_oklab(c: Color) -> _RGB:
    r, g, b = (_lin(c[0] / 255), _lin(c[1] / 255), _lin(c[2] / 255))
    l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l_, m_, s_ = _cbrt(l), _cbrt(m), _cbrt(s)
    return (
        0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
        1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
        0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_,
    )


def oklab_to_rgb(lab: _RGB) -> Color:
    L, A, B = lab
    l_ = L + 0.3963377774 * A + 0.2158037573 * B
    m_ = L - 0.1055613458 * A - 0.0638541728 * B
    s_ = L - 0.0894841775 * A - 1.2914855480 * B
    l, m, s = l_ ** 3, m_ ** 3, s_ ** 3
    r = 4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s
    g = -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s
    b = -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s
    return (
        max(0, min(255, round(_gam(r) * 255))),
        max(0, min(255, round(_gam(g) * 255))),
        max(0, min(255, round(_gam(b) * 255))),
    )


def oklab_distance(a: Color, b: Color) -> float:
    """Perceptual ΔE between two colors in OKLab (how far a lift moved a color —
    smaller means the designer's intent was better preserved)."""
    la, lb = rgb_to_oklab(a), rgb_to_oklab(b)
    return sqrt(sum((la[i] - lb[i]) ** 2 for i in range(3)))


def _lerp(a: _RGB, b: _RGB, t: float) -> _RGB:
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t)


# --- the headline: contrast-guaranteeing ink ----------------------------------
@lru_cache(maxsize=2048)
def legible_ink(ink: Color, background: Color, target_lc: float = LC_BODY) -> Color:
    """Return ``ink`` if it already clears ``target_lc`` on ``background``
    (floor-only — the designer's exact color is kept). Otherwise lift it in
    OKLab toward whichever pole (white/black) the background is farther from,
    stopping at the *minimum* move that reaches the target, so hue is preserved
    and chroma is spent only as far as necessary.

    If the background physically cannot support the target
    (:func:`max_achievable_lc` ``< target_lc``), returns the best-effort pole —
    the caller should treat that as "fix the background," not a legible result.
    """
    ink3 = _rgb3(ink)
    if abs(apca_lc(ink, background)) >= target_lc:
        return (ink[0], ink[1], ink[2])

    # Push toward the pole that is farther (in APCA terms) from the background.
    pole = _WHITE if abs(apca_lc(_WHITE, background)) >= abs(apca_lc(_BLACK, background)) else _BLACK
    ink_lab, pole_lab = rgb_to_oklab(ink3), rgb_to_oklab(pole)

    if max_achievable_lc(background) < target_lc:
        return oklab_to_rgb(pole_lab)  # unreachable: recipe layer must intervene

    # Contrast is monotonic along ink->pole, so binary-search the smallest blend.
    lo, hi = 0.0, 1.0
    for _ in range(24):
        t = (lo + hi) / 2
        cand = oklab_to_rgb(_lerp(ink_lab, pole_lab, t))
        if abs(apca_lc(cand, background)) >= target_lc:
            hi = t
        else:
            lo = t
    return oklab_to_rgb(_lerp(ink_lab, pole_lab, hi))
