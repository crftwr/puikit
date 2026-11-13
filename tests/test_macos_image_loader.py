"""Decoding image files off the UI thread on macOS (xefm#732)."""

import sys
import threading
import time

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="macOS-only backend"
)
pytest.importorskip("AppKit", reason="pyobjc not installed")
pytest.importorskip("Quartz", reason="pyobjc Quartz not installed")

from puikit.backends import _macos_image_loader as loader  # noqa: E402
from puikit.backends._image_cache import MISS  # noqa: E402
from puikit.backends.macos_backend import MacOSBackend  # noqa: E402
from puikit.image import source_key  # noqa: E402


def _png(path, w, h):
    Image = pytest.importorskip("PIL.Image")
    Image.new("RGB", (w, h), (10, 120, 200)).save(path)
    return str(path)


# --- decode -----------------------------------------------------------------

def test_decode_full_size_keeps_the_lazy_images_size(tmp_path):
    path = _png(tmp_path / "a.png", 300, 200)
    entry = loader.decode(path)
    assert (entry.pixels, entry.full) == (300, 300)
    assert entry.nbytes == 300 * 200 * 4
    size = entry.image.size()
    assert (size.width, size.height) == (300, 200)


def test_decode_to_a_side_shrinks_the_bitmap_not_the_size(tmp_path):
    path = _png(tmp_path / "a.png", 400, 200)
    entry = loader.decode(path, 100)
    assert (entry.pixels, entry.full) == (100, 400)
    assert entry.nbytes == 100 * 50 * 4
    # The fit / src math keeps working in the source's own units.
    size = entry.image.size()
    assert (size.width, size.height) == (400, 200)


def test_decode_never_enlarges(tmp_path):
    path = _png(tmp_path / "a.png", 40, 20)
    assert loader.decode(path, 4000).pixels == 40


def test_decode_applies_exif_orientation(tmp_path):
    from AppKit import (NSBitmapImageRep, NSDeviceRGBColorSpace,
                        NSGraphicsContext, NSMakeRect)

    Image = pytest.importorskip("PIL.Image")
    # Stored landscape, red left / blue right, orientation 6 (turn clockwise to
    # show): shown portrait, red on top.
    stored = Image.new("RGB", (64, 32), (0, 0, 255))
    stored.paste((255, 0, 0), (0, 0, 32, 32))
    exif = Image.Exif()
    exif[0x0112] = 6
    path = str(tmp_path / "o6.jpg")
    stored.save(path, "JPEG", quality=95, exif=exif)

    entry = loader.decode(path, 32)
    size = entry.image.size()
    assert (size.width, size.height) == (32, 64)
    assert entry.pixels == 32

    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, 16, 32, 8, 4, True, False, NSDeviceRGBColorSpace, 0, 0)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(
        NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep))
    entry.image.drawInRect_(NSMakeRect(0, 0, 16, 32))
    NSGraphicsContext.restoreGraphicsState()
    top, bottom = rep.colorAtX_y_(8, 4), rep.colorAtX_y_(8, 28)
    assert top.redComponent() > 0.8 and top.blueComponent() < 0.2
    assert bottom.blueComponent() > 0.8 and bottom.redComponent() < 0.2


def test_decode_of_a_non_image_is_none(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("not a picture")
    assert loader.decode(str(path)) is None
    assert loader.decode(str(tmp_path / "missing.png")) is None


def test_decode_of_what_only_nsimage_reads_stays_lazy(tmp_path):
    path = tmp_path / "a.svg"
    path.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18">'
                    '<rect width="18" height="18" fill="red"/></svg>')
    entry = loader.decode(str(path))
    if entry is None:
        pytest.skip("this macOS's NSImage does not read SVG")
    assert (entry.pixels, entry.full) == (0, 0)
    assert not loader.wants_redecode(entry, 1000)


# --- sizing -------------------------------------------------------------------

def test_needed_side_follows_dest_scale_and_crop():
    # 6000x4000 photo contained in 1500x1000 points on a 2x display.
    assert loader.needed_side(1500, 1000, 6000, 4000, 6000, 4000, 2.0) == 3000
    # Zoomed to its top-left quarter: four times the pixels per side... capped
    # later by the caller at full size.
    assert loader.needed_side(1500, 1000, 1500, 1000, 6000, 4000, 2.0) == 12000
    assert loader.needed_side(0, 0, 0, 0, 10, 10, 1.0) == 0


def test_wants_redecode_grows_at_once_and_shrinks_with_slack():
    entry = loader.Decoded(image=None, pixels=2000, full=6000)
    assert not loader.wants_redecode(entry, 2000)
    assert loader.wants_redecode(entry, 2001)
    assert not loader.wants_redecode(entry, 1500)
    assert loader.wants_redecode(entry, 1000)
    full = loader.Decoded(image=None, pixels=6000, full=6000)
    assert not loader.wants_redecode(full, 12000)   # nothing more to give


