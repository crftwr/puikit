"""Image widget tests: the GUI profile draws a real image, the TUI profile
falls back to a framed alt text — one widget, two fidelities — and the fit
modes resolve identically on both backends (only the draw fidelity differs)."""

import struct
import zlib

import pytest

from puikit import (
    Event,
    EventType,
    HSplit,
    Item,
    Panel,
    PROFILE_GUI_DESKTOP,
    PROFILE_TUI,
    VSplit,
)
from puikit.backends.memory_backend import MemoryBackend
from puikit.image import aspect_extent, contain_box, cover_source, image_size
from puikit.widgets import Button, ImageView, Label


def _png(path, w, h):
    """Write a minimal valid RGB PNG of the given pixel size."""
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


@pytest.fixture(params=[PROFILE_TUI, PROFILE_GUI_DESKTOP], ids=["tui", "gui"])
def backend(request):
    return MemoryBackend(width=30, height=12, capabilities=request.param)


def _has_images(backend):
    return backend.capabilities.supports("images")


def _rect_of(panel, widget):
    for slot in panel._children:
        if slot.widget is widget:
            return slot.rect
    raise AssertionError("widget not placed")


# --- image.py header reader and fit geometry --------------------------------


def test_image_size_reads_png_header(tmp_path):
    assert image_size(_png(tmp_path / "a.png", 100, 50)) == (100, 50)
    assert image_size(str(tmp_path / "missing.png")) is None


def test_aspect_extent_locks_ratio():
    # 2:1 image, square base unit: width 20 -> height 10, height 10 -> width 20.
    assert aspect_extent(20, True, 100, 50, 1, 1) == pytest.approx(10)
    assert aspect_extent(10, False, 100, 50, 1, 1) == pytest.approx(20)
    # A non-square base unit keeps the *pixel* aspect correct.
    assert aspect_extent(20, True, 100, 50, 1, 2) == pytest.approx(5)


def test_contain_box_letterboxes_and_cover_crops():
    # 100x50 into 20x8: contain fits width (16x8, centered horizontally).
    ox, oy, w, h = contain_box(20, 8, 100, 50)
    assert (round(w), round(h)) == (16, 8)
    assert (round(ox), round(oy)) == (2, 0)
    # cover crops the source so the full target is covered, aspect preserved.
    sx, sy, sw, sh = cover_source(100, 50, 20, 8)
    assert sw / sh == pytest.approx(20 / 8)
    # Full width kept, height cropped to match the wider target (centered).
    assert (round(sw), round(sh)) == (100, 40)
    assert (round(sx), round(sy)) == (0, 5)


# --- ImageView draw / fallback ----------------------------------------------


def test_imageview_draws_image_on_gui_else_alt_fallback(backend):
    panel = Panel(backend)
    panel.add(ImageView("logo.png", alt="LOGO"), x=0, y=0, w=10, h=4)
    panel.render()
    if _has_images(backend):
        assert len(backend.image_calls) == 1
        x, y, path, hints = backend.image_calls[0]
        assert (x, y, path) == (0, 0, "logo.png")
        assert (hints["w"], hints["h"], hints["fit"]) == (10, 4, "fill")
    else:
        assert backend.image_calls == []
        joined = "\n".join(backend.snapshot())
        assert "LOGO" in joined
        assert "┌" in joined  # the placeholder frame


def test_imageview_invalid_fit_rejected():
    with pytest.raises(ValueError):
        ImageView("logo.png", fit="stretchy")


def test_imageview_contain_fit_flows_to_draw_or_letterboxes_fallback(backend, tmp_path):
    path = _png(tmp_path / "wide.png", 100, 50)  # 2:1
    panel = Panel(backend)
    panel.add(ImageView(path, fit="contain"), x=0, y=0, w=20, h=8)
    panel.render()
    if _has_images(backend):
        _, _, _, hints = backend.image_calls[0]
        assert hints["fit"] == "contain"
    else:
        # The placeholder frames only the aspect-correct sub-rect: a 2:1 image
        # in a 20x8 box letterboxes to a 16-wide box, centered (left edge at 2).
        row = backend.snapshot()[0]
        assert row[0] == " " and row[2] == "┌"


def test_imageview_cover_fit_flows_to_draw(backend):
    panel = Panel(backend)
    panel.add(ImageView("logo.png", fit="cover"), x=0, y=0, w=10, h=4)
    panel.render()
    if _has_images(backend):
        assert backend.image_calls[0][3]["fit"] == "cover"


# --- ImageView aspect sizing (resolves the same on both backends) -----------


