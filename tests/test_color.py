"""Unit tests for the perceptual color primitives (APCA + OKLab + legible_ink)."""

from math import atan2, degrees

import pytest

from puikit.color import (
    LC_BODY,
    LC_LARGE,
    apca_lc,
    ensure_text_headroom,
    legible_ink,
    max_achievable_lc,
    oklab_to_rgb,
    rgb_to_oklab,
)

WHITE = (255, 255, 255)
BLACK = (0, 0, 0)


# --- APCA metric --------------------------------------------------------------
def test_apca_known_extremes_and_polarity():
    # Reference values from the APCA calculator (~106 / ~-108), and the sign
    # convention: dark-on-light positive, light-on-dark negative.
    assert 104.0 <= apca_lc(BLACK, WHITE) <= 108.0
    assert -109.0 <= apca_lc(WHITE, BLACK) <= -105.0
    assert apca_lc(BLACK, WHITE) > 0
    assert apca_lc(WHITE, BLACK) < 0


def test_apca_midgray_on_white_is_mid_range():
    # #888 on #fff is a well-cited ~63 Lc.
    assert 58.0 <= abs(apca_lc((136, 136, 136), WHITE)) <= 68.0


def test_apca_monotonic_lightening_on_dark_bg():
    # On a dark background, lifting the text lighter must not decrease contrast.
    bg = (30, 30, 30)
    prev = -1.0
    for v in range(40, 256, 24):
        lc = abs(apca_lc((v, v, v), bg))
        assert lc >= prev - 1e-9
        prev = lc


def test_max_achievable_lc_bounds():
    # A mid-luminance background caps contrast: no ink reaches body level on it.
    assert max_achievable_lc((128, 128, 128)) < LC_BODY
    # White/black backgrounds have full headroom.
    assert max_achievable_lc(WHITE) > 100
    assert max_achievable_lc(BLACK) > 100


# --- OKLab round-trip ---------------------------------------------------------
@pytest.mark.parametrize("c", [
    (0, 0, 0), (255, 255, 255), (0, 122, 204), (166, 226, 46),
    (189, 147, 249), (253, 246, 227), (88, 110, 117), (30, 30, 30),
])
def test_oklab_roundtrip(c):
    back = oklab_to_rgb(rgb_to_oklab(c))
    assert all(abs(back[i] - c[i]) <= 1 for i in range(3)), (c, back)


# --- legible_ink --------------------------------------------------------------
def test_legible_ink_is_floor_only_when_already_legible():
    # White on near-black already clears body level -> returned untouched.
    ink = (212, 212, 212)
    assert abs(apca_lc(ink, (30, 30, 30))) >= LC_BODY
    assert legible_ink(ink, (30, 30, 30), LC_BODY) == ink


def test_legible_ink_reaches_target_when_headroom_exists():
    # Dark+ directory blue on its content bg falls short; must be lifted to meet.
    bg = (30, 30, 30)
    blue = (0, 122, 204)
    assert abs(apca_lc(blue, bg)) < LC_BODY          # starts below
    fixed = legible_ink(blue, bg, LC_BODY)
    assert abs(apca_lc(fixed, bg)) >= LC_BODY - 0.5   # ends at/above target


def test_legible_ink_preserves_hue_while_lifting():
    # The lifted directory blue must still read as blue: OKLab hue angle barely
    # moves even though lightness climbs.
    bg = (30, 30, 30)
    blue = (0, 122, 204)
    fixed = legible_ink(blue, bg, LC_BODY)

    def hue(c):
        _, a, b = rgb_to_oklab(c)
        return degrees(atan2(b, a))

    dh = abs(hue(fixed) - hue(blue))
    dh = min(dh, 360 - dh)
    assert dh < 12.0, (blue, fixed, dh)


def test_legible_ink_moves_minimally():
    # Floor-only + minimal blend: a color only slightly short should move little.
    bg = (30, 30, 30)
    near = (150, 150, 150)  # already close to the body target on this bg
    fixed = legible_ink(near, bg, LC_BODY)
    # It should not overshoot to white.
    assert fixed != WHITE
    assert abs(apca_lc(fixed, bg)) >= LC_BODY - 0.5


def test_legible_ink_best_effort_on_unreachable_bg():
    # On a mid-luminance bg the body target is impossible; the result is the
    # strongest available ink (its |Lc| reaches the bg's ceiling).
    bg = (128, 128, 128)
    assert max_achievable_lc(bg) < LC_BODY
    fixed = legible_ink((100, 100, 100), bg, LC_BODY)
    assert abs(apca_lc(fixed, bg)) >= max_achievable_lc(bg) - 0.5


# --- ensure_text_headroom -----------------------------------------------------
def test_ensure_headroom_is_floor_only():
    # A background that can already bear the text is returned unchanged.
    bg = (0, 122, 204)  # blue accent, ceiling ~76
    assert max_achievable_lc(bg) >= LC_LARGE + 3
    assert ensure_text_headroom(bg, (30, 30, 30), LC_LARGE) == bg


def test_ensure_headroom_lifts_mid_background_to_target():
    # A mid-luminance accent that can't bear chrome text is deepened until it can.
    bg = (189, 147, 249)  # Dracula accent, ceiling ~57 < 60
    assert max_achievable_lc(bg) < LC_LARGE
    fixed = ensure_text_headroom(bg, (40, 42, 54), LC_LARGE)
    assert fixed != bg
    assert max_achievable_lc(fixed) >= LC_LARGE


def test_ensure_headroom_polarity_follows_toward():
    mid = (150, 150, 150)
    deep = ensure_text_headroom(mid, (0, 0, 0), LC_BODY)      # toward dark
    pale = ensure_text_headroom(mid, (255, 255, 255), LC_BODY)  # toward light
    assert sum(deep) < sum(mid) < sum(pale)
