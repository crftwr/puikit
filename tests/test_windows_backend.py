"""WindowsBackend tests. Most run without opening a window (mirroring
test_macos_backend.py's philosophy); a couple exercise a real window since,
unlike PyObjC/AppKit, this backend's only dependency is ctypes/stdlib, so
opening one is cheap and safe in CI on a Windows runner."""

import struct
import sys
import zlib

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only backend")
pytest.importorskip("ctypes.wintypes", reason="Windows-only backend")

from puikit import Rect  # noqa: E402
from puikit.backend import Style, TextAttribute  # noqa: E402
from puikit.font import Font, FontWeight  # noqa: E402
from puikit.backends.windows_backend import Animation, WindowsBackend  # noqa: E402


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
        assert backend.measure_text("hello") == 5.0
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


def test_font_params_resolves_family_by_monospace():
    backend = WindowsBackend()
    try:
        mono_family, _, _, _ = backend._font_params(Font(monospace=True))
        prop_family, _, _, _ = backend._font_params(Font())
        assert mono_family == "Consolas"
        assert prop_family == "Segoe UI"
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
    # Unused by any PuiKit app to date (see MacOSBackend.PROFILE, same four False):
    assert not profile.supports("clipboard_rich")
    assert not profile.supports("native_file_dialog")
    assert not profile.supports("system_tray")
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
    assert backend._front[0] == ("group_begin", id(scale_w), rect)
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
