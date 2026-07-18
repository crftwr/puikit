"""MacOSBackend tests that run without opening a window."""

import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="macOS-only backend"
)
pytest.importorskip("AppKit", reason="pyobjc not installed")

from AppKit import NSFontAttributeName  # noqa: E402

from puikit import Font, FontSlant, FontWeight, Style, TextAttribute  # noqa: E402
from puikit.backends.macos_backend import (  # noqa: E402
    _BUNDLED_MONO,
    _BUNDLED_UI,
    MacOSBackend,
    _PuiKitView,
    _attr_string,
    _ensure_bundled_fonts,
    translate_key,
)
from puikit.event import EventType  # noqa: E402


def _advance(font, ch):
    """Rendered advance width of one glyph in ``font`` — used to check a face is
    monospaced by advance (the grid requirement) rather than by the unreliable
    post-table isFixedPitch flag."""
    return _attr_string(ch, {NSFontAttributeName: font}).size().width


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
    # monospace=False gives a proportional one. Check by advance width, not the
    # post-table isFixedPitch flag: the bundled Noto Sans Mono default is
    # monospaced by advance yet reports isFixedPitch False.
    backend = MacOSBackend()
    mono = backend.resolve_font(Font(monospace=True))
    prop = backend.resolve_font(Font())  # default UI font
    assert _advance(mono, "i") == _advance(mono, "M")  # equal advances -> grid
    assert _advance(prop, "i") != _advance(prop, "M")  # proportional


def test_resolve_font_defaults_to_bundled_noto():
    # With no family configured, the default mono/proportional pair is the
    # bundled Noto superfamily (matched metrics keep text from clipping),
    # registered with Core Text so it renders without being installed — the same
    # default the Windows backend uses. The font files are fetched at build time,
    # not committed, so both outcomes are valid: Noto when present, the OS system
    # faces (still mono / proportional) when not.
    backend = MacOSBackend()
    mono = backend.resolve_font(Font(monospace=True))
    prop = backend.resolve_font(Font())
    if _ensure_bundled_fonts():
        assert mono.familyName() == _BUNDLED_MONO
        assert prop.familyName() == _BUNDLED_UI
    else:
        assert mono.isFixedPitch()
        assert not prop.isFixedPitch()


def test_resolve_font_uses_configured_default_faces():
    # An unnamed Font() resolves to the configured ui_font family, and an unnamed
    # Font(monospace=True) to the base (mono) font family — so widgets share one
    # configurable pair of faces instead of each hardcoding the OS system font.
    backend = MacOSBackend(
        base_font=Font(family="Menlo", size=13, monospace=True),
        ui_font=Font(family="Helvetica Neue"),
    )
    assert backend.resolve_font(Font()).familyName() == "Helvetica Neue"
    assert backend.resolve_font(Font(monospace=True)).familyName() == "Menlo"
    # An explicit family still wins over the defaults.
    assert backend.resolve_font(Font(family="Georgia")).familyName() == "Georgia"
    # ui_font=None drops to the default proportional face (bundled Noto Sans, or
    # the OS system UI font if unavailable) — still proportional either way.
    b2 = MacOSBackend(base_font=Font(family="Menlo", size=13, monospace=True))
    assert not b2.resolve_font(Font()).isFixedPitch()


def test_resolve_font_applies_weight_and_slant():
    backend = MacOSBackend()
    bold = backend.resolve_font(Font(weight=FontWeight.BOLD))
    italic = backend.resolve_font(Font(slant=FontSlant.ITALIC))
    from AppKit import NSFontManager

    mgr = NSFontManager.sharedFontManager()
    assert mgr.traitsOfFont_(bold) & 0x2  # NSBoldFontMask
    # Italic is slanted either by a real italic member (the italic symbolic
    # trait) or, for a face with none — like the bundled Noto default — by a
    # synthesized oblique (a shear in the font matrix, matrix[2] != 0).
    italic_trait = mgr.traitsOfFont_(italic) & 0x1  # NSItalicFontMask
    assert italic_trait or italic.matrix()[2] != 0


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


def test_translate_shift_tab_is_backward_tab():
    from AppKit import NSEventModifierFlagShift

    # Shift+Tab: charactersIgnoringModifiers applies Shift, so the payload is
    # NSBackTabCharacter (0x19). It must resolve to a shift-modified tab so
    # focus traversal goes backward.
    event = translate_key("\x19", NSEventModifierFlagShift)
    assert event.key == "tab"
    assert "shift" in event.modifiers


