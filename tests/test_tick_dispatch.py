"""Tests for the fault-isolated animation-tick dispatch (_run_tick_callbacks).

One raising callback used to abort the whole frame: the survivor list was
never assigned (so nothing unregistered, the raiser included), every callback
after it was skipped, and on timer-driven backends the swallowed exception
then repeated every frame forever (xefm#333)."""

from puikit.backend import _run_tick_callbacks
from puikit.backends.memory_backend import MemoryBackend


def test_true_survives_false_drops():
    calls = []

    def stays():
        calls.append("stays")
        return True

    def done():
        calls.append("done")
        return False

    assert _run_tick_callbacks([stays, done]) == [stays]
    assert calls == ["stays", "done"]


def test_raising_callback_is_dropped_and_later_ones_still_run(caplog):
    calls = []

    def bad():
        raise RuntimeError("boom")

    def after():
        calls.append("after")
        return True

    survivors = _run_tick_callbacks([bad, after])
    assert survivors == [after]  # the raiser is dropped, the frame still ran
    assert calls == ["after"]
    assert "animation tick callback" in caplog.text


def test_callback_registered_mid_frame_is_visited_same_pass():
    # A callback that registers another during its run (a spinner whose draw
    # registers its own tick inside a render) appends to the live list; the
    # same pass must reach it, as the old comprehension's semantics did.
    live = []
    calls = []

    def second():
        calls.append("second")
        return True

    def first():
        calls.append("first")
        live.append(second)
        return False

    live.append(first)
    assert _run_tick_callbacks(live) == [second]
    assert calls == ["first", "second"]


def test_memory_backend_ticks_survive_a_raising_callback():
    backend = MemoryBackend(width=10, height=4)
    calls = []

    def bad():
        raise RuntimeError("boom")

    def good():
        calls.append(1)
        return True

    backend.request_animation_ticks(bad)
    backend.request_animation_ticks(good)
    backend.run_animation_ticks()
    backend.run_animation_ticks()
    assert calls == [1, 1]  # the healthy callback kept running
    assert bad not in backend.tick_callbacks  # the raiser was unregistered
