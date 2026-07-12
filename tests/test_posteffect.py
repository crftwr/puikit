"""PostEffect descriptor, capability gating, and the macOS Core Image mapping."""

import pytest

from puikit import CRT, PostEffect, PROFILE_GUI_WEB, PROFILE_TUI
from puikit.posteffect import PRESETS
from puikit.backends.memory_backend import MemoryBackend

# A tinted, strongly-rolling effect for the tint / roll-render coverage (no named
# preset ships a tint).
TINTED = PostEffect(tint=(70, 240, 130), bloom=0.28, scanline=0.42, vignette=0.4,
                    glow=0.32, roll=0.45)


# --- the backend-agnostic descriptor -------------------------------------------

def test_default_is_a_noop():
    e = PostEffect()
    assert e.name == "crt"
    assert e.tint is None
    assert e.is_noop


def test_any_strength_or_tint_is_not_a_noop():
    assert not PostEffect(bloom=0.2).is_noop
    assert not PostEffect(tint=(0, 255, 0)).is_noop


def test_strengths_are_clamped_to_unit_range():
    e = PostEffect(bloom=5.0, scanline=-1.0, vignette=0.5)
    assert e.bloom == 1.0
    assert e.scanline == 0.0
    assert e.vignette == 0.5


def test_frozen():
    with pytest.raises(Exception):
        PostEffect().bloom = 0.5  # type: ignore[misc]


def test_with_tint_derives_a_copy():
    base = CRT
    tinted = base.with_tint((10, 20, 30))
    assert base.tint is None            # original untouched
    assert tinted.tint == (10, 20, 30)
    assert tinted.bloom == base.bloom   # everything else preserved


def test_presets():
    assert CRT.tint is None             # meant to pair with a monochrome theme
    assert CRT.bloom > 0 and CRT.glow > 0
    assert PRESETS == {"crt": CRT}      # only the CRT preset ships


def test_roll_field():
    assert PostEffect().roll == 0.0
    assert PostEffect(roll=5).roll == 1.0       # clamped
    assert not PostEffect(roll=0.3).is_noop     # roll alone is a real effect


def test_pixelgrid_field():
    assert PostEffect().pixelgrid == 0.0
    assert PostEffect(pixelgrid=5).pixelgrid == 1.0    # clamped
    assert PostEffect(pixelgrid=-1).pixelgrid == 0.0   # clamped
    assert not PostEffect(pixelgrid=0.2).is_noop       # the LCD grid alone is a real effect
    assert CRT.roll > 0                          # preset rolls by default


# --- capability gating ---------------------------------------------------------

def test_tui_has_no_post_effects():
    assert not PROFILE_TUI.supports("post_effects")


def test_gui_web_off_until_a_backend_implements_it():
    # The shared GUI profile leaves it off; each backend flips it True in its own
    # PROFILE as the composite pass lands (macOS has, the web canvas has not).
    assert not PROFILE_GUI_WEB.supports("post_effects")


def test_memory_backend_no_post_effects_and_base_set_is_a_safe_noop():
    be = MemoryBackend()
    assert not be.capabilities.supports("post_effects")
    # Inherited base no-op: accepting a call without a capability must not raise.
    be.set_post_effect(CRT)
    be.set_post_effect(None)


# --- macOS Core Image mapping (skipped where Core Image is unavailable) ---------

def _macos():
    mb = pytest.importorskip("puikit.backends.macos_backend")
    if not mb._HAS_COREIMAGE:
        pytest.skip("Core Image unavailable")
    return mb


def test_macos_declares_post_effects_when_coreimage_present():
    mb = _macos()
    assert mb.MacOSBackend().capabilities.supports("post_effects")


def test_macos_filter_chain_with_tint():
    mb = _macos()
    # A tinted effect's color chain (scanlines and vignette are painted in the
    # render pass, not filters).
    names = [f.name() for f in mb._build_ci_filters(TINTED)]
    assert names == ["CIColorMonochrome", "CIColorControls", "CIBloom"]


def test_macos_filter_chain_for_crt_has_no_monochrome():
    mb = _macos()
    names = [f.name() for f in mb._build_ci_filters(CRT)]
    assert "CIColorMonochrome" not in names
    assert "CIBloom" in names


def test_macos_bloom_radius_stays_below_scanline_pitch():
    # Bloom is a content filter applied over the painted scanlines, so its radius
    # must stay under the pitch or it washes the lines out.
    mb = _macos()
    bloom = [f for f in mb._build_ci_filters(TINTED) if f.name() == "CIBloom"][0]
    assert float(bloom.valueForKey_("inputRadius")) <= mb._SCANLINE_PERIOD * 0.6


