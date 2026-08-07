"""WindowsBackend tests. Most run without opening a window (mirroring
test_macos_backend.py's philosophy); a couple exercise a real window since,
unlike PyObjC/AppKit, this backend's only dependency is ctypes/stdlib, so
opening one is cheap and safe in CI on a Windows runner."""

import ctypes
import struct
import sys
import threading
import zlib
from ctypes import wintypes

import pytest

# Skip at COLLECTION time: the imports below reach _win32_native, whose module
# body calls ctypes.WinDLL, which does not exist off Windows. importorskip on
# "ctypes.wintypes" does NOT work as a guard here -- that module imports fine on
# macOS/Linux -- and a pytestmark gates execution, not import.
if sys.platform != "win32":
    pytest.skip("Windows-only backend", allow_module_level=True)

from puikit import CRT, PostEffect, Rect  # noqa: E402
from puikit.backend import Backend, Style, TextAttribute  # noqa: E402
from puikit.font import Font, FontWeight  # noqa: E402
from puikit.backends.windows_backend import (  # noqa: E402
    Animation, WindowsBackend, _SHADOW_KERNEL, _drop_shadow_params, _glow_matrix,
    _roll_band_top, _roll_falloff, _shadow_tap_alpha, _tint_matrix,
)
from puikit.backends import _win32_native as native  # noqa: E402
from puikit.backends import windows_backend  # noqa: E402

# A tinted, strongly-rolling effect for the tint / roll coverage (no named preset
# ships a tint). Mirrors tests/test_posteffect.py's TINTED.
_TINTED = PostEffect(tint=(70, 240, 130), bloom=0.28, scanline=0.42, vignette=0.4, glow=0.32, roll=0.45)


def _png(path, w, h):
    """Write a minimal valid RGB PNG of the given pixel size (same helper as
    tests/test_image_widgets.py, duplicated to keep this file standalone)."""
    raw = bytearray()
    for _ in range(h):
        raw.append(0)  # filter type 0
        raw += bytes((120, 120, 120) * w)

    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    data = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(bytes(raw))) + chunk(b"IEND", b"")
    path.write_bytes(data)
    return str(path)


def test_base_font_drives_base_unit():
    # The base unit is derived from the base font's glyph box (font -> base
    # unit), and it scales with the font size. _init_fonts needs no window.
    small = WindowsBackend(base_font=Font(size=12, monospace=True))
    small._init_fonts()
    large = WindowsBackend(base_font=Font(size=24, monospace=True))
    large._init_fonts()
    try:
        assert small.base_size[0] >= 1 and small.base_size[1] >= 1
        # A bigger base font means a bigger base unit, both axes.
        assert large.base_size[0] > small.base_size[0]
        assert large.base_size[1] > small.base_size[1]
    finally:
        small.close()
        large.close()


def test_style_font_is_cached():
    backend = WindowsBackend()
    try:
        style = Style(font=Font(family="Georgia", size=18))
        first = backend._resolve_style_font(style)
        assert backend._resolve_style_font(style) is first
    finally:
        backend.close()


def test_measure_text_base_font_counts_columns():
    backend = WindowsBackend()
    backend._init_fonts()
    try:
        # The grid font is an explicit unsized/unnamed monospace request; the
        # default style (font=None) is no longer grid - see the next test.
        # (Mirrors test_macos_backend.py, which was updated when font=None
        # started measuring as the UI font; this twin was left behind.)
        assert backend.measure_text("hello", Style(font=Font(monospace=True))) == 5.0
    finally:
        backend.close()


def test_measure_text_default_style_measures_as_drawn():
    backend = WindowsBackend()
    backend._init_fonts()
    try:
        # font=None draws as the proportional UI font (Panel._resolve), so it
        # measures as that font too - mirroring measure_line_height. Measuring
        # it by columns over-sized every content-sized default-font label.
        assert backend.measure_text("hello") == backend.measure_text(
            "hello", Style(font=Font()))
    finally:
        backend.close()


def test_measure_text_proportional_is_not_column_count():
    backend = WindowsBackend()
    backend._init_fonts()
    try:
        width = backend.measure_text("WWWWW", Style(font=Font()))
        # A proportional run of wide glyphs measures wider than its column count.
        assert width > 5.0
    finally:
        backend.close()


def test_measure_line_height_default_font_measures_ui_font():
    # font=None is DRAWN as the UI font (Panel._resolve substitutes it), so it
    # must be MEASURED as the UI font too — not the old 1.0 grid shortcut, which
    # under-sized content panes and clipped the taller UI font's descenders.
    # Use a deliberately mismatched pair (short mono base, taller proportional
    # UI face) so the effect is observable; the bundled Noto default matches by
    # design, which is exactly why it doesn't clip.
    backend = WindowsBackend(
        base_font=Font(family="Consolas", size=30.0, monospace=True),
        ui_font=Font(family="Segoe UI"),
    )
    backend._init_fonts()
    try:
        default_h = backend.measure_line_height(Style())  # font=None -> UI font
        grid_h = backend.measure_line_height(Style(font=Font(monospace=True)))
        assert grid_h == 1.0  # a genuine grid font is still exactly one row
        assert default_h > 1.0  # the taller UI font measures past one mono row
    finally:
        backend.close()


def test_font_metrics_split_sums_to_line_height():
    backend = WindowsBackend(base_font=Font(size=30.0, monospace=True))
    backend._init_fonts()
    try:
        fm = backend.font_metrics(Style())  # font=None -> UI font
        assert fm.ascent > 0 and fm.descent > 0
        # ascent+descent is one line box; matches measure_line_height (which
        # ceils to the pixel grid, so allow a small rounding slack).
        assert abs(fm.line_height - backend.measure_line_height(Style())) < 0.05
        # A larger explicit font has a proportionally taller box.
        big = backend.font_metrics(Style(font=Font(size=60)))
        assert big.line_height > fm.line_height * 1.5
    finally:
        backend.close()


def test_draw_text_baseline_offsets_by_ascent():
    # The default draw_text_baseline puts the top of the box one ascent above
    # the baseline. Two fonts drawn at the same baseline_y therefore share a
    # baseline even though their box tops differ.
    backend = WindowsBackend(base_font=Font(size=30.0, monospace=True))
    backend.open()
    try:
        drawn = []
        original = backend.draw_text
        backend.draw_text = lambda x, y, text, style=Style(): drawn.append((y, style))
        backend.draw_text_baseline(0, 5.0, "Ag", Style(font=Font()))
        backend.draw_text_baseline(0, 5.0, "Ag", Style(font=Font(size=60)))
        backend.draw_text = original
        (y_small, s_small), (y_big, s_big) = drawn
        # baseline - ascent: the bigger font (bigger ascent) starts higher up.
        assert y_big < y_small
        assert abs((5.0 - y_small) - backend.font_metrics(s_small).ascent) < 1e-6
        assert abs((5.0 - y_big) - backend.font_metrics(s_big).ascent) < 1e-6
    finally:
        backend.close()


def test_font_params_resolves_default_fonts():
    # The default mono/proportional pair is the bundled Noto superfamily (whose
    # matched metrics keep text from clipping), loaded from a custom DirectWrite
    # collection. The font files are fetched at build time, not committed, so
    # both outcomes are valid: Noto when present, the OS pair when not.
    backend = WindowsBackend()
    try:
        mono = backend._font_params(Font(monospace=True))[0]
        prop = backend._font_params(Font())[0]
        if backend._ensure_font_collection() is not None:
            assert (mono, prop) == ("Noto Sans Mono", "Noto Sans")
        else:
            assert (mono, prop) == ("Consolas", "Segoe UI")
    finally:
        backend.close()


def test_explicit_family_overrides_bundled_default():
    # An app that names a family still gets it (via the system collection).
    backend = WindowsBackend()
    try:
        assert backend._font_params(Font(family="Consolas", monospace=True))[0] == "Consolas"
        assert backend._font_params(Font(family="Segoe UI"))[0] == "Segoe UI"
    finally:
        backend.close()


