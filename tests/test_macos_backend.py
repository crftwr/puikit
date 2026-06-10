"""MacOSBackend tests that run without opening a window."""

import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="macOS-only backend"
)
pytest.importorskip("AppKit", reason="pyobjc not installed")

from puikit import Style, TextAttribute  # noqa: E402
from puikit.backends.macos_backend import (  # noqa: E402
    MacOSBackend,
    translate_key,
)
from puikit.event import EventType  # noqa: E402


def test_translate_arrow_key():
    event = translate_key("\uf700")  # NSUpArrowFunctionKey
    assert event.type is EventType.KEY
    assert event.key == "up"


def test_translate_printable_char():
    event = translate_key("q")
    assert event.key == "q"
    assert event.char == "q"


def test_translate_control_keys():
    assert translate_key("\r").key == "enter"
    assert translate_key("\x1b").key == "escape"
    assert translate_key("\x7f").key == "backspace"


def test_translate_modifiers():
    from AppKit import NSEventModifierFlagCommand, NSEventModifierFlagShift

    event = translate_key("a", NSEventModifierFlagShift | NSEventModifierFlagCommand)
    assert event.modifiers == frozenset({"shift", "cmd"})


def test_translate_unknown_returns_none():
    assert translate_key("") is None
    assert translate_key("\x00") is None


def test_display_list_swaps_on_present():
    backend = MacOSBackend()  # not opened: no window is created
    backend.draw_text(1, 2, "hi", Style(attr=TextAttribute.BOLD))
    backend.draw_box(0, 0, 10, 5)
    assert backend._front == []
    backend.present()
    assert [cmd[0] for cmd in backend._front] == ["text", "box"]
    assert backend._back == []


def test_icons_become_glyph_text_commands():
    backend = MacOSBackend()
    backend.draw_icon(3, 4, "folder")
    backend.present()
    kind, x, y, glyph, _style = backend._front[0]
    assert (kind, x, y, glyph) == ("text", 3, 4, "📁")


def test_profile_declares_gui_capabilities():
    profile = MacOSBackend.PROFILE
    assert profile.supports("pixel_layout")
    assert profile.supports("icons")
    assert profile.supports("images")
    # Not implemented yet in the MVP:
    assert not profile.supports("animation")
    assert not profile.supports("system_tray")
