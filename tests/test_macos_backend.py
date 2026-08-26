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
    _load_tray_image,
    _tray_image_2x_path,
    _tray_image_is_template,
    translate_key,
)
from puikit.event import EventType  # noqa: E402
from puikit.text import display_width, glyph_runs  # noqa: E402


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
    # The grid font is an explicit unsized/unnamed monospace request; the
    # default style (font=None) is no longer grid - see the next test.
    assert backend.measure_text("hello", Style(font=Font(monospace=True))) == 5.0


def test_measure_text_default_style_measures_as_drawn():
    backend = MacOSBackend()
    backend._init_fonts()
    # font=None draws as the proportional UI font (Panel._resolve), so it
    # measures as that font too - mirroring measure_line_height. Measuring it
    # by columns over-sized every content-sized default-font label.
    assert backend.measure_text("hello") == backend.measure_text(
        "hello", Style(font=Font()))


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
    # set_tray shipped (NSStatusItem menu bar extra):
    assert profile.supports("system_tray")


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
    """Stand-in for NSTimer that records its interval, invalidation, and the
    run-loop mode it was added in (None until added)."""

    def __init__(self, interval):
        self.interval = interval
        self.invalidated = False
        self.mode = None

    def invalidate(self):
        self.invalidated = True


def _patch_nstimer(monkeypatch):
    """Replace NSTimer and NSRunLoop so the frame timer can be exercised without
    a real run loop. The frame timer is created unscheduled and added in the
    common modes (see _ensure_animation_timer); the fake run loop records the
    mode on the timer so a test can assert it."""
    from puikit.backends import macos_backend as mb

    created = []

    class _FakeNSTimer:
        @staticmethod
        def scheduledTimerWithTimeInterval_repeats_block_(interval, repeats, block):
            timer = _FakeTimer(interval)
            created.append(timer)
            return timer

        @staticmethod
        def timerWithTimeInterval_repeats_block_(interval, repeats, block):
            timer = _FakeTimer(interval)
            created.append(timer)
            return timer

    class _FakeNSRunLoop:
        @staticmethod
        def currentRunLoop():
            return _FakeNSRunLoop()

        def addTimer_forMode_(self, timer, mode):
            timer.mode = mode

    monkeypatch.setattr(mb, "NSTimer", _FakeNSTimer)
    monkeypatch.setattr(mb, "NSRunLoop", _FakeNSRunLoop)
    return created


def test_frame_timer_runs_slow_for_idle_pump_only(monkeypatch):
    # A permanent tick callback (e.g. XeFM's filesystem pump) with no animation
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


def test_frame_timer_fires_during_live_resize_tracking(monkeypatch):
    # The frame timer is added in the *common* run-loop modes: a default-mode
    # timer stops firing while the loop is in event-tracking mode (a live window
    # resize, menu tracking), which froze the shader background for the whole
    # drag and left the newly exposed window area white (xefm issue #290).
    from puikit.backends.macos_backend import NSRunLoopCommonModes

    _patch_nstimer(monkeypatch)
    backend = MacOSBackend()
    backend.request_animation_ticks(lambda: True)
    assert backend._anim_timer.mode == NSRunLoopCommonModes


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


def _batch_segments(backend, text):
    """The column segmentation _render_text performs, as (text, column) pairs:
    one entry per batched run or solo glyph. Mirrors the draw loop without
    needing a graphics context."""
    ns_font = backend._fonts[TextAttribute.NORMAL]
    runs = glyph_runs(text)
    widths = [max(1, display_width(g)) for g in runs]
    on_grid = backend._grid_batchable(ns_font, runs, widths)
    out, col, i, n = [], 0, 0, len(runs)
    while i < n:
        if on_grid[i]:
            j = i
            while j < n and on_grid[j]:
                j += 1
            out.append(("".join(runs[i:j]), col))
            col += j - i
            i = j
        else:
            out.append((runs[i], col))
            col += widths[i]
            i += 1
    return out