def test_translate_modifiers():
    from AppKit import NSEventModifierFlagCommand, NSEventModifierFlagShift

    event = translate_key("a", NSEventModifierFlagShift | NSEventModifierFlagCommand)
    assert event.modifiers == frozenset({"shift", "cmd"})


def test_translate_option_arrows_and_delete_carry_alt_for_word_editing():
    from AppKit import NSEventModifierFlagOption

    # Option+Left/Right and Option+Backspace/Delete reach the field as
    # alt-modified keys — the TextEdit widget turns those into whole-word caret
    # moves and deletions. doCommandBySelector_ re-translates the raw key event
    # (keeping Option), so the word-editing command selectors need no per-name
    # mapping.
    assert translate_key("", NSEventModifierFlagOption).key == "left"
    assert "alt" in translate_key("", NSEventModifierFlagOption).modifiers
    assert translate_key("", NSEventModifierFlagOption).key == "right"
    back = translate_key("\x7f", NSEventModifierFlagOption)
    assert back.key == "backspace" and "alt" in back.modifiers
    fwd = translate_key("", NSEventModifierFlagOption)  # NSDeleteFunctionKey
    assert fwd.key == "delete" and "alt" in fwd.modifiers


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
    assert backend._front[0] == ("group_begin", id(scale_w), rect, False)
    backend.close()


class _FakeTimer:
    """Stand-in for NSTimer that records its interval and invalidation."""

    def __init__(self, interval):
        self.interval = interval
        self.invalidated = False

    def invalidate(self):
        self.invalidated = True


def _patch_nstimer(monkeypatch):
    """Replace NSTimer so the frame timer can be exercised without a run loop."""
    from puikit.backends import macos_backend as mb

    created = []

    class _FakeNSTimer:
        @staticmethod
        def scheduledTimerWithTimeInterval_repeats_block_(interval, repeats, block):
            timer = _FakeTimer(interval)
            created.append(timer)
            return timer

    monkeypatch.setattr(mb, "NSTimer", _FakeNSTimer)
    return created


def test_frame_timer_runs_slow_for_idle_pump_only(monkeypatch):
    # A permanent tick callback (e.g. TFM's filesystem pump) with no animation
    # keeps the timer alive but at the slow idle rate, not 60fps.
    _patch_nstimer(monkeypatch)
    backend = MacOSBackend()
    backend.request_animation_ticks(lambda: True)
    assert backend._anim_timer.interval == pytest.approx(MacOSBackend._IDLE_TICK_INTERVAL)


def test_frame_timer_speeds_up_for_animation_then_slows_back(monkeypatch):
    _patch_nstimer(monkeypatch)
    backend = MacOSBackend()

    # Idle pump established at the slow rate.
    backend.request_animation_ticks(lambda: True)
    idle_timer = backend._anim_timer
    assert idle_timer.interval == pytest.approx(MacOSBackend._IDLE_TICK_INTERVAL)

    # An animation starts: recreate at 60fps, retiring the slow timer.
    backend.animate(object(), {"duration_ms": 200})
    assert backend._anim_timer is not idle_timer
    assert idle_timer.invalidated
    assert backend._anim_timer.interval == pytest.approx(MacOSBackend._ANIM_INTERVAL)

    # Animation finishes but the pump remains: drop back to the slow rate.
    fast_timer = backend._anim_timer
    backend._animations.clear()
    backend._on_animation_tick(fast_timer)
    assert fast_timer.invalidated
    assert backend._anim_timer.interval == pytest.approx(MacOSBackend._IDLE_TICK_INTERVAL)


def test_frame_timer_stops_when_nothing_left(monkeypatch):
    _patch_nstimer(monkeypatch)
    backend = MacOSBackend()

    # Register a callback that unregisters itself on the next tick.
    backend._tick_callbacks = [lambda: False]
    backend._ensure_animation_timer()
    timer = backend._anim_timer
    assert timer is not None

    backend._on_animation_tick(timer)
    assert timer.invalidated
    assert backend._anim_timer is None
    assert backend._anim_timer_interval is None


def test_call_on_main_thread_posts_via_apphelper(monkeypatch):
    # The backend hands the callback to AppHelper.callAfter, which performs a
    # selector on the main thread (waking a blocked run loop). We only assert the
    # hand-off; the actual main-thread hop needs a running loop.
    from puikit.backends import macos_backend as mb

    posted = []
    monkeypatch.setattr(mb.AppHelper, "callAfter", lambda fn, *a, **k: posted.append(fn))

    backend = MacOSBackend()
    sentinel = lambda: None  # noqa: E731
    backend.call_on_main_thread(sentinel)
    assert posted == [sentinel]


