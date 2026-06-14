"""Tests for the interactive widgets, run against TUI and GUI profiles alike."""

import pytest

from puikit import Event, EventType, Panel, PROFILE_GUI_DESKTOP, PROFILE_TUI, TextAttribute
from puikit.backends.memory_backend import MemoryBackend
from puikit.widgets import (
    Checkbox,
    DropDown,
    Label,
    RadioGroup,
    ScrollView,
    TextEdit,
)


@pytest.fixture(params=[PROFILE_TUI, PROFILE_GUI_DESKTOP], ids=["tui", "gui"])
def backend(request):
    return MemoryBackend(width=30, height=12, capabilities=request.param)


def _key(name, char=None, modifiers=frozenset()):
    return Event(type=EventType.KEY, key=name, char=char, modifiers=modifiers)


# --- Checkbox ----------------------------------------------------------------


def test_checkbox_renders_state_and_focus_cue(backend):
    panel = Panel(backend)
    box = Checkbox("Enable", checked=True)
    panel.add(box, x=0, y=0, w=12, h=1)
    panel.render()
    assert backend.snapshot()[0].startswith("[x] Enable")
    # The single focusable widget holds focus, so its mark is reversed.
    assert backend.style_at(0, 0).attr & TextAttribute.REVERSE


def test_checkbox_unfocused_mark_is_plain(backend):
    panel = Panel(backend)
    first = Checkbox("first")
    second = Checkbox("second")
    panel.add(first, x=0, y=0, w=12, h=1)
    panel.add(second, x=0, y=1, w=12, h=1)
    panel.focus(first)
    panel.render()
    assert not backend.style_at(0, 1).attr & TextAttribute.REVERSE  # second unfocused


def test_checkbox_toggles_on_space_enter_and_click(backend):
    changes = []
    panel = Panel(backend)
    box = Checkbox("x", on_change=changes.append)
    panel.add(box, x=0, y=0, w=12, h=1)
    panel.dispatch_event(_key("space", char=" "))
    assert box.checked is True
    panel.dispatch_event(_key("enter"))
    assert box.checked is False
    panel.dispatch_event(Event(type=EventType.MOUSE_CLICK, x=1, y=0, button="left"))
    assert box.checked is True
    assert changes == [True, False, True]


# --- RadioGroup --------------------------------------------------------------


def test_radiogroup_arrow_keys_move_selection(backend):
    changes = []
    panel = Panel(backend)
    group = RadioGroup(["a", "b", "c"], selected=0, on_change=lambda i, t: changes.append(t))
    panel.add(group, x=0, y=0, w=12, h=3)
    panel.dispatch_event(_key("down"))
    assert group.selected == 1
    panel.dispatch_event(_key("down"))
    panel.dispatch_event(_key("down"))  # clamped at the last option
    assert group.selected == 2
    panel.dispatch_event(_key("up"))
    assert group.selected == 1
    # down->b, down->c, down (clamped, no fire), up->b
    assert changes == ["b", "c", "b"]


def test_radiogroup_click_selects_row(backend):
    panel = Panel(backend)
    group = RadioGroup(["a", "b", "c"])
    panel.add(group, x=0, y=0, w=12, h=3)
    panel.dispatch_event(Event(type=EventType.MOUSE_CLICK, x=1, y=2, button="left"))
    assert group.selected == 2


def test_radiogroup_renders_marks(backend):
    panel = Panel(backend)
    group = RadioGroup(["a", "b"], selected=1)
    panel.add(group, x=0, y=0, w=12, h=2)
    panel.render()
    lines = backend.snapshot()
    assert lines[0].startswith("( ) a")
    assert lines[1].startswith("(•) b")


# --- DropDown ----------------------------------------------------------------


def test_dropdown_opens_grows_and_commits(backend):
    changes = []
    panel = Panel(backend)
    dd = DropDown(["Red", "Green", "Blue"], on_change=lambda i, t: changes.append(t))
    panel.add(dd, x=0, y=0, w=22, h=8)
    assert dd.view_height() == 1
    panel.dispatch_event(_key("enter"))  # open
    assert dd.open is True
    assert dd.view_height() == 1 + 3
    panel.dispatch_event(_key("down"))  # cursor 0 -> 1
    panel.dispatch_event(_key("enter"))  # commit Green
    assert dd.open is False
    assert dd.selected == 1
    assert changes == ["Green"]


def test_dropdown_escape_closes_without_change(backend):
    panel = Panel(backend)
    dd = DropDown(["Red", "Green"], on_change=lambda i, t: pytest.fail("should not commit"))
    panel.add(dd, x=0, y=0, w=22, h=8)
    panel.dispatch_event(_key("down"))  # open via down
    assert dd.open is True
    panel.dispatch_event(_key("escape"))
    assert dd.open is False
    assert dd.selected == 0