def test_grid_batches_only_glyphs_that_advance_one_column():
    # The grid path draws a run of glyphs as ONE kerned string, which is only
    # sound while every glyph in it advances by the base face's advance. Ordinary
    # text does; a glyph the face draws at another advance must be excluded, or
    # it drags everything after it in the same string off the column grid.
    backend = MacOSBackend(base_font=Font(size=12, monospace=True))
    backend._init_fonts()
    ns_font = backend._fonts[TextAttribute.NORMAL]

    def batchable(glyph):
        return backend._grid_batchable(ns_font, [glyph], [max(1, display_width(glyph))])[0]

    # Latin from the base face itself: batchable, and that is the fast path.
    for ch in "Ma0 .-_/":
        assert batchable(ch), ch
    # Off-grid by fallback: the base face lacks these, so the cascade draws them
    # at its own advance (Noto CJK, 12.0 against a 7.2 grid).
    for ch in "♡★":
        assert not batchable(ch), ch
        assert _advance(ns_font, ch) != _advance(ns_font, "M")
    # Off-grid while *covered*: U+25B6 is in Noto Sans Mono at double advance,
    # yet East-Asian-Ambiguous, so display_width calls it one column. A coverage
    # test would wrongly batch this one.
    assert ns_font.coveredCharacterSet().longCharacterIsMember_(0x25B6)
    assert not batchable("▶")


def test_grid_segmentation_keeps_columns_after_an_off_grid_glyph():
    # The regression itself: text after ♡ / ▶ must still start on its own column.
    backend = MacOSBackend(base_font=Font(size=12, monospace=True))
    backend._init_fonts()

    assert _batch_segments(backend, "Deselected: ♡aaa") == [
        ("Deselected: ", 0), ("♡", 12), ("aaa", 13),
    ]
    assert _batch_segments(backend, "cols ▶ after") == [
        ("cols ", 0), ("▶", 5), (" after", 6),
    ]
    # A wide glyph still takes two columns, and Latin either side stays batched.
    assert _batch_segments(backend, "a漢b") == [("a", 0), ("漢", 1), ("b", 3)]
    # Pure ASCII is untouched: one segment, one draw call.
    assert _batch_segments(backend, "Selected: test.txt") == [("Selected: test.txt", 0)]


def test_glyph_metrics_memoized_per_face():
    backend = MacOSBackend(base_font=Font(size=12, monospace=True))
    backend._init_fonts()
    ns_font = backend._fonts[TextAttribute.NORMAL]
    backend._grid_batchable(ns_font, ["M", "♡"], [1, 1])
    cache = backend._glyph_metric_cache[id(ns_font)]
    assert set(cache) == {"M", "♡"}
    # (advance, ink_x, ink_width): the base face advances "M" by the grid
    # advance; ♡ comes from the cascade at a wider one.
    assert cache["M"][0] == pytest.approx(backend._grid_advance)
    assert cache["♡"][0] > backend._grid_advance
    assert cache["♡"][2] > 0.0  # real ink, so _solo_fit has something to seat


def test_solo_fit_seats_an_oversized_glyph_without_touching_one_that_fits():
    # A glyph excluded from a batched run is placed alone at its column, but it
    # was drawn for a wider box than the grid gives it. _solo_fit is the
    # horizontal (scale, translate) that keeps its ink inside that cell.
    backend = MacOSBackend(base_font=Font(size=12, monospace=True))
    backend._init_fonts()
    ns_font = backend._fonts[TextAttribute.NORMAL]
    cell = float(backend._base_w)

    def seated(glyph, columns=1):
        """Where the glyph's ink lands in its cell after _solo_fit, as
        (left, right) offsets from the cell's left edge."""
        slot = columns * cell
        _, ink_x, ink_w = backend._glyph_metrics(ns_font, glyph)
        fit = backend._solo_fit(ns_font, glyph, slot)
        scale, shift = (1.0, 0.0) if fit is None else fit
        left = scale * ink_x + shift
        return left, left + scale * ink_w, slot

    # ♡ is inked wider than a column: scaled down, and the result fills the cell
    # exactly without crossing either edge.
    left, right, slot = seated("♡")
    assert backend._solo_fit(ns_font, "♡", cell)[0] < 1.0    # scaled
    assert left == pytest.approx(0.0) and right == pytest.approx(slot)

    # ▶ carries narrow ink in a double-wide advance: no scaling, just a nudge
    # back inside the cell. Squeezing this one would needlessly distort it.
    assert backend._solo_fit(ns_font, "▶", cell)[0] == 1.0
    left, right, slot = seated("▶")
    assert left >= -0.01 and right <= slot + 0.01

    # A glyph whose ink already fits is left exactly as the face designed it —
    # ideographic punctuation hugs one side of its em box on purpose, and a CJK
    # ideograph's slack in a two-column slot is the face's own side bearing.
    for glyph in "。「」漢あ":
        assert backend._solo_fit(ns_font, glyph, 2 * cell) is None, glyph
    for glyph in "ｱｲｳ":  # halfwidth katakana: one column, narrow ink
        assert backend._solo_fit(ns_font, glyph, cell) is None, glyph


# --- set_tray image loading -------------------------------------------------

