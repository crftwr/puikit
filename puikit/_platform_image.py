"""The operating system's own image decoder, without a backend attached.

Every backend already has a decoder, because every backend has to turn a file
into *its* kind of bitmap — an ``NSImage``, an ``ID2D1Bitmap``, a Pillow image
for a terminal encoder. ``Backend.image_formats()`` reports what that decoder
reads, and for a GUI backend that is also the answer to a second, different
question: what can this *machine* decode at all?

On a terminal the two answers come apart. The decoder there is Pillow, because
Pillow is what produces pixels for the sixel and kitty encoders — so a terminal
on macOS reports it cannot read a HEIC, while ImageIO sits in the same process
and reads HEIC perfectly well. Same machine, same file, and the answer depends
on which backend happens to be drawing. That is not a fact about the file.

This module is the second question asked separately: **what can the OS decode,
whichever backend is running?** It is not a Backend, does not know one exists,
and takes no part in drawing. It hands back a :class:`~puikit.image.RasterImage`
and lets whoever asked put the pixels wherever they belong.

``puikit.image.image_size`` is the same shape one step down — a
backend-independent answer about an image file that a backend may override with
something better. This is that, for pixels instead of dimensions.

What it covers
--------------

**8-bit RGB and RGBA only**, which is what photographs are: HEIC, JPEG XL, JPEG,
PNG. A 16-bit or CMYK image comes back ``None`` rather than converted, because
the cheap conversions do not exist — a ``CGBitmapContext`` cannot even represent
straight alpha, and round-tripping through the platform's own re-encoder costs
more than it saves. ``None`` means "ask someone else", which is exactly where the
caller already was.

Per platform:

- **macOS** — ``NSBitmapImageRep``, i.e. ImageIO. Reads HEIC, JPEG XL, camera RAW
  and about eighty other types out of the box. AppKit only; no Quartz import, so
  a terminal session pays one framework rather than two.
- **Windows** — WIC, whose codec set is whatever the machine has installed. The
  same calls the Windows backend already makes, and they need no render target,
  so they work in a terminal session as well as a windowed one.
- **Everything else** — nothing. Linux has no system image decoder to borrow, so
  a Pillow plugin remains the answer there, and this module says so by answering
  with an empty set.
"""

from __future__ import annotations

import sys
import threading
from typing import Any

#: Resolved once: the platform implementation, or ``None`` where there is none.
#: ``False`` means "not looked up yet", so a platform with no decoder is not
#: re-probed on every call.
_impl: Any = False

#: Memoized extension list. Enumerating it imports a platform framework, so it
#: happens at the first question and not before.
_extensions_cache: frozenset[str] | None = None


def _implementation():
    global _impl
    if _impl is False:
        if sys.platform == "darwin":
            _impl = _MacOS()
        elif sys.platform == "win32":
            _impl = _Windows()
        else:
            _impl = None
    return _impl


def extensions() -> frozenset[str]:
    """Lowercase, dotted file extensions the OS decoder reads, or an empty set
    where there is no OS decoder to ask.

    Empty is a real answer here, not a failure: on Linux it is simply true."""
    global _extensions_cache
    if _extensions_cache is None:
        impl = _implementation()
        try:
            _extensions_cache = frozenset(impl.extensions()) if impl else frozenset()
        except Exception:
            _extensions_cache = frozenset()
    return _extensions_cache


def decode(path: Any):
    """``path`` as a :class:`~puikit.image.RasterImage`, or ``None``.

    ``None`` covers every way this can decline — no OS decoder, a format it does
    not read, a file it cannot open, and a pixel layout outside the 8-bit RGB /
    RGBA this handles. They are one answer because the caller does the same thing
    with all of them."""
    impl = _implementation()
    if impl is None:
        return None
    try:
        return impl.decode(str(path))
    except Exception:
        return None


def _rgba_raster(width: int, height: int, data: bytearray):
    from .image import RasterImage

    return RasterImage(width, height, bytes(data))


def _repack(data, height: int, stride: int, row_bytes: int) -> bytearray:
    """Drop the padding a decoder may leave at the end of each row.

    Both platforms report a stride, and both usually hand back tightly packed
    rows — but "usually" is not a contract, and a row-padded buffer read as if it
    were packed shears the picture diagonally. One slice per row, so the cost is
    the height, not the pixel count."""
    if stride == row_bytes:
        return bytearray(data)
    out = bytearray(row_bytes * height)
    for y in range(height):
        start = y * stride
        out[y * row_bytes:(y + 1) * row_bytes] = data[start:start + row_bytes]
    return out


