"""Inline-image protocol tests: detection from the environment, the crop/scale
render step, and the three wire encoders — including a round-trip that decodes
the hand-written sixel output back to pixels, since a sixel stream that merely
*looks* well-formed can still render garbage."""

import re

import pytest

from puikit.backends import _terminal_graphics as tg

pytestmark = pytest.mark.skipif(
    not tg.have_pillow(), reason="Pillow is an optional dependency"
)


@pytest.fixture
def quadrants(tmp_path):
    """A 24x12 image split into four solid quadrants, so a crop can be told
    apart from the whole image by sampling one pixel."""
    from PIL import Image

    image = Image.new("RGB", (24, 12))
    for x in range(24):
        for y in range(12):
            top, left = y < 6, x < 12
            image.putpixel(
                (x, y),
                (255, 0, 0) if (top and left) else
                (0, 255, 0) if top else
                (0, 0, 255) if left else (255, 255, 0),
            )
    path = tmp_path / "quadrants.png"
    image.save(path)
    return str(path)


# --- detection ---------------------------------------------------------------


@pytest.mark.parametrize("env,expected", [
    ({"TERM": "xterm-kitty"}, tg.KITTY),
    ({"TERM": "xterm", "KITTY_WINDOW_ID": "1"}, tg.KITTY),
    ({"TERM": "xterm", "KONSOLE_VERSION": "220400"}, tg.KITTY),
    ({"TERM_PROGRAM": "WezTerm"}, tg.KITTY),
    ({"TERM_PROGRAM": "iTerm.app"}, tg.ITERM2),
    ({"TERM_PROGRAM": "mintty"}, tg.ITERM2),
    ({"TERM": "foot"}, tg.SIXEL),
    ({"TERM": "mlterm"}, tg.SIXEL),
    ({"TERM": "xterm-256color"}, None),
    ({}, None),
])
def test_detect_protocol_from_environment(env, expected):
    assert tg.detect_protocol(env) == expected


def test_detect_protocol_override_forces_and_disables():
    # An explicit protocol wins over the emulator's own signature...
    assert tg.detect_protocol({"TERM": "xterm", "PUIKIT_TERM_GRAPHICS": "sixel"}) == tg.SIXEL
    # ...and "none" turns the feature off even where it would work.
    for off in ("none", "off", "0"):
        assert tg.detect_protocol({"TERM": "xterm-kitty", "PUIKIT_TERM_GRAPHICS": off}) is None


def test_detect_protocol_is_none_without_pillow(monkeypatch):
    # Every protocol needs Pillow to crop/scale/encode, so its absence must
    # disable detection rather than yield a protocol that cannot render.
    monkeypatch.setattr(tg, "have_pillow", lambda: False)
    assert tg.detect_protocol({"TERM": "xterm-kitty"}) is None


# --- render (crop + scale) ---------------------------------------------------


def test_cell_pixels_divides_fractionally(monkeypatch):
    # Integer division truncated the sub-pixel remainder (ws_ypixel is rarely an
    # exact multiple of the row count), losing a pixel per row -> a couple of
    # blank rows across a tall image and a mismatched cell aspect. Keep the
    # fraction so a scaled image lines up with the emulator's real cell grid.
    import fcntl
    import struct

    # rows=80, cols=100, xpixel=805, ypixel=1290 -> cell 8.05 x 16.125 (not 8x16).
    packed = struct.pack("HHHH", 80, 100, 805, 1290)
    monkeypatch.setattr(fcntl, "ioctl", lambda *a, **k: packed)
    w, h = tg.cell_pixels(fd=1)
    assert w == pytest.approx(805 / 100)
    assert h == pytest.approx(1290 / 80)  # 16.125, not truncated to 16


def test_render_returns_image_and_png(quadrants):
    image, png = tg.render(quadrants, 24, 12)
    assert image.size == (24, 12)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")


