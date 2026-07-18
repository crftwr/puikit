"""Reduced motion — the framework's ``prefers-reduced-motion`` equivalent.

The contract has three halves, and the third is the one that is easy to get
wrong:

1. animated things resolve to their **final state** (never a frozen mid-pose);
2. self-driven decoration (a background scene, a post effect's roll) stops;
3. the animation **tick keeps running**, because it also drives functional work
   — draining a worker thread's results, following a growing log — and silencing
   it would turn a motion preference into a broken app.
"""

import pytest

from puikit import PROFILE_GUI_DESKTOP, PROFILE_TUI, Panel, PostEffect
from puikit.backends.memory_backend import MemoryBackend


@pytest.fixture(params=[PROFILE_TUI, PROFILE_GUI_DESKTOP], ids=["tui", "gui"])
def panel(request):
    return Panel(MemoryBackend(width=30, height=10, capabilities=request.param))


class TestSwitch:

    def test_defaults_off(self, panel):
        assert panel.reduced_motion is False

    def test_round_trips(self, panel):
        panel.set_reduced_motion(True)
        assert panel.reduced_motion is True
        panel.set_reduced_motion(False)
        assert panel.reduced_motion is False

    def test_backend_default_needs_no_constructor_call(self):
        # The flag is a class-level default precisely so a backend that never
        # calls super().__init__() still has the attribute.
        assert MemoryBackend(width=4, height=2).reduced_motion is False

    def test_panel_and_backend_agree(self, panel):
        panel.set_reduced_motion(True)
        assert panel.backend.reduced_motion is True


class TestTransitionsResolveImmediately:

    def test_animate_declines_under_reduced_motion(self, panel):
        widget = object()
        # Both fixture profiles can animate (GUI composites, TUI steps), so the
        # only thing changing the answer below is the setting itself.
        assert panel.animate(widget, hints={"transition": "fade"}) is True
        panel.set_reduced_motion(True)
        assert panel.animate(widget, hints={"transition": "fade"}) is False

    @pytest.mark.parametrize(
        "transition", ["fade", "slide", "scale", "size", "color", "highlight"]
    )
    def test_every_transition_kind_declines(self, panel, transition):
        panel.set_reduced_motion(True)
        hints = {"transition": transition, "from": (0, 0, 0), "to": (255, 255, 255),
                 "from_w": 1, "from_h": 1}
        assert panel.animate(object(), hints=hints) is False

    def test_no_animation_state_is_registered(self, panel):
        panel.set_reduced_motion(True)
        widget = object()
        panel.animate(widget, hints={"transition": "size", "from_w": 1, "from_h": 1})
        assert widget not in panel._size_anims

    def test_returning_false_lets_on_complete_callers_act_at_once(self, panel):
        # A drawer pops its layer in on_complete; if animate() reported True and
        # then never ticked, the drawer would stay on screen forever.
        panel.set_reduced_motion(True)
        widget = object()
        scheduled = panel.animate(
            widget,
            hints={"transition": "slide", "from_dx": 10, "out": True,
                   "on_complete": lambda: pytest.fail("must not be deferred to a tick")},
        )
        assert scheduled is False, "caller must be told to do its follow-up itself"
        # And nothing is left holding the on_complete, so it can never fire late.
        assert widget not in panel._size_anims

    def test_animated_color_falls_back_to_the_resting_color(self, panel):
        panel.set_reduced_motion(True)
        widget = object()
        panel.animate(widget, hints={"transition": "color",
                                     "from": (255, 0, 0), "to": (0, 255, 0)})
        # No tween registered, so the widget draws its resting color — the target,
        # not the "from" color, and not a blend.
        assert panel.animated_color(widget, default=(0, 255, 0)) == (0, 255, 0)


class TestTicksKeepRunning:
    """The critical carve-out: reduced motion must not silence the tick."""

    def test_request_animation_ticks_still_registers(self, panel):
        panel.set_reduced_motion(True)
        registered = []
        panel.backend.request_animation_ticks = lambda cb: registered.append(cb)
        assert panel.request_animation_ticks(lambda: True) is True
        assert registered, "functional work (queue draining, log tailing) needs the tick"


class TestPostEffectMotionStripping:

    def test_without_motion_drops_only_the_moving_fields(self):
        effect = PostEffect(name="crt", bloom=0.4, scanline=0.2, vignette=0.3,
                            curvature=0.1, glow=0.5, drop_shadow=0.6,
                            flicker=0.7, roll=0.8)
        still = effect.without_motion()
        assert (still.flicker, still.roll) == (0.0, 0.0)
        # The screen keeps its material identity — a CRT theme still looks CRT.
        assert (still.bloom, still.scanline, still.vignette) == (0.4, 0.2, 0.3)
        assert (still.curvature, still.glow, still.drop_shadow) == (0.1, 0.5, 0.6)

    def test_leaves_the_original_untouched(self):
        effect = PostEffect(name="crt", roll=0.5, flicker=0.5)
        effect.without_motion()
        assert (effect.roll, effect.flicker) == (0.5, 0.5), \
            "turning the setting back off must restore the full effect"

    def test_a_purely_static_effect_is_unchanged(self):
        effect = PostEffect(name="crt", bloom=0.3, glow=0.2)
        assert effect.without_motion() == effect

    def test_an_effect_that_was_only_motion_becomes_a_noop(self):
        assert PostEffect(name="crt", roll=0.4, flicker=0.2).without_motion().is_noop