def test_dropdown_click_opens_then_selects_option(backend):
    panel = Panel(backend)
    dd = DropDown(["Red", "Green", "Blue"])
    panel.add(dd, x=0, y=0, w=22, h=8)
    panel.dispatch_event(Event(type=EventType.MOUSE_CLICK, x=1, y=0, button="left"))
    assert dd.open is True
    # Row 2 on screen is option index 1 (header occupies row 0).
    panel.dispatch_event(Event(type=EventType.MOUSE_CLICK, x=2, y=2, button="left"))
    assert dd.open is False
    assert dd.selected == 1


# --- TextEdit ----------------------------------------------------------------


def test_textedit_inserts_and_deletes(backend):
    edits = []
    panel = Panel(backend)
    field = TextEdit("ab", on_change=edits.append)
    panel.add(field, x=0, y=0, w=12, h=1)
    assert field.cursor == 2
    panel.dispatch_event(_key("c", char="c"))
    assert field.text == "abc"
    panel.dispatch_event(_key("home"))
    panel.dispatch_event(_key("X", char="X"))
    assert field.text == "Xabc"
    assert field.cursor == 1
    panel.dispatch_event(_key("backspace"))
    assert field.text == "abc"
    panel.dispatch_event(_key("delete"))
    assert field.text == "bc"
    assert edits[-1] == "bc"


def test_textedit_cursor_movement_keys(backend):
    panel = Panel(backend)
    field = TextEdit("hello")
    panel.add(field, x=0, y=0, w=12, h=1)
    panel.dispatch_event(_key("home"))
    assert field.cursor == 0
    panel.dispatch_event(_key("right"))
    assert field.cursor == 1
    panel.dispatch_event(_key("end"))
    assert field.cursor == 5
    panel.dispatch_event(_key("left"))
    assert field.cursor == 4


def test_textedit_space_inserts_not_activates(backend):
    panel = Panel(backend)
    field = TextEdit("a")
    panel.add(field, x=0, y=0, w=12, h=1)
    panel.dispatch_event(_key("space", char=" "))
    assert field.text == "a "


# --- ScrollView --------------------------------------------------------------


def _scroller():
    cb1, cb2 = Checkbox("one"), Checkbox("two")
    items = [
        (Label("head"), 1),
        (cb1, 1),
        (cb2, 1),
        (Label("filler a"), 1),
        (Label("filler b"), 1),
        (Label("filler c"), 1),
        (Label("filler d"), 1),
    ]
    return ScrollView(items, gap=1), cb1, cb2


def test_scrollview_scrolls_and_clamps(backend):
    panel = Panel(backend)
    scroller, _, _ = _scroller()
    panel.add(scroller, x=0, y=0, w=20, h=5)
    panel.render()
    assert scroller.offset == 0.0
    panel.dispatch_event(Event(type=EventType.MOUSE_SCROLL, x=1, y=1, scroll=-3))
    assert scroller.offset == 3.0
    panel.dispatch_event(Event(type=EventType.MOUSE_SCROLL, x=1, y=1, scroll=99))
    assert scroller.offset == 0.0  # clamped back to the top


def test_scrollview_tab_cycles_focus_with_wraparound(backend):
    panel = Panel(backend)
    scroller, cb1, cb2 = _scroller()
    panel.add(scroller, x=0, y=0, w=20, h=5)
    assert scroller._focused is cb1
    panel.dispatch_event(_key("tab"))
    assert scroller._focused is cb2
    panel.dispatch_event(_key("tab"))
    assert scroller._focused is cb1  # wrapped around
    panel.dispatch_event(_key("tab", modifiers=frozenset({"shift"})))
    assert scroller._focused is cb2  # shift+tab goes backward


def test_scrollview_mouse_click_routes_with_offset(backend):
    panel = Panel(backend)
    scroller, cb1, cb2 = _scroller()
    panel.add(scroller, x=0, y=0, w=20, h=5)
    panel.render()
    # cb1 sits at content y=2 (head row 0, gap row 1). Click it directly.
    panel.dispatch_event(Event(type=EventType.MOUSE_CLICK, x=1, y=2, button="left"))
    assert cb1.checked is True
    # Scroll down by 2, then the same screen row 2 lands on a later widget.
    panel.dispatch_event(Event(type=EventType.MOUSE_SCROLL, x=1, y=1, scroll=-2))
    panel.render()
    panel.dispatch_event(Event(type=EventType.MOUSE_CLICK, x=1, y=2, button="left"))
    assert cb2.checked is True  # content y = 2 + offset(2) = 4 -> cb2


def test_scrollview_focused_child_shows_focus_cue(backend):
    panel = Panel(backend)
    scroller, cb1, cb2 = _scroller()
    panel.add(scroller, x=0, y=0, w=20, h=8)
    panel.render()
    # cb1 is the focused child of the focused ScrollView: its mark is reversed.
    # cb1 is at content/screen y=2.
    assert backend.style_at(0, 2).attr & TextAttribute.REVERSE
    assert not backend.style_at(0, 4).attr & TextAttribute.REVERSE  # cb2 not focused