# --- the worker ---------------------------------------------------------------

class _Inbox:
    """Stands in for AppHelper.callAfter: collects what the worker posts, and
    the test runs it on its own thread, as the UI thread would."""

    def __init__(self):
        self.items = []
        self.cond = threading.Condition()

    def __call__(self, fn, *args):
        with self.cond:
            self.items.append((fn, args))
            self.cond.notify_all()

    def run(self, count, timeout=10.0):
        deadline = time.monotonic() + timeout
        with self.cond:
            while len(self.items) < count:
                left = deadline - time.monotonic()
                assert left > 0, "worker did not report back"
                self.cond.wait(left)
            items, self.items = self.items, []
        for fn, args in items:
            fn(*args)


def test_loader_serves_newest_first_and_drops_the_oldest():
    gate = threading.Event()
    order = []

    def decoder(path, side):
        gate.wait(5)
        order.append(path)
        return path

    done = []
    inbox = _Inbox()
    worker = loader.ImageLoader(lambda k, r: done.append((k, r)), inbox,
                                decoder=decoder, limit=2)
    worker.submit("a", "a", 0)      # picked up at once, blocks on the gate
    time.sleep(0.05)
    for name in "bcd":
        worker.submit(name, name, 0)
    gate.set()
    inbox.run(4)
    worker.close()
    assert ("b", loader.DROPPED) in done
    assert order == ["a", "d", "c"]


def test_loader_reports_a_failing_decoder_as_none():
    def decoder(path, side):
        raise OSError("boom")

    done = []
    inbox = _Inbox()
    worker = loader.ImageLoader(lambda k, r: done.append((k, r)), inbox,
                                decoder=decoder)
    worker.submit("k", "p", 0)
    inbox.run(1)
    worker.close()
    assert done == [("k", None)]


# --- the backend --------------------------------------------------------------

def _backend_with_inbox():
    backend = MacOSBackend()
    inbox = _Inbox()
    backend._image_loader = loader.ImageLoader(backend._image_decoded, inbox)
    return backend, inbox


def test_small_picture_is_decoded_on_the_spot(tmp_path):
    backend, inbox = _backend_with_inbox()
    path = _png(tmp_path / "icon.png", 32, 32)
    entry = backend._load_image(path, 0, 0, {"w": 4, "h": 2})
    assert entry is not None and entry.pixels == 32
    assert not backend._image_jobs


def test_large_picture_is_decoded_on_the_worker_at_the_shown_size(tmp_path):
    backend, inbox = _backend_with_inbox()
    path = _png(tmp_path / "photo.png", 2400, 1200)
    hints = {"w": 30, "h": 30, "fit": "contain"}
    assert backend._load_image(path, 0, 0, hints) is None
    assert source_key(path) in backend._image_jobs
    # Asked again while decoding: still nothing, and no second job.
    assert backend._load_image(path, 0, 0, hints) is None
    inbox.run(1)
    assert not backend._image_jobs
    entry = backend._load_image(path, 0, 0, hints)
    target = backend._unit_rect(0, 0, 30, 30)
    # Contained: the long side spans the box's width, at this test's scale 1.
    assert entry.pixels == round(target.size.width)
    assert entry.full == 2400
    backend._image_loader.close()


def test_a_failed_worker_decode_is_remembered(tmp_path):
    backend, inbox = _backend_with_inbox()
    path = _png(tmp_path / "photo.png", 2400, 1200)
    backend._image_decoded(source_key(path), None)
    assert backend._image_cache.get(source_key(path)) is None
    assert backend._image_cache.get(source_key(path)) is not MISS
    backend._image_loader.close()


def test_zooming_in_asks_for_more_pixels_and_draws_the_old_meanwhile(tmp_path):
    from AppKit import NSBitmapImageRep, NSDeviceRGBColorSpace, NSGraphicsContext

    backend, inbox = _backend_with_inbox()
    path = _png(tmp_path / "photo.png", 2400, 1200)
    hints = {"w": 30, "h": 30, "fit": "contain"}
    backend._load_image(path, 0, 0, hints)
    inbox.run(1)
    first = backend._load_image(path, 0, 0, hints)

    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, 400, 400, 8, 4, True, False, NSDeviceRGBColorSpace, 0, 0)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(
        NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep))
    try:
        backend._render_image(0, 0, path, {**hints, "src": (0.0, 0.0, 0.25, 0.25)})
    finally:
        NSGraphicsContext.restoreGraphicsState()
    assert source_key(path) in backend._image_jobs
    inbox.run(1)
    zoomed = backend._load_image(path, 0, 0, hints)
    assert zoomed.pixels == min(4 * first.pixels, 2400)
    backend._image_loader.close()