def test_render_applies_src_crop(quadrants):
    # The bottom-right quadrant is solid yellow; cropping to it (normalized: the
    # far half of each axis) must leave no trace of the other three. With a crop
    # the image is resized to fill the pixel box exactly (the caller matched the
    # aspect), so it comes back box-sized, still all one colour.
    image, _ = tg.render(quadrants, 24, 12, src=(0.5, 0.5, 0.5, 0.5))
    assert image.size == (24, 12)
    assert [color for _, color in image.getcolors()] == [(255, 255, 0)]


def test_render_fills_the_box_exactly_for_a_crop(quadrants):
    # A crop whose aspect does NOT match the box is still resized to the full box
    # (fill), because the caller guarantees the match; the terminal must not add a
    # letterbox that piles blank space at the bottom.
    image, _ = tg.render(quadrants, 30, 10, src=(0.0, 0.0, 1.0, 1.0))
    assert image.size == (30, 10)


def test_render_downscales_to_pixel_box_but_never_upscales(quadrants):
    small, _ = tg.render(quadrants, 12, 6)
    assert small.size == (12, 6)
    # Asking for a box larger than the source leaves it alone: magnifying is the
    # emulator's job, and upscaling here would only inflate the payload.
    same, _ = tg.render(quadrants, 240, 120)
    assert same.size == (24, 12)


def test_render_preserves_aspect_ratio(quadrants):
    # A box with the wrong aspect fits the limiting axis, not both.
    image, _ = tg.render(quadrants, 12, 100)
    assert image.size == (12, 6)


def test_render_missing_file_returns_none(tmp_path):
    assert tg.render(str(tmp_path / "nope.png"), 10, 10) is None


# --- encoders ----------------------------------------------------------------


def test_kitty_encodes_png_with_placement_and_id(quadrants):
    image, png = tg.render(quadrants, 24, 12)
    sequence = tg.encode(tg.KITTY, image, png, cols=8, rows=4, image_id=7)
    assert sequence.startswith("\x1b_G")
    assert sequence.endswith("\x1b\\")
    assert "a=T" in sequence and "f=100" in sequence  # transmit+display, PNG
    assert "i=7" in sequence and "c=8,r=4" in sequence
    assert "C=1" in sequence  # must not move the cursor curses is tracking
    assert "q=2" in sequence  # replies suppressed, else they arrive as keys


def test_kitty_chunks_large_payloads(quadrants, monkeypatch):
    monkeypatch.setattr(tg, "_KITTY_CHUNK", 32)
    image, png = tg.render(quadrants, 24, 12)
    sequence = tg.encode(tg.KITTY, image, png, cols=8, rows=4)
    chunks = sequence.count("\x1b_G")
    assert chunks > 1
    # Every chunk but the last says "more follows"; the last says m=0.
    assert sequence.count("m=1") == chunks - 1
    assert sequence.count("m=0") == 1


def test_kitty_clear_targets_the_image_id():
    assert tg.clear(tg.KITTY, 7) == "\x1b_Ga=d,d=i,i=7\x1b\\"


def test_iterm2_encodes_inline_file(quadrants):
    image, png = tg.render(quadrants, 24, 12)
    sequence = tg.encode(tg.ITERM2, image, png, cols=8, rows=4)
    assert sequence.startswith("\x1b]1337;File=")
    assert sequence.endswith("\a")
    assert f"size={len(png)}" in sequence
    assert "width=8;height=4" in sequence
    assert "preserveAspectRatio=1" in sequence  # fallback: whole image, letterboxed
    assert "inline=1" in sequence
    # doNotMoveCursor is NOT a real iTerm2 File argument (kitty's C=1 has no
    # analog); sending it stopped the image rendering. Cursor drift is handled
    # by the backend bracketing emission in DECSC/DECRC instead.
    assert "doNotMoveCursor" not in sequence


def test_iterm2_fill_stretches_to_the_cell_box(quadrants):
    # When the image was already sized to the cell box (a crop was applied),
    # iTerm2 must stretch it to fill — preserveAspectRatio=0 — or its own cell
    # rounding re-letterboxes and blank rows return at the bottom.
    image, png = tg.render(quadrants, 24, 12, src=(0.0, 0.0, 1.0, 1.0))
    sequence = tg.encode(tg.ITERM2, image, png, cols=8, rows=4, fill=True)
    assert "preserveAspectRatio=0" in sequence


