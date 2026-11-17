"""A cover layer replaces the UI beneath it only once it is at rest: while it
slides, scales or fades, the part of the window it does not cover yet shows
that UI (xefm#900 - XeFM's terminal slides up over the panes)."""

import pytest

from puikit import PROFILE_GUI_DESKTOP, PROFILE_TUI, Panel
from puikit.backends.memory_backend import MemoryBackend
from puikit.widgets import Label


def _setup(profile):
    backend = MemoryBackend(width=20, height=6, capabilities=profile)
    panel = Panel(backend)
    panel.add(Label("BASE"), x=0, y=0, w=6, h=1)
    # The cover layer writes on its own row, clear of the base's, so the base
    # showing means it was drawn, not left behind.
    view = Label("VIEW")
    panel.push_layer(view, z=80, hints={"x": 0, "y": 4, "w": 4, "h": 1, "cover": True})
    return backend, panel, view


def _rows(panel, backend):
    backend.clear()
    panel.render()
    return backend.snapshot()


@pytest.mark.parametrize("transition", [
    {"transition": "slide", "from_dy": 6},
    {"transition": "scale", "from_scale": 0.92, "fade": True},
    {"transition": "fade"},
])
def test_a_composited_cover_shows_the_base_until_it_settles(transition):
    backend, panel, view = _setup(PROFILE_GUI_DESKTOP)
    assert panel.animate(view, hints={**transition, "duration_ms": 150})
    assert "BASE" in _rows(panel, backend)[0]
    backend.fire_timers()  # the transition's length has passed
    assert "BASE" not in _rows(panel, backend)[0]


def test_settling_renders_the_cover_at_rest():
    backend, panel, view = _setup(PROFILE_GUI_DESKTOP)
    panel.animate(view, hints={"transition": "slide", "from_dy": 6, "duration_ms": 150})
    panel.render()
    backend.clear()
    backend.fire_timers()  # renders by itself: no caller does after a slide-in
    assert "BASE" not in backend.snapshot()[0]
    assert "VIEW" in backend.snapshot()[4]


def test_a_cover_sliding_out_shows_the_base_until_it_is_gone():
    backend, panel, view = _setup(PROFILE_GUI_DESKTOP)
    gone = []
    panel.animate(view, hints={"transition": "slide", "from_dy": 6, "out": True,
                               "duration_ms": 150, "on_complete": lambda: gone.append(1)})
    assert "BASE" in _rows(panel, backend)[0]


def test_a_later_transition_outlives_the_earlier_settle():
    backend, panel, view = _setup(PROFILE_GUI_DESKTOP)
    panel.animate(view, hints={"transition": "slide", "from_dy": 6, "duration_ms": 150})
    first = backend.later_timers.pop(0)
    panel.animate(view, hints={"transition": "slide", "from_dy": 6, "out": True,
                               "duration_ms": 150})
    first[1]()  # the first slide's settle fires while the second still runs
    assert "BASE" in _rows(panel, backend)[0]


def test_a_stepped_cover_shows_the_base_while_it_moves():
    backend, panel, view = _setup(PROFILE_TUI)
    panel.animate(view, hints={"transition": "slide", "from_dy": 6, "duration_ms": 1})
    assert "BASE" in _rows(panel, backend)[0]
    backend.fire_timers()
    for _ in range(4):  # the 2-frame step, then the frame at rest
        if not panel._size_anims:
            break
        backend.run_animation_ticks()
    assert not panel._size_anims
    assert "BASE" not in backend.snapshot()[0]


def test_color_and_highlight_do_not_reveal_the_base():
    backend, panel, view = _setup(PROFILE_GUI_DESKTOP)
    panel.animate(view, hints={"transition": "highlight", "duration_ms": 150})
    assert "BASE" not in _rows(panel, backend)[0]


def test_reduced_motion_keeps_the_cover_opaque():
    backend, panel, view = _setup(PROFILE_GUI_DESKTOP)
    backend.set_reduced_motion(True)
    assert not panel.animate(view, hints={"transition": "slide", "from_dy": 6})
    assert "BASE" not in _rows(panel, backend)[0]


