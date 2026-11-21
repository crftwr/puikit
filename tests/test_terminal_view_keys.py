"""TerminalView key encoding that needs no libghostty."""

from puikit.event import Event, EventType
from puikit.widgets.terminal_view import TerminalView


def test_a_bare_control_character_is_sent_as_is():
    # A text system that turns an unbound Ctrl chord into its C0 code, with no
    # key name, still reaches the program (xefm#900).
    view = TerminalView(["true"])
    event = Event(EventType.KEY, key="", char="\x03", modifiers=frozenset({"ctrl"}))
    assert view._encode(event) == b"\x03"


class _Term:
    def __init__(self):
        self.scrolled = []

    def mouse_tracking(self):
        return False

    def scroll(self, rows):
        self.scrolled.append(rows)


def _scrolling_view():
    view = TerminalView(["true"])
    view._term = _Term()
    view._pitch = 20.0
    view._scrollbar_mouse = lambda event: False
    view._scrolled = lambda: None
    return view


def test_a_trackpad_scrolls_by_distance_not_by_event_count():
    view = _scrolling_view()
    # Ten small events of 0.5 rows each (10 units at a 20-unit row pitch),
    # every one also flagged scroll=1 for notch-only widgets.
    for _ in range(10):
        view._mouse(Event(EventType.MOUSE_SCROLL, scroll=1, hints={"scroll_units": 10.0}))
    assert sum(view._term.scrolled) == -5


def test_a_wheel_notch_scrolls_three_rows():
    view = _scrolling_view()
    view._mouse(Event(EventType.MOUSE_SCROLL, scroll=-1))
    assert view._term.scrolled == [3]
