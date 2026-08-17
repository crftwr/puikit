"""The VT backend's POSIX half: the stream parser and the tty console.

The parser tests map what a terminal actually sends — named CSI/SS3 keys, meta
chords, SGR mouse gestures — and the two hazards the design guards against: a
CSI reply being typed into the app (the bug PR #108 fixed on Windows arrives
here for free, as parse_vt_input drops unrecognized CSI), and a lone ESC being
confused with a sequence still in flight.

The console tests run against a real pty, because termios modes and the
select loop are exactly the parts a fake would vouch for without testing.
"""

import fcntl
import os
import pty
import select
import struct
import sys
import termios

import pytest

from puikit.backends._vt_input import parse_vt_input
from puikit.event import EventType

if sys.platform != "win32":
    from puikit.backends._vt_posix import PosixConsole

pytestmark = pytest.mark.skipif(sys.platform == "win32",
                                reason="POSIX console: termios/pty")


def records(text, flush=False):
    recs, _rest = parse_vt_input(text, flush=flush)
    return recs


def one(text):
    recs = records(text)
    assert len(recs) == 1, recs
    return recs[0]


# --- parser: plain typing ---------------------------------------------------


def test_plain_text_including_cjk_passes_through():
    recs = records("aあ本.")
    assert [r["char"] for r in recs] == ["a", "あ", "本", "."]
    assert all(r["type"] == "key" and r["name"] is None for r in recs)


def test_uppercase_implies_shift():
    # A terminal cannot report Shift for a printable; the inferred modifier is
    # what lets the shared contract helper lowercase the key.
    assert one("A")["mods"] == frozenset({"shift"})
    assert one("a")["mods"] == frozenset()
    assert one("あ")["mods"] == frozenset()  # caseless scripts stay unmodified


def test_control_bytes_arrive_as_chars_for_the_engine():
    # \r -> enter, \x01 -> ctrl+a etc. is the ENGINE's mapping, shared with the
    # Windows console's records; the parser just delivers the byte.
    assert one("\r")["char"] == "\r"
    assert one("\x01")["char"] == "\x01"


# --- parser: escape sequences -----------------------------------------------


def test_csi_arrows_and_modifiers():
    assert one("\x1b[A")["name"] == "up"
    ctrl_left = one("\x1b[1;5D")
    assert ctrl_left["name"] == "left"
    assert ctrl_left["mods"] == frozenset({"ctrl"})


def test_ss3_arrows_and_function_keys():
    # Application-cursor mode (what an interactive shell usually leaves on).
    assert one("\x1bOC")["name"] == "right"
    assert one("\x1bOP")["name"] == "f1"


def test_tilde_function_and_edit_keys():
    assert one("\x1b[3~")["name"] == "delete"
    assert one("\x1b[15~")["name"] == "f5"
    assert one("\x1b[21~")["name"] == "f10"
    ctrl_f5 = one("\x1b[15;5~")
    assert (ctrl_f5["name"], ctrl_f5["mods"]) == ("f5", frozenset({"ctrl"}))


def test_shift_tab_is_backtab():
    rec = one("\x1b[Z")
    assert rec["name"] == "tab"
    assert rec["mods"] == frozenset({"shift"})


def test_meta_word_editing_chords():
    assert one("\x1bb")["name"] == "left"
    assert "alt" in one("\x1bb")["mods"]
    assert one("\x1b\x7f")["name"] == "backspace"
    # An unhandled Alt chord falls back to Escape, matching the curses backend.
    assert one("\x1bz")["name"] == "escape"


def test_a_csi_reply_is_dropped_not_typed():
    # The size probe's answer ("[8;26;136t") pressed keys on Windows until a
    # filter was added (PR #108); here the parser itself refuses to type it.
    assert records("\x1b[8;26;136t") == []
    assert records("\x1b[4;480;800t") == []


def test_a_reply_split_across_feeds_is_still_dropped():
    recs, rest = parse_vt_input("\x1b[8;26")
    assert recs == [] and rest == "\x1b[8;26"
    assert records(rest + ";136t") == []


def test_incomplete_escape_is_held_not_flushed():
    recs, rest = parse_vt_input("ab\x1b[1;5")
    assert [r["char"] for r in recs] == ["a", "b"]
    assert rest == "\x1b[1;5"