def test_protocols_without_a_delete_verb_report_it():
    # An honest empty string: the backend repaints the covered cells instead.
    assert tg.clear(tg.ITERM2) == ""
    assert tg.clear(tg.SIXEL) == ""


def _decode_sixel(stream):
    """Decode a sixel stream back to ``{(x, y): (r, g, b)}``. Only the subset
    the encoder emits (palette defs, color selects, repeats, ``$``/``-``)."""
    body = stream[stream.index("q") + 1:].removesuffix("\x1b\\")
    body = re.sub(r'^"[\d;]*', "", body)
    palette, pixels = {}, {}
    x = y = color = 0
    index = 0
    while index < len(body):
        char = body[index]
        if char == "#":
            match = re.match(r"#(\d+)(?:;\d+;(\d+);(\d+);(\d+))?", body[index:])
            number = int(match.group(1))
            if match.group(2) is not None:  # a palette definition
                palette[number] = tuple(
                    int(match.group(g)) * 255 // 100 for g in (2, 3, 4)
                )
            color = number
            index += match.end()
            continue
        if char == "$":  # carriage return: overlay the next color on this band
            x, index = 0, index + 1
            continue
        if char == "-":  # next band
            x, y, index = 0, y + 6, index + 1
            continue
        match = re.match(r"!(\d+)(.)", body[index:])
        if match:
            count, glyph, index = int(match.group(1)), match.group(2), index + match.end()
        else:
            count, glyph, index = 1, char, index + 1
        bits = ord(glyph) - 63
        for step in range(count):
            for bit in range(6):
                if bits >> bit & 1:
                    pixels[(x + step, y + bit)] = palette.get(color)
        x += count
    return pixels


def test_sixel_round_trips_to_the_original_pixels(quadrants):
    from PIL import Image

    source = Image.open(quadrants).convert("RGB")
    pixels = _decode_sixel(tg._sixel(source))
    assert len(pixels) == source.width * source.height
    for x in range(source.width):
        for y in range(source.height):
            want, got = source.getpixel((x, y)), pixels[(x, y)]
            # Sixel color components are percentages, so allow rounding drift.
            assert max(abs(a - b) for a, b in zip(want, got)) <= 3, f"at {(x, y)}"


def test_sixel_flattens_alpha_onto_black(tmp_path):
    from PIL import Image

    image = Image.new("RGBA", (6, 6), (255, 0, 0, 0))  # fully transparent red
    stream = tg._sixel(image)
    assert stream.startswith("\x1bP") and stream.endswith("\x1b\\")
    # Transparent pixels composite to black rather than carrying alpha through.
    assert set(_decode_sixel(stream).values()) == {(0, 0, 0)}


def test_sixel_run_uses_repeat_only_when_shorter():
    assert tg._sixel_run("?", 1) == "?"
    assert tg._sixel_run("?", 3) == "???"
    assert tg._sixel_run("?", 9) == "!9?"


# --- curses backend placement pipeline ---------------------------------------
#
# The backend paints images out-of-band, after curses has committed its grid, so
# nothing here can be observed through the character cells: these drive
# _present_images directly and assert on the escape sequences it writes.


class _FakeScreen:
    """Enough of a curses window for present()'s image phase."""

    def __init__(self):
        self.redrawn = 0

    def getmaxyx(self):
        return (24, 80)

    def erase(self):
        pass

    def redrawwin(self):
        self.redrawn += 1

    def refresh(self):
        pass

    def addstr(self, *args, **kwargs):
        pass

    def move(self, *args):
        pass


@pytest.fixture
def kitty_backend(monkeypatch):
    monkeypatch.setenv("PUIKIT_TERM_GRAPHICS", "kitty")
    from puikit.backends.curses_backend import CursesBackend

    backend = CursesBackend()
    backend._stdscr = _FakeScreen()
    backend._cell_px = (8, 16)  # skip the TIOCGWINSZ probe
    return backend