def _write_png(path, size, rgba=(0, 0, 0, 255)):
    """Minimal solid-color RGBA PNG, enough for NSImage to load."""
    import struct
    import zlib

    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(
            ">I", zlib.crc32(body))

    raw = b"".join(b"\x00" + bytes(rgba) * size for _ in range(size))
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def test_tray_image_template_naming_convention():
    # AppKit's imageNamed: rule, applied to paths: "Template" stem suffix,
    # judged before the @2x scale suffix.
    assert _tray_image_is_template("/a/MenuExtraTemplate.png")
    assert _tray_image_is_template("/a/MenuExtraTemplate@2x.png")
    assert not _tray_image_is_template("/a/icon.png")
    assert not _tray_image_is_template("/a/icon@2x.png")
    assert not _tray_image_is_template("/a/Template-notes.png")  # prefix only
    assert _tray_image_2x_path("/a/icon.png") == "/a/icon@2x.png"


def test_load_tray_image_applies_template_and_2x_sibling(tmp_path):
    _write_png(tmp_path / "MenuExtraTemplate.png", 18)
    _write_png(tmp_path / "MenuExtraTemplate@2x.png", 36)
    image = _load_tray_image(str(tmp_path / "MenuExtraTemplate.png"))
    assert image is not None
    assert image.isTemplate()
    # The point size stays the 1x size; the @2x file rides along as a second
    # representation at that same point size, so AppKit picks it on Retina.
    assert (image.size().width, image.size().height) == (18.0, 18.0)
    assert len(image.representations()) == 2
    assert all((rep.size().width, rep.size().height) == (18.0, 18.0)
               for rep in image.representations())


def test_load_tray_image_plain_png_is_not_template(tmp_path):
    _write_png(tmp_path / "icon.png", 16)
    image = _load_tray_image(str(tmp_path / "icon.png"))
    assert image is not None
    assert not image.isTemplate()
    assert len(image.representations()) == 1


def test_load_tray_image_missing_file_returns_none(tmp_path):
    assert _load_tray_image(str(tmp_path / "nope.png")) is None


def test_load_tray_image_svg_vector_template(tmp_path):
    # NSImage loads SVG natively on macOS 11+, so a "…Template.svg" gives a
    # resolution-independent template image — no @2x sibling needed (and the
    # rep reports no fixed pixel size; it rasterizes on demand at any scale).
    (tmp_path / "MenuExtraTemplate.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18">'
        '<rect width="18" height="18"/></svg>')
    image = _load_tray_image(str(tmp_path / "MenuExtraTemplate.svg"))
    assert image is not None
    assert image.isTemplate()
    assert (image.size().width, image.size().height) == (18.0, 18.0)
    assert len(image.representations()) == 1


# --- native menu: key equivalents stay display-only --------------------------
#
# A puikit MenuItem.shortcut is a display hint; the app owns key dispatch
# (_NonFiringMenu). But an active app's menu-bar dispatch can fire an item
# from AppKit's cached equivalent table without consulting that override, so
# _MenuResponder.fire_ re-routes an activation carried by the item's own chord
# back to the app's key handling. These cover that routing decision.


class _FakeKeyEvent:
    """Stands in for the NSEvent _current_key_event returns: fire_'s matching
    only reads charactersIgnoringModifiers() and modifierFlags()."""

    def __init__(self, chars, flags):
        self._chars = chars
        self._flags = flags

    def charactersIgnoringModifiers(self):
        return self._chars

    def modifierFlags(self):
        return self._flags


def _menu_fixture(forward=None, shortcut="Command-Shift-C"):
    """A responder plus one registered NSMenuItem carrying ``shortcut``.
    Returns (responder, ns_item, activated) where ``activated`` records the
    puikit item's on_select firing."""
    from puikit.backends import _macos_menu
    from puikit.menu import Menu, MenuItem

    activated = []
    menu = Menu(MenuItem("Copy", on_select=lambda: activated.append(True),
                         shortcut=shortcut))
    ns_menu, responder = _macos_menu.build_popup_menu(menu)
    responder._forward_key = forward
    return responder, ns_menu.itemArray()[0], activated


def test_menu_fire_forwards_its_own_key_equivalent(monkeypatch):
    from AppKit import NSEventModifierFlagCommand, NSEventModifierFlagShift

    from puikit.backends import _macos_menu

    forwarded = []
    responder, ns_item, activated = _menu_fixture(forward=forwarded.append)
    chord = _FakeKeyEvent("C", NSEventModifierFlagCommand | NSEventModifierFlagShift)
    monkeypatch.setattr(_macos_menu, "_current_key_event", lambda: chord)
    responder.fire_(ns_item)
    assert forwarded == [chord]
    assert activated == []


