"""Battery behaviour for animated backgrounds: coasting to a stop when the app is
idle or unfocused, parking the frame timer, and spinning back up on input.

An animated background is the one thing that keeps an otherwise-idle app redrawing
forever, so it is also the one thing that must stop when nobody is looking. These
tests cover the rate ramp (pure functions, exactly checkable), the park/re-arm
lifecycle, and the property that makes parking invisible: the scene's clock counts
*animated* time, so a background never jumps when it resumes.
"""

import pytest

from puikit import Shader, Wallpaper

#: The one animated background kind. Idle parking is about the tick, not the
#: scene, so a shader that never has to compile stands in throughout.
_SHADER = Shader(source="fragment float4 puikit_bg_fragment() { return 0; }")

mb = pytest.importorskip("puikit.backends.macos_backend")

from puikit.backends.macos_backend import (  # noqa: E402
    _BG_IDLE_TIMEOUT, _BG_RAMP_DOWN, _BG_RAMP_UP, _approach, _smoothstep,
)

FRAME = 1 / 60.0


class FakeClock:
    """Stand-in for the ``time`` module so a tick sequence is deterministic."""

    def __init__(self, start=1000.0):
        self.now = start

    def monotonic(self):
        return self.now

    def perf_counter(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _backend(monkeypatch, background, clock):
    be = mb.MacOSBackend()
    monkeypatch.setattr(mb, "time", clock)
    be._background = background
    be._last_input_time = clock.now
    be._bg_last_tick = clock.now
    be._bg_running = True
    be._bg_rate = 1.0
    be._bg_clock = 0.0
    return be


def _run(be, clock, seconds, frame=FRAME):
    """Tick for ``seconds`` of fake time; returns False once it parks."""
    alive = True
    for _ in range(int(seconds / frame)):
        clock.advance(frame)
        alive = be._background_tick()
        if not alive:
            break
    return alive


# --- the rate ramp -------------------------------------------------------------

class TestSmoothstep:

    def test_endpoints(self):
        assert _smoothstep(0.0) == 0.0
        assert _smoothstep(1.0) == 1.0

    def test_clamps_out_of_range(self):
        assert _smoothstep(-5.0) == 0.0
        assert _smoothstep(5.0) == 1.0

    def test_is_monotonic(self):
        vals = [_smoothstep(i / 50) for i in range(51)]
        assert vals == sorted(vals)

    def test_flattens_at_both_ends(self):
        # Zero slope at 0 and 1 is the whole point: it is what stops the motion
        # from starting and ending with a visible kick.
        eps = 0.01
        assert _smoothstep(eps) < eps / 2
        assert _smoothstep(1 - eps) > 1 - eps / 2


class TestApproach:

    def test_moves_toward_the_target(self):
        assert _approach(0.0, 1.0, 0.1, 1.0, 1.0) == pytest.approx(0.1)
        assert _approach(1.0, 0.0, 0.1, 1.0, 1.0) == pytest.approx(0.9)

    def test_never_overshoots(self):
        assert _approach(0.9, 1.0, 10.0, 1.0, 1.0) == 1.0
        assert _approach(0.1, 0.0, 10.0, 1.0, 1.0) == 0.0

    def test_rise_and_fall_use_different_spans(self):
        # Falling is slower than rising, so a background coasts gently to a halt
        # but answers input briskly.
        assert _approach(0.5, 1.0, 0.1, 1.0, 4.0) == pytest.approx(0.6)
        assert _approach(0.5, 0.0, 0.1, 1.0, 4.0) == pytest.approx(0.475)

    def test_zero_span_snaps(self):
        assert _approach(0.0, 1.0, 0.001, 0.0, 0.0) == 1.0

    def test_ramps_take_their_configured_time(self):
        rate, elapsed = 1.0, 0.0
        while rate > 0.0:
            rate = _approach(rate, 0.0, FRAME, _BG_RAMP_UP, _BG_RAMP_DOWN)
            elapsed += FRAME
        assert elapsed == pytest.approx(_BG_RAMP_DOWN, abs=0.05)

    def test_speed_never_changes_abruptly(self):
        # The requirement in one assertion: across the whole ramp, no single frame
        # may change the eased speed perceptibly. The bound is set well below what
        # the current spans achieve (~0.17%) but above nothing — shortening either
        # ramp back toward a couple of seconds would fail here.
        worst = 0.0
        rate = 0.0
        while rate < 1.0:
            nxt = _approach(rate, 1.0, FRAME, _BG_RAMP_UP, _BG_RAMP_DOWN)
            worst = max(worst, abs(_smoothstep(nxt) - _smoothstep(rate)))
            rate = nxt
        assert worst < 0.005, f"speed jumps {worst:.2%} in one frame"


# --- when the background should be running -------------------------------------

class TestTarget:

    def test_recent_input_wants_full_rate(self, monkeypatch):
        clock = FakeClock()
        be = _backend(monkeypatch, _SHADER, clock)
        assert be._bg_target(clock.now) == 1.0

    def test_idle_wants_zero(self, monkeypatch):
        clock = FakeClock()
        be = _backend(monkeypatch, _SHADER, clock)
        assert be._bg_target(clock.now + _BG_IDLE_TIMEOUT + 1) == 0.0

    def test_just_inside_the_timeout_still_runs(self, monkeypatch):
        clock = FakeClock()
        be = _backend(monkeypatch, _SHADER, clock)
        assert be._bg_target(clock.now + _BG_IDLE_TIMEOUT - 0.1) == 1.0


# --- park and re-arm -----------------------------------------------------------

class TestParking:

    def test_it_parks_once_idle(self, monkeypatch):
        # The shader is the easy one to forget, because it never repaints the UI
        # in the first place -- its cost is the tick alone.
        background = _SHADER
        clock = FakeClock()
        be = _backend(monkeypatch, background, clock)
        clock.advance(_BG_IDLE_TIMEOUT + 1)          # go idle
        alive = _run(be, clock, _BG_RAMP_DOWN + 2)
        assert alive is False
        assert be._bg_running is False

    def test_it_keeps_running_while_in_use(self, monkeypatch):
        clock = FakeClock()
        be = _backend(monkeypatch, _SHADER, clock)
        for _ in range(600):                          # 10s of frames, always active
            clock.advance(FRAME)
            be._last_input_time = clock.now           # user still typing
            assert be._background_tick() is True
        assert be._bg_rate == 1.0

    def test_it_coasts_rather_than_stopping_dead(self, monkeypatch):
        clock = FakeClock()
        be = _backend(monkeypatch, _SHADER, clock)
        clock.advance(_BG_IDLE_TIMEOUT + 1)
        # Halfway through the coast it must still be moving, just slower.
        _run(be, clock, _BG_RAMP_DOWN / 2)
        assert 0.0 < be._bg_rate < 1.0

    def test_input_re_arms_it(self, monkeypatch):
        clock = FakeClock()
        be = _backend(monkeypatch, _SHADER, clock)
        clock.advance(_BG_IDLE_TIMEOUT + 1)
        _run(be, clock, _BG_RAMP_DOWN + 2)
        assert be._bg_running is False
        be._last_input_time = clock.now               # user comes back
        be._ensure_background_ticker()
        assert be._bg_running is True

    def test_re_arming_is_idempotent(self, monkeypatch):
        clock = FakeClock()
        be = _backend(monkeypatch, _SHADER, clock)
        be._ensure_background_ticker()
        be._ensure_background_ticker()
        assert be._bg_running is True

    @pytest.mark.parametrize("background", [None, Wallpaper(image="x.png")],
                             ids=["solid", "wallpaper"])
    def test_static_backgrounds_never_arm(self, monkeypatch, background):
        clock = FakeClock()
        be = _backend(monkeypatch, background, clock)
        be._bg_running = False
        be._ensure_background_ticker()
        assert be._bg_running is False

    def test_a_cleared_background_stops_the_tick(self, monkeypatch):
        clock = FakeClock()
        be = _backend(monkeypatch, _SHADER, clock)
        be._background = None
        assert be._background_tick() is False
        assert be._bg_running is False


# --- the animation clock -------------------------------------------------------

class TestClock:

    def test_it_advances_while_running(self, monkeypatch):
        clock = FakeClock()
        be = _backend(monkeypatch, _SHADER, clock)
        _run(be, clock, 1.0)
        assert be._bg_clock == pytest.approx(1.0, abs=0.05)

    def test_it_never_goes_backwards(self, monkeypatch):
        clock = FakeClock()
        be = _backend(monkeypatch, _SHADER, clock)
        seen = []
        for _ in range(300):
            clock.advance(FRAME)
            be._background_tick()
            seen.append(be._bg_clock)
        assert seen == sorted(seen)

    def test_it_does_not_jump_across_a_park(self, monkeypatch):
        # The reason the clock exists. Wall-clock time would have the scene leap
        # ten minutes forward on resume; animated time resumes where it stopped.
        clock = FakeClock()
        be = _backend(monkeypatch, _SHADER, clock)
        clock.advance(_BG_IDLE_TIMEOUT + 1)
        _run(be, clock, _BG_RAMP_DOWN + 2)
        parked_at = be._bg_clock

        clock.advance(600.0)                    # ten minutes away from the machine
        be._last_input_time = clock.now
        be._ensure_background_ticker()
        clock.advance(FRAME)
        be._background_tick()
        assert be._bg_clock - parked_at < 0.05, "scene jumped after resuming"

    def test_a_stall_does_not_lurch_the_scene(self, monkeypatch):
        # A blocked main thread must not be paid back all at once when it recovers.
        clock = FakeClock()
        be = _backend(monkeypatch, _SHADER, clock)
        clock.advance(5.0)                      # a five second stall
        be._last_input_time = clock.now
        be._background_tick()
        assert be._bg_clock <= 0.25

    def test_a_new_background_starts_from_zero(self, monkeypatch):
        clock = FakeClock()
        be = _backend(monkeypatch, _SHADER, clock)
        _run(be, clock, 1.0)
        assert be._bg_clock > 0
        be.set_background(_SHADER)
        assert be._bg_clock == 0.0
        assert be._bg_rate == 1.0       # a theme switch is itself user activity
        assert be._bg_running is True


# --- the frame timer -----------------------------------------------------------

class TestFrameTimer:

    def _wants_fast(self, be):
        return bool(be._animations) or be._roll_active() or be._bg_running

    def test_running_background_holds_the_fast_rate(self, monkeypatch):
        clock = FakeClock()
        be = _backend(monkeypatch, _SHADER, clock)
        assert self._wants_fast(be)

    def test_parked_background_releases_it(self, monkeypatch):
        # The point of parking: the timer must be allowed back down to the idle
        # rate, not held at 60Hz by a background that is no longer moving.
        clock = FakeClock()
        be = _backend(monkeypatch, _SHADER, clock)
        clock.advance(_BG_IDLE_TIMEOUT + 1)
        _run(be, clock, _BG_RAMP_DOWN + 2)
        assert not self._wants_fast(be)