def test_font_params_weight_matches_dwrite_scale():
    backend = WindowsBackend()
    try:
        _, weight, _italic, _size = backend._font_params(Font(weight=FontWeight.BOLD))
        assert weight == 700
    finally:
        backend.close()


def test_display_list_swaps_on_present():
    backend = WindowsBackend()  # not opened: no window is created
    try:
        backend.draw_text(1, 2, "hi", Style(attr=TextAttribute.BOLD))
        backend.draw_box(0, 0, 10, 5)
        assert backend._front == []
        backend.present()
        assert [cmd[0] for cmd in backend._front] == ["text", "box"]
        assert backend._back == []
    finally:
        backend.close()


def test_icons_become_glyph_text_commands():
    backend = WindowsBackend()
    try:
        backend.draw_icon(3, 4, "folder")
        backend.present()
        kind, x, y, glyph, _style = backend._front[0]
        assert (kind, x, y, glyph) == ("text", 3, 4, "📁")
    finally:
        backend.close()


def test_profile_declares_gui_capabilities():
    profile = WindowsBackend.PROFILE
    assert profile.supports("pixel_layout")
    assert profile.supports("icons")
    assert profile.supports("animation")
    assert profile.supports("vector_shapes")
    assert profile.supports("native_menus")
    assert profile.supports("images")  # WIC-decoded ID2D1Bitmap, see _render_image
    # IME + both drag-and-drop directions (_win32_ime.py / _win32_dragdrop.py):
    assert profile.supports("ime")
    assert profile.supports("drag_and_drop")
    assert profile.supports("os_drag_drop")
    assert profile.supports("system_tray")  # set_tray: Shell_NotifyIconW
    # Unused by any PuiKit app to date (see MacOSBackend.PROFILE, same three False):
    assert not profile.supports("clipboard_rich")
    assert not profile.supports("native_file_dialog")
    assert not profile.supports("media_keys")


def test_vector_primitives_record_display_list_commands():
    backend = WindowsBackend()  # not opened: no window is created
    try:
        backend.draw_round_rect(0, 0, 4, 1, 4.0, Style(bg=(1, 2, 3)), {"fill": True})
        backend.draw_check(0, 0, 1, 1, Style(fg=(255, 255, 255)))
        backend.present()
        assert [cmd[0] for cmd in backend._front] == ["round_rect", "check"]
        rr = backend._front[0]
        assert rr[5] == 4.0  # radius carried through
        assert rr[7] == {"fill": True}
    finally:
        backend.close()


def test_animation_progress_and_easing():
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
    backend = WindowsBackend()  # not opened: no window, no timer needed
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
    backend.close()  # tolerates closing a never-opened backend


def test_animation_kinds_carry_their_hints():
    backend = WindowsBackend()
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


# --- a real window: cheap and safe here since the only dependency is ctypes ---


def test_open_close_roundtrip_creates_real_window():
    backend = WindowsBackend(width=40, height=12, title="puikit-test")
    backend.open()
    try:
        assert backend._hwnd != 0
        assert backend.size[0] > 0 and backend.size[1] > 0
        backend.draw_text(1, 1, "hello")
        backend.draw_box(0, 0, 10, 5, hints={"fill": True}, style=Style(bg=(10, 20, 30)))
        backend.present()
        backend._render()  # exercises the real Direct2D/DirectWrite draw path
    finally:
        backend.close()
    assert backend._hwnd == 0


# --- frame autosave: minimized saves and unreachable restores ---


def _delete_autosave(name):
    import winreg

    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, rf"Software\PuiKit\FrameAutosave\{name}")
    except OSError:
        pass


def test_autosave_while_minimized_saves_the_normal_frame():
    """Saving with the window minimized must not persist the iconic
    placeholder rect (parked at -32000,-32000, caption-bar sized) — the next
    launch would restore the window unreachably off-screen, leaving only a
    taskbar button whose hover thumbnail is a bare title bar."""
    name = "puikit-test-autosave-iconic"
    _delete_autosave(name)
    backend = WindowsBackend(width=40, height=12, title="puikit-test",
                             frame_autosave_name=name)
    backend.open()
    try:
        before = wintypes.RECT()
        assert native.user32.GetWindowRect(backend._hwnd, ctypes.byref(before))
        native.user32.ShowWindow(backend._hwnd, native.SW_MINIMIZE)
        backend._save_autosave_frame()
        saved = windows_backend._load_autosave_rect(name)
        assert saved is not None
        x, y, w, h = saved
        # The normal-state frame: same size as before minimizing, nowhere
        # near the -32000 parking position. (x/y may differ from the screen
        # rect by the work-area offset — placement coordinates.)
        assert (w, h) == (before.right - before.left, before.bottom - before.top)
        assert x > -30000 and y > -30000
    finally:
        backend.close()
        _delete_autosave(name)


def test_unreachable_autosave_frame_is_ignored():
    """A saved frame on no current monitor (a disconnected display, or a
    poisoned pre-fix iconic rect) is discarded in favor of the default
    frame, not restored to where the user cannot reach it."""
    name = "puikit-test-autosave-offscreen"
    windows_backend._save_autosave_rect(name, -32000, -32000, 314, 71)
    backend = WindowsBackend(width=40, height=12, title="puikit-test",
                             frame_autosave_name=name)
    backend.open()
    try:
        rect = wintypes.RECT()
        assert native.user32.GetWindowRect(backend._hwnd, ctypes.byref(rect))
        assert rect.left > -30000 and rect.top > -30000
        assert rect.right - rect.left > 314  # default frame, not the saved one
    finally:
        backend.close()
        _delete_autosave(name)


def test_autosave_roundtrip_restores_a_visible_frame():
    name = "puikit-test-autosave-roundtrip"
    _delete_autosave(name)
    first = WindowsBackend(width=40, height=12, title="puikit-test",
                           frame_autosave_name=name)
    first.open()
    native.user32.SetWindowPos(first._hwnd, None, 220, 140, 500, 320,
                               native.SWP_NOZORDER | native.SWP_NOACTIVATE)
    first.close()  # saves the frame
    second = WindowsBackend(width=40, height=12, title="puikit-test",
                            frame_autosave_name=name)
    second.open()
    try:
        rect = wintypes.RECT()
        assert native.user32.GetWindowRect(second._hwnd, ctypes.byref(rect))
        assert (rect.left, rect.top) == (220, 140)
        assert (rect.right - rect.left, rect.bottom - rect.top) == (500, 320)
    finally:
        second.close()
        _delete_autosave(name)


def test_autosave_rect_on_screen_validation():
    # The minimized parking rect and degenerate sizes are rejected...
    assert not windows_backend._autosave_rect_on_screen(-32000, -32000, 314, 71)
    assert not windows_backend._autosave_rect_on_screen(100, 100, 0, 0)
    # ...while a rect overlapping the primary monitor (which owns the
    # origin by definition) is accepted.
    assert windows_backend._autosave_rect_on_screen(10, 10, 300, 200)


def test_draw_shadow_renders_with_blur_effect():
    """Exercises the D3D11/DXGI/ID2D1DeviceContext + ID2D1Effect(Gaussian
    Blur) path directly: command-list capture, effect retarget, DrawImage
    compositing (see WindowsBackend._render_shadow)."""
    backend = WindowsBackend(width=40, height=20, title="puikit-shadow-test")
    backend.open()
    try:
        assert backend._shadow_effect is not None
        backend.draw_shadow(5, 5, 20, 10, radius=4.0, bg=(40, 40, 40))
        backend.draw_box(5, 5, 20, 10, hints={"fill": True}, style=Style(bg=(40, 40, 40)))
        backend.present()
        backend._render()
    finally:
        backend.close()


