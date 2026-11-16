"""TerminalView key encoding that needs no libghostty."""

from puikit.event import Event, EventType
from puikit.widgets.terminal_view import TerminalView


def test_a_bare_control_character_is_sent_as_is():
    # A text system that turns an unbound Ctrl chord into its C0 code, with no
    # key name, still reaches the program (xefm#900).
    view = TerminalView(["true"])
    event = Event(EventType.KEY, key="", char="\x03", modifiers=frozenset({"ctrl"}))
    assert view._encode(event) == b"\x03"
