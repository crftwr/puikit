"""Curses input translation: get_wch() returns str chars and int key codes.

These exercise the pure translation logic without opening a real terminal
(constructing CursesBackend does not touch curses).
"""

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
