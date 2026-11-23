"""TextArea: the multi-line field, against TUI and GUI profiles alike."""

import pytest

from puikit import Event, EventType, Panel, PROFILE_GUI_DESKTOP, PROFILE_TUI
from puikit.backends.memory_backend import MemoryBackend
from puikit.widgets import TextArea


@pytest.fixture(params=[PROFILE_TUI, PROFILE_GUI_DESKTOP], ids=["tui", "gui"])
def backend(request):
    return MemoryBackend(width=30, height=12, capabilities=request.param)


def _key(name, char=None, modifiers=frozenset()):
    return Event(type=EventType.KEY, key=name, char=char, modifiers=frozenset(modifiers))


def _type(panel, text):
    for ch in text:
        panel.dispatch_event(_key("space" if ch == " " else ch, char=ch))


def _field(backend, text="", w=12, h=4, **kw):
    panel = Panel(backend)
    field = TextArea(text, **kw)
    panel.add(field, x=0, y=0, w=w, h=h)
    panel.render()
    return panel, field


def test_enter_inserts_a_line_break_and_text_keeps_it(backend):
    panel, field = _field(backend)
    _type(panel, "ab")
    panel.dispatch_event(_key("enter", char="\r"))
    _type(panel, "cd")
    assert field.text == "ab\ncd"
    assert field.cursor == 5


def test_submit_on_enter_and_the_line_break_keys(backend):
    sent = []
    panel, field = _field(backend, submit_on_enter=True, on_submit=sent.append)
    _type(panel, "a")
    panel.dispatch_event(_key("enter", char="\r", modifiers={"shift"}))
    _type(panel, "b")
    panel.dispatch_event(_key("enter", char="\r", modifiers={"alt"}))
    _type(panel, "c")
    # A terminal's Ctrl+J: an Enter whose char is a line feed.
    panel.dispatch_event(_key("enter", char="\n"))
    _type(panel, "d")
    # A GUI's Ctrl+J.
    panel.dispatch_event(_key("j", char="j", modifiers={"ctrl"}))
    assert field.text == "a\nb\nc\nd\n"
    panel.dispatch_event(_key("enter", char="\r"))
    assert sent == ["a\nb\nc\nd\n"]


def test_ctrl_enter_submits_when_enter_breaks_lines(backend):
    sent = []
    panel, field = _field(backend, "x", on_submit=sent.append)
    panel.dispatch_event(_key("enter", char="\r", modifiers={"ctrl"}))
    assert sent == ["x"]
    assert field.text == "x"


def test_long_line_wraps_at_a_space(backend):
    panel, field = _field(backend, "hello world again", w=10, h=4)
    assert field._rows[:2] == [(0, 6), (6, 12)]  # "hello " / "world "
    # The visual rows show the wrapped text; the buffer is unchanged.
    assert field.text == "hello world again"


def test_word_without_space_breaks_at_the_edge(backend):
    panel, field = _field(backend, "abcdefghijklmnop", w=8, h=4)
    starts = [r[0] for r in field._rows]
    assert starts[0] == 0 and len(field._rows) >= 2
    assert "".join(field.text[a:b] for a, b in field._rows) == field.text


def test_up_down_keep_the_column(backend):
    panel, field = _field(backend, "abcdef\nab\nabcdef", w=20, h=4)
    field.cursor = 5  # line 1, column 5
    panel.dispatch_event(_key("down"))
    assert field.cursor == 9   # end of "ab" (shorter line)
    panel.dispatch_event(_key("down"))
    assert field.cursor == 15  # back to column 5 on the third line


def test_up_on_first_row_is_left_to_the_host(backend):
    panel, field = _field(backend, "one\ntwo", w=20)
    field.cursor = 1
    assert field.handle_event(_key("up")) is False
    assert field.cursor == 1
    # Shift+Up there still selects to the start.
    assert field.handle_event(_key("up", modifiers={"shift"})) is True
    assert field.selection_text == "o"