def test_shadow_inside_a_fading_group_keeps_the_device():
    """The exact shape of a modal opening: a drop shadow drawn *inside* a group
    that is mid-transition, so an offscreen layer (PushLayer) is open around it.

    The shadow used to record its caster by splitting the frame's batch —
    EndDraw, retarget to a command list, retarget back, BeginDraw. That holds
    only while nothing else is open on the context. With a fading group's layer
    unbalanced, the interposed EndDraw put the device context into an error
    state; the backend read the failure as device loss and answered with a full
    _recreate_render_target(), ~280ms, on the first frame of every shadowed
    modal — the one frame the user is actually waiting for.

    The caster is now recorded before the frame opens, so this frame must
    complete on the same device it started on.
    """
    backend = WindowsBackend(width=40, height=20, title="puikit-shadow-fade-test")
    backend.open()
    try:
        # A post effect is part of the repro, not decoration: it captures the
        # frame into a command list, so the error state left by the interposed
        # EndDraw surfaces as a failure to close that capture. This is the Cyber
        # theme's shape (bloom + glow + vignette).
        backend.set_post_effect(PostEffect(bloom=0.78, glow=0.60, vignette=0.30))
        device, target = backend._d3d_device, backend._target_bitmap
        widget = object()
        backend.animate(widget, {"transition": "scale", "from_scale": 0.92,
                                 "fade": True, "duration_ms": 200})
        rect = Rect(5, 5, 20, 10)
        backend.begin_group(widget, rect)
        backend.draw_shadow(5, 5, 20, 10, radius=4.0, bg=(40, 40, 40))
        backend.draw_box(5, 5, 20, 10, hints={"fill": True}, style=Style(bg=(40, 40, 40)))
        backend.draw_text(6, 6, "opening")
        backend.end_group(widget)
        backend.present()
        backend._render()
        # A recreate swaps both of these for fresh COM objects, so identity here
        # is the observable for "the frame was not dropped".
        assert backend._d3d_device is device
        assert backend._target_bitmap is target
    finally:
        backend.close()


def test_fade_group_renders_through_pushlayer():
    """A fade transition composites its group through an offscreen layer
    (ID2D1RenderTarget::PushLayer[40] / PopLayer[41] with
    D2D1_LAYER_PARAMETERS.opacity) instead of folding the group opacity into
    every brush — the Direct2D analog of macOS's transparency layer (see
    docs/animation.md). This drives the real vtable calls against a
    live device context: a wrong slot index or struct layout would fault or
    fail EndDraw here."""
    backend = WindowsBackend(width=40, height=20, title="puikit-fade-test")
    backend.open()
    try:
        widget = object()
        backend.animate(widget, {"transition": "fade", "duration_ms": 200})
        rect = Rect(2, 2, 20, 8)
        backend.begin_group(widget, rect)
        backend.draw_box(2, 2, 20, 8, hints={"fill": True}, style=Style(bg=(40, 40, 40)))
        backend.draw_text(3, 3, "fading")
        backend.end_group(widget)
        backend.present()
        backend._render()  # mid-fade: opens/pops a real transparency layer
        # The group alpha is applied by the layer, never folded into a brush.
        assert not hasattr(backend, "_group_alpha_stack")
    finally:
        backend.close()


def test_layer_params_struct_matches_native_layout():
    """D2D1_LAYER_PARAMETERS must match d2d1.h's v1.0 struct byte-for-byte
    (default ctypes alignment, no _pack_): 2 rects/matrix worth of floats plus
    two pointers, an enum, a float, and an options enum. A silently wrong size
    would corrupt the PushLayer call at runtime."""
    import ctypes

    p = native.D2D1_LAYER_PARAMETERS(
        contentBounds=native.infinite_rect(),
        geometricMask=None,
        maskAntialiasMode=native.D2D1_ANTIALIAS_MODE_PER_PRIMITIVE,
        maskTransform=native.D2D1_MATRIX_3X2_F.identity(),
        opacity=0.5,
        opacityBrush=None,
        layerOptions=native.D2D1_LAYER_OPTIONS_NONE,
    )
    assert p.opacity == pytest.approx(0.5)
    # contentBounds(4f) + geometricMask(ptr) + maskAntialiasMode(u32) +
    # maskTransform(6f) + opacity(f) + opacityBrush(ptr) + layerOptions(u32),
    # padded to 8-byte pointer alignment.
    ptr = ctypes.sizeof(ctypes.c_void_p)
    expected = 4 * 4 + ptr + 4 + 6 * 4 + 4 + ptr + 4
    # Round up to the struct's alignment (pointer-sized) for the trailing pad.
    expected = (expected + ptr - 1) // ptr * ptr
    assert ctypes.sizeof(native.D2D1_LAYER_PARAMETERS) == expected


def test_resize_rebinds_swapchain_target():
    """WM_SIZE must unbind, ResizeBuffers, and rebind a fresh target bitmap
    (native.swapchain_resize) rather than the old single-call rt_resize."""
    backend = WindowsBackend(width=40, height=20, title="puikit-resize-test")
    backend.open()
    try:
        original_bitmap = backend._target_bitmap
        lparam = (60 << 16) | 320
        backend._handle_message(backend._hwnd, native.WM_SIZE, 0, lparam)
        assert backend._target_bitmap is not None
        assert backend._target_bitmap is not original_bitmap
        backend.draw_text(1, 1, "resized")
        backend.present()
        backend._render()  # target bitmap must still be drawable post-resize
    finally:
        backend.close()


def test_clipboard_roundtrip():
    backend = WindowsBackend()
    backend.open()
    try:
        backend.set_clipboard("puikit windows backend clipboard test")
        assert backend.get_clipboard() == "puikit windows backend clipboard test"
    finally:
        backend.close()


# --- images (WIC decode -> ID2D1Bitmap) -------------------------------------


def test_image_decodes_and_caches(tmp_path):
    backend = WindowsBackend()
    backend.open()
    try:
        path = _png(tmp_path / "test.png", 40, 20)
        cached = backend._get_image(path)
        assert cached is not None
        bitmap, iw, ih = cached
        assert (iw, ih) == (40, 20)
        assert backend._get_image(path)[0] is bitmap  # cache hit, same bitmap
    finally:
        backend.close()


def test_image_missing_path_caches_as_failure(tmp_path):
    backend = WindowsBackend()
    backend.open()
    try:
        missing = str(tmp_path / "does_not_exist.png")
        assert backend._get_image(missing) is None
        assert backend._image_cache.get(missing) is None
        assert missing in backend._image_cache  # cached as a known failure
    finally:
        backend.close()


def test_draw_image_renders_without_error(tmp_path):
    backend = WindowsBackend(width=20, height=10)
    backend.open()
    try:
        path = _png(tmp_path / "test.png", 40, 20)
        backend.draw_image(1, 1, path, {"w": 10, "h": 5, "fit": "contain"})
        backend.present()
        backend._render()  # exercises CreateBitmapFromWicBitmap + DrawBitmap
    finally:
        backend.close()


def test_profile_supports_images():
    assert WindowsBackend.PROFILE.supports("images")


# --- IME (mode gating + surrogate pairs) ------------------------------------


def test_ime_mode_gating_round_trips_context():
    backend = WindowsBackend(width=20, height=10, title="puikit-ime-test")
    backend.open()
    try:
        assert backend._text_input_active is False
        assert backend._default_himc != 0  # captured by disable_ime() in open()
        backend.begin_text_input()
        assert backend._text_input_active is True
        backend.end_text_input()
        assert backend._text_input_active is False
    finally:
        backend.close()


def test_target_clause_start_finds_first_target_attribute():
    from puikit.backends import _win32_ime

    # "input input TARGET TARGET input" — 0 = ATTR_INPUT, ATTR_TARGET_CONVERTED marks the clause under conversion.
    attrs = bytes([0, 0, _win32_ime.ATTR_TARGET_CONVERTED, _win32_ime.ATTR_TARGET_CONVERTED, 0])
    original = _win32_ime._get_composition_attrs
    _win32_ime._get_composition_attrs = lambda himc: attrs
    try:
        assert _win32_ime._target_clause_start(0) == 2
    finally:
        _win32_ime._get_composition_attrs = original