def test_macos_scanlines_darken_alternating_rows():
    """The render-pass scanline overlay actually dims alternate rows."""
    mb = _macos()
    from AppKit import NSBitmapImageRep, NSColor, NSImage, NSRectFill
    from Foundation import NSMakeRect

    w, h = 8, 40
    be = mb.MacOSBackend()
    be._view = mb._PuiKitView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))
    img = NSImage.alloc().initWithSize_((w, h))
    img.lockFocus()
    try:
        NSColor.whiteColor().setFill()
        NSRectFill(((0, 0), (w, h)))
        be._render_scanlines(0.9)
        rep = NSBitmapImageRep.alloc().initWithFocusedViewRect_(((0, 0), (w, h)))
    finally:
        img.unlockFocus()
    if rep is None:
        import pytest
        pytest.skip("no offscreen bitmap (headless without a window server)")

    def lum(y):
        c = rep.colorAtX_y_(w // 2, y).colorUsingColorSpaceName_("NSCalibratedRGBColorSpace")
        return c.redComponent()  # grayscale content, so R == luminance

    vals = [lum(y) for y in range(h)]
    assert min(vals) < 0.7, "expected dark scanline rows"
    assert max(vals) > 0.9, "expected light rows between scanlines"


def test_macos_pixel_grid_darkens_both_axes():
    """The dot-matrix pixelgrid overlay dims gaps on BOTH axes — unlike scanlines
    (constant along each row), a fixed row here still has dark vertical gaps."""
    mb = _macos()
    from AppKit import NSBitmapImageRep, NSColor, NSImage, NSRectFill
    from Foundation import NSMakeRect

    w, h = 40, 40
    be = mb.MacOSBackend()
    be._view = mb._PuiKitView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))
    img = NSImage.alloc().initWithSize_((w, h))
    img.lockFocus()
    try:
        NSColor.whiteColor().setFill()
        NSRectFill(((0, 0), (w, h)))
        be._render_pixel_grid(0.9)
        rep = NSBitmapImageRep.alloc().initWithFocusedViewRect_(((0, 0), (w, h)))
    finally:
        img.unlockFocus()
    if rep is None:
        pytest.skip("no offscreen bitmap (headless without a window server)")

    def lum(x, y):
        c = rep.colorAtX_y_(x, y).colorUsingColorSpaceName_("NSCalibratedRGBColorSpace")
        return c.redComponent()  # grayscale content, so R == luminance

    # A single non-gap row has dark vertical gaps along it (the vertical grid) AND
    # bright cells between them — the two-axis signature a scanline can't produce.
    row = [lum(x, 20) for x in range(w)]
    assert min(row) < 0.7, "expected dark vertical grid gaps along a row"
    assert max(row) > 0.9, "expected bright pixel cells between the gaps"
    col = [lum(20, y) for y in range(h)]
    assert min(col) < 0.7, "expected dark horizontal grid gaps down a column"


def test_macos_vignette_is_aspect_correct_on_a_wide_window():
    """The render-pass vignette darkens all four edges equally on a wide/short
    window (no porthole) — the bug CIVignette caused."""
    mb = _macos()
    if not mb._HAS_QUARTZ:
        import pytest
        pytest.skip("Quartz unavailable")
    from AppKit import NSBitmapImageRep, NSColor, NSImage, NSRectFill
    from Foundation import NSMakeRect

    w, h = 200, 60  # deliberately wide, like the reported window
    be = mb.MacOSBackend()
    be._view = mb._PuiKitView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))
    img = NSImage.alloc().initWithSize_((w, h))
    img.lockFocus()
    try:
        NSColor.whiteColor().setFill()
        NSRectFill(((0, 0), (w, h)))
        be._render_vignette(0.85)
        rep = NSBitmapImageRep.alloc().initWithFocusedViewRect_(((0, 0), (w, h)))
    finally:
        img.unlockFocus()
    if rep is None:
        import pytest
        pytest.skip("no offscreen bitmap (headless without a window server)")

    pw, ph = rep.pixelsWide(), rep.pixelsHigh()  # 2x on Retina — sample in px space

    def lum(fx, fy):
        x = min(int(fx * pw), pw - 1)
        y = min(int(fy * ph), ph - 1)
        c = rep.colorAtX_y_(x, y).colorUsingColorSpaceName_("NSCalibratedRGBColorSpace")
        return c.redComponent()

    center = lum(0.5, 0.5)
    left, right = lum(0.01, 0.5), lum(0.99, 0.5)
    top, bottom = lum(0.5, 0.01), lum(0.5, 0.99)
    corner = lum(0.01, 0.01)

    assert center > 0.9, "center should stay clear"
    assert corner < center - 0.3, "corners should darken"
    # Aspect-correct: opposite edges match, and the wide (L/R) edges dim about the
    # same as the short (T/B) edges rather than far more (the porthole symptom).
    assert abs(left - right) < 0.05 and abs(top - bottom) < 0.05
    assert abs(left - top) < 0.15, f"L/R vs T/B mismatch: {left:.2f} vs {top:.2f}"


def test_macos_roll_band_top_sweeps_through():
    mb = _macos()
    h, bh = 600.0, 48.0
    assert mb._roll_band_top(0.0, h, bh) == -bh          # starts just above the top
    assert mb._roll_band_top(1.0, h, bh) == h            # ends just below the bottom
    assert 0 < mb._roll_band_top(0.5, h, bh) < h         # mid-sweep is on screen