def test_home_end_go_to_the_visual_row(backend):
    panel, field = _field(backend, "aa\nbbbb", w=20)
    field.cursor = 5
    panel.dispatch_event(_key("home"))
    assert field.cursor == 3
    panel.dispatch_event(_key("end"))
    assert field.cursor == 7
    panel.dispatch_event(_key("home", modifiers={"ctrl"}))
    assert field.cursor == 0


def test_paste_keeps_line_breaks(backend):
    panel, field = _field(backend)
    backend.set_clipboard("x\r\ny\rz")
    panel.dispatch_event(_key("v", char="v", modifiers={"ctrl"}))
    assert field.text == "x\ny\nz"


def test_select_all_and_cut(backend):
    panel, field = _field(backend, "a\nb")
    panel.dispatch_event(_key("a", char="a", modifiers={"ctrl"}))
    assert field.selection_text == "a\nb"
    panel.dispatch_event(_key("x", char="x", modifiers={"ctrl"}))
    assert field.text == ""
    assert backend.get_clipboard() == "a\nb"


def test_measure_grows_with_the_text_up_to_max_rows(backend):
    panel = Panel(backend)
    field = TextArea("", min_rows=1, max_rows=3)
    lc = panel.layout_context()
    pad = 0.0 if lc.snap else 0.5
    assert field.measure(lc, "y", 12).preferred == 1 + pad
    field.set_text("a\nb")
    assert field.measure(lc, "y", 12).preferred == 2 + pad
    field.set_text("a\nb\nc\nd\ne")
    assert field.measure(lc, "y", 12).preferred == 3 + pad


def test_scrolls_to_keep_the_caret_visible(backend):
    panel, field = _field(backend, "\n".join("abcdefgh"), w=12, h=3)
    # 8 rows in a field showing fewer: the caret at the end is in view.
    assert field._top > 0
    assert field._top + field._visible >= len(field._rows)
    panel.dispatch_event(_key("home", modifiers={"ctrl"}))
    panel.render()
    assert field._top == 0


def test_wheel_scrolls_without_moving_the_caret(backend):
    panel, field = _field(backend, "\n".join("abcdefgh"), w=12, h=3)
    top, caret = field._top, field.cursor
    field.handle_event(Event(type=EventType.MOUSE_SCROLL, x=1, y=1, scroll=1))
    panel.render()
    assert field._top == top - 1
    assert field.cursor == caret


def test_word_delete_and_line_start_delete(backend):
    panel, field = _field(backend, "one two\nthree four", w=30)
    panel.dispatch_event(_key("backspace", modifiers={"alt"}))
    assert field.text == "one two\nthree "
    panel.dispatch_event(_key("backspace", modifiers={"cmd"}))
    assert field.text == "one two\n"
    panel.dispatch_event(_key("backspace", modifiers={"cmd"}))
    assert field.text == "one two"


def test_ime_composition_does_not_touch_the_buffer(backend):
    panel, field = _field(backend, "a\n")
    panel.dispatch_event(Event(type=EventType.IME_COMPOSITION,
                               hints={"preedit": "にほん", "caret": 3}))
    panel.render()
    assert field.text == "a\n"
    panel.dispatch_event(_key("日", char="日"))
    assert field.text == "a\n日"
    assert field._preedit == ""


def test_placeholder_is_drawn_only_while_empty(backend):
    panel, field = _field(backend, placeholder="Ask", w=12, h=2)
    assert "Ask" in "".join(backend.snapshot()[0])
    _type(panel, "z")
    panel.render()
    assert "Ask" not in "".join(backend.snapshot()[0])
    assert field.text == "z"


def test_click_places_the_caret_on_the_row(backend):
    panel, field = _field(backend, "abc\ndef", w=12, h=3)
    panel.render()
    y = field._text_y + 1.2  # inside the second row
    field.handle_event(Event(type=EventType.MOUSE_DOWN, x=2.0, y=y, button="left"))
    assert field.cursor == 5  # 'd' 'e' | 'f'