class _MacOS:
    """ImageIO, reached through ``NSBitmapImageRep``.

    AppKit rather than Quartz on purpose. ``CGImageSourceCreateImageAtIndex``
    plus a ``CGBitmapContext`` is the more obvious route and cannot express what
    is needed: a bitmap context refuses ``kCGImageAlphaLast``, so the only
    formats it will write are premultiplied — and premultiplying a decoded
    picture destroys it, crushing an ``(255, 255, 255, 8)`` pixel to
    ``(8, 8, 8, 8)``. ``NSBitmapImageRep`` holds straight alpha natively
    (``NSBitmapFormatAlphaNonpremultiplied``), which is what a decoder produced
    and what :class:`~puikit.image.RasterImage` promises.
    """

    #: ``NSBitmapFormatAlphaNonpremultiplied``. Spelled out because the name is
    #: missing from some PyObjC builds; the value is fixed by the framework.
    NON_PREMULTIPLIED = 2

    #: The colour spaces whose samples are red, green and blue in that order.
    #: Checked because the shape of the buffer does not say what is in it: a CMYK
    #: TIFF is also 8 bits per sample, also four samples, also 32 bits per pixel,
    #: and reading one as RGBX yields a picture in the wrong colours with no
    #: error anywhere. Every RGB file measured — plain, ICC-tagged, HEIC —
    #: reports ``NSCalibratedRGBColorSpace``; CMYK reports its own, and greyscale
    #: reports a white space (and is rejected by the 32-bit test in any case).
    RGB_SPACES = frozenset({"NSCalibratedRGBColorSpace", "NSDeviceRGBColorSpace"})

    def extensions(self):
        from AppKit import NSImage

        # imageFileTypes() mixes modern filename extensions with Classic-era
        # four-character OSType codes ("'jpeg'", "'bmp '", quotes and padding
        # included). Only the first kind means anything to a caller holding a
        # filename, and they are exactly the alphanumeric entries.
        return (f".{str(entry).lower()}" for entry in (NSImage.imageFileTypes() or [])
                if entry and str(entry).isalnum())

    def decode(self, path: str):
        from AppKit import NSBitmapImageRep

        rep = NSBitmapImageRep.imageRepWithContentsOfFile_(path)
        if rep is None:
            return None
        width, height = int(rep.pixelsWide()), int(rep.pixelsHigh())
        if width <= 0 or height <= 0 or rep.isPlanar():
            return None
        if rep.bitsPerSample() != 8 or rep.bitsPerPixel() != 32:
            return None  # 16-bit or exotic; see the module docstring
        if str(rep.colorSpaceName()) not in self.RGB_SPACES:
            return None  # CMYK and friends
        samples = rep.samplesPerPixel()
        has_alpha = bool(rep.hasAlpha())
        if samples not in (3, 4):
            return None
        if has_alpha and not (rep.bitmapFormat() & self.NON_PREMULTIPLIED):
            # Premultiplied, and un-premultiplying in Python is not worth it.
            return None
        data = _repack(rep.bitmapData(), height, int(rep.bytesPerRow()), width * 4)
        if not has_alpha:
            # 32 bits with three samples is RGBX: the fourth byte is padding the
            # decoder never defined, so it has to be made opaque rather than
            # trusted. A strided slice assignment, so this is one C-level pass
            # rather than a loop over every pixel.
            data[3::4] = b"\xff" * (width * height)
        return _rgba_raster(width, height, data)


class _Windows:
    """WIC — the same decoder the Windows backend draws through.

    ``wic_load_bitmap_source`` converts to 32bpp BGRA with **straight** alpha
    (the "PBGRA" format name notwithstanding — see that function), which is one
    channel swap away from what a raster wants. The backend's own path premultiplies
    at the very end, for Direct2D's sake; nothing here does, because nothing here
    is drawing.

    None of this needs a render target, which is what makes it usable from a
    terminal session.

    The factory is held **per thread**, because a COM apartment is. An
    application may ask which formats exist from a worker (a directory scan
    deciding what counts as an image) and decode from the thread that draws, and
    handing an object made in one single-threaded apartment to another thread is
    undefined however often it happens to work. A factory is cheap; borrowing one
    across apartments is not."""

    def __init__(self):
        self._local = threading.local()

    def _wic(self):
        from .backends import _win32_native as native

        factory = getattr(self._local, "factory", None)
        if factory is None:
            factory = native.create_wic_factory()
            self._local.factory = factory
        return native, factory

    def extensions(self):
        native, factory = self._wic()
        return native.wic_decoder_extensions(factory)

    def decode(self, path: str):
        native, factory = self._wic()
        source = native.wic_load_bitmap_source(factory, path)
        if source is None:
            return None
        try:
            width, height = native.wic_bitmap_size(source)
            if width <= 0 or height <= 0:
                return None
            raw = native.wic_copy_pixels_bgra(source, width, height)
        finally:
            source.release()
        # The swizzle is its own inverse: B and R trade places either way.
        return _rgba_raster(width, height, bytearray(native.rgba_to_bgra(raw)))
