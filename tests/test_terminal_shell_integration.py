"""Shell integration through libghostty (xefm#900): semantic prompt marks
(OSC 133), whether the cursor sits at the prompt, notifications (OSC 9 /
777) and progress (OSC 9;4) - on the real library where it loads, and the
view's pass-through without it."""

import pytest

from puikit import _ghostty as G
from puikit.widgets.terminal_view import TerminalView

needs_lib = pytest.mark.skipif(G.load() is None, reason="libghostty-vt not available")


def _terminal(events):
    return G.Terminal(
        40, 10, write_pty=lambda b: None,
        on_semantic_prompt=lambda *a: events.append(("prompt", *a)),
        on_notification=lambda *a: events.append(("notify", *a)),
        on_progress=lambda *a: events.append(("progress", *a)))


@needs_lib
def test_a_command_s_steps_and_the_prompt_state():
    events = []
    t = _terminal(events)
    try:
        assert not t.at_prompt()  # no marks yet: unknown counts as no
        t.write(b"\x1b]133;A\x07$ \x1b]133;B\x07")
        assert t.at_prompt()
        t.write(b"make\r\n\x1b]133;C;cmdline=make\x07building\r\n")
        assert not t.at_prompt()
        t.write(b"\x1b]133;D;2\x07")
        assert events == [("prompt", G.PROMPT_START, None, ""),
                          ("prompt", G.INPUT_START, None, ""),
                          ("prompt", G.OUTPUT_START, None, "make"),
                          ("prompt", G.COMMAND_END, 2, "")]
    finally:
        t.close()


@needs_lib
def test_the_alternate_screen_is_never_at_the_prompt():
    t = _terminal([])
    try:
        t.write(b"\x1b]133;A\x07$ \x1b]133;B\x07")
        assert t.at_prompt()
        t.write(b"\x1b[?1049h")
        assert not t.at_prompt()
    finally:
        t.close()


@needs_lib
def test_notifications_and_progress():
    events = []
    t = _terminal(events)
    try:
        t.write(b"\x1b]9;done\x07\x1b]777;notify;Build;ok\x07")
        t.write(b"\x1b]9;4;1;42\x07\x1b]9;4;3\x07\x1b]9;4;0\x07")
        assert events == [("notify", "", "done"), ("notify", "Build", "ok"),
                          ("progress", G.PROGRESS_SET, 42),
                          ("progress", G.PROGRESS_INDETERMINATE, None),
                          ("progress", G.PROGRESS_REMOVE, None)]
    finally:
        t.close()


def test_the_view_passes_them_on_and_knows_no_prompt_without_a_terminal():
    seen = []
    view = TerminalView(["true"], on_prompt=lambda *a: seen.append(a),
                        on_notification=lambda *a: seen.append(a),
                        on_progress=lambda *a: seen.append(a))
    view._prompt_event(G.COMMAND_END, 0, "")
    view._notification("t", "b")
    view._progress(G.PROGRESS_SET, 10)
    assert seen == [(G.COMMAND_END, 0, ""), ("t", "b"), (G.PROGRESS_SET, 10)]
    assert view.at_prompt is False


def test_an_exited_child_is_not_at_a_prompt():
    class _T:
        def at_prompt(self):
            return True
    view = TerminalView(["true"])
    view._term = _T()
    assert view.at_prompt is True
    view._exited = 0
    assert view.at_prompt is False
