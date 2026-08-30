"""The VT backend's console-facing behaviour, driven through a stream console.

The input tests carry the most weight. Under ``ReadConsoleInputW`` a committed
IME character arrives as a KEY_EVENT with no usable virtual key code (or
carrying VK_PROCESSKEY), so the obvious way to find command keys — filter on the
virtual key — drops every Japanese character typed. That would fix CJK *display*
while making CJK *impossible to type*, which is the worst outcome available to
this backend given why it is being written (puikit#98 §8.4).
"""

import io

import pytest

from puikit.backends.vt_backend import (
    VTBackend,
    _StreamConsole,
    _supports_underline_color,
    _VK_PROCESSKEY,
    _win_key_record,
)
from puikit.backend import Style, TextAttribute
from puikit.event import EventType


def wkey(char, vk=0, control=0):
    """A KEY_EVENT as the Windows console would hand it to the engine — through
    the same translation the real _read_records applies."""
    return _win_key_record(char, vk, control)


class FakeConsole(_StreamConsole):
    """A stream console with a fixed size and a scripted input queue."""

    def __init__(self, width=40, height=10):
        super().__init__(stream=io.StringIO(), size=(width, height))
        self._fixed = (width, height)
        self.queue: list[list[dict]] = []
        self.clipboard = ""

    def size(self):
        return self._fixed

    def resize_to(self, width, height):
        self._fixed = (width, height)

    def read_input(self, timeout_ms):
        return self.queue.pop(0) if self.queue else []

    def set_clipboard(self, text):
        self.clipboard = text

    def get_clipboard(self):
        return self.clipboard


@pytest.fixture
def backend():
    con = FakeConsole()
    be = VTBackend(console=con)
    be.open()
    yield be, con
    be.close()


def collect(be, con):
    events = []
    be.run_event_loop_iteration(events.append, 0)
    return events


# --- input: IME ------------------------------------------------------------


def test_ime_commit_with_processkey_is_not_dropped(backend):
    # The failure this guards against: filtering on wVirtualKeyCode discards the
    # composed character and Japanese input stops working entirely.
    be, con = backend
    con.queue.append([wkey("あ", _VK_PROCESSKEY)])
    events = collect(be, con)
    assert [e.key for e in events] == ["あ"]
    assert events[0].char == "あ"


def test_ime_commit_with_no_virtual_key_is_not_dropped(backend):
    be, con = backend
    con.queue.append([wkey("本")])
    events = collect(be, con)
    assert [e.key for e in events] == ["本"]


def test_full_cjk_filename_arrives_intact(backend):
    # Typing あいうえお.txt into a rename dialog, one commit record per glyph.
    be, con = backend
    text = "あいうえお.txt"
    con.queue.append([wkey(ch, _VK_PROCESSKEY) for ch in text])
    got = ""
    for _ in range(len(text)):
        for e in collect(be, con):
            got += e.char or ""
    assert got == text


def test_plain_letters_still_dispatch_as_commands(backend):
    # The other half of the acceptance criterion: f and j must keep working as
    # command keys while a pane has focus.
    be, con = backend
    con.queue.append([wkey("f", 0x46), wkey("j", 0x4A)])
    keys = [e.key for e in collect(be, con)] + [e.key for e in collect(be, con)]
    assert keys == ["f", "j"]


# --- input: modifiers and specials ----------------------------------------


def test_modifiers_come_from_control_key_state(backend):
    # Native modifier state, rather than the CSI byte sequences a VT stream
    # forces the curses backend to parse for itself.
    be, con = backend
    con.queue.append([wkey("", 0x26, 0x0010 | 0x0008)])  # up, SHIFT | LEFT_CTRL
    e = collect(be, con)[0]
    assert e.key == "up"
    assert e.modifiers == frozenset({"shift", "ctrl"})