def test_target_clause_start_defaults_to_zero_without_a_target_run():
    # Raw kana input before any conversion: every character is ATTR_INPUT, no
    # ATTR_TARGET_* run exists yet, so the anchor stays at the composition
    # start (no jitter while typing — see _win32_ime's module docstring).
    from puikit.backends import _win32_ime

    original = _win32_ime._get_composition_attrs
    _win32_ime._get_composition_attrs = lambda himc: bytes([0, 0, 0])
    try:
        assert _win32_ime._target_clause_start(0) == 0
    finally:
        _win32_ime._get_composition_attrs = original


def test_ime_commit_dispatches_committed_characters_as_key_events():
    """WM_IME_COMPOSITION carrying a GCS_RESULTSTR must deliver the committed
    text as KEY events directly (see _win32_ime's module docstring) — Windows
    does not synthesize WM_CHAR for it since DefWindowProc is never invoked
    for this message."""
    from puikit.backends import _win32_ime
    from puikit.event import EventType

    backend = WindowsBackend()
    events = []
    backend._handler = events.append
    backend._hwnd = 12345  # any nonzero placeholder; read_composition is mocked
    try:
        original = _win32_ime.read_composition
        _win32_ime.read_composition = lambda hwnd, lparam: (None, 0, 0, "あい")  # "あい"
        try:
            backend._on_ime_composition(0)
        finally:
            _win32_ime.read_composition = original

        assert len(events) == 3
        assert events[0].type == EventType.IME_COMPOSITION
        assert events[0].hints == {"preedit": "", "caret": 0}
        assert [e.char for e in events[1:]] == ["あ", "い"]
    finally:
        backend.close()


def test_ime_composition_update_without_result_does_not_dispatch_commit():
    from puikit.backends import _win32_ime
    from puikit.event import EventType

    backend = WindowsBackend()
    events = []
    backend._handler = events.append
    backend._hwnd = 12345
    try:
        original = _win32_ime.read_composition
        _win32_ime.read_composition = lambda hwnd, lparam: ("あ", 1, 0, None)
        try:
            backend._on_ime_composition(0)
        finally:
            _win32_ime.read_composition = original

        assert len(events) == 1
        assert events[0].type == EventType.IME_COMPOSITION
        assert events[0].hints == {"preedit": "あ", "caret": 1, "target_start": 0}
    finally:
        backend.close()


def test_surrogate_pair_combines_into_one_astral_character():
    backend = WindowsBackend()
    events = []
    backend._handler = events.append
    try:
        backend._on_char(0xD83D)  # high surrogate half of U+1F600
        assert events == []  # buffered, nothing dispatched until the pair completes
        backend._on_char(0xDE00)  # low surrogate half
        assert len(events) == 1
        assert events[0].char == "\U0001F600"
        assert events[0].key == "\U0001F600"
        assert backend._pending_high_surrogate is None
    finally:
        backend.close()


def test_lone_low_surrogate_without_pending_high_is_dropped():
    backend = WindowsBackend()
    events = []
    backend._handler = events.append
    try:
        backend._on_char(0xDE00)  # low surrogate with no preceding high half
        assert events == []
    finally:
        backend.close()


def test_ctrl_backspace_is_a_ctrl_modified_backspace(monkeypatch):
    # Windows delivers Ctrl+Backspace as WM_CHAR 0x7F (plain Backspace is 0x08);
    # it must reach the field as a ctrl-modified backspace so the widget deletes
    # a whole word. The Ctrl comes from live key state, faked here.
    import puikit.backends.windows_backend as wb
    from puikit.event import EventType

    monkeypatch.setattr(wb, "_key_modifiers", lambda: frozenset({"ctrl"}))
    backend = WindowsBackend()
    events = []
    backend._handler = events.append
    try:
        backend._on_char(0x7F)
        assert len(events) == 1
        assert events[0].type == EventType.KEY
        assert events[0].key == "backspace"
        assert "ctrl" in events[0].modifiers
    finally:
        backend.close()


# --- drag-out (begin_file_drag) ---------------------------------------------


def test_begin_file_drag_returns_false_for_empty_paths():
    backend = WindowsBackend()  # not opened: no window, no OLE calls needed
    try:
        assert backend.begin_file_drag([]) is False
    finally:
        backend.close()


def test_premultiply_bgra_matches_reference():
    """Premultiplied output must equal channel*alpha//255 for every pixel."""
    import random

    from puikit.backends import _win32_native as native

    random.seed(0)
    raw = bytes(random.randrange(256) for _ in range(4 * 50))
    result = native._premultiply_bgra(raw)
    for i in range(0, len(raw), 4):
        b, g, r, a = raw[i], raw[i + 1], raw[i + 2], raw[i + 3]
        assert result[i] == (b * a) // 255
        assert result[i + 1] == (g * a) // 255
        assert result[i + 2] == (r * a) // 255
        assert result[i + 3] == a


# --- CRT post-effect (see WindowsBackend.set_post_effect / _composite_post_effect) ---


def test_profile_declares_post_effects():
    # The Direct2D-effects CRT composite is implemented, so the capability is on
    # (a terminal backend leaves it off and set_post_effect no-ops).
    assert WindowsBackend.PROFILE.supports("post_effects")


def test_tint_matrix_maps_luminance_to_hue():
    # A green tint: white maps to full green (its luma sums the Rec.601 weights),
    # black stays black, and no input channel bleeds into red or blue outputs.
    m = _tint_matrix((0, 255, 0))
    assert m._12 == pytest.approx(0.299)   # R-in -> G-out (luma weight * tg=1)
    assert m._22 == pytest.approx(0.587)   # G-in -> G-out
    assert m._32 == pytest.approx(0.114)   # B-in -> G-out
    assert (m._11, m._13) == (0.0, 0.0)    # nothing lands in R or B out
    assert m._44 == pytest.approx(1.0)     # alpha passes through


def test_glow_matrix_is_identity_at_zero_and_lifts_above():
    ident = _glow_matrix(0.0)
    assert ident._11 == pytest.approx(1.0) and ident._51 == pytest.approx(0.0)
    lifted = _glow_matrix(1.0)
    assert lifted._11 == pytest.approx(1.15)                       # contrast up
    assert lifted._51 == pytest.approx((0.5 - 0.5 * 1.15) + 0.12)  # brightness lift


def test_roll_helpers_sweep_and_fall_off():
    # Pure geometry, mirroring test_posteffect.py's macOS coverage.
    h, bh = 600.0, 48.0
    assert _roll_band_top(0.0, h, bh) == -bh          # starts just above the top
    assert _roll_band_top(1.0, h, bh) == h            # ends just below the bottom
    assert 0 < _roll_band_top(0.5, h, bh) < h         # mid-sweep is on screen
    assert _roll_falloff(0.0) == 0.0                  # transparent at the trailing edge
    assert _roll_falloff(1.0) == pytest.approx(0.0)   # and the leading edge
    assert _roll_falloff(0.85) == pytest.approx(1.0)  # brightest at _ROLL_PEAK


def test_roll_scheduling_lifecycle():
    # No window needed: set_post_effect wires the roll ticker; the chain build
    # no-ops without a render target. Mirrors the macOS roll lifecycle test.
    import time

    be = WindowsBackend()
    be.set_post_effect(CRT)                               # roll > 0
    be._window_active = True
    be._last_input_time = time.monotonic()               # pretend the app is in use
    assert be._crt_roll is not None
    assert be._crt_roll_tick in be._tick_callbacks
    assert not be._roll_active()                          # waits before the first roll

    be._crt_roll["next"] = time.monotonic() - 1          # due now
    be._crt_roll_tick()
    assert be._roll_active()                              # a roll started

    be._crt_roll["start"] = time.monotonic() - 999       # past its duration
    be._crt_roll_tick()
    assert not be._roll_active()                          # and ended
    assert be._roll_needs_clear                           # a final clean frame is forced

    be.set_post_effect(None)                              # clearing stops it
    assert be._crt_roll is None
    assert be._crt_roll_tick() is False                   # tick unregisters itself


