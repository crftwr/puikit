"""Curses input translation: get_wch() returns str chars and int key codes.

These exercise the pure translation logic without opening a real terminal
(constructing CursesBackend does not touch curses).
"""

import curses

from puikit.backends.curses_backend import CursesBackend
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


def test_set_clipboard_emits_osc52(monkeypatch, capsys):
    monkeypatch.delenv("TMUX", raising=False)
    be = CursesBackend()
    be.set_clipboard("hi")
    out = capsys.readouterr().out
    # OSC 52 to selection "c" with base64("hi") == "aGk=", BEL-terminated.
    assert out == "\x1b]52;c;aGk=\x07"
    assert be.get_clipboard() == "hi"  # process-local buffer kept for paste


def test_set_clipboard_wraps_for_tmux(monkeypatch, capsys):
    monkeypatch.setenv("TMUX", "/tmp/tmux-1/default,123,0")
    be = CursesBackend()
    be.set_clipboard("hi")
    out = capsys.readouterr().out
    assert out.startswith("\x1bPtmux;") and out.endswith("\x1b\\")
    assert "aGk=" in out


def test_pointer_shape_capability_is_opt_in():
    # A terminal cannot reliably honor OSC 22 and the support is unprobeable, so
    # the capability is off unless the backend is constructed with it.
    assert CursesBackend().capabilities.supports("pointer_shape") is False
    assert CursesBackend(pointer_shape=True).capabilities.supports("pointer_shape") is True


def test_set_pointer_shape_emits_osc22_only_on_change(capsys):
    be = CursesBackend(pointer_shape=True)
    be.set_pointer_shape("text")
    be.set_pointer_shape("text")  # unchanged: no second emit
    be.set_pointer_shape(None)    # reset to default arrow
    out = capsys.readouterr().out
    assert out == "\x1b]22;text\x07\x1b]22;\x07"


def test_set_pointer_shape_noop_when_disabled(capsys):
    be = CursesBackend()  # capability off
    be.set_pointer_shape("text")
    assert capsys.readouterr().out == ""


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