@pytest.mark.parametrize("vk,name", [(0x08, "backspace"), (0x1B, "escape"),
                                     (0x25, "left"), (0x70, "f1")])
def test_special_keys_translate(backend, vk, name):
    be, con = backend
    con.queue.append([wkey("", vk)])
    assert collect(be, con)[0].key == name


def test_key_up_records_are_ignored(backend):
    be, con = backend
    con.queue.append([])
    assert collect(be, con) == []


# --- resize ---------------------------------------------------------------


def test_resize_record_becomes_an_event_and_resizes_the_grid(backend):
    # Windows has no SIGWINCH; the size change arrives as an input record.
    be, con = backend
    con.resize_to(60, 20)
    con.queue.append([{"type": "resize"}])
    events = collect(be, con)
    assert [e.type for e in events] == [EventType.RESIZE]
    assert be.size == (60, 20)


# --- output ---------------------------------------------------------------


def test_open_enters_the_alternate_screen_and_close_restores(backend):
    be, con = backend
    assert "\x1b[?1049h" in "".join(con.written)
    con.written.clear()
    be.close()
    tail = "".join(con.written)
    assert "\x1b[?1049l" in tail   # shell scrollback comes back
    assert "\x1b[0m" in tail       # no attributes leak into the shell
    be.open()  # so the fixture's close() is harmless


def test_a_frame_is_one_write(backend):
    # The reason for the backend: curses' refresh turns a frame into a stream of
    # console calls, each a round trip. Here a frame leaves in one.
    be, con = backend
    con.written.clear()
    be.clear()
    be.draw_text(0, 0, "hello")
    be.present()
    assert len(con.written) == 1


def test_unchanged_frame_writes_almost_nothing(backend):
    be, con = backend
    be.clear()
    be.draw_text(0, 0, "hello")
    be.present()
    con.written.clear()
    be.clear()
    be.draw_text(0, 0, "hello")
    be.present()
    # Only the cursor-hide; no cell content is re-sent.
    assert "hello" not in "".join(con.written)


def test_caret_request_moves_the_hardware_cursor(backend):
    # A terminal IME composes at the hardware cursor, so the caret position IS
    # the composition position.
    be, con = backend
    be.clear()
    be.draw_text(0, 0, "name: ")
    be.request_text_input(6, 0)
    con.written.clear()
    be.present()
    out = "".join(con.written)
    assert "\x1b[1;7H" in out  # 1-based row 1, column 7
    assert "\x1b[?25h" in out  # and the cursor is shown


def test_cursor_hidden_when_no_field_asks_for_it(backend):
    be, con = backend
    be.clear()
    be.draw_text(0, 0, "x")
    con.written.clear()
    be.present()
    assert "\x1b[?25l" in "".join(con.written)


# --- drawing --------------------------------------------------------------


def test_cjk_row_keeps_its_columns(backend):
    # The xefm#283 case at the backend level: a name column of CJK, then a size.
    be, con = backend
    be.clear()
    be.draw_text(0, 0, "日本語")
    be.draw_text(6, 0, "1.2K")
    assert be._grid.cell_at(0, 0)[0] == "日"
    assert be._grid.cell_at(6, 0)[0] == "1"


def test_clipboard_round_trips(backend):
    # OSC 52 is write-only, so the curses path cannot read back at all.
    be, con = backend
    be.set_clipboard("copied")
    assert be.get_clipboard() == "copied"


def test_underline_color_reaches_the_cell(backend):
    be, con = backend
    be._underline_colors = True   # the detection is env-dependent; pin it
    be.clear()
    be.draw_text(0, 0, "x", Style(fg=(1, 2, 3), attr=TextAttribute.UNDERLINE,
                                  underline_color=(231, 76, 76)))
    assert be._grid.cell_at(0, 0)[4] == (231, 76, 76)


