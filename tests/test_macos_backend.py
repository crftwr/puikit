"""MacOSBackend tests that run without opening a window."""

import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="macOS-only backend"
)
pytest.importorskip("AppKit", reason="pyobjc not installed")

from puikit import Font, FontSlant, FontWeight, Style, TextAttribute  # noqa: E402
from puikit.backends.macos_backend import (  # noqa: E402
    MacOSBackend,
    translate_key,
)
from puikit.event import EventType  # noqa: E402


def test_base_font_drives_base_unit():
    # The base unit is derived from the base font's glyph box (font -> base
    # unit), and it scales with the font size. _init_fonts needs NSFont only,
    # not a window.
    small = MacOSBackend(base_font=Font(size=12, monospace=True))
    small._init_fonts()
    large = MacOSBackend(base_font=Font(size=24, monospace=True))
    large._init_fonts()
    assert small.base_size[0] >= 1 and small.base_size[1] >= 1
    # A bigger base font means a bigger base unit, both axes.
    assert large.base_size[0] > small.base_size[0]
    assert large.base_size[1] > small.base_size[1]


def test_resolve_font_honors_monospace_and_proportional():
    # No family: monospace=True gives a fixed-advance face (the base grid font),
    # monospace=False gives the proportional system UI font.
    backend = MacOSBackend()
    mono = backend.resolve_font(Font(monospace=True))
    prop = backend.resolve_font(Font())  # default UI font
    assert mono.isFixedPitch()
    assert not prop.isFixedPitch()


def test_resolve_font_applies_weight_and_slant():
    backend = MacOSBackend()
    bold = backend.resolve_font(Font(weight=FontWeight.BOLD))
    italic = backend.resolve_font(Font(slant=FontSlant.ITALIC))
    from AppKit import NSFontManager

    mgr = NSFontManager.sharedFontManager()
    assert mgr.traitsOfFont_(bold) & 0x2  # NSBoldFontMask
    assert mgr.traitsOfFont_(italic) & 0x1  # NSItalicFontMask


def test_style_font_is_cached():
    backend = MacOSBackend()
    style = Style(font=Font(family="Georgia", size=18))
    first = backend._resolve_style_font(style)
    assert backend._resolve_style_font(style) is first


def test_measure_text_base_font_counts_columns():
    backend = MacOSBackend()
    backend._init_fonts()
    assert backend.measure_text("hello") == 5.0


def test_measure_text_proportional_is_not_column_count():
    backend = MacOSBackend()
    backend._init_fonts()
    width = backend.measure_text("WWWWW", Style(font=Font()))
    # A proportional run of wide glyphs measures wider than its column count.
    assert width > 5.0


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
    assert profile.supports("animation")
    assert profile.supports("vector_shapes")
    # Not implemented yet in the MVP:
    assert not profile.supports("system_tray")


def test_vector_primitives_record_display_list_commands():
    backend = MacOSBackend()  # not opened: no window is created
    backend.draw_round_rect(0, 0, 4, 1, 4.0, Style(bg=(1, 2, 3)), {"fill": True})
    backend.draw_check(0, 0, 1, 1, Style(fg=(255, 255, 255)))
    backend.present()
    assert [cmd[0] for cmd in backend._front] == ["round_rect", "check"]
    rr = backend._front[0]
    assert rr[5] == 4.0  # radius carried through
    assert rr[7] == {"fill": True}


def test_animation_progress_and_easing():
    from puikit.backends.macos_backend import Animation

    anim = Animation(kind="fade", duration=0.2, start=100.0)
    assert anim.progress(100.0) == 0.0
    assert anim.eased(100.0) == 0.0
    assert anim.progress(100.1) == pytest.approx(0.5)
    assert anim.eased(100.1) == pytest.approx(0.75)  # ease-out is past linear
    assert anim.progress(100.2) == 1.0
    assert anim.eased(100.2) == 1.0
    assert not anim.done(100.19)
    assert anim.done(100.2)
    # Zero duration completes immediately (defensive).
    assert Animation(kind="fade", duration=0.0, start=100.0).done(100.0)


def test_animate_registers_and_groups_wrap_commands():
    backend = MacOSBackend()  # not opened: no window, no timer thread needed
    widget = object()
    backend.animate(widget, {"transition": "fade", "duration_ms": 150})
    assert id(widget) in backend._animations
    assert backend._animations[id(widget)].duration == pytest.approx(0.15)

    backend.begin_group(widget)
    backend.draw_text(0, 0, "hi")
    backend.end_group(widget)
    backend.present()
    kinds = [cmd[0] for cmd in backend._front]
    assert kinds == ["group_begin", "text", "group_end"]
    assert backend._front[0][1] == id(widget)
    backend.close()  # invalidates the animation timer


def test_animation_kinds_carry_their_hints():
    from puikit import Rect

    backend = MacOSBackend()
    slide_w, scale_w, color_w = object(), object(), object()
    backend.animate(slide_w, {"transition": "slide", "from_dx": -8, "duration_ms": 300})
    backend.animate(scale_w, {"transition": "scale", "from_scale": 0.5})
    backend.animate(color_w, {"transition": "highlight", "color": (205, 49, 49)})
    assert backend._animations[id(slide_w)].kind == "slide"
    assert backend._animations[id(slide_w)].hints["from_dx"] == -8
    assert backend._animations[id(scale_w)].hints["from_scale"] == 0.5
    assert backend._animations[id(color_w)].hints["color"] == (205, 49, 49)

    # Group markers carry the widget rect so transforms know their pivot.
    rect = Rect(2, 3, 10, 5)
    backend.begin_group(scale_w, rect)
    backend.end_group(scale_w)
    backend.present()
    assert backend._front[0] == ("group_begin", id(scale_w), rect)
    backend.close()
