"""Easing curves and how animations consume them.

Two things are under test: the curves themselves (pure functions, exactly
checkable), and the wiring that decides *when* a curve is applied — which is
where the real behaviour lives, because a stepped (terminal) animation must
deliberately stay linear.
"""

import pytest

from puikit import easing
from puikit.easing import EASINGS, resolve


ALL_CURVES = sorted(EASINGS)


# --- the curves ----------------------------------------------------------------

class TestCurveContract:
    """Every registered curve must be a well-behaved 0..1 -> 0..1 mapping, or a
    transition using it would start or land off-target."""

    @pytest.mark.parametrize("name", ALL_CURVES)
    def test_pins_both_endpoints(self, name):
        curve = EASINGS[name]
        assert curve(0.0) == pytest.approx(0.0, abs=1e-9)
        assert curve(1.0) == pytest.approx(1.0, abs=1e-9)

    @pytest.mark.parametrize("name", ALL_CURVES)
    def test_clamps_out_of_range_input(self, name):
        curve = EASINGS[name]
        assert curve(-2.0) == pytest.approx(0.0, abs=1e-9)
        assert curve(3.0) == pytest.approx(1.0, abs=1e-9)

    @pytest.mark.parametrize("name", [n for n in ALL_CURVES if n != "ease_out_back"])
    def test_stays_in_range(self, name):
        # ease_out_back is exempt by design: it overshoots past 1 and settles.
        curve = EASINGS[name]
        for i in range(101):
            assert 0.0 <= curve(i / 100) <= 1.0

    @pytest.mark.parametrize("name", [n for n in ALL_CURVES if n != "ease_out_back"])
    def test_is_monotonic(self, name):
        curve = EASINGS[name]
        vals = [curve(i / 100) for i in range(101)]
        assert all(b >= a - 1e-12 for a, b in zip(vals, vals[1:]))


def test_linear_is_identity():
    for i in range(11):
        assert easing.linear(i / 10) == pytest.approx(i / 10)


def test_ease_out_expo_lands_exactly_on_one():
    # The whole reason ease_out_expo special-cases p == 1: the raw formula gives
    # 0.99902, which would leave a transition permanently a hair short of target.
    assert easing.ease_out_expo(1.0) == 1.0
    assert easing.ease_out_expo(0.999) < 1.0


def test_ease_out_expo_front_loads_the_motion():
    # The defining property of the curve — most of the distance covered early,
    # then a glide. This is what reads as "snapped to attention and settled".
    assert easing.ease_out_expo(0.3) > 0.85
    assert easing.ease_out_expo(0.5) > 0.95


def test_ease_out_back_overshoots_then_settles():
    peak = max(easing.ease_out_back(i / 100) for i in range(101))
    assert peak > 1.0, "ease_out_back must overshoot; that is its whole point"
    assert easing.ease_out_back(1.0) == pytest.approx(1.0)


def test_ease_out_quad_matches_the_historical_hardcoded_curve():
    # Both GUI backends hardcoded exactly this before easing became selectable;
    # it stays the default so existing transitions are bit-identical.
    for i in range(101):
        p = i / 100
        assert easing.ease_out_quad(p) == pytest.approx(1.0 - (1.0 - p) ** 2)


# --- resolution ----------------------------------------------------------------

class TestResolve:

    def test_resolves_registered_name(self):
        assert resolve("ease_out_expo") is easing.ease_out_expo

    def test_passes_callable_through(self):
        curve = (lambda p: p * p)
        assert resolve(curve) is curve

    def test_none_uses_the_default(self):
        assert resolve(None) is easing.ease_out_quad
        assert resolve(None, default="linear") is easing.linear

    def test_unknown_name_falls_back_rather_than_raising(self):
        # A curve name can come from a theme or a user config file; a typo should
        # cost the intended timing, not take down the app mid-transition.
        assert resolve("no_such_curve") is easing.ease_out_quad
        assert resolve("no_such_curve", default="linear") is easing.linear

    def test_unknown_name_with_unknown_default_degrades_to_linear(self):
        assert resolve("nope", default="also_nope") is easing.linear

    def test_default_is_ease_out_quad(self):
        assert EASINGS[easing.DEFAULT_EASING] is easing.ease_out_quad


# --- wiring into the Panel's animation channels --------------------------------

class TestPanelProgressWiring:
    """``_anim_progress`` is the seam that decides whether a curve applies."""

    def _anim(self, **kw):
        from puikit.panel import _GeometryAnimation
        base = dict(start=0.0, duration=1.0, from_w=None, from_h=None)
        base.update(kw)
        return _GeometryAnimation(**base)

    def test_no_easing_is_linear(self):
        from puikit.panel import _anim_progress
        anim = self._anim()
        assert _anim_progress(anim, 0.5) == pytest.approx(0.5)

    def test_easing_shapes_continuous_progress(self):
        from puikit.panel import _anim_progress
        anim = self._anim(easing=easing.ease_out_expo)
        assert _anim_progress(anim, 0.5) == pytest.approx(easing.ease_out_expo(0.5))

    def test_stepped_progress_stays_linear_despite_easing(self):
        # The 2-frame policy's intermediate frame exists to be VISIBLY
        # intermediate. ease_out_expo(0.5) is 0.999, so applying the curve would
        # collapse that frame onto the target and turn the beat into one abrupt
        # jump. Stepped animations must ignore the curve.
        from puikit.panel import _anim_progress
        anim = self._anim(easing=easing.ease_out_expo, stepped=True, step=1)
        assert _anim_progress(anim, 999.0) == pytest.approx(0.5)

    def test_zero_duration_completes_immediately(self):
        from puikit.panel import _anim_progress
        anim = self._anim(duration=0.0, easing=easing.ease_out_expo)
        assert _anim_progress(anim, 0.0) == 1.0

    def test_effect_animation_without_easing_attr_is_unaffected(self):
        # _EffectAnimation carries no easing field; _anim_progress must not
        # require one (its progress is only ever read as a "< 1" binary).
        from puikit.panel import _anim_progress, _EffectAnimation
        anim = _EffectAnimation(kind="fade", start=0.0, duration=1.0, hints={})
        assert _anim_progress(anim, 0.5) == pytest.approx(0.5)