def _frame(backend, draws, force=False):
    """Run one clear/draw/present-images cycle, returning what was written.

    The backend writes escapes to ``_raw_out`` (the real terminal, bypassing any
    sys.stdout redirection), so the buffer is injected there — not by swapping
    sys.stdout, which the backend deliberately no longer uses."""
    import io

    backend.clear()
    for draw in draws:
        backend.draw_image(*draw)
    buffer = io.StringIO()
    backend._raw_out = buffer
    backend._present_images(force=force)
    return buffer.getvalue()


def test_curses_advertises_images_when_a_protocol_is_present(kitty_backend):
    assert kitty_backend._term_graphics == tg.KITTY
    assert kitty_backend.PROFILE.supports("images")


def test_base_pixel_size_reports_the_real_cell_not_one_by_one(kitty_backend):
    # base_size stays (1, 1) — the grid's layout unit — but a cell is not square,
    # so base_pixel_size must report its true pixel dimensions or aspect-sensitive
    # code (image fit) crops to the wrong aspect and the terminal letterboxes it.
    kitty_backend._cell_px = (7, 15)
    assert kitty_backend.base_size == (1, 1)
    assert kitty_backend.base_pixel_size == (7, 15)


def test_base_pixel_size_falls_back_when_the_terminal_is_silent(kitty_backend, monkeypatch):
    monkeypatch.setattr(tg, "cell_pixels", lambda fd=None: None)
    kitty_backend._cell_px = None
    assert kitty_backend.base_pixel_size == (8, 16)  # nominal 1:2, not (1, 1)


def test_oversized_image_is_clipped_to_the_screen(kitty_backend):
    # A picture is painted out-of-band, so push_clip does not trim it like text.
    # An ImageView taller than the screen must be clipped here, or it draws off
    # the bottom (over the status bar, etc.). The source is cropped to match.
    kitty_backend._stdscr = _FakeScreen()  # 80 cols x 24 rows
    kitty_backend.draw_image(10, 20, "img.png", {"w": 30, "h": 30, "src": None})
    x, y, cols, rows, _, src = next(iter(kitty_backend._images.values()))
    assert (x, y, cols, rows) == (10, 20, 30, 4)  # 30 rows -> 4 (screen bottom)
    assert src == pytest.approx((0.0, 0.0, 1.0, 4 / 30))  # only the visible top


def test_image_clipped_to_a_pushed_clip_rect(kitty_backend):
    kitty_backend._stdscr = _FakeScreen()
    kitty_backend.push_clip(0, 0, 40, 15)  # a pane
    kitty_backend.draw_image(5, 5, "img.png", {"w": 50, "h": 50})
    x, y, cols, rows, _, _ = next(iter(kitty_backend._images.values()))
    assert (x, y, cols, rows) == (5, 5, 35, 10)  # trimmed to the pane


def test_fully_offscreen_image_is_dropped(kitty_backend):
    kitty_backend._stdscr = _FakeScreen()
    kitty_backend.draw_image(10, 30, "img.png", {"w": 10, "h": 10})  # below row 24
    assert kitty_backend._images == {}


def test_object_fits_resolve_distinctly_on_the_terminal(kitty_backend, tmp_path):
    # fill / contain / cover must read differently, like the GUI backends. A 16:9
    # image in a portrait-ish cell box: fill stretches the whole image across it;
    # contain centers the whole image in an aspect-locked sub-box (letterbox);
    # cover crops a centered slice to fill the box.
    from tests.test_image_widgets import _png

    kitty_backend._stdscr = _FakeScreen()  # 80 cols x 24 rows
    kitty_backend._cell_px = (8, 16)
    scene = _png(tmp_path / "scene.png", 1600, 900)  # 16:9

    def place(fit):
        kitty_backend._images = {}
        # A 30x20-cell (8x16px) box is 240x320px — portrait — so the 16:9 scene
        # fits the three ways distinctly. Kept inside the 24-row screen (no clip).
        kitty_backend.draw_image(5, 1, scene, {"w": 30, "h": 20, "fit": fit})
        return next(iter(kitty_backend._images.values()))

    fx, fy, fcols, frows, _, fsrc = place("fill")
    cx, cy, ccols, crows, _, csrc = place("contain")
    vx, vy, vcols, vrows, _, vsrc = place("cover")

    # fill: whole image, whole box.
    assert (fcols, frows) == (30, 20) and fsrc == (0.0, 0.0, 1.0, 1.0)
    # contain: whole image (src unchanged) but a shorter, centered box (letterbox).
    assert csrc == (0.0, 0.0, 1.0, 1.0)
    assert crows < 20 and cy > 1  # shrunk and pushed down to centre
    # cover: full box, but a cropped source (narrower than the whole image).
    assert (vcols, vrows) == (30, 20)
    assert vsrc[2] < 1.0  # a horizontal crop of the 16:9 scene


