"""Curses input translation: get_wch() returns str chars and int key codes.

These exercise the pure translation logic without opening a real terminal
(constructing CursesBackend does not touch curses).
"""

import curses

from puikit.backends.curses_backend import (
    CursesBackend,
    _csi_modifiers,
    _escape_complete,
    _extended_key_event,
    _meta_char_event,
    _parse_csi_key,
)
from puikit.event import EventType


def test_translate_char_multibyte_is_character_event():
    be = CursesBackend()
    ev = be._translate_char("あ")  # committed CJK character from get_wch()
    assert ev.type is EventType.KEY
    assert ev.key == "あ"
    assert ev.char == "あ"


def test_translate_char_ascii():
    be = CursesBackend()
    ev = be._translate_char("a")
    assert ev.key == "a" and ev.char == "a"


def test_translate_char_control_keys():
    be = CursesBackend()
    assert be._translate_char("\t").key == "tab"
    assert be._translate_char("\r").key == "enter"
    assert be._translate_char("\x1b").key == "escape"
    assert be._translate_char("\x7f").key == "backspace"
    # Control chars carry no printable char payload.
    assert be._translate_char("\t").char is None


def test_translate_str_dispatches_to_char_path():
    be = CursesBackend()
    ev = be._translate("漢")
    assert ev.type is EventType.KEY and ev.char == "漢"


def test_ctrl_letter_becomes_ctrl_modified_key():
    # Ctrl+A/C/X/V arrive as bytes 0x01/0x03/0x18/0x16; they drive the same
    # selection/clipboard shortcuts the GUI gets from Cmd.
    be = CursesBackend()
    for byte, letter in [("\x01", "a"), ("\x03", "c"), ("\x18", "x"), ("\x16", "v")]:
        ev = be._translate_char(byte)
        assert ev.type is EventType.KEY
        assert ev.key == letter
        assert "ctrl" in ev.modifiers
        assert ev.char is None  # a command chord, not text


def test_ctrl_letter_does_not_shadow_named_control_keys():
    # Ctrl+I/J/M/H/[ collide with tab/enter/backspace/escape; the named key wins.
    be = CursesBackend()
    assert be._translate_char("\t").key == "tab"
    assert be._translate_char("\r").key == "enter"
    assert be._translate_char("\x08").key == "backspace"
    assert be._translate_char("\x1b").key == "escape"


# --- modified function-key (word-editing) decoding ------------------------------


def test_csi_modifiers_decode_xterm_parameter():
    # mod = 1 + bitmask(Shift=1, Alt=2, Ctrl=4): 5 -> Ctrl, 3 -> Alt, 6 -> Shift+Ctrl.
    assert _csi_modifiers(1) == frozenset()
    assert _csi_modifiers(5) == frozenset({"ctrl"})
    assert _csi_modifiers(3) == frozenset({"alt"})
    assert _csi_modifiers(2) == frozenset({"shift"})
    assert _csi_modifiers(6) == frozenset({"shift", "ctrl"})


def test_parse_csi_key_modified_arrows_and_delete():
    # xterm: CSI 1 ; <mod> <final> for arrows, CSI <n> ; <mod> ~ for delete etc.
    ev = _parse_csi_key("[1;5D")  # Ctrl+Left
    assert ev.key == "left" and ev.modifiers == frozenset({"ctrl"})
    ev = _parse_csi_key("[1;3C")  # Alt+Right
    assert ev.key == "right" and ev.modifiers == frozenset({"alt"})
    ev = _parse_csi_key("[3;5~")  # Ctrl+Delete
    assert ev.key == "delete" and ev.modifiers == frozenset({"ctrl"})
    ev = _parse_csi_key("[1;6D")  # Shift+Ctrl+Left
    assert ev.key == "left" and ev.modifiers == frozenset({"shift", "ctrl"})