def test_roll_gated_on_active_use():
    import time

    be = WindowsBackend()
    be.set_post_effect(CRT)
    now = time.monotonic()
    be._crt_roll["next"] = now - 1                        # a roll is due
    # Inactive window -> not in use: the due roll must NOT start and the ticker
    # parks itself (returns False) so it stops consuming frames.
    be._window_active = False
    assert be._roll_user_active(now) is False
    assert be._crt_roll_tick() is False
    assert not be._roll_active()
    # Actively used -> the due roll starts.
    be._window_active = True
    be._last_input_time = now
    assert be._crt_roll_tick() is True
    assert be._roll_active()
    # An in-flight roll keeps going to completion even once the app goes inactive.
    be._window_active = False
    assert be._crt_roll_tick() is True
    assert be._roll_active()


def test_dispatch_records_input_and_rearms_parked_roll():
    from puikit import Event, EventType

    be = WindowsBackend()
    be.set_post_effect(CRT)
    be._tick_callbacks = []                               # simulate a parked ticker
    be._last_input_time = 0.0
    be._dispatch(Event(type=EventType.MOUSE_MOVE, x=1.0, y=1.0))
    assert be._last_input_time > 0.0                      # activity stamped
    assert be._crt_roll_tick in be._tick_callbacks        # ticker re-armed


def test_render_with_crt_effect_composites():
    """The full composite path against a real swap chain: the frame is captured
    into a command list, run through the ColorMatrix/GaussianBlur/Opacity chain,
    and the scanline/vignette overlays are painted. A wrong effect graph, matrix
    blob, or gradient-brush vtable slot would fault or fail EndDraw here."""
    backend = WindowsBackend(width=40, height=16, title="puikit-crt-test")
    backend.open()
    try:
        backend.set_post_effect(CRT)
        assert backend._post_effect is not None and backend._crt is not None
        backend.draw_text(1, 1, "phosphor")
        backend.draw_box(0, 0, 12, 6, hints={"fill": True}, style=Style(bg=(4, 15, 7)))
        backend.present()
        backend._render()  # captures the frame + runs the effect composite
    finally:
        backend.close()


def test_crt_chain_composition_matches_effect():
    backend = WindowsBackend(width=40, height=16, title="puikit-crt-chain")
    backend.open()
    try:
        backend.set_post_effect(CRT)                       # no tint, glow + bloom
        assert len(backend._crt["color_chain"]) == 1       # glow only
        assert backend._crt["blur"] is not None            # bloom present
        backend.set_post_effect(_TINTED)                   # tint + glow
        assert len(backend._crt["color_chain"]) == 2
        backend.set_post_effect(None)                      # cleared
        assert backend._crt is None
    finally:
        backend.close()


def test_render_with_active_roll_band_draws():
    """Forces a roll on and renders it: exercises the linear-gradient-brush band
    fill inside the real composite pass."""
    import time

    backend = WindowsBackend(width=40, height=20, title="puikit-crt-roll")
    backend.open()
    try:
        backend.set_post_effect(_TINTED)
        backend._crt_roll.update(active=True, start=time.monotonic(), duration=1.0)
        assert backend._roll_active()
        backend.draw_box(0, 0, 20, 10, hints={"fill": True}, style=Style(bg=(4, 15, 7)))
        backend.present()
        backend._render()
    finally:
        backend.close()


def _crt_readback_bright_band(effect):
    """Render a bright phosphor band through the real CRT composite into an
    offscreen target, copy it to a CPU-readable bitmap, and return the mean green
    value of the band. No window; needs only a D2D device (WARP-capable). Used to
    assert the bloom composite *brightens* rather than darkens — a wrong composite
    mode (e.g. DESTINATION_IN vs PLUS) silently crushes brightness ~3.5x and no
    HRESULT check would catch it."""
    import ctypes

    from puikit.backends._win32_native import (
        ComPtr, D2D1_COLOR_F, D2D1_RECT_F, D2D1_SIZE_U, D2D1_PIXEL_FORMAT,
        D2D1_BITMAP_PROPERTIES1, hresult_ok, DXGI_FORMAT_B8G8R8A8_UNORM,
        D2D1_ALPHA_MODE_PREMULTIPLIED, D2D1_BITMAP_OPTIONS_TARGET,
    )

    class _MappedRect(ctypes.Structure):
        _fields_ = [("pitch", ctypes.c_uint32), ("bits", ctypes.POINTER(ctypes.c_uint8))]

    w, h = 128, 96
    try:
        factory = native.create_d2d_factory()
        d3d = native.create_d3d11_device()
        _, dc = native.create_d2d_device_context(factory, d3d)
    except OSError:
        pytest.skip("no D2D device available")

    def make(options):
        props = D2D1_BITMAP_PROPERTIES1(
            D2D1_PIXEL_FORMAT(DXGI_FORMAT_B8G8R8A8_UNORM, D2D1_ALPHA_MODE_PREMULTIPLIED),
            96.0, 96.0, options, None)
        out = ctypes.c_void_p()
        hr = dc.call(57, ctypes.c_int32,
                     [D2D1_SIZE_U, ctypes.c_void_p, ctypes.c_uint32,
                      ctypes.POINTER(D2D1_BITMAP_PROPERTIES1), ctypes.POINTER(ctypes.c_void_p)],
                     D2D1_SIZE_U(w, h), None, 0, ctypes.byref(props), ctypes.byref(out))
        assert hresult_ok(hr), hex(hr & 0xFFFFFFFF)
        return ComPtr(out.value or 0)

    target, staging = make(D2D1_BITMAP_OPTIONS_TARGET), make(2 | 4)  # target; CANNOT_DRAW|CPU_READ
    be = WindowsBackend.__new__(WindowsBackend)
    for name, val in dict(
        _render_target=dc, _target_bitmap=target, _frame_target=target, _dpi_scale=1.0,
        _hwnd=0, _post_effect=None, _crt=None, _crt_roll=None, _tick_callbacks=[],
        _animations={}, _anim_timer_running=False, _last_input_time=0.0, _window_active=True,
    ).items():
        setattr(be, name, val)
    be._brush = native.rt_create_solid_color_brush(dc, D2D1_COLOR_F(1, 1, 1, 1))
    be._client_size_px = lambda: (w, h)
    be.set_post_effect(effect)

    native.dc_set_target(dc, target)
    frame = native.dc_create_command_list(dc)
    native.dc_set_target(dc, frame)
    native.rt_begin_draw(dc)
    native.rt_clear(dc, D2D1_COLOR_F(4 / 255, 15 / 255, 7 / 255, 1.0))  # phosphor bg
    native.brush_set_color(be._brush, D2D1_COLOR_F(51 / 255, 245 / 255, 121 / 255, 1.0))
    native.rt_fill_rectangle(dc, D2D1_RECT_F(8, 30, w - 8, 66), be._brush)  # bright band
    native.rt_end_draw(dc)
    native.command_list_close(frame)

    native.dc_set_target(dc, target)
    native.rt_begin_draw(dc)
    native.rt_clear(dc, D2D1_COLOR_F(0, 0, 0, 1))
    be._composite_post_effect(frame, 0.0)
    assert hresult_ok(native.rt_end_draw(dc))
    frame.release()

    hr = staging.call(8, ctypes.c_int32, [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p], None, target.addr, None)
    assert hresult_ok(hr), "CopyFromBitmap " + hex(hr & 0xFFFFFFFF)
    mapped = _MappedRect()
    hr = staging.call(14, ctypes.c_int32, [ctypes.c_uint32, ctypes.POINTER(_MappedRect)], 1, ctypes.byref(mapped))
    assert hresult_ok(hr), "Map " + hex(hr & 0xFFFFFFFF)
    buf = ctypes.cast(mapped.bits, ctypes.POINTER(ctypes.c_uint8 * (mapped.pitch * h)))[0]
    tot = n = 0
    for y in range(40, 56):          # inside the bright band
        for x in range(16, w - 16):
            tot += buf[y * mapped.pitch + x * 4 + 1]  # BGRA -> green
            n += 1
    staging.call(15, None, [])       # Unmap
    return tot / n


