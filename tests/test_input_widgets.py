"""Tests for the interactive widgets, run against TUI and GUI profiles alike."""

import pytest

from puikit import Event, EventType, Panel, PROFILE_GUI_DESKTOP, PROFILE_TUI, TextAttribute
from puikit.backends.memory_backend import MemoryBackend
from puikit.widgets.button import _FOCUS_RING
from puikit.widgets import (
    Button,
    Checkbox,
    ComboBox,
    DropDown,
    Label,
    RadioGroup,
    ScrollView,
    TextBlock,
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


def test_checkbox_hit_region_limited_to_content(backend):
    # In a slot wider than the control, a click in the empty space to the right
    # of the mark + label is ignored; only the control itself is clickable.
    changes = []
    box = Checkbox("Feature", on_change=changes.append)
    panel = Panel(backend)
    panel.add(box, x=0, y=0, w=40, h=1)
    panel.render()  # draw captures the content width
    panel.dispatch_event(Event(type=EventType.MOUSE_CLICK, x=30, y=0, button="left"))
    assert box.checked is False and changes == []   # empty area: no toggle
    panel.dispatch_event(Event(type=EventType.MOUSE_CLICK, x=2, y=0, button="left"))
    assert box.checked is True                       # on the control: toggles


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


def test_textedit_wide_chars_advance_two_columns(backend):
    # Wide (CJK) glyphs occupy two display columns, so the second glyph is
    # placed two cells along, not one — no overlap.
    panel = Panel(backend)
    field = TextEdit("あい", width=12)
    panel.add(field, x=0, y=0, w=12, h=1)
    panel.render()
    line = backend.snapshot()[0]
    assert line[1] == "あ"
    assert line[3] == "い"  # placed at column 1 + width("あ")=2
    assert line[2] == " "   # the wide glyph's second cell, left blank on the grid


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


def test_textedit_input_position_keeps_fractional_row():
    # A field laid out at a fractional row origin (a dialog centers it by a
    # fraction of a row on a pixel backend) must report a fractional caret y, so
    # the IME candidate window aligns with the field's bottom edge rather than
    # rounding down a whole row. Drives _notify_input_position directly with a
    # fractional screen_rect so no pixel-layout backend is needed.
    from types import SimpleNamespace

    calls = []
    ctx = SimpleNamespace(
        panel=SimpleNamespace(request_text_input=lambda x, y, hints: calls.append((x, y))),
        screen_rect=(2.0, 3.25, 12.0, 1.0),
    )
    TextEdit("hi")._notify_input_position(ctx, anchor_col=0.0, field_h=1.0)
    (_x, y), = calls
    assert y == 3.25  # sy + field_h - 1, not int() -> 3


def test_textedit_input_position_stays_fixed_while_composition_grows(backend):
    # The IME candidate window must anchor to where composition *started*, not
    # chase the in-progress preedit caret — native apps (Notepad, VS Code) keep
    # the candidate window stationary for the duration of one composition;
    # feeding the backend the moving caret instead makes the window visibly
    # jitter rightward as a word gets longer.
    calls = []
    backend.request_text_input = lambda x, y, hints: calls.append((x, y))
    panel = Panel(backend)
    field = TextEdit("")
    panel.add(field, x=2, y=1, w=16, h=1)
    panel.render()  # draw() is what reports the caret position, via _notify_input_position
    panel.dispatch_event(
        Event(type=EventType.IME_COMPOSITION, hints={"preedit": "に", "caret": 1})
    )
    panel.render()
    panel.dispatch_event(
        Event(type=EventType.IME_COMPOSITION, hints={"preedit": "にほ", "caret": 2})
    )
    panel.render()
    panel.dispatch_event(
        Event(type=EventType.IME_COMPOSITION, hints={"preedit": "にほん", "caret": 3})
    )
    panel.render()
    xs = {x for x, _y in calls}
    assert len(xs) == 1  # the anchor never moved as the preedit grew


def test_textedit_input_position_follows_target_clause(backend):
    # Once a multi-clause conversion is underway (space pressed, then the user
    # cycles clauses with left/right), the candidate window must follow the
    # clause currently selected for conversion — carried as "target_start" in
    # the IME_COMPOSITION hints (GCS_COMPATTR on Windows, a nonzero-length
    # selectedRange on macOS).
    calls = []
    backend.request_text_input = lambda x, y, hints: calls.append((x, y))
    panel = Panel(backend)
    field = TextEdit("")
    panel.add(field, x=2, y=1, w=16, h=1)
    panel.dispatch_event(
        Event(
            type=EventType.IME_COMPOSITION,
            hints={"preedit": "にほんご", "caret": 2, "target_start": 0},
        )
    )
    panel.render()
    first_call = calls[-1]
    panel.dispatch_event(
        Event(
            type=EventType.IME_COMPOSITION,
            hints={"preedit": "にほんご", "caret": 4, "target_start": 2},
        )
    )
    panel.render()
    second_call = calls[-1]
    assert first_call != second_call  # the anchor moved to the new target clause


def test_blink_tick_retires_when_field_leaves_the_tree():
    # The caret-blink tick re-renders to advance the blink phase. When the field
    # leaves the widget tree (its dialog closed) draw stops running, so a stale
    # _focused_now would keep the tick re-rendering forever — a CPU-burning loop
    # leaked per dialog open/close. The tick must retire instead. (Driven directly:
    # the blink only *registers* on a vector backend, but the retire logic — clear
    # _focused_now, render, keep only if draw re-set it — is backend-independent.)
    backend = MemoryBackend(width=20, height=6)
    panel = Panel(backend)
    field = TextEdit("hi")
    panel.add(field, x=1, y=1, w=10, h=1)
    panel.render()  # draws the field focused -> _focused_now True
    assert field._focused_now is True
    field._blinking = True  # simulate the registered blink

    # Still in the tree: the tick re-renders, draw re-sets the flag, keeps ticking.
    assert field._blink_tick() is True
    assert field._focused_now is True

    # Dialog closes — the field is no longer drawn: the tick retires.
    panel.remove(field)
    assert field._blink_tick() is False
    assert field._blinking is False


def test_textedit_shift_arrow_selects_and_typing_replaces(backend):
    panel = Panel(backend)
    field = TextEdit("hello")
    panel.add(field, x=0, y=0, w=12, h=1)
    panel.dispatch_event(_key("home"))
    panel.dispatch_event(_key("right", modifiers=frozenset({"shift"})))
    panel.dispatch_event(_key("right", modifiers=frozenset({"shift"})))
    assert field.selection_text == "he"
    panel.dispatch_event(_key("X", char="X"))  # typing replaces the selection
    assert field.text == "Xllo"
    assert field.selection_text == ""  # selection cleared after the edit


def test_textedit_backspace_deletes_selection(backend):
    panel = Panel(backend)
    field = TextEdit("hello")
    panel.add(field, x=0, y=0, w=12, h=1)
    panel.dispatch_event(_key("home"))
    for _ in range(3):
        panel.dispatch_event(_key("right", modifiers=frozenset({"shift"})))
    assert field.selection_text == "hel"
    panel.dispatch_event(_key("backspace"))
    assert field.text == "lo"
    assert field.cursor == 0


def test_textedit_select_all_then_replace(backend):
    panel = Panel(backend)
    field = TextEdit("hello")
    panel.add(field, x=0, y=0, w=12, h=1)
    panel.dispatch_event(_key("a", char="a", modifiers=frozenset({"cmd"})))
    assert field.selection_text == "hello"
    panel.dispatch_event(_key("Z", char="Z"))
    assert field.text == "Z"  # whole field replaced, "a" never typed in


def test_textedit_plain_arrow_collapses_selection(backend):
    panel = Panel(backend)
    field = TextEdit("hello")
    panel.add(field, x=0, y=0, w=12, h=1)
    panel.dispatch_event(_key("a", char="a", modifiers=frozenset({"cmd"})))
    panel.dispatch_event(_key("left"))  # collapse to selection start
    assert field.selection_text == ""
    assert field.cursor == 0


def test_textedit_mouse_drag_selects(backend):
    panel = Panel(backend)
    field = TextEdit("hello", width=12)
    panel.add(field, x=0, y=0, w=12, h=1)
    panel.render()
    # Press at column 1 (buffer index 0), drag to column 4 (index 3). The press
    # is a MOUSE_DOWN (the click only fires on release, over the same widget).
    panel.dispatch_event(Event(type=EventType.MOUSE_DOWN, x=1, y=0, button="left"))
    assert field.selection_text == ""
    panel.dispatch_event(Event(type=EventType.MOUSE_DRAG, x=4, y=0, button="left"))
    assert field.selection_text == "hel"


def test_textedit_double_click_selects_word(backend):
    panel = Panel(backend)
    field = TextEdit("foo bar baz", width=16)
    panel.add(field, x=0, y=0, w=16, h=1)
    panel.render()
    # Two presses in place on "bar" grab the whole word.
    panel.dispatch_event(Event(type=EventType.MOUSE_DOWN, x=6, y=0, button="left"))
    panel.dispatch_event(Event(type=EventType.MOUSE_DOWN, x=6, y=0, button="left"))
    assert field.selection_text == "bar"


def test_textedit_triple_click_selects_all(backend):
    panel = Panel(backend)
    field = TextEdit("foo bar baz", width=16)
    panel.add(field, x=0, y=0, w=16, h=1)
    panel.render()
    for _ in range(3):
        panel.dispatch_event(Event(type=EventType.MOUSE_DOWN, x=6, y=0, button="left"))
    assert field.selection_text == "foo bar baz"


def test_textedit_double_click_drag_extends_by_word(backend):
    panel = Panel(backend)
    field = TextEdit("foo bar baz", width=16)
    panel.add(field, x=0, y=0, w=16, h=1)
    panel.render()
    # Double-click "foo", then drag into "baz": whole-word edges are kept, so the
    # selection spans all three words even though the drag ends mid-word.
    panel.dispatch_event(Event(type=EventType.MOUSE_DOWN, x=2, y=0, button="left"))
    panel.dispatch_event(Event(type=EventType.MOUSE_DOWN, x=2, y=0, button="left"))
    assert field.selection_text == "foo"
    panel.dispatch_event(Event(type=EventType.MOUSE_DRAG, x=10, y=0, button="left"))
    assert field.selection_text == "foo bar baz"


def test_textedit_outside_press_drag_in_does_not_select(backend):
    panel = Panel(backend)
    field = TextEdit("abcdef", width=10)
    # Leave columns 0-2 as empty panel space to the left of the field.
    panel.add(field, x=3, y=0, w=10, h=1)
    panel.render()
    # Press on empty space, then drag across the field: not a selection gesture.
    panel.dispatch_event(Event(type=EventType.MOUSE_DOWN, x=0, y=0, button="left"))
    panel.dispatch_event(Event(type=EventType.MOUSE_DRAG, x=5, y=0, button="left"))
    panel.dispatch_event(Event(type=EventType.MOUSE_DRAG, x=9, y=0, button="left"))
    assert field.selection_text == ""


def test_textedit_release_ends_gesture_so_later_drag_is_ignored(backend):
    panel = Panel(backend)
    field = TextEdit("abcdef", width=10)
    panel.add(field, x=3, y=0, w=10, h=1)
    panel.render()
    panel.dispatch_event(Event(type=EventType.MOUSE_DOWN, x=4, y=0, button="left"))
    panel.dispatch_event(Event(type=EventType.MOUSE_DRAG, x=6, y=0, button="left"))
    assert field.selection_text == "ab"
    panel.dispatch_event(Event(type=EventType.MOUSE_UP, x=6, y=0, button="left"))
    # A press on empty space then a drag back into the field after the release
    # must not resume extending the earlier selection.
    panel.dispatch_event(Event(type=EventType.MOUSE_DOWN, x=0, y=0, button="left"))
    panel.dispatch_event(Event(type=EventType.MOUSE_DRAG, x=9, y=0, button="left"))
    assert field.selection_text == "ab"


def test_textedit_copy_and_paste(backend):
    panel = Panel(backend)
    field = TextEdit("hello", width=12)
    panel.add(field, x=0, y=0, w=12, h=1)
    panel.render()  # let the field capture its panel for clipboard access
    panel.dispatch_event(_key("a", char="a", modifiers=frozenset({"cmd"})))  # select all
    panel.dispatch_event(_key("c", char="c", modifiers=frozenset({"cmd"})))  # copy
    assert panel.get_clipboard() == "hello"
    panel.dispatch_event(_key("end"))  # collapse selection to the end
    panel.dispatch_event(_key("v", char="v", modifiers=frozenset({"cmd"})))  # paste
    assert field.text == "hellohello"


def test_textedit_cut_removes_selection_to_clipboard(backend):
    panel = Panel(backend)
    field = TextEdit("hello", width=12)
    panel.add(field, x=0, y=0, w=12, h=1)
    panel.render()
    panel.dispatch_event(_key("home"))
    for _ in range(3):
        panel.dispatch_event(_key("right", modifiers=frozenset({"shift"})))
    panel.dispatch_event(_key("x", char="x", modifiers=frozenset({"cmd"})))  # cut "hel"
    assert field.text == "lo"
    assert panel.get_clipboard() == "hel"


def test_textedit_paste_flattens_newlines(backend):
    panel = Panel(backend)
    field = TextEdit("", width=20)
    panel.add(field, x=0, y=0, w=20, h=1)
    panel.render()
    panel.set_clipboard("a\nb\r\nc")
    panel.dispatch_event(_key("v", char="v", modifiers=frozenset({"cmd"})))
    assert field.text == "a b c"  # single-line field flattens newlines


def test_textedit_selection_renders_highlight(backend):
    panel = Panel(backend)
    field = TextEdit("hello", width=12)
    panel.add(field, x=0, y=0, w=12, h=1)
    panel.dispatch_event(_key("home"))
    panel.dispatch_event(_key("right", modifiers=frozenset({"shift"})))
    panel.render()
    # First glyph 'h' sits at field column 1 and is the only selected cell. The
    # field holds focus, so the selection uses the focused text-selection color.
    assert backend.style_at(1, 0).bg == panel.theme.text_selection_bg
    assert backend.style_at(2, 0).bg != panel.theme.text_selection_bg
    # Move focus away: the same selection falls back to the muted (inactive) color.
    other = TextEdit("x", width=4)
    panel.add(other, x=0, y=2, w=4, h=1)
    panel.focus(other)
    panel.render()
    assert backend.style_at(1, 0).bg == panel.theme.text_selection_inactive_bg


# --- static text selection ---------------------------------------------------


def test_label_not_selectable_by_default(backend):
    panel = Panel(backend)
    label = Label("hello")
    panel.add(label, x=0, y=0, w=12, h=1)
    assert label.focusable is False
    consumed = panel.dispatch_event(Event(type=EventType.MOUSE_CLICK, x=1, y=0, button="left"))
    assert consumed is False  # a plain label ignores the click


def test_label_drag_selects_and_copies(backend):
    panel = Panel(backend)
    label = Label("hello", selectable=True)
    panel.add(label, x=0, y=0, w=12, h=1)
    assert label.focusable is True
    panel.render()
    # The gesture starts on the raw press (MOUSE_DOWN); the click only fires on
    # release, so a drag-select must anchor at the button-down point.
    panel.dispatch_event(Event(type=EventType.MOUSE_DOWN, x=0, y=0, button="left"))
    panel.dispatch_event(Event(type=EventType.MOUSE_DRAG, x=3, y=0, button="left"))
    assert label.selection_text() == "hel"
    panel.dispatch_event(_key("c", char="c", modifiers=frozenset({"cmd"})))
    assert panel.get_clipboard() == "hel"


def test_label_select_all_and_highlight(backend):
    panel = Panel(backend)
    label = Label("hi", selectable=True)
    panel.add(label, x=0, y=0, w=12, h=1)
    panel.render()
    panel.dispatch_event(_key("a", char="a", modifiers=frozenset({"cmd"})))
    assert label.selection_text() == "hi"
    panel.render()
    # Both glyphs sit selected: their cells take the focused-selection color
    # (the label holds focus, since the Cmd+A reached it).
    assert backend.style_at(0, 0).bg == panel.theme.text_selection_bg
    assert backend.style_at(1, 0).bg == panel.theme.text_selection_bg


def test_label_selection_dims_when_focus_leaves(backend):
    panel = Panel(backend)
    label = Label("hi", selectable=True)
    other = Label("x", selectable=True)
    panel.add(label, x=0, y=0, w=12, h=1)
    panel.add(other, x=0, y=2, w=12, h=1)
    panel.render()
    panel.dispatch_event(_key("a", char="a", modifiers=frozenset({"cmd"})))
    panel.render()
    assert backend.style_at(0, 0).bg == panel.theme.text_selection_bg  # focused
    # Move focus to the other widget: the selection stays but reads as inactive.
    panel.focus_tab(1)
    panel.render()
    assert backend.style_at(0, 0).bg == panel.theme.text_selection_inactive_bg


def test_textblock_selects_across_rows(backend):
    panel = Panel(backend)
    block = TextBlock("ab\ncd", selectable=True)
    panel.add(block, x=0, y=0, w=12, h=4)
    panel.render()
    panel.dispatch_event(Event(type=EventType.MOUSE_DOWN, x=0, y=0, button="left"))
    panel.dispatch_event(Event(type=EventType.MOUSE_DRAG, x=1, y=1, button="left"))
    # Whole first row plus the first glyph of the second, copied as two lines.
    assert block.selection_text() == "ab\nc"


def test_label_double_click_selects_word(backend):
    panel = Panel(backend)
    label = Label("foo bar baz", selectable=True)
    panel.add(label, x=0, y=0, w=20, h=1)
    panel.render()
    panel.dispatch_event(Event(type=EventType.MOUSE_DOWN, x=5, y=0, button="left"))
    panel.dispatch_event(Event(type=EventType.MOUSE_DOWN, x=5, y=0, button="left"))
    assert label.selection_text() == "bar"


def test_label_triple_click_selects_whole_row(backend):
    panel = Panel(backend)
    label = Label("foo bar baz", selectable=True)
    panel.add(label, x=0, y=0, w=20, h=1)
    panel.render()
    for _ in range(3):
        panel.dispatch_event(Event(type=EventType.MOUSE_DOWN, x=5, y=0, button="left"))
    assert label.selection_text() == "foo bar baz"


def test_label_press_reseeds_anchor_not_previous_click(backend):
    panel = Panel(backend)
    label = Label("abcdef", selectable=True)
    panel.add(label, x=0, y=0, w=20, h=1)
    panel.render()
    panel.dispatch_event(Event(type=EventType.MOUSE_DOWN, x=0, y=0, button="left"))
    panel.dispatch_event(Event(type=EventType.MOUSE_DRAG, x=2, y=0, button="left"))
    assert label.selection_text() == "ab"
    # A fresh press elsewhere must not inherit the previous gesture's anchor.
    panel.dispatch_event(Event(type=EventType.MOUSE_DOWN, x=3, y=0, button="left"))
    panel.dispatch_event(Event(type=EventType.MOUSE_DRAG, x=5, y=0, button="left"))
    assert label.selection_text() == "de"


def test_label_outside_press_drag_in_does_not_select(backend):
    panel = Panel(backend)
    label = Label("abcdef", selectable=True)
    # Leave columns 0-2 as empty panel space to the left of the label.
    panel.add(label, x=3, y=0, w=10, h=1)
    panel.render()
    # Press on empty space, then drag across the label: not a selection gesture.
    # Two drag points, so a missing guard would leave a non-empty range.
    panel.dispatch_event(Event(type=EventType.MOUSE_DOWN, x=0, y=0, button="left"))
    panel.dispatch_event(Event(type=EventType.MOUSE_DRAG, x=5, y=0, button="left"))
    panel.dispatch_event(Event(type=EventType.MOUSE_DRAG, x=9, y=0, button="left"))
    assert label.selection_text() == ""


def test_label_release_ends_gesture_so_later_drag_is_ignored(backend):
    panel = Panel(backend)
    label = Label("abcdef", selectable=True)
    panel.add(label, x=3, y=0, w=10, h=1)
    panel.render()
    panel.dispatch_event(Event(type=EventType.MOUSE_DOWN, x=3, y=0, button="left"))
    panel.dispatch_event(Event(type=EventType.MOUSE_DRAG, x=5, y=0, button="left"))
    assert label.selection_text() == "ab"
    panel.dispatch_event(Event(type=EventType.MOUSE_UP, x=5, y=0, button="left"))
    # A press on empty space then a drag back into the label after the release
    # must not resume extending the earlier selection.
    panel.dispatch_event(Event(type=EventType.MOUSE_DOWN, x=0, y=0, button="left"))
    panel.dispatch_event(Event(type=EventType.MOUSE_DRAG, x=9, y=0, button="left"))
    assert label.selection_text() == "ab"


def test_textblock_double_click_drag_extends_by_word(backend):
    panel = Panel(backend)
    block = TextBlock("foo bar\nbaz qux", selectable=True)
    panel.add(block, x=0, y=0, w=20, h=4)
    panel.render()
    # Double-click "foo", then drag into the second row's "baz": whole-word
    # edges are kept across the row boundary.
    panel.dispatch_event(Event(type=EventType.MOUSE_DOWN, x=1, y=0, button="left"))
    panel.dispatch_event(Event(type=EventType.MOUSE_DOWN, x=1, y=0, button="left"))
    assert block.selection_text() == "foo"
    panel.dispatch_event(Event(type=EventType.MOUSE_DRAG, x=1, y=1, button="left"))
    assert block.selection_text() == "foo bar\nbaz"


def test_textblock_select_all_copies_all_rows(backend):
    panel = Panel(backend)
    block = TextBlock("ab\ncd", selectable=True)
    panel.add(block, x=0, y=0, w=12, h=4)
    panel.render()
    panel.dispatch_event(_key("a", char="a", modifiers=frozenset({"cmd"})))
    panel.dispatch_event(_key("c", char="c", modifiers=frozenset({"cmd"})))
    assert panel.get_clipboard() == "ab\ncd"


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
    # Focused single-row button: accent fill. On a grid the focus ring resolves
    # to bold bracket markers in the padding columns. A primary button's fill is
    # *already* the accent, so the brackets take the light fill-contrasting ring
    # (not accent-on-accent) and are drawn bold.
    assert backend.style_at(0, 0).bg == panel.theme.button_bg
    if not panel.backend.capabilities.supports("vector_shapes"):
        cell = backend.style_at(0, 0)
        assert backend.snapshot()[0].startswith("[")
        assert cell.fg == _FOCUS_RING
        assert cell.fg != panel.theme.button_bg   # contrasts the accent fill
        assert cell.attr & TextAttribute.BOLD
    # Hover lightens the fill.
    panel.dispatch_event(Event(type=EventType.MOUSE_MOVE, x=3, y=0))
    panel.render()
    assert backend.style_at(0, 0).bg == panel.theme.button_hover_bg


def test_focused_short_text_button_keeps_label_on_grid(backend):
    # A two-row focused text button must not draw a box-drawing frame on a grid
    # (its top/bottom borders would overwrite the single label row); it gets the
    # accent bracket markers instead, so the label stays visible.
    panel = Panel(backend)
    btn = Button("Apply")
    panel.add(btn, x=0, y=0, w=10, h=2)
    panel.render()
    snap = "\n".join(backend.snapshot())
    assert "Apply" in snap
    if not panel.backend.capabilities.supports("vector_shapes"):
        assert "┌" not in snap and "─" not in snap  # no box frame ate the label
        assert "[" in snap and "]" in snap          # bracket focus cue instead


def test_button_variants_use_accent_and_neutral_fills(backend):
    panel = Panel(backend)
    primary = Button("OK")                          # default variant
    secondary = Button("Cancel", variant="secondary")
    panel.add(primary, x=0, y=0, w=10, h=1)
    panel.add(secondary, x=0, y=2, w=10, h=1)
    panel.render()
    # Primary wears the accent fill; secondary wears the neutral fill, no accent.
    assert backend.style_at(0, 0).bg == panel.theme.button_bg
    assert backend.style_at(0, 2).bg == panel.theme.button_secondary_bg
    assert panel.theme.button_secondary_bg != panel.theme.button_bg


def test_button_rejects_unknown_variant():
    with pytest.raises(ValueError):
        Button("OK", variant="tertiary")


def test_button_fires_on_release_over_button(backend):
    fired = []
    panel = Panel(backend)
    btn = Button("OK", on_click=lambda: fired.append(True))
    panel.add(btn, x=0, y=0, w=10, h=1)
    panel.render()
    # Press does not fire; the release over the button does.
    panel.dispatch_event(Event(type=EventType.MOUSE_DOWN, x=2, y=0, button="left"))
    assert fired == []
    panel.dispatch_event(Event(type=EventType.MOUSE_UP, x=2, y=0, button="left"))
    assert fired == [True]


def test_press_moves_focus_and_reports_handled(backend):
    # A press must report handled so the host re-renders immediately — otherwise
    # focus moves internally but the cue only repaints on the next event.
    panel = Panel(backend)
    first, second = Button("A"), Button("B")
    panel.add(first, x=0, y=0, w=6, h=1)
    panel.add(second, x=0, y=2, w=6, h=1)
    panel.render()
    assert panel.focused is first
    handled = panel.dispatch_event(Event(type=EventType.MOUSE_DOWN, x=1, y=2, button="left"))
    assert panel.focused is second  # focus moved on press
    assert handled is True          # so the host re-renders the new focus cue


def test_button_press_cancelled_by_dragging_off(backend):
    fired = []
    panel = Panel(backend)
    btn = Button("OK", on_click=lambda: fired.append(True))
    panel.add(btn, x=0, y=0, w=10, h=1)
    panel.render()
    # Press, drag the pointer off the button, then release: no click fires.
    panel.dispatch_event(Event(type=EventType.MOUSE_DOWN, x=2, y=0, button="left"))
    panel.dispatch_event(Event(type=EventType.MOUSE_DRAG, x=40, y=0, button="left"))
    panel.dispatch_event(Event(type=EventType.MOUSE_UP, x=40, y=0, button="left"))
    assert fired == []


def test_button_pressed_cue_held_then_cleared(backend):
    from puikit.widgets.button import _darken
    panel = Panel(backend)
    btn = Button("OK")
    panel.add(btn, x=0, y=0, w=10, h=1)
    panel.render()
    base = backend.style_at(0, 0).bg
    # While pressed the fill darkens; the press anchor is captured.
    panel.dispatch_event(Event(type=EventType.MOUSE_DOWN, x=2, y=0, button="left"))
    panel.render()
    assert backend.style_at(0, 0).bg == _darken(base)
    # Dragging off clears the cue (the press is cancelled), fill returns to base.
    panel.dispatch_event(Event(type=EventType.MOUSE_DRAG, x=40, y=0, button="left"))
    panel.render()
    assert backend.style_at(0, 0).bg == base


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


@pytest.mark.parametrize(
    "make",
    [
        lambda: DropDown(["Red", "Green", "Blue"]),
        lambda: TextEdit("edit me"),
        lambda: ComboBox(["Alpha", "Beta"], text="Al"),
    ],
    ids=["dropdown", "textedit", "combobox"],
)
def test_single_row_control_focus_brackets_on_grid(backend, make):
    # On a grid the accent focus ring resolves to bold bracket markers in the
    # field's padding columns. (MemoryBackend renders the grid path under both
    # palettes; the real vector path returns early in draw_focus_brackets — see
    # test_vector_widgets.)
    panel = Panel(backend)
    spacer = Button("x")  # a second focusable widget to hold focus away
    control = make()
    panel.add(spacer, x=0, y=0, w=20, h=1)
    panel.add(control, x=0, y=1, w=24, h=1)
    panel.focus(control)
    panel.render()
    line = backend.snapshot()[1]
    cell = backend.style_at(0, 1)
    assert line.startswith("[") and line.rstrip().endswith("]")
    assert cell.fg == panel.theme.accent
    assert cell.attr & TextAttribute.BOLD
    # Blurred: no brackets.
    panel.focus(spacer)
    panel.render()
    assert not backend.snapshot()[1].startswith("[")