def test_parse_csi_key_unmodified_ss3_and_csi():
    assert _parse_csi_key("OC").key == "right"       # SS3 arrow, app-cursor mode
    assert _parse_csi_key("[C") == _parse_csi_key("[C")
    assert _parse_csi_key("[C").modifiers == frozenset()
    assert _parse_csi_key("[999X") is None           # not a key we recognize


def test_meta_char_event_readline_word_keys():
    # Alt+b / Alt+f move by word, Alt+d deletes forward, Alt+Backspace deletes back.
    assert _meta_char_event("b") == _meta_char_event("b")
    assert _meta_char_event("b").key == "left" and "alt" in _meta_char_event("b").modifiers
    assert _meta_char_event("f").key == "right"
    assert _meta_char_event("d").key == "delete"
    assert _meta_char_event("\x7f").key == "backspace"
    assert _meta_char_event("\x08").key == "backspace"
    assert _meta_char_event("z") is None  # not a word-editing chord


def test_extended_key_event_ncurses_capnames():
    # ncurses pre-assembles Ctrl+Left etc. into an extended key whose capability
    # name (kLFT5) carries the base and the xterm modifier digit.
    ev = _extended_key_event("kLFT5")
    assert ev.key == "left" and ev.modifiers == frozenset({"ctrl"})
    ev = _extended_key_event("kDC3")   # Alt+Delete
    assert ev.key == "delete" and ev.modifiers == frozenset({"alt"})
    ev = _extended_key_event("kRIT")   # no trailing digit -> unmodified
    assert ev.key == "right" and ev.modifiers == frozenset()
    assert _extended_key_event("kSOMETHING") is None


def test_escape_complete_recognizes_sequence_kinds():
    assert not _escape_complete("")        # a bare ESC never completes
    assert not _escape_complete("[")       # CSI still gathering
    assert not _escape_complete("[1;5")    # ...still no final byte
    assert _escape_complete("[1;5D")       # final byte 'D'
    assert _escape_complete("[<0;5;3M")    # SGR mouse ends at 'M'
    assert not _escape_complete("O")       # SS3 needs its final char
    assert _escape_complete("OC")
    assert _escape_complete("b")           # ESC + single meta char


def _drive_key(chars):
    """Feed an ESC-prefixed char stream through one event-loop pass and return
    the single KEY event it produces (mirrors the mouse tests' _FakeStdscr)."""
    be = CursesBackend()
    be._stdscr = _FakeStdscr(chars)
    events = []
    be.run_event_loop_iteration(events.append, timeout_ms=0)
    return events


def test_terminal_ctrl_left_decodes_to_word_move():
    events = _drive_key(["\x1b"] + list("[1;5D"))
    assert len(events) == 1
    assert events[0].type is EventType.KEY
    assert events[0].key == "left" and events[0].modifiers == frozenset({"ctrl"})


def test_terminal_alt_backspace_decodes_to_word_delete():
    events = _drive_key(["\x1b", "\x7f"])
    assert len(events) == 1
    assert events[0].key == "backspace" and events[0].modifiers == frozenset({"alt"})


def test_terminal_bare_escape_is_still_the_escape_key():
    events = _drive_key(["\x1b"])  # nothing follows: a real Escape press
    assert len(events) == 1
    assert events[0].key == "escape"


def test_sgr_modifiers_decode_shift():
    be = CursesBackend()
    assert "shift" in be._sgr_modifiers(0x04)
    assert "ctrl" in be._sgr_modifiers(0x10)
    assert be._sgr_modifiers(0x00) == frozenset()