def test_macos_backend_advertises_main_thread_dispatch():
    backend = MacOSBackend()
    assert backend.capabilities.supports("main_thread_dispatch")


def test_menu_shortcut_parsed_to_key_equivalent():
    # A puikit shortcut hint parses into (keyEquivalent char, modifier mask) for
    # native NSMenuItem rendering: letters lowercased (Shift lives in the mask),
    # named keys mapped to their control/function chars, punctuation kept.
    from AppKit import (
        NSEventModifierFlagCommand,
        NSEventModifierFlagOption,
        NSEventModifierFlagShift,
    )
    from puikit.backends._macos_menu import _key_equivalent

    assert _key_equivalent("V") == ("v", 0)
    assert _key_equivalent("Enter") == ("\r", 0)
    assert _key_equivalent("Backspace") == ("\x08", 0)
    assert _key_equivalent("Tab") == ("\t", 0)
    assert _key_equivalent("Shift-X") == ("x", NSEventModifierFlagShift)
    assert _key_equivalent("Cmd-Enter") == ("\r", NSEventModifierFlagCommand)
    assert _key_equivalent("Alt-Enter") == ("\r", NSEventModifierFlagOption)
    assert _key_equivalent("Cmd-Shift-C") == (
        "c", NSEventModifierFlagCommand | NSEventModifierFlagShift)
    assert _key_equivalent("Shift-=") == ("=", NSEventModifierFlagShift)
    assert _key_equivalent(";") == (";", 0)
    # An unknown modifier makes the whole hint unrepresentable (no wrong glyph).
    assert _key_equivalent("Hyper-Z") is None


def test_menu_sets_display_only_key_equivalent_and_does_not_fire():
    # The content menu shows accelerators (keyEquivalent set) but is a
    # _NonFiringMenu whose performKeyEquivalent: declines, so the keystroke is
    # never swallowed by the menu — it falls through to the app's key handling.
    from AppKit import NSEventModifierFlagCommand, NSMenu
    from puikit.backends import _macos_menu as mm
    from puikit.menu import Menu, MenuItem

    menu = Menu(
        MenuItem("Copy Name(s)", on_select=lambda: None, shortcut="Cmd-Shift-C"),
        MenuItem("View File", on_select=lambda: None, shortcut="V"),
        MenuItem("Reverse Sort", on_select=lambda: None),          # unbound
        MenuItem("Sort By", submenu=Menu(title="Sort By"), shortcut="S"),  # parent
    )
    responder = mm._MenuResponder.alloc().init()
    ns_menu = mm._build_menu(menu, responder)

    assert isinstance(ns_menu, mm._NonFiringMenu)
    assert isinstance(ns_menu, NSMenu)
    assert ns_menu.performKeyEquivalent_(None) is False  # declines -> no hijack

    copy, view, reverse, sort_by = (ns_menu.itemAtIndex_(i) for i in range(4))
    assert copy.keyEquivalent() == "c"
    assert copy.keyEquivalentModifierMask() & NSEventModifierFlagCommand
    assert view.keyEquivalent() == "v"
    assert reverse.keyEquivalent() == ""    # unbound -> no accelerator
    assert sort_by.keyEquivalent() == ""    # parents carry no accelerator
    assert sort_by.hasSubmenu()


# --- IME context gating --------------------------------------------------------

class _SpyContext:
    """Stand-in for the NSTextInputContext, recording activate/deactivate so the
    focus-gated IME engagement can be checked without a key window."""

    def __init__(self):
        self.calls = []

    def activate(self):
        self.calls.append("activate")

    def deactivate(self):
        self.calls.append("deactivate")

    def invalidateCharacterCoordinates(self):
        self.calls.append("invalidate")

    def discardMarkedText(self):
        pass


class _FakeBackend:
    _text_input_active = False


def _view_with_spy():
    from Foundation import NSMakeRect
    view = _PuiKitView.alloc().initWithFrame_(NSMakeRect(0, 0, 10, 10))
    view.backend = _FakeBackend()
    view._input_context = _SpyContext()
    return view, view._input_context


def test_input_context_hidden_in_command_mode():
    # inputContext() reports nil in command mode so the system treats the view as
    # not a text-input client — no inline IME UI on the window, even on app
    # reactivation (which the system re-queries, unlike becomeFirstResponder). It
    # exposes the real context once a text widget holds focus.
    view, spy = _view_with_spy()
    view.backend._text_input_active = False
    assert view.inputContext() is None
    view.backend._text_input_active = True
    assert view.inputContext() is spy


