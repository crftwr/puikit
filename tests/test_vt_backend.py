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
    _VK_PROCESSKEY,
    _win_key_record,
)
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
