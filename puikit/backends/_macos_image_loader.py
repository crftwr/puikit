"""Decoding image files for the macOS backend off the UI thread (xefm#732).

``NSImage.initWithContentsOfFile_`` is lazy: it reads the header and leaves the
decode to the first ``drawInRect``, which runs on the main thread. For a 24 MP
photo that first draw cost 120 ms (JPEG), 230 ms (HEIC) and 380 ms (PNG), and a
lazy JPEG decoded the whole file again on *every* later draw. A viewer that
opens a camera photo froze the window for as long as that took.

Two things fix it, and both are needed:

**The decode moves to a worker.** ImageIO's ``CGImageSourceCreateThumbnailAtIndex``
with ``kCGImageSourceShouldCacheImmediately`` returns a fully decoded
``CGImage``, and ImageIO is safe to call from any thread. The backend draws
nothing for that source until the result arrives, then redraws.

**The decode is sized to what is shown.** A decoded full-size bitmap still cost
60 ms on its first draw, because AppKit resamples 24 MP down to the window and
caches the result on the main thread. A bitmap decoded at the displayed pixel
size draws in a few milliseconds. The backend asks for the size it needs, and
asks again when a zoom needs more pixels than it has, or a zoom-out or a smaller
window needs far fewer. The bitmap already held is drawn until the new one
arrives.

What stays as it was: ImageIO keeps the embedded colour profile (Display P3
photos stay P3), applies the EXIF orientation (``WithTransform``), and reads
every format AppKit's own loader reads. A file ImageIO cannot read (SVG) keeps
the old lazy ``NSImage``, decoded on draw, because nothing else can draw it.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from typing import Any, Callable

import objc
from AppKit import NSImage
from Foundation import NSURL

try:
    from Quartz import (
        CGImageGetHeight,
        CGImageGetWidth,
        CGImageSourceCopyPropertiesAtIndex,
        CGImageSourceCreateThumbnailAtIndex,
        CGImageSourceCreateWithURL,
        kCGImageSourceCreateThumbnailFromImageAlways,
        kCGImageSourceCreateThumbnailWithTransform,
        kCGImageSourceShouldCacheImmediately,
        kCGImageSourceThumbnailMaxPixelSize,
    )
    HAVE_IMAGEIO = True
except ImportError:  # pragma: no cover - older/partial PyObjC
    HAVE_IMAGEIO = False

from ..image import upright_size

#: A picture this small (in pixels) is decoded on the UI thread, at full size.
#: It costs a few milliseconds, and drawing it straight away is worth more than
#: a blank frame: icons and inline pictures would otherwise flash in.
SYNC_PIXELS = 1024 * 1024

#: A held bitmap is re-decoded smaller only when it is this many times larger
#: than needed, so a window resized back and forth does not decode on every
#: step. Needing more is re-decoded at once: a blurry picture is the visible
#: failure, a too-sharp one is not.
SHRINK_FACTOR = 1.5

#: Requests queued at once. The newest are served first; the oldest beyond
#: this are dropped, which is what a viewer stepping quickly through a
#: directory needs - only the picture now on screen matters.
QUEUE_LIMIT = 8


@dataclass
class Decoded:
    """A drawable image and what is known about the pixels behind it."""

    #: The ``NSImage`` to draw, sized in the source's own points, so the fit and
    #: ``src`` math sees the same size a lazily loaded file reported.
    image: Any
    #: Long side, in pixels, of the bitmap behind ``image``. 0 for the lazy
    #: fallback, which decodes at whatever size it is drawn.
    pixels: int = 0
    #: Long side of the source at full resolution. 0 when unknown (the
    #: fallback), and then no other size is ever asked for.
    full: int = 0
    #: What the cache weighs the entry at.
    nbytes: int = 0


def pixel_size(path: Any) -> tuple[int, int] | None:
    """The upright ``(width, height)`` of ``path`` in pixels, from ImageIO's
    metadata alone - no frame is decoded - or ``None`` when ImageIO cannot read
    it.

    ``PixelWidth`` / ``PixelHeight`` are the stored dimensions, but ``NSImage``
    draws the file upright, applying its EXIF orientation. A phone photo held
    upright is stored landscape with orientation 6, so the stored size handed a
    portrait picture a landscape box and stretched it; the size is swapped for
    the quarter-turn orientations so it agrees with what is drawn."""
    if not HAVE_IMAGEIO:
        return None
    try:
        source = CGImageSourceCreateWithURL(NSURL.fileURLWithPath_(str(path)), None)
        if source is None:
            return None
        properties = CGImageSourceCopyPropertiesAtIndex(source, 0, None)
        if not properties:
            return None
        width = properties.get("PixelWidth")
        height = properties.get("PixelHeight")
        if not width or not height:
            return None
        return upright_size(int(width), int(height), properties.get("Orientation"))
    except Exception:
        return None


def decode(path: Any, side: int = 0) -> Decoded | None:
    """Decode ``path`` with its long side at most ``side`` pixels (0: full
    size). Safe on any thread. ``None`` when the file is not an image at all;
    a file only ``NSImage`` reads comes back lazy (``pixels == 0``)."""
    with objc.autorelease_pool():
        lazy = NSImage.alloc().initWithContentsOfFile_(str(path))
        if lazy is None:
            return None
        size = lazy.size()
        full = pixel_size(path)
        if full is None:
            weight = int(size.width * size.height * 4)
            return Decoded(lazy, 0, 0, weight)
        full_side = max(full)
        want = full_side if side <= 0 else max(1, min(int(side), full_side))
        try:
            source = CGImageSourceCreateWithURL(NSURL.fileURLWithPath_(str(path)), None)
            cg = CGImageSourceCreateThumbnailAtIndex(source, 0, {
                kCGImageSourceCreateThumbnailFromImageAlways: True,
                kCGImageSourceCreateThumbnailWithTransform: True,
                kCGImageSourceShouldCacheImmediately: True,
                kCGImageSourceThumbnailMaxPixelSize: want,
            }) if source is not None else None
        except Exception:
            cg = None
        if cg is None:
            weight = int(size.width * size.height * 4)
            return Decoded(lazy, 0, 0, weight)
        width, height = CGImageGetWidth(cg), CGImageGetHeight(cg)
        image = NSImage.alloc().initWithCGImage_size_(cg, size)
        return Decoded(image, max(width, height), full_side, width * height * 4)


def needed_side(dest_w: float, dest_h: float, src_w: float, src_h: float,
                image_w: float, image_h: float, scale: float) -> int:
    """Pixels the image's long side needs so that the ``src_w`` x ``src_h``
    part of it (in the image's own units) fills ``dest_w`` x ``dest_h`` points
    at ``scale`` device pixels per point."""
    if src_w <= 0 or src_h <= 0 or image_w <= 0 or image_h <= 0:
        return 0
    per_unit = max(dest_w / src_w, dest_h / src_h) * scale
    return max(1, math.ceil(max(image_w, image_h) * per_unit))


def wants_redecode(entry: Decoded, need: int) -> bool:
    """Whether ``entry`` should be decoded again for a need of ``need`` pixels
    on its long side."""
    if entry.full <= 0 or entry.pixels <= 0 or need <= 0:
        return False
    need = min(need, entry.full)
    if need > entry.pixels:
        return True
    return entry.pixels > need * SHRINK_FACTOR


#: Handed to ``on_done`` for a request that was dropped unserved.
DROPPED = object()


class ImageLoader:
    """One worker thread decoding queued requests, newest first.

    ``post(fn, *args)`` must run ``fn(*args)`` on the UI thread; every
    ``on_done(key, result)`` arrives through it, ``result`` being a
    :class:`Decoded`, ``None`` for a file that cannot be read, or
    :data:`DROPPED`."""

    def __init__(self, on_done: Callable[[Any, Any], None],
                 post: Callable[..., None],
                 decoder: Callable[[Any, int], Any] = decode,
                 limit: int = QUEUE_LIMIT):
        self._on_done = on_done
        self._post = post
        self._decode = decoder
        self._limit = max(1, limit)
        self._jobs: list[tuple[Any, Any, int]] = []
        self._cond = threading.Condition()
        self._thread: threading.Thread | None = None
        self._closed = False

    def submit(self, key: Any, path: Any, side: int) -> None:
        dropped = []
        with self._cond:
            if self._closed:
                return
            self._jobs.append((key, path, side))
            while len(self._jobs) > self._limit:
                dropped.append(self._jobs.pop(0)[0])
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, name="puikit-image-decode", daemon=True)
                self._thread.start()
            self._cond.notify()
        for key in dropped:
            self._post(self._on_done, key, DROPPED)

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._jobs.clear()
            self._cond.notify()

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._jobs and not self._closed:
                    self._cond.wait()
                if self._closed:
                    return
                key, path, side = self._jobs.pop()
            try:
                result = self._decode(path, side)
            except Exception:
                result = None
            self._post(self._on_done, key, result)