def test_imageview_fit_width_derives_height(backend, tmp_path):
    path = _png(tmp_path / "wide.png", 100, 50)  # 2:1 -> height = width / 2
    panel = Panel(backend)
    img = ImageView(path, fit="width")
    # A vertical stack hands the image the full pane width; its height follows.
    panel.set_layout(VSplit(Item(img, size="content"), Item(Label(""), weight=1)))
    panel.render()
    rect = _rect_of(panel, img)
    assert rect.w == pytest.approx(30)
    assert rect.h == pytest.approx(15)  # 30 * 50/100


def test_imageview_fit_height_derives_width(backend, tmp_path):
    path = _png(tmp_path / "tall.png", 50, 100)  # 1:2 -> width = height / 2
    panel = Panel(backend)
    img = ImageView(path, fit="height")
    # A horizontal split hands the image the full pane height; its width follows.
    panel.set_layout(HSplit(Item(img, size="content"), Item(Label(""), weight=1)))
    panel.render()
    rect = _rect_of(panel, img)
    assert rect.h == pytest.approx(12)
    assert rect.w == pytest.approx(6)  # 12 * 50/100


# --- Button image face (image-only and image+text) --------------------------


def test_image_button_fires_on_click(backend):
    panel = Panel(backend)
    clicks = []
    btn = Button(image="ok.png", on_click=lambda: clicks.append(1), alt="OK")
    panel.add(btn, x=0, y=0, w=8, h=4)
    panel.dispatch_event(Event(type=EventType.MOUSE_CLICK, x=2, y=2, button="left"))
    assert clicks == [1]


def test_image_button_fires_on_activate_key(backend):
    panel = Panel(backend)
    clicks = []
    btn = Button(image="ok.png", on_click=lambda: clicks.append(1))
    panel.add(btn, x=0, y=0, w=8, h=4)
    panel.dispatch_event(Event(type=EventType.KEY, key="enter"))
    assert clicks == [1]


def test_image_button_focus_ring_and_hover(backend):
    panel = Panel(backend)
    btn = Button(image="ok.png", alt="OK")
    panel.add(btn, x=0, y=0, w=8, h=4)
    panel.render()
    assert "┌" in "\n".join(backend.snapshot())
    # An image-only button reads as a neutral tile, not a primary action.
    base_bg = backend.style_at(0, 0).bg
    assert base_bg == panel.theme.control_bg
    panel.dispatch_event(Event(type=EventType.MOUSE_MOVE, x=3, y=2))
    panel.render()
    assert backend.style_at(7, 3).bg != base_bg


def test_image_button_inset_image_uses_contain_by_default(backend):
    panel = Panel(backend)
    panel.add(Button(image="ok.png", pad=1), x=0, y=0, w=8, h=4)
    panel.render()
    if _has_images(backend):
        x, y, path, hints = backend.image_calls[0]
        assert (x, y) == (1, 1)
        assert (hints["w"], hints["h"]) == (6, 2)
        assert hints["fit"] == "contain"


def test_image_button_invalid_fit_rejected():
    with pytest.raises(ValueError):
        Button(image="ok.png", fit="width")  # aspect modes are not face fits


def test_button_needs_label_or_image():
    with pytest.raises(ValueError):
        Button()


def test_image_text_button_draws_icon_and_label(backend):
    panel = Panel(backend)
    btn = Button("Play", image="ok.png", alt="P", pad=1, gap=1)
    panel.add(btn, x=0, y=0, w=14, h=4)
    panel.render()
    # The label is drawn alongside the icon...
    assert "Play" in "\n".join(backend.snapshot())
    # ...over the accent action fill (a label makes it a primary action).
    assert backend.style_at(0, 0).bg == panel.theme.button_bg
    if _has_images(backend):
        # The icon is a square sized to the inner height (h - 2*pad = 2).
        x, y, path, hints = backend.image_calls[0]
        assert (hints["w"], hints["h"]) == (2, 2)


def test_image_text_button_width_is_intrinsic(backend):
    # In a horizontal split, size="content" reserves icon + gap + label + pads.
    panel = Panel(backend)
    btn = Button("Go", image="ok.png")  # icon square = height-2*pad
    panel.set_layout(
        HSplit(Item(btn, size="content"), Item(Label(""), weight=1))
    )
    panel.render()
    rect = _rect_of(panel, btn)
    height = backend.size[1]
    inner_h = max(1.0, height - 2 * 1)  # default pad=1
    expected = 2 * 1 + inner_h + 1 + len("Go")  # 2*pad + icon + gap + text
    assert rect.w == pytest.approx(expected)
