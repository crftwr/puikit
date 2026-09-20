"""Image natural-size reader and object-fit geometry, shared by every backend.

A file's pixel dimensions and the way an image fits a target rect are
backend-independent facts, so the header parse and the fit math live here
once. Backends render through their own primitives; this module only answers
"how big is it" (for aspect-ratio layout) and "where does it go" (for the
contain/cover fit, computed in the target's own coordinate space).

Fit modes (see ``ImageView``):

- ``FILL``    — stretch to the target rect, ignoring aspect ratio.
- ``CONTAIN`` — largest aspect-preserving box inside the rect (background
                bands may show around it).
- ``COVER``   — cover the rect with aspect preserved (the image is cropped).
- ``WIDTH``   — the target width is given; the height follows the aspect ratio.
- ``HEIGHT``  — the target height is given; the width follows the aspect ratio.

``WIDTH`` / ``HEIGHT`` size the widget itself (resolved in ``measure``); at
draw time the rect is already aspect-correct, so they render as ``FILL``.

It also holds :class:`RasterImage`, the other thing an image source can be: a
block of pixels the caller decoded itself, rather than a file for the backend to
open. A path is still the fast lane — every backend has a decoder behind it and
reads the file directly — but it is only ever a *file*, and an application that
produced its pixels some other way (a format the backend's decoder does not
know, a frame it rendered, bytes that never touched a disk) had no way to hand
them over except to write a PNG back out and pass its path. That round trip
costs an encode and a decode to move data the backend was about to be given.
"""

from __future__ import annotations

import os
import struct
from functools import lru_cache
from typing import Any

FILL = "fill"
CONTAIN = "contain"
COVER = "cover"
WIDTH = "width"
HEIGHT = "height"

#: Fits that size the widget itself (the dependent axis is intrinsic).
ASPECT_FITS = frozenset({WIDTH, HEIGHT})
#: All recognized fit modes.
FITS = frozenset({FILL, CONTAIN, COVER, WIDTH, HEIGHT})

#: Bytes per pixel of a :class:`RasterImage` buffer — RGBA8, four channels of
#: eight bits. Named so a caller sizing a buffer does not have to spell ``4``.
RGBA_BPP = 4

#: Hands out one identity per :class:`RasterImage` ever built. A counter rather
#: than ``id(self)``: CPython reuses the address of a collected object, and a
#: cache keyed on a dead raster's identity would then serve its pixels for the
#: next one to land there.
_next_identity = 0