def test_sgr_mouse_press_drag_release_emits_drag():
    # The sequences VS Code's terminal actually sends: ESC[<b;x;y M/m. A left
    # press (b=0) arms drag tracking; a held-button motion (b=32) becomes a
    # MOUSE_DRAG; the lowercase 'm' release disarms it. Coords are 1-based.
    be = CursesBackend()
    # A left press is reported as MOUSE_DOWN (the Panel pairs it with the
    # release); a held-button motion becomes MOUSE_DRAG; the release is MOUSE_UP.
    press = be._parse_sgr_mouse("[<0;5;3M")
    assert press.type is EventType.MOUSE_DOWN and (press.x, press.y) == (4, 2)
    drag = be._parse_sgr_mouse("[<32;8;3M")
    assert drag.type is EventType.MOUSE_DRAG and (drag.x, drag.y) == (7, 2)
    release = be._parse_sgr_mouse("[<0;8;3m")
    assert release.type is EventType.MOUSE_UP and (release.x, release.y) == (7, 2)
    # A stray motion after release (button no longer held) is ignored.
    assert be._parse_sgr_mouse("[<32;9;3M") is None


def test_sgr_mouse_shift_click_and_wheel():
    be = CursesBackend()
    shifted = be._parse_sgr_mouse("[<4;2;2M")  # b=4 -> shift + left press
    assert shifted.type is EventType.MOUSE_DOWN and "shift" in shifted.modifiers
    up = be._parse_sgr_mouse("[<64;2;2M")      # wheel up
    down = be._parse_sgr_mouse("[<65;2;2M")    # wheel down
    assert up.type is EventType.MOUSE_SCROLL and up.scroll == 1
    assert down.type is EventType.MOUSE_SCROLL and down.scroll == -1


def _capture_raw(be):
    """Redirect the backend's real-terminal output to a buffer. The backend
    writes escapes to _raw_out (sys.__stdout__), not sys.stdout, so a plain
    capsys/capfd would miss them — inject a StringIO instead."""
    import io

    buffer = io.StringIO()
    be._raw_out = buffer
    return buffer