def test_crt_bloom_composite_is_additive_not_darkening():
    # The bloom stage draws a blurred copy with PLUS (additive) compositing; a
    # bright phosphor band (green 245) must stay bright, not get crushed. Guards
    # the composite-mode enum value: DESTINATION_IN (3) instead of PLUS (9) here
    # silently reads ~74 (=245*0.3) and the whole theme goes "too dark".
    assert native.D2D1_COMPOSITE_MODE_PLUS == 9
    bright = _crt_readback_bright_band(PostEffect(bloom=0.30))
    assert bright > 220, f"bloom crushed the band to {bright:.0f} (expected it to stay bright)"


# --- text drop shadow (the Segment LCD look; see _drop_shadow_params / _render_text) ---


def test_drop_shadow_params_grow_and_cap():
    # Pure mapping, mirroring the roll-helper coverage above.
    assert _drop_shadow_params(0.0) is None          # off at/below zero
    assert _drop_shadow_params(-1.0) is None
    dx, dy, peak, blur = _drop_shadow_params(0.6)
    assert dy > dx > 0                               # offset down-right, deeper than wide
    assert blur > 0                                  # a soft spread, not a hard copy
    assert peak == pytest.approx(min(0.6, 0.2 + 0.6 * 0.45))
    dx2, dy2, peak2, blur2 = _drop_shadow_params(1.0)   # a deeper shadow spreads further
    assert dx2 > dx and dy2 > dy and blur2 > blur
    assert peak2 == pytest.approx(0.6)               # and its alpha saturates at the cap


def test_shadow_tap_alpha_reaches_peak_when_fully_overlapped():
    # The kernel's per-tap alpha is chosen so all taps overlapping compose up to
    # the requested core alpha; a single tap is much lighter (the feathered edge).
    peak = 0.47
    a = _shadow_tap_alpha(peak)
    assert 0 < a < peak
    composited = 1.0 - (1.0 - a) ** len(_SHADOW_KERNEL)
    assert composited == pytest.approx(peak)


def test_drop_shadow_only_effect_skips_the_composite_pass():
    # drop_shadow is painted inline per glyph, not composited, so a shadow-only
    # effect leaves _crt None (no frame capture) while still arming the shadow.
    be = WindowsBackend()
    be.set_post_effect(PostEffect(drop_shadow=0.6))
    assert be._post_effect is not None
    assert be._drop_shadow is not None
    assert be._crt is None
    # Clearing the effect disarms the shadow.
    be.set_post_effect(None)
    assert be._drop_shadow is None


def test_render_text_draws_soft_offset_shadow_under_each_glyph(monkeypatch):
    # With a drop shadow active, each glyph gets a soft shadow: one kernel of
    # translucent-black copies (all glyphs), then the real foreground pass, so the
    # segments read as embossed. No GPU: the D2D draw calls and the brush setter
    # are stubbed; _render_text's geometry (_unit_rect) is pure.
    be = WindowsBackend(base_font=Font(size=14, monospace=True))
    be._init_fonts()
    be._render_target = object()      # non-None sentinel; the draw calls are stubbed
    be._dpi_scale = 1.0
    be.set_post_effect(PostEffect(drop_shadow=0.6))
    calls = []
    monkeypatch.setattr(native, "rt_draw_text",
                        lambda rt, text, fmt, rect, brush: calls.append((text, rect)))
    monkeypatch.setattr(native, "rt_fill_rectangle", lambda *a, **k: None)
    monkeypatch.setattr(be, "_set_brush", lambda *a, **k: None)
    be._render_text(2, 3, "AB", Style(fg=(20, 30, 20)))
    k = len(_SHADOW_KERNEL)
    # 2 glyphs x (k shadow taps + 1 foreground): shadow taps first, then the fg pass.
    assert [t for t, _ in calls] == ["A"] * k + ["B"] * k + ["A", "B"]
    a_taps = [r for t, r in calls[:k]]                    # the "A" glyph's shadow kernel
    a_fg = calls[2 * k][1]                                # the "A" glyph's foreground rect
    # The kernel spreads around its offset center (a soft blur, not one hard copy)...
    assert max(r.left for r in a_taps) > min(r.left for r in a_taps)
    assert max(r.top for r in a_taps) > min(r.top for r in a_taps)
    # ...and its center of mass sits down-right of the glyph (the offset).
    assert sum(r.left for r in a_taps) / k > a_fg.left
    assert sum(r.top for r in a_taps) / k > a_fg.top
    try:
        be.close()
    except Exception:
        pass


def test_render_with_drop_shadow_effect():
    """The real draw path for the Segment LCD look: text (grid and flow fonts)
    drawn through the inline shadow-ink pass against a live swap chain. A wrong
    offset rect or a missing brush reset would fault or fail EndDraw here."""
    backend = WindowsBackend(width=40, height=16, title="puikit-lcd-test")
    backend.open()
    try:
        backend.set_post_effect(PostEffect(drop_shadow=0.6))
        assert backend._post_effect is not None and backend._crt is None
        lcd = Style(fg=(22, 32, 22), bg=(209, 225, 173))
        backend.draw_box(0, 0, 20, 6, hints={"fill": True}, style=Style(bg=(209, 225, 173)))
        backend.draw_text(1, 1, "88:88", style=lcd)                       # grid path
        backend.draw_text(1, 3, "segment", style=Style(                   # flow path
            fg=(22, 32, 22), font=Font(family="Consolas", size=16)))
        backend.present()
        backend._render()   # paints the offset shadow ink inline
    finally:
        backend.close()


# --- DPI scale vs. cached text formats -------------------------------------


def test_style_font_cache_does_not_survive_a_dpi_change():
    """A text format is created at a point size already multiplied by
    _dpi_scale, so one cached at a different scale draws and measures at the
    wrong size. _rebuild_fonts must drop them all."""
    backend = WindowsBackend()
    prop = Style(font=Font())
    backend._dpi_scale = 1.0
    backend._init_fonts()
    small_px = backend.measure_line_height(prop) * backend._base_h
    backend._dpi_scale = 2.0
    backend._rebuild_fonts()
    large_px = backend.measure_line_height(prop) * backend._base_h
    assert large_px == pytest.approx(2 * small_px, rel=0.05)


def test_open_discards_fonts_resolved_before_the_window_existed():
    """A Panel measures its content-sized widgets at set_layout() time, before
    open() -- when _dpi_scale is still the 1.0 placeholder. open() is the first
    point the window's monitor is known, so nothing resolved before it may
    survive, or on a HiDPI display that text renders at half the size of the
    grid text beside it (which _init_fonts always builds at the real scale)."""
    backend = WindowsBackend(width=40, height=12, title="puikit-dpi-test")
    backend.measure_line_height(Style(font=Font()))
    backend.measure_text("Keyboard hook", Style(font=Font()))
    assert backend._style_fonts, "precondition: the pre-open measure cached a format"
    backend.open()
    try:
        assert backend._style_fonts == {}
        assert backend._line_height_cache == {}
        assert backend._width_cache == {}
    finally:
        backend.close()


def test_system_tray_capability_matches_the_implementation():
    """set_tray is implemented (Shell_NotifyIconW), so the capability must say
    so — an app that asks before calling would otherwise skip a working tray."""
    import inspect

    assert WindowsBackend.PROFILE.supports("system_tray")
    assert WindowsBackend.set_tray is not Backend.set_tray
    # The additive image parameter (a .ico path) is part of the override too.
    assert "image" in inspect.signature(WindowsBackend.set_tray).parameters


# --- multi-window: create_window / WinWindowHandle -------------------------


def _secondary(backend, w=30, h=5, **kw):
    """A secondary window bound to a Panel, plus the panel."""
    from puikit import Panel
    win = backend.create_window(w, h, **kw)
    return win, Panel(backend, window=win)


def test_multi_window_capability_declared():
    assert WindowsBackend.PROFILE.supports("multi_window")


def test_create_window_before_open_raises():
    backend = WindowsBackend(width=20, height=6)
    with pytest.raises(RuntimeError):
        backend.create_window(10, 3)


