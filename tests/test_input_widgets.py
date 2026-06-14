"""Tests for the interactive widgets, run against TUI and GUI profiles alike."""

import pytest

from puikit import Event, EventType, Panel, PROFILE_GUI_DESKTOP, PROFILE_TUI, TextAttribute
from puikit.backends.memory_backend import MemoryBackend
from puikit.widgets import (
    Button,
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
    # The single focusable widget holds focus: its mark gets the accent ring.
    assert backend.style_at(0, 0).bg == panel.theme.accent


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


def test_dropdown_opens_as_layer_and_commits(backend):
    changes = []
    panel = Panel(backend)
    dd = DropDown(["Red", "Green", "Blue"], on_change=lambda i, t: changes.append(t))
    panel.add(dd, x=0, y=0, w=22, h=1)
    panel.render()  # let the field capture panel + screen_rect
    assert dd.view_height() == 1  # the field never grows; the popup floats
    panel.dispatch_event(_key("enter"))  # open -> pushes a popup layer
    assert dd.open is True
    assert len(panel._layers) == 1
    panel.dispatch_event(_key("down"))  # popup cursor 0 -> 1
    panel.dispatch_event(_key("enter"))  # commit Green
    assert dd.open is False
    assert dd.selected == 1
    assert changes == ["Green"]
    assert panel._layers == []  # popup popped


def test_dropdown_escape_closes_without_change(backend):
    panel = Panel(backend)
    dd = DropDown(["Red", "Green"], on_change=lambda i, t: pytest.fail("should not commit"))
    panel.add(dd, x=0, y=0, w=22, h=1)
    panel.render()
    panel.dispatch_event(_key("down"))  # open via down
    assert dd.open is True
    panel.dispatch_event(_key("escape"))
    assert dd.open is False
    assert dd.selected == 0
    assert panel._layers == []


def test_dropdown_click_opens_then_selects_option(backend):
    panel = Panel(backend)
    dd = DropDown(["Red", "Green", "Blue"])
    panel.add(dd, x=0, y=0, w=22, h=1)
    panel.render()
    panel.dispatch_event(Event(type=EventType.MOUSE_CLICK, x=1, y=0, button="left"))
    assert dd.open is True
    panel.render()  # the popup draws and captures its width for hit-testing
    # The popup layer sits at y=1; option index 1 is its local row 1.
    panel.dispatch_event(Event(type=EventType.MOUSE_CLICK, x=2, y=2, button="left"))
    assert dd.open is False
    assert dd.selected == 1
    assert panel._layers == []


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


def test_textedit_ime_composition_then_commit(backend):
    # Marked (preedit) text is shown without touching the buffer; a committed
    # character (the shape insertText: produces) clears it and inserts.
    panel = Panel(backend)
    field = TextEdit("")
    panel.add(field, x=0, y=0, w=16, h=1)
    panel.dispatch_event(
        Event(type=EventType.IME_COMPOSITION, hints={"preedit": "に", "caret": 1})
    )
    assert field._preedit == "に"
    assert field.text == ""  # buffer untouched while composing
    panel.dispatch_event(_key("本", char="本"))  # commit
    assert field.text == "本"
    assert field._preedit == ""


def test_textedit_requests_input_position_when_focused(backend):
    # While focused the field reports its caret to the backend (drives the IME
    # candidate window). The memory backend records the call via Panel.
    calls = []
    backend.request_text_input = lambda x, y, hints: calls.append((x, y))
    panel = Panel(backend)
    field = TextEdit("hi")
    panel.add(field, x=2, y=1, w=12, h=1)
    panel.render()
    assert calls  # at least one caret report from the focused field


# --- hover / accent ----------------------------------------------------------


def test_pointer_hover_tints_checkbox_row(backend):
    panel = Panel(backend)
    box = Checkbox("hover me")
    panel.add(box, x=0, y=0, w=12, h=1)
    panel.dispatch_event(Event(type=EventType.MOUSE_MOVE, x=3, y=0))
    panel.render()
    assert backend.style_at(5, 0).bg == panel.theme.hover_bg


def test_mouse_move_updates_pointer_but_is_not_consumed(backend):
    panel = Panel(backend)
    panel.add(Checkbox("x"), x=0, y=0, w=8, h=1)
    consumed = panel.dispatch_event(Event(type=EventType.MOUSE_MOVE, x=2, y=0))
    assert consumed is False
    assert panel.pointer == (2, 0)


def test_button_focus_ring_and_hover(backend):
    panel = Panel(backend)
    btn = Button("OK")
    panel.add(btn, x=0, y=0, w=10, h=1)
    panel.render()
    # Focused single-row button: accent fill, label underlined.
    assert backend.style_at(0, 0).bg == panel.theme.button_bg
    # Hover lightens the fill.
    panel.dispatch_event(Event(type=EventType.MOUSE_MOVE, x=3, y=0))
    panel.render()
    assert backend.style_at(0, 0).bg == panel.theme.button_hover_bg


def test_dropdown_outside_click_dismisses(backend):
    panel = Panel(backend)
    dd = DropDown(["Red", "Green"])
    panel.add(dd, x=0, y=0, w=22, h=1)
    panel.render()
    panel.dispatch_event(_key("down"))  # open
    assert dd.open is True
    panel.render()
    # A click well outside the popup's rows cancels it without changing value.
    panel.dispatch_event(Event(type=EventType.MOUSE_CLICK, x=2, y=15, button="left"))
    assert dd.open is False
    assert dd.selected == 0
    assert panel._layers == []


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
    # cb1 is the focused child of the focused ScrollView: its mark gets the
    # accent ring. cb1 is at content/screen y=2.
    assert backend.style_at(0, 2).bg == panel.theme.accent
    assert backend.style_at(0, 4).bg != panel.theme.accent  # cb2 not focused