def test_set_clipboard_emits_osc52(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    be = CursesBackend()
    out = _capture_raw(be)
    be.set_clipboard("hi")
    # OSC 52 to selection "c" with base64("hi") == "aGk=", BEL-terminated.
    assert out.getvalue() == "\x1b]52;c;aGk=\x07"
    assert be.get_clipboard() == "hi"  # process-local buffer kept for paste


def test_set_clipboard_wraps_for_tmux(monkeypatch):
    monkeypatch.setenv("TMUX", "/tmp/tmux-1/default,123,0")
    be = CursesBackend()
    out = _capture_raw(be)
    be.set_clipboard("hi")
    text = out.getvalue()
    assert text.startswith("\x1bPtmux;") and text.endswith("\x1b\\")
    assert "aGk=" in text


def test_pointer_shape_capability_is_opt_in():
    # A terminal cannot reliably honor OSC 22 and the support is unprobeable, so
    # the capability is off unless the backend is constructed with it.
    assert CursesBackend().capabilities.supports("pointer_shape") is False
    assert CursesBackend(pointer_shape=True).capabilities.supports("pointer_shape") is True


def test_set_pointer_shape_emits_osc22_only_on_change():
    be = CursesBackend(pointer_shape=True)
    out = _capture_raw(be)
    be.set_pointer_shape("text")
    be.set_pointer_shape("text")  # unchanged: no second emit
    be.set_pointer_shape(None)    # reset to default arrow
    assert out.getvalue() == "\x1b]22;text\x07\x1b]22;\x07"


def test_set_pointer_shape_noop_when_disabled():
    be = CursesBackend()  # capability off
    out = _capture_raw(be)
    be.set_pointer_shape("text")
    assert out.getvalue() == ""


def test_bare_motion_is_mouse_move_only_under_all_motion_tracking():
    # Mode 1002 (default) never reports button-less motion, so a stray is ignored.
    assert CursesBackend()._parse_sgr_mouse("[<32;9;3M") is None
    # Mode 1003 (with pointer shapes) reports hover as MOUSE_MOVE.
    ev = CursesBackend(pointer_shape=True)._parse_sgr_mouse("[<32;9;3M")
    assert ev.type is EventType.MOUSE_MOVE and (ev.x, ev.y) == (8, 2)


# --- dead-terminal (EOF busy-spin) detection ------------------------------------


def test_idle_wake_that_blocked_does_not_arm_quit():
    # A live but idle terminal: get_wch() blocked ~the full timeout before
    # returning "no input". No streak accumulates and the loop keeps running.
    be = CursesBackend()
    for _ in range(CursesBackend._DEAD_TERMINAL_WAKE_STREAK * 2):
        be._note_idle_wake(timeout_ms=50, elapsed_s=0.050)
    assert be._empty_wake_streak == 0
    assert be._quit_requested is False


def test_instant_idle_wakes_arm_quit_after_streak():
    # A dead terminal: timeout(50) returns instantly with no input, over and
    # over. Once the streak crosses the threshold the backend requests quit so
    # the caller's loop exits instead of busy-spinning a CPU core.
    be = CursesBackend()
    for _ in range(CursesBackend._DEAD_TERMINAL_WAKE_STREAK - 1):
        be._note_idle_wake(timeout_ms=50, elapsed_s=0.0)
    assert be._quit_requested is False  # not yet
    be._note_idle_wake(timeout_ms=50, elapsed_s=0.0)
    assert be._quit_requested is True


def test_a_real_blocked_wake_resets_the_streak():
    # A single genuine idle wake (blocked ~timeout) breaks a run of instant
    # wakes, so transient instant returns never accumulate to a false positive.
    be = CursesBackend()
    for _ in range(CursesBackend._DEAD_TERMINAL_WAKE_STREAK - 1):
        be._note_idle_wake(timeout_ms=50, elapsed_s=0.0)
    be._note_idle_wake(timeout_ms=50, elapsed_s=0.050)  # really blocked
    assert be._empty_wake_streak == 0
    for _ in range(CursesBackend._DEAD_TERMINAL_WAKE_STREAK - 1):
        be._note_idle_wake(timeout_ms=50, elapsed_s=0.0)
    assert be._quit_requested is False


def test_nonblocking_poll_instant_returns_are_not_eof():
    # timeout_ms == 0 is a non-blocking poll; an instant empty read is normal
    # (e.g. escape-sequence collection) and must never be read as a dead terminal.
    be = CursesBackend()
    for _ in range(CursesBackend._DEAD_TERMINAL_WAKE_STREAK * 2):
        be._note_idle_wake(timeout_ms=0, elapsed_s=0.0)
    assert be._empty_wake_streak == 0
    assert be._quit_requested is False


# --- mouse-wheel scroll coalescing ---------------------------------------------


class _FakeStdscr:
    """A stdscr whose get_wch() replays a scripted list of chars, one per call,
    raising curses.error (as a real timeout does) once the script is exhausted.
    Lets the event-loop iteration run against buffered input without a terminal."""

    def __init__(self, chars):
        self._chars = list(chars)

    def timeout(self, ms):
        pass

    def get_wch(self):
        if not self._chars:
            raise curses.error("no input")
        return self._chars.pop(0)


def _sgr(seq):
    """Expand an SGR mouse report body (e.g. "[<64;2;2M") into the ESC-prefixed
    char stream get_wch() would deliver it as."""
    return ["\x1b"] + list(seq)


_WHEEL_UP = "[<64;2;2M"
_WHEEL_DOWN = "[<65;2;2M"


def _drain(be):
    events = []
    while be.run_event_loop_iteration(events.append, timeout_ms=0):
        if not be._stdscr._chars and be._pending_event is None:
            break
    return events


def test_scroll_burst_coalesces_into_one_event():
    # Three wheel-up notches already buffered collapse to a single MOUSE_SCROLL
    # carrying the summed delta, so the app repaints once for the whole burst.
    be = CursesBackend()
    be._stdscr = _FakeStdscr(_sgr(_WHEEL_UP) + _sgr(_WHEEL_UP) + _sgr(_WHEEL_UP))
    events = _drain(be)
    scrolls = [e for e in events if e.type is EventType.MOUSE_SCROLL]
    assert len(scrolls) == 1
    assert scrolls[0].scroll == 3


def test_scroll_burst_nets_opposite_directions():
    # Up + up + down sums to the net displacement rather than three frames.
    be = CursesBackend()
    be._stdscr = _FakeStdscr(_sgr(_WHEEL_UP) + _sgr(_WHEEL_UP) + _sgr(_WHEEL_DOWN))
    scrolls = [e for e in _drain(be) if e.type is EventType.MOUSE_SCROLL]
    assert len(scrolls) == 1
    assert scrolls[0].scroll == 1


def test_scroll_coalesce_defers_trailing_key():
    # A key typed right after a scroll burst ends the run: the burst is delivered
    # coalesced and the key survives, in order, on the next iteration.
    be = CursesBackend()
    be._stdscr = _FakeStdscr(_sgr(_WHEEL_UP) + _sgr(_WHEEL_UP) + ["a"])
    events = _drain(be)
    assert len(events) == 2
    assert events[0].type is EventType.MOUSE_SCROLL and events[0].scroll == 2
    assert events[1].type is EventType.KEY and events[1].key == "a"


def test_scroll_coalesce_stops_at_different_modifiers():
    # Shift+wheel (horizontal intent) must not merge into a plain vertical burst;
    # it ends the run and is delivered separately.
    shift_up = "[<68;2;2M"  # 64 wheel | 4 shift
    be = CursesBackend()
    be._stdscr = _FakeStdscr(_sgr(_WHEEL_UP) + _sgr(shift_up))
    events = _drain(be)
    scrolls = [e for e in events if e.type is EventType.MOUSE_SCROLL]
    assert len(scrolls) == 2
    assert scrolls[0].scroll == 1 and "shift" not in scrolls[0].modifiers
    assert scrolls[1].scroll == 1 and "shift" in scrolls[1].modifiers


# --- pointer-motion (hover / drag) coalescing ----------------------------------


def test_drag_burst_keeps_latest_position():
    # A quick splitter drag buffers many motion reports; only the final position
    # matters, so the burst collapses to a single MOUSE_DRAG at the last point
    # (the preceding press is a distinct gesture and stays separate).
    be = CursesBackend()
    be._stdscr = _FakeStdscr(
        _sgr("[<0;6;3M")      # left press: arms the drag
        + _sgr("[<32;6;3M")   # drag to x=5
        + _sgr("[<32;9;3M")   # drag to x=8
        + _sgr("[<32;13;3M")  # drag to x=12
    )
    events = _drain(be)
    assert [e.type for e in events] == [EventType.MOUSE_DOWN, EventType.MOUSE_DRAG]
    assert (events[1].x, events[1].y) == (12, 2)


def test_move_burst_keeps_latest_position():
    # Bare hover motion (all-motion tracking) collapses the same way.
    be = CursesBackend(pointer_shape=True)
    be._stdscr = _FakeStdscr(
        _sgr("[<32;6;3M") + _sgr("[<32;9;3M") + _sgr("[<32;13;3M")
    )
    moves = [e for e in _drain(be) if e.type is EventType.MOUSE_MOVE]
    assert len(moves) == 1
    assert (moves[0].x, moves[0].y) == (12, 2)


def test_drag_coalesce_defers_release():
    # The button release that ends a drag is a different gesture: it stops the
    # run and is delivered after the coalesced drag, in order.
    be = CursesBackend()
    be._mouse_down = True  # already dragging
    be._stdscr = _FakeStdscr(
        _sgr("[<32;9;3M")     # drag to x=8
        + _sgr("[<32;13;3M")  # drag to x=12
        + _sgr("[<0;13;3m")   # left release -> MOUSE_UP
    )
    events = _drain(be)
    assert [e.type for e in events] == [EventType.MOUSE_DRAG, EventType.MOUSE_UP]
    assert (events[0].x, events[0].y) == (12, 2)