def test_flush_turns_the_esc_into_the_escape_key():
    recs, rest = parse_vt_input("\x1b", flush=True)
    assert rest == ""
    assert [r["name"] for r in recs] == ["escape"]
    # The partial body is dropped with it, matching curses' fallback for a
    # sequence that never completed — not typed into the app.
    recs, _ = parse_vt_input("\x1b[1;5", flush=True)
    assert [r["name"] for r in recs] == ["escape"]


# --- parser: SGR mouse ------------------------------------------------------


def test_sgr_press_and_release_name_the_button():
    down = one("\x1b[<0;5;3M")
    assert (down["action"], down["button"], down["x"], down["y"]) == ("down", "left", 4, 2)
    up = one("\x1b[<2;5;3m")
    assert (up["action"], up["button"]) == ("up", "right")


def test_sgr_motion_with_a_button_is_a_drag():
    drag = one("\x1b[<32;8;2M")
    assert (drag["action"], drag["button"]) == ("drag", "left")
    middle = one("\x1b[<33;8;2M")
    assert (middle["action"], middle["button"]) == ("drag", "middle")


def test_sgr_bare_motion_is_a_move():
    # Only mode 1003 sends these and the VT backend never enables it, but a
    # stray must translate to the gesture the engine explicitly drops.
    assert one("\x1b[<35;8;2M")["action"] == "move"


def test_sgr_wheel_and_horizontal_wheel():
    up = one("\x1b[<64;3;3M")
    assert (up["action"], up["wheel"], up["axis"]) == ("wheel", 1, "v")
    down = one("\x1b[<65;3;3M")
    assert down["wheel"] == -1
    right = one("\x1b[<67;3;3M")
    assert (right["wheel"], right["axis"]) == (1, "h")
    left = one("\x1b[<66;3;3M")
    assert (left["wheel"], left["axis"]) == (-1, "h")


def test_sgr_modifiers_ride_along():
    rec = one("\x1b[<16;5;3M")  # ctrl+click
    assert rec["mods"] == frozenset({"ctrl"})
    rec = one("\x1b[<4;5;3M")   # shift+click
    assert rec["mods"] == frozenset({"shift"})


def test_a_mixed_burst_keeps_its_order():
    recs = records("j\x1b[A\x1b[<0;2;2Mk")
    assert [r.get("name") or r.get("char") or r.get("action") for r in recs] == \
        ["j", "up", "down", "k"]


# --- the engine consumes what the parser produces ---------------------------


def test_parsed_records_drive_the_engine():
    # End to end: SGR bytes -> records -> puikit events, through the same
    # engine the Windows console feeds.
    import io
    from puikit.backends.vt_backend import VTBackend, _StreamConsole

    class Scripted(_StreamConsole):
        def __init__(self):
            super().__init__(stream=io.StringIO(), size=(40, 10))
            self.queue = []

        def size(self):
            return (40, 10)

        def read_input(self, timeout_ms):
            return self.queue.pop(0) if self.queue else []

    con = Scripted()
    be = VTBackend(console=con)
    be.open()
    con.queue.append(records("\x1b[<0;5;3M\x1b[<32;9;3M\x1b[<0;9;3mq\x1b[1;5D"))
    events = []
    for _ in range(10):
        be.run_event_loop_iteration(events.append, 0)
    be.close()
    assert [e.type for e in events] == [
        EventType.MOUSE_DOWN, EventType.MOUSE_DRAG, EventType.MOUSE_UP,
        EventType.KEY, EventType.KEY,
    ]
    assert events[1].x == 8.0
    assert events[3].key == "q"
    assert (events[4].key, events[4].modifiers) == ("left", frozenset({"ctrl"}))


# --- the pty console --------------------------------------------------------


@pytest.fixture
def tty_pair():
    master, slave = pty.openpty()
    yield master, slave
    for fd in (master, slave):
        try:
            os.close(fd)
        except OSError:
            pass


def set_winsize(fd, rows, cols, xpix=0, ypix=0):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, xpix, ypix))


def drain_master(master, timeout=0.2):
    out = b""
    while select.select([master], [], [], timeout)[0]:
        out += os.read(master, 65536)
        timeout = 0.05  # keep reading only as long as more is coming
    return out