class RasterImage:
    """Decoded pixels a backend can draw without opening a file — the second
    thing every ``draw_image`` accepts, beside a path.

    The buffer is **straight-alpha RGBA8**, tightly packed, top-left origin, with
    a stride of ``width * 4``. Straight rather than premultiplied because that is
    what a decoder hands over: Pillow's ``tobytes()`` on an ``RGBA`` image is
    already exactly this, and so is every other library's. One backend (Windows)
    needs premultiplied BGRA and pays a swizzle for it, which is the cheaper
    trade than making every producer premultiply for a backend it cannot see.

    **Identity, not content, is what a cache keys on.** ``cache_key`` is
    ``(identity, revision)``: the identity is this object's own, handed out once
    at construction, and the revision counts the times it has been written to.
    Hashing the pixels would be the obvious alternative and is the wrong one — an
    application that paints into its buffer changes it every frame, and hashing
    megabytes per frame costs more than the work the cache exists to avoid. So a
    raster says when it changed and is believed; see
    ``puikit.backends._terminal_graphics.source_key``, which is where every cache
    in the toolkit asks.

    Writing to ``data`` in place without saying so is the one way to get a stale
    picture. :meth:`update` is how a producer says so, and a producer that
    mutates the bytes it passed in must call :meth:`touch`.
    """

    __slots__ = ("_width", "_height", "_data", "_identity", "_revision")

    def __init__(self, width: int, height: int, data: bytes | bytearray | memoryview):
        global _next_identity
        _next_identity += 1
        self._identity = _next_identity
        self._revision = 0
        self._width = self._height = 0
        self._data: bytes | bytearray | memoryview = b""
        self._assign(width, height, data)

    def _assign(self, width: int, height: int, data) -> None:
        width, height = int(width), int(height)
        if width <= 0 or height <= 0:
            raise ValueError(f"RasterImage size must be positive, got {width}x{height}")
        expected = width * height * RGBA_BPP
        if len(data) != expected:
            raise ValueError(
                f"RasterImage({width}x{height}) needs {expected} bytes of RGBA8 "
                f"(stride {width * RGBA_BPP}), got {len(data)}")
        self._width, self._height, self._data = width, height, data

    # --- the pixels ---------------------------------------------------------

    @property
    def width(self) -> int:
        return self._width

    @property
    def height(self) -> int:
        return self._height

    @property
    def size(self) -> tuple[int, int]:
        """``(width, height)`` in pixels — what ``Backend.image_size`` answers
        for a raster, with no file to parse."""
        return (self._width, self._height)

    @property
    def data(self):
        """The RGBA8 buffer. Writing through it is allowed (that is the point of
        accepting a ``bytearray``), but :meth:`touch` has to follow, or every
        cache keyed on this raster keeps serving the pixels it saw last."""
        return self._data

    @property
    def nbytes(self) -> int:
        """Size of the buffer, for a cache spending a byte budget."""
        return self._width * self._height * RGBA_BPP

    # --- identity -----------------------------------------------------------

    @property
    def cache_key(self) -> tuple[int, int]:
        """``(identity, revision)`` — the shape ``source_key`` looks for when it
        asks a source to name itself."""
        return (self._identity, self._revision)

    def touch(self) -> None:
        """Declare the pixels changed. A producer that wrote through ``data``
        calls this; :meth:`update` does it for you."""
        self._revision += 1

    def update(self, width: int, height: int, data) -> None:
        """Replace the pixels, and the size with them, bumping the revision so
        every cache re-reads. The identity is unchanged — this is still the same
        raster, holding a later picture."""
        self._assign(width, height, data)
        self._revision += 1

    # --- construction -------------------------------------------------------

    @classmethod
    def from_pillow(cls, image) -> "RasterImage":
        """A raster from a ``PIL.Image``, converting to ``RGBA`` where it is not
        already. The common way to build one, since a decoder for a format no
        backend reads is nearly always a Pillow plugin.

        Pillow is imported by the caller, never by PuiKit: whatever produced the
        image already has it."""
        if image.mode != "RGBA":
            image = image.convert("RGBA")
        return cls(image.width, image.height, image.tobytes())

    def to_pillow(self):
        """A ``PIL.Image`` over these pixels — how the terminal backends get a
        raster into their encoders. Requires Pillow, and raises ``ImportError``
        without it, like every other Pillow-dependent path in the toolkit."""
        from PIL import Image

        return Image.frombuffer(
            "RGBA", (self._width, self._height), bytes(self._data), "raw", "RGBA", 0, 1)

    def __repr__(self) -> str:
        return (f"RasterImage({self._width}x{self._height}, "
                f"identity={self._identity}, revision={self._revision})")