def test_underline_color_without_an_underline_is_dropped(backend):
    # It would put a difference into the cell that no screen can show, and every
    # pen change is paid for in the frame diff.
    be, con = backend
    be._underline_colors = True
    be.clear()
    be.draw_text(0, 0, "x", Style(fg=(1, 2, 3), underline_color=(231, 76, 76)))
    assert be._grid.cell_at(0, 0)[4] is None


def test_underline_color_is_dropped_where_sgr_58_cannot_be_sent(backend):
    # The underline attribute stays: the rule is drawn, in fg. Losing the color
    # is the degradation; losing the cue is not.
    be, con = backend
    be._underline_colors = False
    be.clear()
    style = Style(fg=(1, 2, 3), attr=TextAttribute.UNDERLINE, underline_color=(231, 76, 76))
    be.draw_text(0, 0, "x", style)
    glyph, fg, _bg, attr, ul = be._grid.cell_at(0, 0)
    assert ul is None
    assert attr & TextAttribute.UNDERLINE
    assert "58:" not in be._grid.render()


# --- who may be sent a colored underline ----------------------------------

def test_terminal_app_is_not_sent_sgr_58():
    # It abandons the whole sequence at the first colon, losing the underline
    # attribute and the row's colors with it (xefm#350).
    assert not _supports_underline_color({"TERM_PROGRAM": "Apple_Terminal", "TERM": "xterm-256color"})


def test_an_unknown_terminal_is_not_sent_sgr_58():
    # The whitelist's whole point: unrecognized costs a color, guessing costs the row.
    assert not _supports_underline_color({"TERM": "xterm-256color"})
    assert not _supports_underline_color({})


def test_multiplexers_are_not_sent_sgr_58():
    # TERM says nothing about the emulator underneath, and neither forwards 58
    # unless configured to.
    assert not _supports_underline_color({"TERM": "tmux-256color"})
    assert not _supports_underline_color({"TERM": "screen-256color"})


@pytest.mark.parametrize("env", [
    {"KITTY_WINDOW_ID": "1"},
    {"WT_SESSION": "abc"},
    {"VTE_VERSION": "6003"},
    {"KONSOLE_VERSION": "230801"},
    {"TERM_PROGRAM": "iTerm.app"},
    {"TERM_PROGRAM": "WezTerm"},
    {"TERM": "xterm-kitty"},
    {"TERM": "alacritty"},
])
def test_terminals_that_implement_sgr_58_are(env):
    assert _supports_underline_color(env)


def test_a_version_below_the_floor_is_not():
    assert not _supports_underline_color({"VTE_VERSION": "5002"})        # VTE 0.50
    assert not _supports_underline_color({"KONSOLE_VERSION": "220401"})  # 22.04


def test_xterm_is_not_on_the_list():
    # It parses the parameter away safely, so sending 58 would cost nothing —
    # but it draws no colored rule, and the bracket spelling a widget picks
    # instead is better than a rule inked in fg. The predicate is IMPLEMENTS.
    assert not _supports_underline_color({"XTERM_VERSION": "XTerm(370)",
                                          "TERM": "xterm-256color"})


def test_the_capability_follows_the_detection(monkeypatch):
    # Widgets never see _supports_underline_color; they see this, and a cue that
    # needs a colored rule reads it to pick its other spelling.
    monkeypatch.setenv("PUIKIT_UNDERLINE_COLOR", "1")
    assert VTBackend(FakeConsole()).capabilities.supports("colored_underlines")
    monkeypatch.setenv("PUIKIT_UNDERLINE_COLOR", "0")
    assert not VTBackend(FakeConsole()).capabilities.supports("colored_underlines")


def test_the_override_wins_both_ways():
    assert _supports_underline_color({"PUIKIT_UNDERLINE_COLOR": "1",
                                      "TERM_PROGRAM": "Apple_Terminal"})
    assert not _supports_underline_color({"PUIKIT_UNDERLINE_COLOR": "0",
                                          "TERM": "xterm-kitty"})
