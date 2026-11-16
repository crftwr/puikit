"""TerminalView's synchronized-output hold, and how long it outlasts its end
on a pty whose frames lag the program's marks (ConPTY), without libghostty."""

import time

from puikit.widgets.terminal_view import TerminalView


class _Term:
    """Feeds the view's hold callback from markers in the written bytes."""

    def __init__(self, view):
        self.view = view

    def write(self, data: bytes) -> None:
        if b"<h>" in data:
            self.view._render_hold(True)
        if b"<l>" in data:
            self.view._render_hold(False)


class _Proc:
    def __init__(self, settle):
        self.sync_settle_seconds = settle


def _view(settle):
    view = TerminalView(["true"])
    view._term = _Term(view)
    view._proc = _Proc(settle)
    return view


def _feed(view, data: bytes) -> None:
    view._pending.append(data)
    view._drain()


def test_the_hold_ends_with_its_mark_where_frames_come_as_written():
    view = _view(0.0)
    _feed(view, b"<h>clear")
    assert view._held_since is not None
    _feed(view, b"prompt<l>")
    assert view._held_since is None


def test_on_conpty_the_hold_lasts_until_the_redraw_after_its_mark():
    # Conhost: the cleared line inside the hold, its end mark, and only then
    # the prompt - which a frame in between showed blank (eosh flicker).
    view = _view(0.5)
    _feed(view, b"<h>clear<l>")
    assert view._held_since is not None and view._settle_until is not None
    _feed(view, b"prompt")
    assert view._held_since is None and view._settle_until is None


def test_on_conpty_a_hold_with_nothing_after_it_ends_by_itself():
    view = _view(0.01)
    _feed(view, b"<h>whole frame<l>")
    assert view._held_since is not None
    time.sleep(0.02)
    # draw() ends an expired settle before it decides what to show.
    assert view._settle_until is not None and time.monotonic() >= view._settle_until


def test_a_new_hold_during_the_settle_starts_over():
    view = _view(0.5)
    _feed(view, b"<h>a<l>")
    _feed(view, b"<h>b")
    assert view._held_since is not None and view._settle_until is None