def source_key(source: Any) -> tuple:
    """A cache identity for an image source: ``(kind, identity..., revision)``.

    Backends cache expensive per-image work — a decoded, scaled, quantized
    picture, or a fully encoded payload — and must be able to tell when the
    pixels behind a source have changed.

    **A path alone is not that identity.** It names a location, and the same
    location holds different pixels after a rebuild, a thumbnail refresh, or a
    file replaced mid-copy; a path-keyed cache then serves the old picture until
    it happens to be evicted. So a file source is identified by its path plus the
    modification time and size — one ``stat`` per emission, nothing beside the
    decode it protects.

    That is the same bound every mtime-based invalidation lives with: two writes
    close enough to land on one filesystem timestamp, producing a file of the
    same size, are indistinguishable. Content hashing would close it and costs
    more than the decode it guards, so it is not worth paying here; a source that
    needs exactness names its own revision instead, below.

    The tuple shape exists so a **raster** source — pixel data handed straight to
    the backend, as a photo editor would, rather than a file on disk — slots in
    without any cache having to change. Content hashing is the obvious identity
    and the wrong one: an editor mutates its buffer between frames, and hashing
    megabytes per frame costs more than the encode being avoided. Such a source
    instead names itself, exposing a ``cache_key`` of ``(identity, revision)``
    whose revision it bumps when written to. Every cache keyed through here then
    invalidates correctly the moment the pixels change, and not before.
    """
    own = getattr(source, "cache_key", None)
    if own is not None:
        return ("raster", *tuple(own))
    try:
        stat = os.stat(source)
    except (OSError, TypeError, ValueError):
        # Unreadable or not a filesystem path: fall back to the bare name. The
        # picture cannot be loaded either, so nothing is cached against it.
        return ("path", source, None, None)
    return ("path", source, stat.st_mtime_ns, stat.st_size)


def is_raster(source) -> bool:
    """Whether ``source`` is decoded pixels rather than a path.

    Tests for the *shape* (a ``cache_key`` and a ``size``) rather than the class,
    so an application with its own pixel buffer — a photo editor's canvas, a
    video frame — can satisfy the contract without inheriting from
    :class:`RasterImage`. That is the same latitude ``source_key`` already
    extends: it asks a source to name itself and does not ask what it is."""
    return hasattr(source, "cache_key") and hasattr(source, "size")


@lru_cache(maxsize=256)
def image_size(path: str) -> tuple[int, int] | None:
    """Natural ``(width, height)`` of the image in pixels from its file header,
    or ``None`` if the format is unknown or the file is unreadable.

    A dependency-free header parse (PNG / GIF / BMP / JPEG), so the aspect
    ratio is available on every backend — TUI included, where it shapes the
    placeholder footprint and the layout the same way it does on GUI.

    Path-only, and memoized on the path, which is why a :class:`RasterImage`
    never reaches here: it already knows its size (``RasterImage.size``), and an
    LRU keyed on an object whose pixels change would hold the first answer
    forever. ``Backend.image_size`` is the one that takes either."""
    try:
        with open(path, "rb") as f:
            head = f.read(26)
    except OSError:
        return None
    if head[:8] == b"\x89PNG\r\n\x1a\n":  # PNG: IHDR width/height
        w, h = struct.unpack(">II", head[16:24])
        return (w, h)
    if head[:6] in (b"GIF87a", b"GIF89a"):  # GIF: logical screen descriptor
        w, h = struct.unpack("<HH", head[6:10])
        return (w, h)
    if head[:2] == b"BM":  # BMP: BITMAPINFOHEADER width/height (height may be < 0)
        w, h = struct.unpack("<ii", head[18:26])
        return (abs(w), abs(h))
    if head[:2] == b"\xff\xd8":  # JPEG: scan for a start-of-frame marker
        return _jpeg_size(path)
    return None


def _jpeg_size(path: str) -> tuple[int, int] | None:
    try:
        with open(path, "rb") as f:
            f.read(2)  # SOI
            while True:
                marker = f.read(2)
                if len(marker) < 2 or marker[0] != 0xFF:
                    return None
                # SOF0..SOF15 carry the frame size; DHT/JPG/DAC do not.
                if 0xC0 <= marker[1] <= 0xCF and marker[1] not in (0xC4, 0xC8, 0xCC):
                    f.read(3)  # segment length (2) + sample precision (1)
                    h, w = struct.unpack(">HH", f.read(4))
                    return (w, h)
                (seg_len,) = struct.unpack(">H", f.read(2))
                f.seek(seg_len - 2, 1)
    except (OSError, struct.error):
        return None


