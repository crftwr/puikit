"""Borrowing the operating system's image decoder (:mod:`puikit._platform_image`).

Every backend has a decoder, and ``image_formats()`` reports what *that* one
reads. On a terminal the decoder is Pillow, so a terminal on macOS reported it
could not show a HEIC while ImageIO sat in the same process reading HEIC
perfectly well. These cover the module that asks the second question — what can
the machine decode, whichever backend is drawing — and the terminal path that
now falls through to it.

The macOS half runs here. The Windows half reuses the calls the Windows backend
already makes (verified on hardware) and is skipped elsewhere; what is checked on
every platform is that the module declines cleanly where there is nothing to
borrow, because that is what Linux gets.
"""

import sys

import pytest

from puikit import _platform_image
from puikit.backends import _terminal_graphics as tg

macos = pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
no_os_decoder = pytest.mark.skipif(
    sys.platform in ("darwin", "win32"), reason="platforms with an OS decoder")


@pytest.fixture(autouse=True)
def clean_decode_cache():
    tg.clear_cache()
    yield
    tg.clear_cache()


@pytest.fixture
def alpha_png(tmp_path):
    """Straight-alpha RGBA with the case premultiplication destroys: a saturated
    colour at very low alpha, which premultiplying crushes to near-black."""
    Image = pytest.importorskip("PIL.Image")
    image = Image.new("RGBA", (4, 1))
    for x, pixel in enumerate([(255, 0, 0, 255), (0, 0, 255, 128),
                               (255, 255, 255, 8), (0, 255, 0, 0)]):
        image.putpixel((x, 0), pixel)
    path = tmp_path / "alpha.png"
    image.save(path)
    return str(path)


def _pixels(raster):
    return [tuple(raster.data[i:i + 4]) for i in range(0, len(raster.data), 4)]


# --------------------------------------------------------------------------- #
# Declining cleanly
# --------------------------------------------------------------------------- #

def test_a_file_that_does_not_exist_decodes_to_nothing(tmp_path):
    assert _platform_image.decode(str(tmp_path / "absent.heic")) is None


def test_every_way_of_declining_is_the_same_answer():
    # No OS decoder, an unreadable file, an unsupported layout — one None,
    # because the caller does the same thing with all of them.
    assert _platform_image.decode("") is None


@no_os_decoder
def test_a_platform_with_nothing_to_borrow_says_so():
    # Linux. An empty set is the truth here, not a failure, and the terminal
    # path is expected to carry on with Pillow alone.
    assert _platform_image.extensions() == frozenset()
    assert _platform_image.decode("/etc/hostname") is None


# --------------------------------------------------------------------------- #
# macOS
# --------------------------------------------------------------------------- #

@macos
def test_the_os_reads_more_than_pillow_does():
    pytest.importorskip("AppKit")
    extensions = _platform_image.extensions()
    assert {".heic", ".heif"} <= extensions
    assert all(e.startswith(".") and e == e.lower() for e in extensions)


@macos
def test_alpha_survives_the_decode(alpha_png):
    # The reason this goes through NSBitmapImageRep and not a CGBitmapContext: a
    # bitmap context refuses straight alpha, so the obvious route would return
    # premultiplied pixels and (255, 255, 255, 8) would arrive as (8, 8, 8, 8).
    pytest.importorskip("AppKit")
    raster = _platform_image.decode(alpha_png)
    assert raster is not None
    assert _pixels(raster) == [(255, 0, 0, 255), (0, 0, 255, 128),
                               (255, 255, 255, 8), (0, 255, 0, 0)]


@macos
def test_an_opaque_image_comes_back_fully_opaque(tmp_path):
    # 32 bits with three samples is RGBX: the fourth byte is padding the decoder
    # never defined, so it has to be made opaque rather than passed through.
    Image = pytest.importorskip("PIL.Image")
    pytest.importorskip("AppKit")
    path = tmp_path / "solid.png"
    Image.new("RGB", (3, 2), (10, 120, 200)).save(path)

    raster = _platform_image.decode(str(path))
    assert raster is not None
    assert raster.size == (3, 2)
    assert _pixels(raster) == [(10, 120, 200, 255)] * 6