def test_menu_fire_activates_on_mouse_selection(monkeypatch):
    from puikit.backends import _macos_menu

    forwarded = []
    responder, ns_item, activated = _menu_fixture(forward=forwarded.append)
    # A mouse activation has no keyDown as the current event.
    monkeypatch.setattr(_macos_menu, "_current_key_event", lambda: None)
    responder.fire_(ns_item)
    assert activated == [True]
    assert forwarded == []


def test_menu_fire_activates_on_return_over_open_menu(monkeypatch):
    from puikit.backends import _macos_menu

    forwarded = []
    responder, ns_item, activated = _menu_fixture(forward=forwarded.append)
    # Keyboard navigation of an OPEN menu ends in a Return keyDown — a real
    # interactive selection, which must keep activating (it does not match the
    # item's own equivalent).
    monkeypatch.setattr(_macos_menu, "_current_key_event",
                        lambda: _FakeKeyEvent("\r", 0))
    responder.fire_(ns_item)
    assert activated == [True]
    assert forwarded == []


def test_menu_fire_without_forwarder_always_activates(monkeypatch):
    from AppKit import NSEventModifierFlagCommand, NSEventModifierFlagShift

    from puikit.backends import _macos_menu

    # A popup's responder has no forwarder: even a matching chord activates
    # (an equivalent pressed while a popup is open is an interactive choice).
    responder, ns_item, activated = _menu_fixture(forward=None)
    chord = _FakeKeyEvent("C", NSEventModifierFlagCommand | NSEventModifierFlagShift)
    monkeypatch.setattr(_macos_menu, "_current_key_event", lambda: chord)
    responder.fire_(ns_item)
    assert activated == [True]


class TestNonactivatingPanelWindow:
    """The real NSPanel, built through create_window (no event loop needed).

    Behaviour verified against a live session and asserted here as the
    properties AppKit ends up holding, so a refactor cannot quietly drop one.
    """

    @pytest.fixture
    def backend(self):
        from puikit.backend import WindowStyle
        b = MacOSBackend(activation_policy="accessory")
        b.open()
        b.hide_main_window()
        yield b, WindowStyle
        b.close()

    def test_plain_non_activating_window_is_not_a_panel(self, backend):
        from AppKit import NSPanel
        b, WindowStyle = backend
        win = b.create_window(20, 4, style=WindowStyle(activates=False))
        assert not isinstance(win.nswindow, NSPanel)

    def test_panel_can_become_key_without_activating(self, backend):
        from AppKit import NSPanel
        b, WindowStyle = backend
        win = b.create_window(20, 4, style=WindowStyle(
            activates=False, nonactivating_panel=True))
        assert isinstance(win.nswindow, NSPanel)
        # The mask needs a titled panel: a borderless one cannot become key.
        assert win.nswindow.canBecomeKeyWindow()
        assert win.nswindow.becomesKeyOnlyIfNeeded() is False
        # Or a utility panel hides itself whenever the app is not active,
        # which for this window is always.
        assert win.nswindow.hidesOnDeactivate() is False

    def test_on_demand_leaves_the_keyboard_alone(self, backend):
        b, WindowStyle = backend
        win = b.create_window(20, 4, style=WindowStyle(
            activates=False, nonactivating_panel=True,
            becomes_key_on_demand=True))
        assert win.nswindow.becomesKeyOnlyIfNeeded() is True
        assert not win.nswindow.isKeyWindow()

    def test_frameless_panel_hides_the_forced_title_bar(self, backend):
        from AppKit import NSWindowTitleHidden
        b, WindowStyle = backend
        win = b.create_window(20, 4, style=WindowStyle(
            frameless=True, activates=False, nonactivating_panel=True))
        ns = win.nswindow
        assert ns.titlebarAppearsTransparent()
        assert ns.titleVisibility() == NSWindowTitleHidden
        # Full-size content also puts the content rect back to the frame
        # rect, so a frameless panel measures like a frameless window.
        assert (ns.contentView().frame().size.height
                == ns.frame().size.height)

    def test_a_never_key_window_tracks_always(self, backend):
        from AppKit import NSTrackingActiveAlways
        b, WindowStyle = backend
        win = b.create_window(20, 4, style=WindowStyle(
            activates=False, nonactivating_panel=True,
            becomes_key_on_demand=True))
        win.view.updateTrackingAreas()
        areas = win.view.trackingAreas()
        assert areas, "a window that is never key still needs cursor updates"
        assert all(a.options() & NSTrackingActiveAlways for a in areas)