def test_crop_src_narrows_an_existing_window():
    # Clipping composes with a pan/zoom crop: the fraction is of the current
    # window, not the whole image.
    from puikit.backends.curses_backend import CursesBackend

    assert CursesBackend._crop_src(None, 0.25, 0.0, 0.5, 1.0) == (0.25, 0.0, 0.5, 1.0)
    # Within a window already covering the middle half, take its right half.
    assert CursesBackend._crop_src((0.25, 0.0, 0.5, 1.0), 0.5, 0.0, 0.5, 1.0) == (0.5, 0.0, 0.25, 1.0)


def test_curses_without_a_protocol_keeps_images_off(monkeypatch):
    monkeypatch.setenv("PUIKIT_TERM_GRAPHICS", "none")
    from puikit.backends.curses_backend import CursesBackend

    backend = CursesBackend()
    assert backend._term_graphics is None
    assert not backend.PROFILE.supports("images")


def test_images_go_to_the_real_terminal_not_a_redirected_stdout(monkeypatch, quadrants):
    # THE reason images never appeared in iTerm2: a host app (TFM) replaces
    # sys.stdout with a log-capture shim that never forwards to the tty — and an
    # image escape has no newline, so a line-buffering shim holds it forever. The
    # backend must write to the real terminal (captured as _raw_out from
    # sys.__stdout__, which no host reassigns), never the live sys.stdout.
    import io
    import sys

    monkeypatch.setenv("PUIKIT_TERM_GRAPHICS", "iterm2")
    from puikit.backends.curses_backend import CursesBackend

    backend = CursesBackend()
    assert backend._raw_out is (sys.__stdout__ or sys.stdout)  # the original stream

    real_terminal = io.StringIO()
    backend._raw_out = real_terminal  # stand-in for the tty
    backend._stdscr = _FakeScreen()
    backend._cell_px = (8, 16)

    # A host redirects sys.stdout AFTER the backend was built, to a sink that
    # never reaches the terminal (like TFM's LogCapture).
    swallowed: list[str] = []

    class _Shim:
        def write(self, text):
            swallowed.append(text)

        def flush(self):
            pass

    monkeypatch.setattr(sys, "stdout", _Shim())

    backend.clear()
    backend.draw_image(2, 1, quadrants, {"w": 20, "h": 10})
    backend._present_images()

    assert "\x1b]1337;File=" in real_terminal.getvalue()  # reached the terminal
    assert not any("1337" in text for text in swallowed)  # NOT the redirected sink


def test_iterm2_resize_reemits_every_image_not_just_the_changed_one(monkeypatch, quadrants):
    # iTerm2/sixel have no delete verb, so a stale image is erased by repainting
    # the whole grid — which wipes EVERY image. All must then be re-sent, or an
    # image that did not move (common while resizing, when only some shift)
    # vanishes. Regression for "images disappear when I change the terminal size".
    import io

    monkeypatch.setenv("PUIKIT_TERM_GRAPHICS", "iterm2")
    from puikit.backends.curses_backend import CursesBackend

    backend = CursesBackend()
    backend._stdscr = _FakeScreen()
    backend._cell_px = (8, 16)

    def frame(placements):
        backend.clear()
        for x, y, w, h in placements:
            backend.draw_image(x, y, quadrants, {"w": w, "h": h})
        buf = io.StringIO()
        backend._raw_out = buf
        backend._present_images()
        return buf.getvalue().count("\x1b]1337;File=")

    assert frame([(2, 2, 20, 10), (30, 2, 20, 10)]) == 2   # both new
    assert frame([(2, 2, 20, 10), (30, 3, 20, 10)]) == 2   # only 2nd moved -> BOTH resent