def test_create_window_is_ui_thread_only():
    backend = WindowsBackend(width=20, height=6, title="puikit-mw-thread")
    backend.open()
    try:
        result = {}

        def worker():
            try:
                backend.create_window(10, 3)
            except Exception as e:  # noqa: BLE001 - the assertion is the type
                result["error"] = e

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        assert isinstance(result.get("error"), RuntimeError)
    finally:
        backend.close()


def test_secondary_window_has_its_own_hwnd_and_display_list():
    from puikit import WindowStyle
    from puikit.widgets import Label

    backend = WindowsBackend(width=40, height=12, title="puikit-mw-main")
    backend.open()
    main = __import__("puikit").Panel(backend)
    main.add(Label("main text"), 0, 0, 20, 1)
    main.render()
    try:
        win, popup = _secondary(
            backend, 30, 5, title="popup",
            style=WindowStyle(frameless=True, topmost=True, activates=False))
        popup.add(Label("popup text"), 0, 0, 20, 1)
        popup.render()
        main.render()  # re-render main AFTER the secondary

        assert win.hwnd and win.hwnd != backend._hwnd
        assert not win.closed
        assert win.window_style.frameless is True
        assert win.size_units == (30.0, 5.0)
        # Each window owns its display list; neither leaks into the other.
        main_text = [c[3] for c in backend._front_main if c and c[0] == "text"]
        popup_text = [c[3] for c in win.front if c and c[0] == "text"]
        assert "main text" in main_text and "popup text" not in main_text
        assert "popup text" in popup_text and "main text" not in popup_text
        # Its own swap chain on the shared device.
        assert win.swap_chain is not None and win.target_bitmap is not None
    finally:
        backend.close()


def test_secondary_window_paints_and_resizes():
    from puikit.widgets import Label

    backend = WindowsBackend(width=40, height=12, title="puikit-mw-paint")
    backend.open()
    try:
        win, popup = _secondary(backend, 30, 5)
        popup.add(Label("painted"), 0, 0, 20, 1)
        popup.render()
        # The real Direct2D path for a secondary window (retarget -> draw ->
        # present), driven through the window procedure as WM_PAINT does.
        backend._handle_message(win.hwnd, native.WM_PAINT, 0, 0)
        # Resizing rebinds its swap-chain bitmap and leaves the device context
        # pointed back at the MAIN window's target.
        before = win.target_bitmap
        backend._handle_message(win.hwnd, native.WM_SIZE, 0, (17 << 16) | 400)
        assert win.target_bitmap is not None and win.target_bitmap is not before
        assert backend._frame_target is backend._target_bitmap
        # The main window still renders after all that.
        backend._render()
    finally:
        backend.close()


def test_secondary_input_goes_to_the_window_not_the_app_handler():
    from puikit.event import EventType
    from puikit.widgets import Button

    backend = WindowsBackend(width=40, height=12, title="puikit-mw-input")
    backend.open()
    app_events = []
    backend._handler = app_events.append
    try:
        clicked = []
        win, popup = _secondary(backend, 30, 5)
        popup.add(Button("Go", on_click=lambda: clicked.append(1)), 0, 0, 10, 1)
        popup.render()
        assert win.on_event is not None  # the Panel bound itself

        # Click inside the button, in that window's client pixels.
        px = int(2 * backend._base_w) | (int(0 * backend._base_h) << 16)
        backend._handle_message(win.hwnd, native.WM_LBUTTONDOWN, 0, px)
        backend._handle_message(win.hwnd, native.WM_LBUTTONUP, 0, px)
        assert clicked == [1]
        assert app_events == []  # the app's main-window handler saw nothing

        # Keyboard too: a chooser's filter field is typed into, so WM_KEYDOWN /
        # WM_CHAR must reach the window's handler, not the app's.
        seen = []
        win.on_event = seen.append
        # VK_DOWN goes through WM_KEYDOWN (_VK_KEYS); a printable character
        # arrives as WM_CHAR, the way TranslateMessage delivers it.
        backend._handle_message(win.hwnd, native.WM_KEYDOWN, native.VK_DOWN, 0)
        backend._handle_message(win.hwnd, native.WM_CHAR, ord("k"), 0)
        assert [(e.type, e.key) for e in seen] == [
            (EventType.KEY, "down"), (EventType.KEY, "k")]
        assert app_events == []
    finally:
        backend._handler = None
        backend.close()


def test_user_close_fires_on_close_and_does_not_quit_the_app():
    backend = WindowsBackend(width=40, height=12, title="puikit-mw-close")
    backend.open()
    try:
        win, _popup = _secondary(backend, 30, 5)
        closed = []
        win.on_close = lambda: closed.append(1)
        backend._handle_message(win.hwnd, native.WM_CLOSE, 0, 0)
        assert closed == [1]
        assert win.closed
        assert not backend._quit_requested       # only the MAIN window quits
        assert win not in backend._secondary_windows
        assert win.swap_chain is None and win.target_bitmap is None
        win.close()                              # idempotent
    finally:
        backend.close()


def test_backend_close_closes_secondary_windows():
    backend = WindowsBackend(width=40, height=12, title="puikit-mw-teardown")
    backend.open()
    win, _popup = _secondary(backend, 30, 5)
    hwnd = win.hwnd
    backend.close()
    assert win.closed
    assert backend._secondary_windows == []
    assert hwnd not in backend._hwnd_windows


# --- multi-window IME: per-window context association ------------------------
#
# IMM32 associates an input context per HWND, so everything the main window
# does for the IME has to be done for a popup too, against the popup's own
# HWND — otherwise a chooser's filter box types ASCII but never composes.


def _has_ime_context(hwnd):
    """ImmGetContext != NULL, i.e. this window currently has an input context
    attached (text mode). NULL == detached by disable_ime (command mode)."""
    from puikit.backends import _win32_ime

    himc = _win32_ime.imm32.ImmGetContext(hwnd)
    if himc:
        _win32_ime.imm32.ImmReleaseContext(hwnd, himc)
    return bool(himc)


def test_secondary_window_starts_ime_gated_like_the_main_window():
    backend = WindowsBackend(width=40, height=12, title="puikit-mw-ime-gate")
    backend.open()
    try:
        win, _popup = _secondary(backend, 30, 5)
        # Its own saved default context, and detached until a field asks for it
        # — a bare `j` in a popup's list must stay a command key.
        assert win.default_himc != 0
        assert not _has_ime_context(win.hwnd)
    finally:
        backend.close()


def test_text_input_engages_the_scoped_window_not_the_main_one():
    backend = WindowsBackend(width=40, height=12, title="puikit-mw-ime-scope")
    backend.open()
    try:
        win, _popup = _secondary(backend, 30, 5)
        # Panel.render() enters _window_scope before _sync_text_input, so this
        # is the state begin_text_input actually sees for a popup's field.
        with backend._window_scope(win):
            backend.begin_text_input()
        assert backend._text_input_hwnd == win.hwnd
        assert _has_ime_context(win.hwnd)
        assert not _has_ime_context(backend._hwnd)  # main window stays gated

        with backend._window_scope(win):
            backend.end_text_input()
        assert backend._text_input_hwnd == 0
        assert not _has_ime_context(win.hwnd)
    finally:
        backend.close()


def test_closing_a_window_mid_text_input_releases_the_flag():
    backend = WindowsBackend(width=40, height=12, title="puikit-mw-ime-close")
    backend.open()
    try:
        win, _popup = _secondary(backend, 30, 5)
        with backend._window_scope(win):
            backend.begin_text_input()
        win.close()  # the popup's Panel never gets to call end_text_input
        assert backend._text_input_active is False
        assert backend._text_input_hwnd == 0
    finally:
        backend.close()