# --- the base as one widget, and transitions that go out -----------------------


def test_the_base_animates_as_one_group_only_while_it_moves():
    backend, panel, view = _setup(PROFILE_GUI_DESKTOP)
    opened = []
    real = backend.begin_group
    backend.begin_group = lambda key, rect=None, opaque=False: (
        opened.append((key, rect)), real(key, rect, opaque))
    panel.render()
    assert all(k is not panel.base for k, _r in opened)
    panel.animate(panel.base, hints={"transition": "scale", "from_scale": 1.08,
                                     "fade": True, "out": True, "duration_ms": 150})
    panel.animate(view, hints={"transition": "fade", "duration_ms": 150})
    opened.clear()
    panel.render()
    keys = [k for k, _r in opened]
    assert keys[0] is panel.base and opened[0][1].w == 20 and opened[0][1].h == 6
    assert "BASE" in backend.snapshot()[0]


def test_a_finished_out_leaves_the_widget_undrawn_until_animated_again():
    backend, panel, view = _setup(PROFILE_GUI_DESKTOP)
    panel.pop_layer()  # no cover: the base would draw at rest
    called = []
    panel.animate(panel.base, hints={"transition": "fade", "out": True, "duration_ms": 150,
                                     "on_complete": lambda: called.append(1)})
    assert "BASE" in _rows(panel, backend)[0]
    backend.run_animation_ticks()  # the backend reports the fade's end
    assert called == [1]
    assert "BASE" not in _rows(panel, backend)[0]
    panel.animate(panel.base, hints={"transition": "fade", "duration_ms": 150})
    assert "BASE" in _rows(panel, backend)[0]


def test_a_layer_fading_out_is_not_drawn_at_rest_and_is_forgotten_once_removed():
    backend, panel, view = _setup(PROFILE_GUI_DESKTOP)
    panel.animate(view, hints={"transition": "fade", "out": True, "duration_ms": 150,
                               "on_complete": lambda: panel.remove_layer(view)})
    backend.run_animation_ticks()
    assert not panel._layers and view not in panel._gone
    assert "BASE" in _rows(panel, backend)[0]


def test_a_stepped_fade_out_completes_and_skips_its_frame_at_rest():
    backend, panel, view = _setup(PROFILE_TUI)
    closed = []
    assert panel.animate(view, hints={"transition": "fade", "out": True, "duration_ms": 1,
                                      "on_complete": lambda: closed.append(1)})
    for _ in range(4):
        if closed:
            break
        backend.run_animation_ticks()
    assert closed == [1]
    assert "VIEW" not in _rows(panel, backend)[4]


def test_removing_a_layer_brings_back_a_base_that_went_out_for_it():
    backend, panel, view = _setup(PROFILE_GUI_DESKTOP)
    panel.animate(panel.base, hints={"transition": "fade", "out": True, "duration_ms": 150})
    backend.run_animation_ticks()
    assert panel.base in panel._gone
    panel.remove_layer(view)  # closed without a transition back
    assert "BASE" in _rows(panel, backend)[0]


@pytest.mark.parametrize("transition", [
    {"transition": "slide", "from_dy": 6},
    {"transition": "scale", "from_scale": 1.08, "fade": True},
])
def test_on_a_stepped_backend_the_base_does_not_move_but_still_completes(transition):
    # docs/animation.md, "What panel.base can play": no motion on a grid, but
    # the transition runs its length and an out leaves the base gone.
    backend, panel, view = _setup(PROFILE_TUI)
    panel.pop_layer()
    done = []
    assert panel.animate(panel.base, hints={**transition, "out": True, "duration_ms": 1,
                                            "on_complete": lambda: done.append(1)})
    assert _rows(panel, backend)[0].startswith("BASE")  # drawn in place
    for _ in range(4):
        if done:
            break
        backend.run_animation_ticks()
    assert done == [1]
    if transition["transition"] == "scale":
        assert "BASE" not in _rows(panel, backend)[0]
