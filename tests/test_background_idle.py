"""Battery behaviour for animated backgrounds: coasting to a stop when the app is
idle or unfocused, parking the frame timer, and spinning back up on input.

An animated background is the one thing that keeps an otherwise-idle app redrawing
forever, so it is also the one thing that must stop when nobody is looking. These
tests cover the rate ramp (pure functions, exactly checkable), the park/re-arm
lifecycle, the property that makes parking invisible (the scene's clock counts
*animated* time, so a background never jumps when it resumes), and what the scene
comes to rest *as* — a held frame, or dissolved away for a scene that declares
``idle="fade"``.
"""

import pytest

from puikit import Shader, Wallpaper

_SRC = "fragment float4 puikit_bg_fragment() { return 0; }"

#: The one animated background kind. Idle parking is about the tick, not the
#: scene, so a shader that never has to compile stands in throughout.
_SHADER = Shader(source=_SRC)
#: The same, but asking to dissolve away rather than freeze when parked.
_FADING = Shader(source=_SRC, idle="fade")

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
    be._bg_fade = 1.0
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


# --- what the scene comes to rest as (Shader.idle) -----------------------------

class TestIdleFade:
    """A scene that is nothing but motion has no frame worth freezing on, so it
    asks to dissolve instead. The fade rides its own ramp over the same spans, so
    the scene dims and slows as one gesture."""

    def test_a_freezing_scene_stays_fully_present(self, monkeypatch):
        clock = FakeClock()
        be = _backend(monkeypatch, _SHADER, clock)
        clock.advance(_BG_IDLE_TIMEOUT + 1)
        _run(be, clock, _BG_RAMP_DOWN + 2)
        assert be._bg_fade == 1.0, "a freeze scene faded out"

    def test_a_fading_scene_is_gone_by_the_time_it_parks(self, monkeypatch):
        clock = FakeClock()
        be = _backend(monkeypatch, _FADING, clock)
        clock.advance(_BG_IDLE_TIMEOUT + 1)
        assert _run(be, clock, _BG_RAMP_DOWN + 2) is False
        assert be._bg_fade == 0.0

    def test_it_dissolves_rather_than_cutting_out(self, monkeypatch):
        clock = FakeClock()
        be = _backend(monkeypatch, _FADING, clock)
        clock.advance(_BG_IDLE_TIMEOUT + 1)
        _run(be, clock, _BG_RAMP_DOWN / 2)
        assert 0.0 < be._bg_fade < 1.0

    def test_it_keeps_its_speed_while_dissolving(self, monkeypatch):
        # A dissolving scene must NOT also decelerate: the slowdown would be on
        # screen for the whole 40s fade, which is the "it has stopped" reading the
        # fade exists to avoid. It keeps moving and simply goes.
        clock = FakeClock()
        be = _backend(monkeypatch, _FADING, clock)
        clock.advance(_BG_IDLE_TIMEOUT + 1)
        for frac in (0.1, 0.25, 0.5, 0.9):
            _run(be, clock, _BG_RAMP_DOWN * frac / 4)
            assert be._bg_fade < 1.0, "not fading"
            assert be._bg_rate == 1.0, f"slowed to {be._bg_rate} at fade {be._bg_fade}"

    def test_the_scene_clock_runs_at_full_rate_through_the_fade(self, monkeypatch):
        # The visible consequence of the rule above: over the dissolve the scene
        # advances by real time, not by a decayed fraction of it.
        clock = FakeClock()
        be = _backend(monkeypatch, _FADING, clock)
        clock.advance(_BG_IDLE_TIMEOUT + 1)
        before = be._bg_clock
        _run(be, clock, _BG_RAMP_DOWN / 2)
        assert be._bg_clock - before == pytest.approx(_BG_RAMP_DOWN / 2, rel=0.02)

    def test_the_rate_drops_only_once_nothing_is_left_to_see(self, monkeypatch):
        # And it drops outright rather than coasting — a 40s coast on an invisible
        # scene is pure battery, and at zero opacity the snap is unobservable.
        clock = FakeClock()
        be = _backend(monkeypatch, _FADING, clock)
        clock.advance(_BG_IDLE_TIMEOUT + 1)
        _run(be, clock, _BG_RAMP_DOWN + 2)
        assert be._bg_fade == 0.0 and be._bg_rate == 0.0

    def test_a_freezing_scene_still_coasts(self, monkeypatch):
        # The other rest state keeps the ramp: for a freeze, coasting is how the
        # scene *arrives* at the frame it will hold.
        clock = FakeClock()
        be = _backend(monkeypatch, _SHADER, clock)
        clock.advance(_BG_IDLE_TIMEOUT + 1)
        _run(be, clock, _BG_RAMP_DOWN / 2)
        assert 0.0 < be._bg_rate < 1.0

    def test_resuming_restores_speed_at_once(self, monkeypatch):
        # Coming back, the scene is invisible, so full speed can be restored
        # immediately; only the presence eases in.
        clock = FakeClock()
        be = _backend(monkeypatch, _FADING, clock)
        clock.advance(_BG_IDLE_TIMEOUT + 1)
        _run(be, clock, _BG_RAMP_DOWN + 2)
        be._last_input_time = clock.now
        be._ensure_background_ticker()
        clock.advance(FRAME)
        be._background_tick()
        assert be._bg_rate == 1.0
        assert be._bg_fade < 0.05, "presence should still be easing in"

    def test_the_frame_it_parks_on_is_the_empty_one(self, monkeypatch):
        # The point of the whole feature. The last frame drawn is the one that
        # stays on screen, so it has to be rendered *after* the fade reaches zero —
        # parking a frame early would leave the scene visibly stuck there.
        clock = FakeClock()
        be = _backend(monkeypatch, _FADING, clock)
        drawn = []
        monkeypatch.setattr(be, "_render_shader",
                            lambda bg, now: (drawn.append(be._bg_fade), True)[1])
        clock.advance(_BG_IDLE_TIMEOUT + 1)
        _run(be, clock, _BG_RAMP_DOWN + 2)
        assert drawn and drawn[-1] == 0.0

    def test_a_withheld_final_frame_is_retried_not_parked_on(self, monkeypatch):
        # The compositor can refuse a drawable (occluded, mid-resize). Parking on a
        # frame that never reached the layer would leave the previous one — for a
        # fading scene, the one still showing it.
        clock = FakeClock()
        be = _backend(monkeypatch, _FADING, clock)
        monkeypatch.setattr(be, "_render_shader", lambda bg, now: False)
        clock.advance(_BG_IDLE_TIMEOUT + 1)
        _run(be, clock, _BG_RAMP_DOWN + 0.2)
        assert be._bg_fade == 0.0          # settled...
        assert be._bg_running is True      # ...but not parked on a lost frame

    def test_retrying_a_withheld_frame_is_bounded(self, monkeypatch):
        # A window that can never present must not hold the frame timer open
        # forever on the strength of one lost frame — the point of parking is the
        # battery, not the pixel.
        clock = FakeClock()
        be = _backend(monkeypatch, _FADING, clock)
        monkeypatch.setattr(be, "_render_shader", lambda bg, now: False)
        clock.advance(_BG_IDLE_TIMEOUT + 1)
        assert _run(be, clock, _BG_RAMP_DOWN + 5) is False
        assert be._bg_running is False

    def test_input_brings_it_back(self, monkeypatch):
        clock = FakeClock()
        be = _backend(monkeypatch, _FADING, clock)
        clock.advance(_BG_IDLE_TIMEOUT + 1)
        _run(be, clock, _BG_RAMP_DOWN + 2)
        assert be._bg_fade == 0.0
        be._last_input_time = clock.now                # user comes back
        be._ensure_background_ticker()
        _run(be, clock, _BG_RAMP_UP / 2)
        assert be._bg_fade > 0.0

    def test_reduced_motion_holds_the_frame_instead(self, monkeypatch):
        # Reduced motion is not idleness — the user is still there, and a slow
        # dissolve is exactly the motion the setting asks to be rid of. So the
        # scene stops but stays, however it is marked.
        clock = FakeClock()
        be = _backend(monkeypatch, _FADING, clock)
        be._reduced_motion = True
        clock.advance(_BG_IDLE_TIMEOUT + 1)
        _run(be, clock, _BG_RAMP_DOWN + 2)
        assert be._bg_fade == 1.0

    def test_reduced_motion_still_parks(self, monkeypatch):
        # Holding the frame must not cost a permanent 60Hz timer.
        clock = FakeClock()
        be = _backend(monkeypatch, _FADING, clock)
        be._reduced_motion = True
        assert _run(be, clock, _BG_RAMP_DOWN + 2) is False
        assert be._bg_running is False

    def test_a_new_background_arrives_fully_present(self, monkeypatch):
        clock = FakeClock()
        be = _backend(monkeypatch, _FADING, clock)
        clock.advance(_BG_IDLE_TIMEOUT + 1)
        _run(be, clock, _BG_RAMP_DOWN + 2)
        be.set_background(_FADING)                     # a theme switch
        assert be._bg_fade == 1.0


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
