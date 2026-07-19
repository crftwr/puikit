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
"""

from __future__ import annotations

import struct
from functools import lru_cache

FILL = "fill"
CONTAIN = "contain"
COVER = "cover"
WIDTH = "width"
HEIGHT = "height"

#: Fits that size the widget itself (the dependent axis is intrinsic).
ASPECT_FITS = frozenset({WIDTH, HEIGHT})
#: All recognized fit modes.
FITS = frozenset({FILL, CONTAIN, COVER, WIDTH, HEIGHT})


@lru_cache(maxsize=256)
def image_size(path: str) -> tuple[int, int] | None:
    """Natural ``(width, height)`` of the image in pixels from its file header,
    or ``None`` if the format is unknown or the file is unreadable.

    A dependency-free header parse (PNG / GIF / BMP / JPEG), so the aspect
    ratio is available on every backend — TUI included, where it shapes the
    placeholder footprint and the layout the same way it does on GUI."""
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