def test_macos_roll_scheduling_lifecycle():
    import time
    mb = _macos()
    be = mb.MacOSBackend()
    be.set_post_effect(CRT)                               # roll > 0
    be._roll_user_active = lambda t: True                # pretend the app is in use
    assert be._crt_roll is not None
    assert be._crt_roll_tick in be._tick_callbacks
    assert not be._roll_active()                          # waits before first roll

    be._crt_roll["next"] = time.monotonic() - 1          # due now
    be._crt_roll_tick()
    assert be._roll_active()                              # a roll started

    be._crt_roll["start"] = time.monotonic() - 999       # past its duration
    be._crt_roll_tick()
    assert not be._roll_active()                          # and ended

    be.set_post_effect(None)                             # clearing stops it
    assert be._crt_roll is None
    assert be._crt_roll_tick() is False                  # tick unregisters itself


def test_macos_roll_gated_on_active_use():
    import time
    mb = _macos()
    be = mb.MacOSBackend()
    be.set_post_effect(CRT)
    now = time.monotonic()
    be._crt_roll["next"] = now - 1                        # a roll is due
    # No key window -> not active: the due roll must NOT start, and the ticker
    # parks itself (returns False) so it stops consuming frames while idle.
    assert be._roll_user_active(now) is False
    assert be._crt_roll_tick() is False
    assert not be._roll_active()
    # Actively used -> the due roll starts.
    be._roll_user_active = lambda t: True
    assert be._crt_roll_tick() is True
    assert be._roll_active()
    # An in-flight roll keeps going to completion even if the app goes inactive.
    be._roll_user_active = lambda t: False
    assert be._crt_roll_tick() is True
    assert be._roll_active()


def test_macos_dispatch_records_input_activity():
    from puikit import Event, EventType
    mb = _macos()
    be = mb.MacOSBackend()
    be._last_input_time = 0.0
    be._dispatch(Event(type=EventType.MOUSE_MOVE, x=1.0, y=1.0))
    assert be._last_input_time > 0.0


def test_macos_roll_band_brightens_its_row():
    """The rolling band lifts luminance where it sits and leaves elsewhere dark."""
    mb = _macos()
    from AppKit import NSBitmapImageRep, NSColor, NSImage, NSRectFill
    from Foundation import NSMakeRect

    w, h = 16, 200
    be = mb.MacOSBackend()
    be._view = mb._PuiKitView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))
    be._crt_roll = {"active": True, "start": 99.5, "duration": 1.0, "next": 0.0}
    img = NSImage.alloc().initWithSize_((w, h))
    img.lockFocus()
    try:
        NSColor.blackColor().setFill()
        NSRectFill(((0, 0), (w, h)))
        be._render_roll_band(TINTED, 100.0)  # progress 0.5 -> band centered ~mid
        rep = NSBitmapImageRep.alloc().initWithFocusedViewRect_(((0, 0), (w, h)))
    finally:
        img.unlockFocus()
    if rep is None:
        import pytest
        pytest.skip("no offscreen bitmap (headless without a window server)")

    pw, ph = rep.pixelsWide(), rep.pixelsHigh()

    def lum(fy):  # brightest column sample at fractional height fy
        y = min(int(fy * ph), ph - 1)
        best = 0.0
        for x in range(pw):
            c = rep.colorAtX_y_(x, y).colorUsingColorSpaceName_("NSCalibratedRGBColorSpace")
            best = max(best, c.greenComponent())
        return best

    # Sample densely across the band region (centered ~0.5) for its brightest row.
    band = max(lum(fy / 100.0) for fy in range(38, 63))
    outside = lum(0.05)                                # far above the band
    assert band > 0.15, "band should brighten its rows"
    assert band > outside + 0.1, "band should be clearly brighter than off-band"
    assert outside < 0.05, "away from the band should stay dark"


def test_macos_roll_is_bottom_weighted():
    """The band is dim at its top (trailing) edge and strongest toward its bottom
    (leading) edge. Tested on the pure falloff, so it's independent of the view's
    flipped-vs-not drawing frame."""
    mb = _macos()
    assert mb._roll_falloff(0.0) == 0.0            # top edge: no light
    assert mb._roll_falloff(0.1) < mb._roll_falloff(0.8)  # weaker up top
    upper = sum(mb._roll_falloff(k / 100.0) for k in range(0, 50)) / 50
    lower = sum(mb._roll_falloff(k / 100.0) for k in range(50, 100)) / 50
    assert lower > upper, "lower half should carry more of the band's intensity"


def test_macos_tint_color_is_carried_through():
    mb = _macos()
    mono = mb._build_ci_filters(TINTED)[0]
    c = mono.valueForKey_("inputColor")
    r, g, b = TINTED.tint
    assert round(c.red() * 255) == r
    assert round(c.green() * 255) == g
    assert round(c.blue() * 255) == b


def test_macos_noop_and_none_produce_empty_chain():
    mb = _macos()
    assert mb._build_ci_filters(PostEffect()) == []
    assert mb._build_ci_filters(None) == []
