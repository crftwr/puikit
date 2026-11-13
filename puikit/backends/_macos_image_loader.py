"""Decoding image files for the macOS backend off the UI thread (xefm#732).

``NSImage.initWithContentsOfFile_`` is lazy: it reads the header and leaves the
decode to the first ``drawInRect``, which runs on the main thread. For a 24 MP
photo that first draw cost 120 ms (JPEG), 230 ms (HEIC) and 380 ms (PNG), and a
lazy JPEG decoded the whole file again on *every* later draw. A viewer that
opens a camera photo froze the window for as long as that took.

Two things fix it, and both are needed:

**The decode moves to a worker.** ImageIO's ``CGImageSourceCreateThumbnailAtIndex``
with ``kCGImageSourceShouldCacheImmediately`` returns a fully decoded
``CGImage``, and ImageIO is safe to call from any thread. Every file goes
there, however small: a 1 MP picture still took 8-17 ms, and a file on a slow
or network disk can take any time at all just to open. A frame waits a few
milliseconds in total for what it asked for, so a small picture still appears
in the frame that asked for it; beyond that the frame draws nothing for the
source, and redraws when the result arrives.

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

from typing import Any

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
from ._image_loader import Decoded

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


def decode(path: Any, side: Any = 0) -> Decoded | None:
    """Decode ``path`` with its long side at most ``side`` pixels (0: full
    size). ``side`` may instead be a function of the file's upright pixel
    ``(width, height)``, for a caller that needs the aspect to know the size
    and should not read the file itself to learn it. Safe on any thread.
    ``None`` when the file is not an image at all; a file only ``NSImage``
    reads comes back lazy (``pixels == 0``)."""
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
        if callable(side):
            side = side(*full)
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