def test_placement_transmits_at_the_right_cell(kitty_backend, quadrants):
    out = _frame(kitty_backend, [(2, 1, quadrants, {"w": 20, "h": 10})])
    assert "\x1b_G" in out and "a=T" in out
    # The cell is addressed in the terminal's own 1-based coordinates.
    assert "\x1b[2;3H" in out


def test_emission_brackets_images_in_save_restore_cursor(monkeypatch, quadrants):
    # iTerm2 and sixel advance the cursor when they draw; the whole batch is
    # bracketed by DECSC (\x1b7) / DECRC (\x1b8) so that drift can't scroll the
    # alternate screen out from under curses.
    monkeypatch.setenv("PUIKIT_TERM_GRAPHICS", "iterm2")
    from puikit.backends.curses_backend import CursesBackend

    backend = CursesBackend()
    backend._stdscr = _FakeScreen()
    backend._cell_px = (8, 16)
    out = _frame(backend, [(2, 1, quadrants, {"w": 20, "h": 10})])
    assert "\x1b]1337;File=" in out  # the image really was emitted
    assert out.index("\x1b7") < out.index("\x1b]1337")  # save before the image
    assert out.rindex("\x1b8") > out.rindex("\x1b]1337")  # restore after it


def test_unchanged_placement_is_not_retransmitted(kitty_backend, quadrants):
    draw = [(2, 1, quadrants, {"w": 20, "h": 10})]
    _frame(kitty_backend, draw)
    # Re-sending a multi-hundred-KB payload every frame would make panning crawl.
    assert _frame(kitty_backend, draw) == ""


def test_a_full_repaint_forces_every_placement_to_be_resent(kitty_backend, quadrants):
    # Recolored pairs / IME make present() redrawwin, which wipes the images off
    # the screen even though none of them changed.
    draw = [(2, 1, quadrants, {"w": 20, "h": 10})]
    _frame(kitty_backend, draw)
    assert "a=T" in _frame(kitty_backend, draw, force=True)


def test_moved_placement_is_deleted_then_redrawn(kitty_backend, quadrants):
    _frame(kitty_backend, [(2, 1, quadrants, {"w": 20, "h": 10})])
    out = _frame(kitty_backend, [(5, 3, quadrants, {"w": 20, "h": 10})])
    assert "a=d,d=i,i=1" in out  # the stale placement is erased by id
    assert "a=T" in out and "\x1b[4;6H" in out  # and redrawn at the new cell


def test_vanished_placement_is_only_deleted(kitty_backend, quadrants):
    _frame(kitty_backend, [(2, 1, quadrants, {"w": 20, "h": 10})])
    out = _frame(kitty_backend, [])
    assert "a=d,d=i,i=1" in out
    assert "a=T" not in out


def test_changing_the_crop_retransmits(kitty_backend, quadrants):
    # This is what a zoom or pan step looks like at the backend (normalized src).
    whole = _frame(kitty_backend, [(2, 1, quadrants, {"w": 20, "h": 10,
                                                      "src": (0.0, 0.0, 1.0, 1.0)})])
    cropped = _frame(kitty_backend, [(2, 1, quadrants, {"w": 20, "h": 10,
                                                        "src": (0.5, 0.5, 0.5, 0.5)})])
    assert "a=T" in cropped and cropped != whole


def test_zero_sized_placement_is_ignored(kitty_backend, quadrants):
    assert _frame(kitty_backend, [(2, 1, quadrants, {"w": 0, "h": 10})]) == ""


def test_close_erases_images_left_on_screen(kitty_backend, quadrants):
    import io

    _frame(kitty_backend, [(2, 1, quadrants, {"w": 20, "h": 10})])
    buffer = io.StringIO()
    kitty_backend._raw_out = buffer  # capture the real-terminal writes
    try:
        kitty_backend.close()
    except Exception:
        pass  # the rest of close() needs a real terminal; the erase came first
    # Images live outside the grid, so endwin() would leave them in scrollback.
    assert "a=d" in buffer.getvalue()