def test_input_context_disengaged_in_command_mode():
    # No text widget focused: the context is deactivated, so a CJK input source
    # is not left armed while navigating (its input-mode indicator won't show).
    view, spy = _view_with_spy()
    view.backend._text_input_active = False
    view._sync_input_context()
    assert spy.calls == ["deactivate"]


def test_input_context_engaged_when_text_focused():
    # A text widget holds focus: the context is activated so IME composition works.
    view, spy = _view_with_spy()
    view.backend._text_input_active = True
    view._sync_input_context()
    assert spy.calls == ["activate"]


def test_request_text_input_defers_reinvalidate_on_move(monkeypatch):
    # macOS pulls the IME caret rect (firstRectForCharacterRange): an invalidate
    # issued from inside the setMarkedText: callback — the widget re-reporting its
    # caret as the user cycles conversion clauses with left/right — is swallowed
    # while the IME is mid-update. So request_text_input re-issues it on the next
    # run-loop turn, but ONLY when the anchor actually moved, so the per-frame
    # caret re-assertion (blink, raw kana typing) schedules nothing.
    from puikit.backends import macos_backend as mb

    posted = []
    monkeypatch.setattr(mb.AppHelper, "callAfter", lambda fn, *a, **k: posted.append(fn))

    backend = MacOSBackend()
    view, spy = _view_with_spy()
    backend._view = view
    view.backend = backend
    backend._text_input_active = True

    # First move off the origin: invalidate now + a deferred re-query scheduled.
    backend.request_text_input(3.0, 5.0)
    assert spy.calls == ["invalidate"]
    assert posted == [backend._reinvalidate_ime_coordinates]

    # Same position (a blink re-assertion): invalidate again, but nothing new
    # deferred — the clause anchor didn't move.
    spy.calls.clear()
    backend.request_text_input(3.0, 5.0)
    assert spy.calls == ["invalidate"]
    assert len(posted) == 1

    # A new position (the selected clause moved): schedule another re-query.
    backend.request_text_input(7.0, 5.0)
    assert len(posted) == 2

    # The deferred callback re-invalidates while the view is alive, and is a safe
    # no-op after teardown (it runs a turn later, possibly after the field closed).
    spy.calls.clear()
    backend._reinvalidate_ime_coordinates()
    assert spy.calls == ["invalidate"]
    backend._view = None
    backend._reinvalidate_ime_coordinates()  # must not raise


def test_ime_caret_x_indexes_reported_character_layout():
    # firstRectForCharacterRange: positions the candidate window under the exact
    # composition character the IME asks about. _ime_caret_x maps that char offset
    # to the base-unit x the widget reported for each character boundary, so the
    # window follows the selected clause; out-of-range offsets clamp to the ends.
    from AppKit import NSNotFound

    backend = MacOSBackend()
    backend._input_caret = (99.0, 5.0)  # the single-anchor fallback

    # No composition reported yet: fall back to the anchor x for any offset.
    assert backend._ime_caret_x(0) == 99.0
    assert backend._ime_caret_x(3) == 99.0

    # A composition of 4 chars → 5 boundary positions.
    backend.request_text_input(10.0, 5.0, {"ime_char_xs": [10.0, 12.0, 14.0, 16.0, 18.0]})
    assert backend._ime_caret_x(0) == 10.0     # composition start
    assert backend._ime_caret_x(2) == 14.0     # the clause starting at char 2
    assert backend._ime_caret_x(4) == 18.0     # past the last char (end boundary)
    assert backend._ime_caret_x(9) == 18.0     # beyond the layout clamps to the end
    assert backend._ime_caret_x(NSNotFound) == 10.0  # unknown range -> reported anchor

    # Composition ends: layout is cleared, back to the single anchor.
    backend.request_text_input(20.0, 5.0)
    assert backend._input_char_xs is None
    assert backend._ime_caret_x(2) == 20.0


def test_begin_end_text_input_toggle_the_context():
    # begin/end_text_input flip the flag and mirror it onto the context, and
    # end_text_input tears down any composition first.
    backend = MacOSBackend()
    view, spy = _view_with_spy()
    backend._view = view
    view.backend = backend

    backend.begin_text_input()
    assert backend._text_input_active is True
    assert spy.calls == ["activate"]

    spy.calls.clear()
    backend.end_text_input()
    assert backend._text_input_active is False
    assert spy.calls == ["deactivate"]