def aspect_extent(
    driver: float, driver_is_width: bool, iw: int, ih: int, base_w: int, base_h: int
) -> float:
    """The dependent extent, in base units, that locks the on-screen aspect
    ratio to the image's. ``driver`` is the given extent on the other axis (in
    base units); ``base_w``/``base_h`` are the pixel size of one base unit, so
    a non-square base unit (GUI) keeps the *pixel* aspect ratio correct, not
    the base-unit one. Returns ``driver`` unchanged for a degenerate image."""
    if iw <= 0 or ih <= 0:
        return driver
    if driver_is_width:  # width given, solve height
        return driver * base_w * ih / (base_h * iw)
    return driver * base_h * iw / (base_w * ih)  # height given, solve width


def contain_box(
    tw: float, th: float, iw: float, ih: float
) -> tuple[float, float, float, float]:
    """The largest aspect-preserving box inside ``tw x th``, centered, as
    ``(offset_x, offset_y, w, h)`` in the target's own units. Ratio-only, so it
    works in pixels (GUI draw) or base units (TUI placeholder) alike."""
    if iw <= 0 or ih <= 0 or tw <= 0 or th <= 0:
        return (0.0, 0.0, tw, th)
    scale = min(tw / iw, th / ih)
    w, h = iw * scale, ih * scale
    return ((tw - w) / 2.0, (th - h) / 2.0, w, h)


def cover_source(
    iw: float, ih: float, tw: float, th: float
) -> tuple[float, float, float, float]:
    """The centered source crop of the image (in image pixels) whose aspect
    matches the target, so drawing it into the full target rect covers it
    without distortion. Returns ``(x, y, w, h)``."""
    if iw <= 0 or ih <= 0 or tw <= 0 or th <= 0:
        return (0.0, 0.0, float(iw), float(ih))
    scale = max(tw / iw, th / ih)
    sw, sh = tw / scale, th / scale
    return ((iw - sw) / 2.0, (ih - sh) / 2.0, sw, sh)


def zoom_window(
    zoom: float, cx: float = 0.5, cy: float = 0.5
) -> tuple[float, float, float, float]:
    """The source window a pan/zoom viewer is looking at, as **normalized**
    ``(x, y, w, h)`` fractions of the image (each ``0..1``, top-left origin) —
    the ``src`` hint ``draw_image`` accepts.

    Normalized on purpose: the crop must be independent of the units a backend
    measures the image in, which differ (a macOS ``NSImage`` reports *points*,
    derived from the file's DPI, while Direct2D and Pillow use *pixels*). Each
    backend multiplies these fractions by its own image size, so a Retina image
    — whose point size is half its pixel size — crops correctly everywhere.

    ``zoom`` is the magnification: ``1.0`` shows the whole image, ``2.0`` shows
    half of each axis (twice as big on screen). ``cx``/``cy`` are the pan center
    in normalized image coordinates (``0.5`` = centered).

    The window is square in *fraction* space (``w == h == 1/zoom``), so scaling
    both axes by the same factor keeps the image's own aspect ratio at every
    zoom: paired with ``CONTAIN`` (whose destination box is aspect-locked to the
    image) the view is undistorted throughout, and magnification changes only how
    much of the source is sampled.

    Panning past an edge *slides* the window back inside the image rather than
    shrinking it, so the zoom level survives a clamp. A ``zoom`` at or below 1
    simply pins to the whole image."""
    zoom = max(1e-6, zoom)
    w = h = min(1.0, 1.0 / zoom)
    # Center on the requested point, then slide (not shrink) back into bounds so
    # the visible extent — and therefore the zoom — is preserved at the edges.
    x = min(max(cx - w / 2.0, 0.0), 1.0 - w)
    y = min(max(cy - h / 2.0, 0.0), 1.0 - h)
    return (x, y, w, h)