@macos
def test_a_layout_it_cannot_read_is_declined_rather_than_guessed(tmp_path):
    # A CMYK TIFF is also 8 bits per sample, also four samples, also 32 bits per
    # pixel. Accepting it on shape alone produced a picture in the wrong colours
    # with nothing reporting a problem, which is worse than not reading it.
    Image = pytest.importorskip("PIL.Image")
    pytest.importorskip("AppKit")
    cmyk = tmp_path / "print.tif"
    Image.new("CMYK", (8, 4), (10, 20, 30, 40)).save(cmyk)
    deep = tmp_path / "deep.png"
    Image.new("I;16", (8, 4), 1000).save(deep)

    assert _platform_image.decode(str(cmyk)) is None
    assert _platform_image.decode(str(deep)) is None


# --------------------------------------------------------------------------- #
# The terminal falls through to it
# --------------------------------------------------------------------------- #

@macos
def test_the_terminal_lists_what_the_os_can_read_too():
    pytest.importorskip("AppKit")
    pytest.importorskip("PIL")
    extensions = tg.extensions()
    assert ".png" in extensions          # Pillow's
    assert ".heic" in extensions         # the OS's
    assert _platform_image.extensions() <= extensions


@macos
def test_the_terminal_renders_a_format_pillow_cannot_open(tmp_path):
    # The whole point: a HEIC on a terminal. Pillow declines it, the OS does not,
    # and the sixel/kitty encoders get their pixels either way.
    Image = pytest.importorskip("PIL.Image")
    pytest.importorskip("Quartz")
    from Foundation import NSURL
    import Quartz

    seed = tmp_path / "seed.png"
    Image.new("RGB", (40, 24), (200, 40, 40)).save(seed)
    source = Quartz.CGImageSourceCreateWithURL(NSURL.fileURLWithPath_(str(seed)), None)
    frame = Quartz.CGImageSourceCreateImageAtIndex(source, 0, None)
    photo = tmp_path / "photo.heic"
    dest = Quartz.CGImageDestinationCreateWithURL(
        NSURL.fileURLWithPath_(str(photo)), "public.heic", 1, None)
    if dest is None:
        pytest.skip("no HEIC encoder on this machine")
    Quartz.CGImageDestinationAddImage(dest, frame, None)
    if not Quartz.CGImageDestinationFinalize(dest):
        pytest.skip("HEIC encode failed on this machine")

    with pytest.raises(Exception):
        Image.open(str(photo)).load()     # Pillow really cannot

    assert tg.natural_size(str(photo)) == (40, 24)
    rendered = tg.render(str(photo), 40, 24)
    assert rendered is not None
    image, png = rendered
    assert image.size == (40, 24)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    # Lossy, so a tolerance rather than an equality.
    r, g, b = image.convert("RGB").getpixel((20, 12))
    assert abs(r - 200) <= 4 and abs(g - 40) <= 4 and abs(b - 40) <= 4


@macos
def test_an_os_decoded_picture_is_decoded_once(tmp_path, monkeypatch):
    # It lands in the same cache a Pillow-decoded one does, so measuring its size
    # and then drawing it costs one decode, not two.
    pytest.importorskip("AppKit")
    Image = pytest.importorskip("PIL.Image")
    path = tmp_path / "pic.png"
    Image.new("RGB", (16, 16), (1, 2, 3)).save(path)

    calls = []
    real = _platform_image.decode

    def counting(p):
        calls.append(p)
        return real(p)

    monkeypatch.setattr(_platform_image, "decode", counting)
    # Force the platform route by making Pillow refuse this file.
    monkeypatch.setattr(Image, "open", lambda *a, **k: (_ for _ in ()).throw(OSError()))

    assert tg.natural_size(str(path)) == (16, 16)
    assert tg.render(str(path), 8, 8) is not None
    assert len(calls) == 1