def test_open_applies_raw_mode_and_close_restores(tty_pair):
    master, slave = tty_pair
    before = termios.tcgetattr(slave)
    con = PosixConsole(in_fd=slave, out_fd=slave)
    con.open()
    attrs = termios.tcgetattr(slave)
    assert not attrs[3] & termios.ECHO     # keys are not echoed over the UI
    assert not attrs[3] & termios.ICANON   # and not line-assembled
    assert not attrs[3] & termios.ISIG     # Ctrl+C/Z are keys for the app
    con.close()

    def settings(attrs):
        # PENDIN / FLUSHO are kernel-transient line-discipline STATUS bits the
        # switch back to canonical mode may flip on its own, not settings.
        attrs = attrs[:]
        attrs[3] &= ~(getattr(termios, "PENDIN", 0) | getattr(termios, "FLUSHO", 0))
        return attrs

    assert settings(termios.tcgetattr(slave)) == settings(before)


def test_open_enters_alt_screen_and_enables_mouse(tty_pair):
    master, slave = tty_pair
    con = PosixConsole(in_fd=slave, out_fd=slave)
    con.open()
    out = drain_master(master).decode()
    assert "\x1b[?1049h" in out
    assert "\x1b[?1006h" in out    # SGR mouse tracking
    con.suspend()
    out = drain_master(master).decode()
    assert "\x1b[?1006l" in out    # a child must not inherit mouse reports
    assert "\x1b[?1049l" in out
    con.resume()
    con.close()


def test_typed_bytes_become_key_records(tty_pair):
    master, slave = tty_pair
    set_winsize(slave, 24, 80)
    con = PosixConsole(in_fd=slave, out_fd=slave)
    con.open()
    drain_master(master)
    os.write(master, "hi 日本\x1b[A".encode())
    recs = con.read_input(500)
    con.close()
    assert "".join(r["char"] for r in recs[:5]) == "hi 日本"
    assert recs[5]["name"] == "up"


def test_a_lone_escape_is_the_escape_key(tty_pair):
    master, slave = tty_pair
    set_winsize(slave, 24, 80)
    con = PosixConsole(in_fd=slave, out_fd=slave)
    con.open()
    drain_master(master)
    os.write(master, b"\x1b")
    recs = con.read_input(500)  # waits out the escape grace, then decides
    con.close()
    assert [r["name"] for r in recs] == ["escape"]


def test_a_size_change_is_reported_and_reflected(tty_pair):
    master, slave = tty_pair
    set_winsize(slave, 24, 80)
    con = PosixConsole(in_fd=slave, out_fd=slave)
    con.open()
    assert con.size() == (80, 24)
    set_winsize(slave, 30, 100)
    recs = con.read_input(0)
    con.close()
    assert {"type": "resize"} in recs
    assert con.size() == (100, 30)


def test_cell_pixels_from_the_ioctl(tty_pair):
    master, slave = tty_pair
    set_winsize(slave, 24, 80, xpix=800, ypix=480)
    con = PosixConsole(in_fd=slave, out_fd=slave)
    assert con.cell_pixels() == (10.0, 20.0)


def test_cell_pixels_from_the_probe_reply(tty_pair):
    # No pixel fields in the ioctl (Terminal.app's shape) -> CSI 14t/18t. The
    # reply is already in flight when the probe reads, as from a terminal that
    # answers promptly. open() first, as real use does — before it the tty is
    # still canonical and would hold the reply until a newline.
    master, slave = tty_pair
    set_winsize(slave, 24, 80)
    con = PosixConsole(in_fd=slave, out_fd=slave)
    con.open()
    drain_master(master)
    os.write(master, b"\x1b[4;480;800t\x1b[8;24;80t")
    assert con.cell_pixels() == (10.0, 20.0)
    con.close()


def test_probe_leftovers_are_kept_for_read_input(tty_pair):
    # Whatever the user typed while the probe waited must not be swallowed
    # with the replies.
    master, slave = tty_pair
    set_winsize(slave, 24, 80)
    con = PosixConsole(in_fd=slave, out_fd=slave)
    con.open()
    drain_master(master)
    os.write(master, b"a\x1b[4;480;800t\x1b[8;24;80tb")
    con.cell_pixels()
    recs = con.read_input(0)
    con.close()
    assert [r["char"] for r in recs] == ["a", "b"]
