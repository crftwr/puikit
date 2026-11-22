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


# --- the clipboard -----------------------------------------------------------------------

class _ClipTerm:
    def __init__(self, selection="", tracking=False):
        self.selection = selection
        self.tracking = tracking
        self.mouse_sent = []

    def has_selection(self):
        return bool(self.selection)

    def selection_text(self):
        return self.selection

    def clear_selection(self):
        self.selection = ""

    def scroll(self, rows):
        pass

    def mouse_tracking(self):
        return self.tracking

    def encode_mouse(self, action, button, col, row, mods, pressed):
        self.mouse_sent.append((action, button))
        return b"<mouse>"


class _ClipPanel:
    def __init__(self, clipboard=""):
        self.clipboard = clipboard

    def get_clipboard(self):
        return self.clipboard

    def set_clipboard(self, text):
        self.clipboard = text


class _Proc:
    def __init__(self):
        self.written = []

    def write(self, data):
        self.written.append(data)


def _clip_view(selection="", clipboard="", tracking=False, ctrl_clipboard=True):
    view = TerminalView(["true"], ctrl_clipboard=ctrl_clipboard)
    view._term = _ClipTerm(selection, tracking)
    view._panel = _ClipPanel(clipboard)
    view._proc = _Proc()
    view._exited = False
    view._cw = view._pitch = 1.0
    view._scrollbar_mouse = lambda event: False
    return view


def test_ctrl_clipboard_is_on_for_windows_unless_the_host_says(monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    assert TerminalView(["true"]).ctrl_clipboard is True
    assert TerminalView(["true"], ctrl_clipboard=False).ctrl_clipboard is False
    monkeypatch.setattr("sys.platform", "darwin")
    assert TerminalView(["true"]).ctrl_clipboard is False
    assert TerminalView(["true"], ctrl_clipboard=True).ctrl_clipboard is True


def _key(key, *mods):
    return Event(EventType.KEY, key=key, modifiers=frozenset(mods))


def _right_click(*mods):
    return Event(EventType.MOUSE_CLICK, x=1.0, y=1.0, button="right",
                 modifiers=frozenset(mods))


def test_ctrl_shift_c_copies_and_keeps_the_selection():
    view = _clip_view(selection="ls -l")
    assert view._clipboard_key(_key("c", "ctrl", "shift"))
    assert view._panel.clipboard == "ls -l" and view._term.selection == "ls -l"


def test_ctrl_c_copies_a_selection_and_clears_it_so_the_next_interrupts():
    view = _clip_view(selection="ls -l")
    assert view._clipboard_key(_key("c", "ctrl"))
    assert view._panel.clipboard == "ls -l" and view._term.selection == ""
    # Nothing selected: the key is the program's (an interrupt).
    assert not view._clipboard_key(_key("c", "ctrl"))


def test_ctrl_c_and_ctrl_v_are_the_programs_where_the_clipboard_is_ctrl_shift():
    view = _clip_view(selection="x", clipboard="y", ctrl_clipboard=False)
    assert not view._clipboard_key(_key("c", "ctrl"))
    assert not view._clipboard_key(_key("v", "ctrl"))
    assert view._panel.clipboard == "y" and view._proc.written == []


def test_ctrl_v_pastes_each_line_break_as_enter():
    view = _clip_view(clipboard="echo a\r\necho b\n")
    assert view._clipboard_key(_key("v", "ctrl"))
    assert view._proc.written == [b"echo a\recho b\r"]


def test_ctrl_insert_copies_and_shift_insert_pastes():
    view = _clip_view(selection="sel", clipboard="old", ctrl_clipboard=False)
    assert view._clipboard_key(_key("insert", "ctrl"))
    assert view._panel.clipboard == "sel"
    assert view._clipboard_key(_key("insert", "shift"))
    assert view._proc.written == [b"sel"]


def test_an_empty_clipboard_pastes_nothing():
    view = _clip_view()
    assert view._clipboard_key(_key("v", "ctrl", "shift"))
    assert view._proc.written == []


def test_a_right_click_copies_a_selection_and_clears_it():
    view = _clip_view(selection="sel", clipboard="old")
    view._mouse(_right_click())
    assert view._panel.clipboard == "sel" and view._term.selection == ""
    assert view._proc.written == []


def test_a_right_click_with_nothing_selected_pastes():
    view = _clip_view(clipboard="pwd")
    view._mouse(_right_click())
    assert view._proc.written == [b"pwd"]


def test_a_program_tracking_the_mouse_gets_the_right_click_unless_shift_is_held():
    view = _clip_view(clipboard="pwd", tracking=True)
    view._mouse(_right_click())
    assert view._proc.written == [b"<mouse><mouse>"]  # press and release
    view._proc.written.clear()
    view._mouse(_right_click("shift"))
    assert view._proc.written == [b"pwd"]