def test_secondary_window_composition_reaches_its_own_panel():
    """WM_IME_COMPOSITION on a popup must produce an IME_COMPOSITION event for
    that window's Panel (inline preedit), not fall through to DefWindowProc's
    floating composition box, and must read the composition from the popup's
    HWND."""
    from puikit.backends import _win32_ime
    from puikit.event import EventType

    backend = WindowsBackend(width=40, height=12, title="puikit-mw-ime-compose")
    backend.open()
    app_events = []
    backend._handler = app_events.append
    try:
        win, _popup = _secondary(backend, 30, 5)
        seen = []
        win.on_event = seen.append
        read_hwnds = []

        original = _win32_ime.read_composition

        def fake(hwnd, lparam):
            read_hwnds.append(hwnd)
            return ("あ", 1, 0, None)

        _win32_ime.read_composition = fake
        try:
            result = backend._handle_message(
                win.hwnd, _win32_ime.WM_IME_COMPOSITION, 0, _win32_ime.GCS_COMPSTR)
        finally:
            _win32_ime.read_composition = original

        assert result == 0  # handled here, never forwarded to DefWindowProc
        assert read_hwnds == [win.hwnd]
        assert [(e.type, e.hints) for e in seen] == [
            (EventType.IME_COMPOSITION, {"preedit": "あ", "caret": 1, "target_start": 0})]
        assert app_events == []  # the main window's handler saw nothing
    finally:
        backend._handler = None
        backend.close()


def test_secondary_window_setcontext_suppresses_the_os_composition_box():
    from puikit.backends import _win32_ime

    backend = WindowsBackend(width=40, height=12, title="puikit-mw-ime-setctx")
    backend.open()
    try:
        win, _popup = _secondary(backend, 30, 5)
        seen = []
        original = _win32_ime.strip_show_composition_window
        _win32_ime.strip_show_composition_window = lambda lp: (seen.append(lp), original(lp))[1]
        try:
            backend._handle_message(
                win.hwnd, _win32_ime.WM_IME_SETCONTEXT, 1,
                _win32_ime.ISC_SHOWUICOMPOSITIONWINDOW)
        finally:
            _win32_ime.strip_show_composition_window = original
        assert seen == [_win32_ime.ISC_SHOWUICOMPOSITIONWINDOW]
    finally:
        backend.close()


# --- animation pacing (WM_TIMER starvation; see _ensure_animation_timer) -------


class _FakeClock:
    """A monotonic clock that advances a fixed step on every read, so the pacing
    logic can be driven deterministically without sleeping."""

    def __init__(self, step=0.010):
        self.now = 1000.0
        self.step = step

    def monotonic(self):
        self.now += self.step
        return self.now


def _armed(monkeypatch, clock):
    """A headless backend with the animation tick armed and _on_animation_tick
    counting instead of rendering. Returns (backend, ticks)."""
    monkeypatch.setattr(windows_backend.time, "monotonic", clock.monotonic)
    be = WindowsBackend()
    ticks = []
    be._on_animation_tick = lambda: ticks.append(1)
    be._anim_timer_running = True
    be._anim_next_tick = clock.now
    return be, ticks


def test_pump_animation_tick_honors_the_frame_deadline(monkeypatch):
    clock = _FakeClock(step=0.001)
    be, ticks = _armed(monkeypatch, clock)
    try:
        be._pump_animation_tick()            # deadline already reached: ticks
        assert len(ticks) == 1
        be._pump_animation_tick()            # 1ms later: too early, no frame
        assert len(ticks) == 1
        for _ in range(30):                  # past the ~16ms frame: ticks again
            be._pump_animation_tick()
        assert len(ticks) == 2
    finally:
        be.close()


def test_pump_animation_tick_is_inert_when_nothing_animates(monkeypatch):
    clock = _FakeClock()
    be, ticks = _armed(monkeypatch, clock)
    try:
        be._anim_timer_running = False
        be._pump_animation_tick()
        assert ticks == []
    finally:
        be.close()


def test_a_slow_frame_never_leaves_the_deadline_in_the_past(monkeypatch):
    # Guards against a tick-storm: advancing the deadline by one frame from a
    # deadline long past would land it behind `now`, so the next pump would fire
    # immediately, _animation_wait_ms would stay 0, and the loop would spin.
    clock = _FakeClock(step=0.200)           # every "frame" takes 200ms
    be, _ticks = _armed(monkeypatch, clock)
    try:
        be._pump_animation_tick()
        assert be._anim_next_tick > clock.now
        clock.step = 0.0                     # hold the clock at the frame it ended on
        assert be._animation_wait_ms() > 0   # so the loop sleeps instead of spinning
    finally:
        be.close()


def test_animation_wait_is_infinite_while_idle_and_bounded_while_animating(monkeypatch):
    clock = _FakeClock(step=0.0)
    be, _ticks = _armed(monkeypatch, clock)
    try:
        assert be._animation_wait_ms() == 0   # a frame is already due
        be._anim_next_tick = clock.now + 0.010
        assert 0 < be._animation_wait_ms() <= 16
        be._anim_timer_running = False
        assert be._animation_wait_ms() == native.INFINITE
    finally:
        be.close()


def test_saturated_message_queue_still_animates(monkeypatch):
    # The regression this pacing exists for: holding a key down floods the queue
    # with autorepeat WM_KEYDOWNs, each dragging a repaint behind it, and WM_TIMER
    # is the lowest-priority message on Windows -- so a loop that waited for the
    # timer to arrive would never tick and an animated background froze until the
    # key was released. Here PeekMessageW always reports a message (the queue is
    # never empty), and frames must still be produced.
    clock = _FakeClock(step=0.010)
    be, ticks = _armed(monkeypatch, clock)
    dispatched = []

    def peek(_msg, _hwnd, _lo, _hi, _flags):
        return 1                              # a message is always waiting

    def dispatch(_msg):
        dispatched.append(1)
        if len(dispatched) >= 20:
            be._quit_requested = True
        return 0

    def never_waits(*_args):
        raise AssertionError("waited for a message while the queue was full")

    monkeypatch.setattr(native.user32, "PeekMessageW", peek)
    monkeypatch.setattr(native.user32, "TranslateMessage", lambda _msg: None)
    monkeypatch.setattr(native.user32, "DispatchMessageW", dispatch)
    monkeypatch.setattr(native.user32, "MsgWaitForMultipleObjectsEx", never_waits)
    try:
        be.run_event_loop(lambda _event: None)
        assert len(dispatched) == 20
        # 20 dispatches x 10ms of clock, one frame per ~16ms: several ticks, not none.
        assert len(ticks) >= 5
    finally:
        be.close()


def test_idle_loop_waits_on_the_queue_instead_of_spinning(monkeypatch):
    # The other half: an empty queue must block in the wait (bounded by the frame
    # deadline) rather than peek in a tight loop.
    clock = _FakeClock(step=0.010)
    be, _ticks = _armed(monkeypatch, clock)
    be._anim_timer_running = False            # nothing animating: an indefinite wait
    waits = []

    def wait(count, handles, timeout, wake_mask, flags):
        waits.append((count, handles, timeout, wake_mask, flags))
        be._quit_requested = True
        return 0

    monkeypatch.setattr(native.user32, "PeekMessageW", lambda *_a: 0)
    monkeypatch.setattr(native.user32, "MsgWaitForMultipleObjectsEx", wait)
    try:
        be.run_event_loop(lambda _event: None)
        assert waits == [(0, None, native.INFINITE, native.QS_ALLINPUT,
                          native.MWMO_INPUTAVAILABLE)]
    finally:
        be.close()


def test_wm_timer_still_ticks_for_native_modal_loops(monkeypatch):
    # The SetTimer source is kept because a native modal loop (a tracked popup
    # menu, a drag) pumps messages while run_event_loop is not running. It goes
    # through the same deadline gate, so it never double-ticks a frame.
    clock = _FakeClock(step=0.001)
    be, ticks = _armed(monkeypatch, clock)
    try:
        be._handle_message(0, native.WM_TIMER, windows_backend._TIMER_ID, 0)
        assert len(ticks) == 1
        be._handle_message(0, native.WM_TIMER, windows_backend._TIMER_ID, 0)
        assert len(ticks) == 1                # same frame: gated, not run twice
    finally:
        be.close()
