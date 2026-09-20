"""Inline-image protocols for terminal emulators.

A character grid has no pixels, so the curses backend normally reports
``images=False`` and the Panel substitutes an alt glyph. Several emulators do
accept real pixel data out-of-band, though, through an escape sequence the
grid never sees: this module detects which one (if any) is available and
encodes an image for it, letting :class:`CursesBackend` flip ``images`` on and
draw genuine pictures in a terminal.

Three protocols, in preference order:

- **kitty** (``kitty``, ``ghostty``, ``WezTerm``, ``konsole``) — transmits PNG
  bytes, places them in a cell box, and can *delete* placements by id. The
  richest of the three, and the only one with real erase semantics.
- **iTerm2** (``iTerm.app``, ``WezTerm``, ``mintty``) — an OSC 1337 payload
  carrying an image file verbatim. No delete verb; a placement is cleared by
  overwriting the cells it covers.
- **sixel** (``xterm -ti vt340``, ``foot``, ``contour``, ``mlterm``) — the
  oldest and most widely implemented. Six vertical pixels per band per byte,
  from a quantized palette (encoded here in :func:`_sixel`).

Detection is deliberately **environment-only**. The alternative — a Device
Attributes query (``\\x1b[c``) — means writing to the tty and blocking on a
reply that a non-supporting emulator never sends, which risks a startup hang
inside curses' raw mode for a cosmetic capability. Env vars are unambiguous
for every emulator that implements these protocols, so the trade is worth it;
``PUIKIT_TERM_GRAPHICS`` overrides the guess either way (a protocol name, or
``none`` to force the alt-glyph fallback).

Pillow is an **optional** dependency. It is what crops (the pan/zoom ``src``
window), scales to the target's pixel box, and re-encodes to the protocol's
wire format — so without it this module reports no protocol at all and the
backend falls back to the alt glyph rather than rendering something wrong.
"""

from __future__ import annotations

import base64
import os
import re
import sys
from typing import Any

KITTY = "kitty"
ITERM2 = "iterm2"
SIXEL = "sixel"

#: When ``PUIKIT_TERM_GRAPHICS_DEBUG`` names a file, the inline-image path appends
#: a trace line per step to it (detection, each placement, each emission). Off by
#: default and zero-cost; a diagnostic hook for "images don't show" reports.
_DEBUG_PATH = os.environ.get("PUIKIT_TERM_GRAPHICS_DEBUG")


def debug(message: str) -> None:
    """Append ``message`` to the debug trace file if ``PUIKIT_TERM_GRAPHICS_DEBUG``
    is set, else do nothing. Never raises — a diagnostic must not break rendering."""
    if not _DEBUG_PATH:
        return
    try:
        with open(_DEBUG_PATH, "a") as handle:
            handle.write(message + "\n")
    except OSError:
        pass

#: Protocols in the order they are preferred when an emulator supports several
#: (WezTerm implements all three; kitty's delete verb makes it the best fit).
PROTOCOLS = (KITTY, ITERM2, SIXEL)

#: kitty caps an escape sequence's payload at 4096 base64 bytes per chunk.
_KITTY_CHUNK = 4096

#: Emulators identified by TERM_PROGRAM, mapped to their best protocol.
_TERM_PROGRAM = {
    "iterm.app": ITERM2,
    "wezterm": KITTY,
    "ghostty": KITTY,
    "mintty": ITERM2,
    "contour": SIXEL,
}

#: Substrings of TERM that imply a protocol (checked after the env vars above).
_TERM_HINTS = (
    ("xterm-kitty", KITTY),
    ("ghostty", KITTY),
    ("foot", SIXEL),
    ("contour", SIXEL),
    ("mlterm", SIXEL),
    ("sixel", SIXEL),
)


def have_pillow() -> bool:
    """True when Pillow is importable. Every protocol needs it to crop/scale/
    re-encode, so this gates the whole feature."""
    try:
        import PIL.Image  # noqa: F401
    except ImportError:
        return False
    return True


def detect_protocol(env: dict[str, str] | None = None) -> str | None:
    """The inline-image protocol this terminal supports, or ``None``.

    ``env`` defaults to ``os.environ`` and is injectable for tests. Returns
    ``None`` whenever Pillow is missing, the override says ``none``, or no
    emulator signature matches — in each case the backend keeps ``images``
    off and the Panel's alt-glyph fallback stands in."""
    env = os.environ if env is None else env
    override = (env.get("PUIKIT_TERM_GRAPHICS") or "").strip().lower()
    if override in ("none", "off", "0"):
        return None
    if override in PROTOCOLS:
        return override if have_pillow() else None
    if not have_pillow():
        return None
    # KITTY_WINDOW_ID is set by kitty itself; konsole advertises its version.
    if env.get("KITTY_WINDOW_ID") or env.get("KONSOLE_VERSION"):
        return KITTY
    # Windows Terminal sets WT_SESSION and has drawn sixel since 1.22. It is the
    # only signature Windows offers — there is no TERM_PROGRAM and TERM is
    # whatever the shell happens to set — so it is checked before those. This
    # only reports what the emulator can decode; whether the escapes actually
    # reach the screen is the backend's problem, and under curses they do not,
    # because PDCurses is displaying a different screen buffer than the one the
    # image is written to (xefm#306).
    if env.get("WT_SESSION"):
        return SIXEL
    protocol = _TERM_PROGRAM.get((env.get("TERM_PROGRAM") or "").strip().lower())
    if protocol is not None:
        return protocol
    term = (env.get("TERM") or "").lower()
    for needle, found in _TERM_HINTS:
        if needle in term:
            return found
    return None


#: Re-exported from :mod:`puikit.image`, where it moved once the GUI backends
#: began keying their own caches through it too — the identity of an image
#: source was never a terminal concern. Kept importable here because that is
#: where every caller in the toolkit already reaches for it.
from ..image import source_key  # noqa: E402,F401  (re-export)
from ._image_cache import MISS, ImageCache  # noqa: E402


def cell_pixels(fd: int | None = None) -> tuple[float, float] | None:
    """Pixel size of one character cell as ``(w, h)``, from the kernel's window
    size (``TIOCGWINSZ``'s ``ws_xpixel``/``ws_ypixel``), or ``None`` when the
    terminal does not report it. Needed to turn a cell box into the pixel box
    an image should be scaled to; callers fall back to a nominal cell.

    The division is **fractional**, not integer: ``ws_ypixel`` is rarely an exact
    multiple of the row count, and truncating the remainder (e.g. 16.125 → 16)
    loses a pixel per row — a couple of blank rows across a tall image, and a cell
    aspect that no longer matches the emulator's, so it letterboxes the picture.
    Keeping the fraction makes a scaled image line up with the real cell grid."""
    try:
        import fcntl
        import struct
        import termios

        if fd is None:
            fd = sys.stdout.fileno()
        packed = fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8)
        rows, cols, xpixel, ypixel = struct.unpack("HHHH", packed)
    except Exception:
        return None
    if not (rows and cols and xpixel and ypixel):
        return None
    return (xpixel / cols, ypixel / rows)


def extensions() -> frozenset[str]:
    """The file extensions :func:`render` can open — Pillow's own registry of
    formats it has a *decoder* for, plugins included, so a ``pillow-heif`` or
    ``pillow-jxl-plugin`` an application installed shows up here without this
    module knowing either exists. Empty without Pillow, which is the truth: the
    terminal backends draw nothing at all then.

    This is what a terminal backend answers ``image_formats`` with, and it is
    why the answer is asked of the running system rather than hardcoded."""
    try:
        from PIL import Image
    except ImportError:
        return frozenset()
    # registered_extensions() only populates fully once the plugins have been
    # imported, which init() is what does; without it the answer is whatever
    # happens to have been imported already.
    Image.init()
    return frozenset(
        ext.lower() for ext, fmt in Image.registered_extensions().items()
        if fmt in Image.OPEN
    )


def natural_size(path: Any) -> tuple[int, int] | None:
    """``(width, height)`` from Pillow's header read, or ``None``.

    ``Image.open`` is lazy — it parses the header and stops — so this costs a
    read of the first few hundred bytes, not a decode. It is what a terminal
    backend falls back to when the dependency-free parse in ``puikit.image``
    does not recognize the format: that one knows four, and Pillow knows every
    format the backend can actually draw. The two have to agree, because an
    unknown size is what an application reads as "this cannot be shown"."""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(path) as image:
            return image.size
    except Exception:
        return None


#: Decoded source images, by identity, under a byte budget.
#:
#: :func:`render` is called again whenever the cell box or the source crop
#: changes, which a scroll and a zoom both do constantly, and each call re-read
#: and re-decoded the file. Measured on a 12-megapixel JPEG that was 7.3ms of
#: every step; on an 800x600 screenshot scrolling through a Markdown document,
#: 0.7ms. Neither is the largest part of a step (the resample is — see
#: :data:`_REDUCING_GAP`), but both are pure repetition: the pixels behind a
#: source cannot change without its identity changing, which is exactly what
#: ``source_key`` is for.
#:
#: Budgeted rather than counted, unlike the caches the backends keep, because
#: what is held here is the picture at its *original* size: one 24-megapixel
#: photo is ~72MB where a screenshot is ~1.4MB, so "how many" says nothing about
#: how much. Module-level, and shared: two backends looking at the same file
#: should not decode it twice. :func:`clear_cache` is how a backend says nobody
#: is looking any more.
_DECODED_BUDGET = 64 * 1024 * 1024

_decoded = ImageCache(budget=_DECODED_BUDGET)

#: How far Pillow may reduce an image by simple averaging before the real
#: resampling filter runs (``Image.resize``'s ``reducing_gap``): the pre-pass
#: stops once the image is within this factor of the target, and LANCZOS
#: finishes from there.
#:
#: It only engages on a large *downscale*, which is precisely the slow case —
#: and, conveniently, the one where fidelity matters least: an image fitted to a
#: terminal window is a thumbnail, and zooming in shrinks the reduction factor
#: until the pre-pass stops happening at all. So the approximation is largest
#: where the picture is smallest on screen, and gone where the user is looking
#: closely.
#:
#: Measured, resampling a 4000x2400 crop to 480x320 of an image made of pure
#: high-frequency noise — the worst case there is, since a reducing pre-pass is
#: what aliases detail:
#:
#: =========  ======  ================================
#: gap        time    difference from a plain LANCZOS
#: =========  ======  ================================
#: (none)     27.9ms  —
#: 1.1         4.2ms  mean 2.95/255, max 23
#: **2.0**     7.6ms  mean 0.87/255, max 21
#: 3.0        11.9ms  mean 0.43/255, max 12
#: =========  ======  ================================
#:
#: 2.0 is the knee. A mean of under one part in 255 is below what a terminal can
#: show at all — sixel quantizes to 256 colours on top of it — for 3.7x the
#: speed, and a real photograph is far gentler than the test image.
_REDUCING_GAP = 2.0


def clear_cache() -> None:
    """Drop the decoded-image cache. A backend calls this when it closes: the
    cache outlives any one frame on purpose, but not the session — an embedding
    application that shuts its TUI down should not be left holding the last
    photograph anyone looked at."""
    _decoded.clear()


def _weight(image: Any) -> int:
    """What a decoded image costs, in bytes of pixel data — the currency
    :data:`_decoded` spends. Bands, not a fixed 4, because RGB is three
    quarters of RGBA and both are common."""
    try:
        return image.width * image.height * len(image.getbands())
    except Exception:
        return 0


def _open(source: Any):
    """The decoded source image in ``RGB`` or ``RGBA``, or ``None`` when it
    cannot be read — **cached by identity, and to be treated as read-only.**

    A :class:`~puikit.image.RasterImage` wraps its own buffer with no decode at
    all; a path is opened once and kept (see :data:`_decoded`). A failure is
    kept too, so an unreadable source is attempted once rather than once per
    frame.

    The mode conversion happens *here* rather than after the crop, which is
    where it used to be, so that a paletted GIF or a CMYK TIFF is converted once
    for its whole life instead of once per frame — and so that the crop can be
    folded into the resample, which needs a resamplable mode to begin with. An
    animated GIF or multi-frame TIFF still renders its first frame.
    """
    from ..image import is_raster

    try:
        from PIL import Image
    except ImportError:
        return None
    key = source_key(source)
    cached = _decoded.get(key)
    if cached is not MISS:
        return cached
    image = None
    if is_raster(source):
        try:
            image = source.to_pillow()
        except Exception:
            image = None
    else:
        try:
            image = Image.open(source)
            image.load()
        except Exception:
            image = None
    if image is not None and image.mode not in ("RGB", "RGBA"):
        try:
            image = image.convert(
                "RGBA" if "A" in image.mode or image.mode == "P" else "RGB")
        except Exception:
            image = None
    _decoded.put(key, image, _weight(image))
    return image


def render(
    source: Any,
    px_w: int,
    px_h: int,
    src: tuple[float, float, float, float] | None = None,
) -> tuple[Any, bytes] | None:
    """Crop ``source`` to ``src`` (normalized ``(x, y, w, h)`` fractions of the
    image, top-left origin — the pan/zoom window) and scale it to fit ``px_w`` x
    ``px_h`` preserving aspect ratio.

    ``source`` is a path Pillow opens or a :class:`~puikit.image.RasterImage`
    whose pixels are already decoded. Returns ``(image, png_bytes)`` — the Pillow
    image for the sixel encoder, and PNG bytes for the two transmit-a-file
    protocols — or ``None`` if the source cannot be read. Scaling happens *here*
    rather than in the emulator so the payload stays proportional to the screen
    box, not the source file: a 24-megapixel photo ships as a few hundred KB, and
    zooming re-crops from the original rather than upscaling an already-
    downscaled copy."""
    try:
        from PIL import Image
    except ImportError:
        return None
    image = _open(source)
    if image is None:
        return None
    box = None
    if src is not None:
        # Scale the normalized crop by Pillow's true pixel size (the same
        # fractions the GUI backends scale by their own image size).
        fx, fy, fw, fh = src
        sx, sy = int(round(fx * image.width)), int(round(fy * image.height))
        sw, sh = max(1, int(round(fw * image.width))), max(1, int(round(fh * image.height)))
        crop = (
            max(0, sx), max(0, sy),
            min(image.width, sx + sw), min(image.height, sy + sh),
        )
        if crop[2] > crop[0] and crop[3] > crop[1]:
            box = crop
    region = ((box[2] - box[0], box[3] - box[1]) if box is not None else image.size)
    px_w, px_h = max(1, int(px_w)), max(1, int(px_h))
    if src is not None:
        # A crop was requested: the caller sized it to match this pixel box's
        # aspect, so resize to fill the box *exactly*. Preserving aspect here
        # would leave a within-box letterbox that the terminal top-left-aligns,
        # so the blank space piles up at the bottom instead of splitting evenly
        # around a centered image. Any residual distortion is sub-cell.
        target = (px_w, px_h)
    else:
        # No crop (the size-unknown fallback): show the whole image letterboxed,
        # aspect preserved, downscaling only.
        scale = min(px_w / region[0], px_h / region[1])
        target = ((max(1, int(region[0] * scale)), max(1, int(region[1] * scale)))
                  if scale < 1.0 else region)
    if box is not None:
        # Cropped first, deliberately, rather than handed to ``resize`` as its
        # ``box``. The two are not the same picture: a resample given a box
        # reaches *outside* it for the filter's support, so a crop drawn from
        # the middle of an image carries a fringe of whatever it was next to —
        # visible as contamination at the edges of a zoomed, panned view. A real
        # crop makes those pixels not exist. It costs one copy of the region,
        # which is a fraction of the resample below.
        image = image.crop(box)
    if target != image.size:
        image = image.resize(target, Image.LANCZOS, reducing_gap=_REDUCING_GAP)
    import io

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return image, buffer.getvalue()


def encode(
    protocol: str, image: Any, png: bytes, cols: int, rows: int, image_id: int = 1,
    fill: bool = False
) -> str:
    """The escape sequence that draws ``image`` at the cursor in a ``cols`` x
    ``rows`` cell box. The caller positions the cursor first; every protocol
    here is told (or asked) to leave it where it found it, so the sequence does
    not disturb the grid curses believes it is managing.

    ``fill`` means the image was already resized to exactly this cell box (a
    crop was applied — see :func:`render`), so the protocol should stretch it to
    fill the box rather than re-fit it with its own letterbox: the emulator's own
    cell-size rounding would otherwise reintroduce a bottom band. iTerm2 honours
    this via ``preserveAspectRatio=0``; kitty's ``c``/``r`` placement fills the
    box regardless."""
    if protocol == KITTY:
        return _kitty(png, cols, rows, image_id)
    if protocol == ITERM2:
        return _iterm2(png, cols, rows, fill)
    if protocol == SIXEL:
        return _sixel(image)
    return ""


def clear(protocol: str, image_id: int = 1) -> str:
    """The sequence that removes a previously drawn placement, or ``""`` when
    the protocol has no erase verb. Only kitty does; for the other two the
    caller repaints the covered cells instead, which is what actually clears
    them, so returning empty here is a real answer and not a stub."""
    if protocol == KITTY:
        return f"\x1b_Ga=d,d=i,i={image_id}\x1b\\"
    return ""


def _kitty(png: bytes, cols: int, rows: int, image_id: int) -> str:
    """kitty graphics: transmit-and-display PNG (``f=100``) into a ``c`` x ``r``
    cell box, chunked at 4096 base64 bytes. ``C=1`` keeps the cursor put, and a
    stable ``i`` (image id) lets :func:`clear` delete exactly this placement."""
    payload = base64.b64encode(png).decode("ascii")
    chunks = [payload[i:i + _KITTY_CHUNK] for i in range(0, len(payload), _KITTY_CHUNK)] or [""]
    out = []
    for index, chunk in enumerate(chunks):
        more = 1 if index < len(chunks) - 1 else 0
        if index == 0:
            # a=T transmit+display, f=100 PNG, q=2 suppress both ok and error
            # replies (an unread reply would surface as junk keystrokes).
            head = f"a=T,f=100,i={image_id},c={cols},r={rows},C=1,q=2,m={more}"
        else:
            head = f"m={more}"
        out.append(f"\x1b_G{head};{chunk}\x1b\\")
    return "".join(out)


def _iterm2(png: bytes, cols: int, rows: int, fill: bool = False) -> str:
    """iTerm2 inline image: OSC 1337 with the file inline. ``width``/``height``
    are given in cells (bare integers). ``preserveAspectRatio`` is ``0`` when the
    caller already sized the image to the cell box (``fill``) so iTerm2 stretches
    it to fill exactly — otherwise its own cell rounding re-letterboxes and blank
    rows creep back in — and ``1`` for the whole-image fallback, which should
    letterbox.

    Only the documented ``File`` arguments are sent — ``inline``, ``size``,
    ``width``, ``height``, ``preserveAspectRatio``. iTerm2 has no "keep the
    cursor put" argument (kitty's ``C=1`` has no analog here), so the cursor
    advances after the draw; the caller brackets the emission in DECSC/DECRC so
    that movement can't scroll the alternate screen out from under curses."""
    payload = base64.b64encode(png).decode("ascii")
    args = (
        f"inline=1;size={len(png)};width={cols};height={rows};"
        f"preserveAspectRatio={0 if fill else 1}"
    )
    return f"\x1b]1337;File={args}:{payload}\a"


class SixelSource:
    """A picture prepared for sixel, from which any sub-rectangle can be encoded.

    Everything here is independent of WHICH part is being shown: the decode, the
    scale, the palette, and each band's per-color column bits. Only the final
    run-encoding depends on the rectangle. Splitting the two is what makes
    scrolling affordable — a partially visible image's crop changes on every
    step, so under the naive flow every step re-did the whole pipeline.

    A band is six pixel rows spanning the full width, which is why the two axes
    are not symmetric:

    * **Vertical** movement selects a different SET of bands; each band's content
      is unchanged, so its encoded string is reused outright.
    * **Horizontal** movement changes every band, because each is a full-width
      strip now showing different columns. The prepared bits are still reused —
      only the run-encoding is redone.
    """

    __slots__ = ("width", "height", "palette", "bands", "_full_rows")

    def __init__(self, width: int, height: int, palette: str, bands: list) -> None:
        self.width = width
        self.height = height
        self.palette = palette
        self.bands = bands
        # Encoded strings for whole-width bands, filled in on first use.
        self._full_rows: list[str | None] = [None] * len(bands)

    def encode_rect(self, x0: int, y0: int, x1: int, y1: int) -> str:
        """The sixel for the pixel rectangle ``[x0, x1) x [y0, y1)``.

        The vertical bounds are snapped OUT to band boundaries, because a band is
        the smallest unit sixel can express — the picture can therefore sit up to
        five pixels off where it was asked for, which is under a fifth of a cell
        and not perceptible, and in exchange a scroll step reuses whole bands
        instead of re-encoding the image.
        """
        x0 = max(0, min(x0, self.width))
        x1 = max(x0, min(x1, self.width))
        y0 = max(0, min(y0, self.height))
        y1 = max(y0, min(y1, self.height))
        # Snap the top DOWN to a band boundary — a band is the smallest unit
        # sixel can express — but never emit more bands than the requested
        # rectangle is tall. Rounding the bottom out as well overshoots the cell
        # box the caller reserved, and those few pixels land in the row BELOW it:
        # a row the frame diff has no reason to re-send, since its text did not
        # change, so the spill is never painted over. Scrolling an image upward
        # then leaves a stack of them behind — one stripe per step.
        first = y0 // 6
        last = min(-(-y1 // 6), first + (y1 - y0) // 6)
        full_width = x0 == 0 and x1 == self.width
        out = ["\x1bP0;1;0q", f'"1;1;{x1 - x0};{(last - first) * 6}', self.palette]
        for index in range(first, min(last, len(self.bands))):
            if full_width:
                row = self._full_rows[index]
                if row is None:
                    row = self._full_rows[index] = _encode_band(self.bands[index])
                out.append(row)
            else:
                out.append(_encode_band(self.bands[index], x0, x1))
        out.append("\x1b\\")
        return "".join(out)


def _encode_band(band: dict, x0: int = 0, x1: int | None = None) -> str:
    """One band's color passes. ``$`` returns to column 0 to overlay the next
    color on the same band; ``-`` after the last one advances to the next."""
    out = []
    last = len(band) - 1
    for position, index in enumerate(sorted(band)):
        columns = band[index] if x1 is None else band[index][x0:x1]
        out.append(f"#{index}")
        for match in _RUN_RE.finditer(columns.translate(_SIXEL_BYTES)):
            run = match.group()
            out.append(_sixel_run(chr(run[0]), len(run)))
        out.append("$" if position < last else "-")
    return "".join(out)


def prepare_sixel(image: Any, max_colors: int = 256) -> SixelSource:
    """Quantize and decompose ``image`` into per-band column bits, once."""
    from PIL import Image

    if image.mode == "RGBA":  # sixel has no alpha; composite onto black
        background = Image.new("RGB", image.size, (0, 0, 0))
        background.paste(image, mask=image.split()[-1])
        image = background
    quantized = image.convert("RGB").quantize(colors=max_colors, method=Image.MEDIANCUT)
    palette = quantized.getpalette() or []
    width, height = quantized.size
    defs = []
    for index in sorted({i for _, i in (quantized.getcolors(max_colors) or [])}):
        r, g, b = palette[index * 3:index * 3 + 3] or (0, 0, 0)
        # Sixel color components are percentages (0-100), not 0-255.
        defs.append(f"#{index};2;{r * 100 // 255};{g * 100 // 255};{b * 100 // 255}")
    bands = [band for _top, band in _sixel_bands(quantized.tobytes(), width, height)]
    return SixelSource(width, height, "".join(defs), bands)


def _sixel(image: Any, max_colors: int = 256) -> str:
    """Encode a Pillow image as a sixel string.

    Sixel packs six *vertical* pixels into one printable byte, so the image is
    walked in bands of six rows. Within a band each color is emitted as its own
    pass (``#<n>`` selects the palette entry) covering only the pixels that use
    it, with runs compressed via ``!<count>``. Pixels of other colors
    contribute bit 0 in that pass and get filled by their own pass, which is
    how overlapping passes compose one band."""
    from PIL import Image

    if image.mode == "RGBA":  # sixel has no alpha; composite onto black
        background = Image.new("RGB", image.size, (0, 0, 0))
        background.paste(image, mask=image.split()[-1])
        image = background
    quantized = image.convert("RGB").quantize(colors=max_colors, method=Image.MEDIANCUT)
    palette = quantized.getpalette() or []
    width, height = quantized.size
    pixels = quantized.load()

    out = ["\x1bP0;1;0q", f'"1;1;{width};{height}']
    # Only define the palette entries actually used. getcolors on a paletted
    # image yields (count, index) pairs.
    used = {index for _, index in (quantized.getcolors(max_colors) or [])}
    for index in sorted(used):
        r, g, b = palette[index * 3:index * 3 + 3] or (0, 0, 0)
        # Sixel color components are percentages (0-100), not 0-255.
        out.append(f"#{index};2;{r * 100 // 255};{g * 100 // 255};{b * 100 // 255}")

    # One flat read of the whole image, as bytes — a paletted image is one byte
    # per pixel, so a row is a slice and iterating it yields the palette indexes
    # directly. Indexing PixelAccess per pixel is a Python-level call each time.
    raw = quantized.tobytes()

    # Each band's per-color column bits; see _sixel_bands for why it is built the
    # way it is, and why numpy changes the shape of the work rather than just
    # speeding up the same loop.
    for _top, band in _sixel_bands(raw, width, height):
        last = len(band) - 1
        for position, index in enumerate(sorted(band)):
            out.append(f"#{index}")
            # Turn the bit column into sixel characters with a 256-byte
            # translation table, then let the regex engine find the runs — both
            # single C-level passes over the row.
            #
            # finditer, not findall: the pattern captures a group for the
            # backreference, and findall would hand back that single character
            # instead of the whole run — silently collapsing every run to one
            # column and truncating the picture.
            for match in _RUN_RE.finditer(band[index].translate(_SIXEL_BYTES)):
                run = match.group()
                out.append(_sixel_run(chr(run[0]), len(run)))
            # "$" returns to column 0 to overlay the next color on this same
            # band; "-" after the last one advances to the next band.
            out.append("$" if position < last else "-")
    out.append("\x1b\\")
    return "".join(out)


#: Bit pattern (0-63) -> the printable byte sixel spells it with. Sized 256 so
#: it can be handed straight to bytes.translate; entries above 63 never occur.
_SIXEL_BYTES = bytes((63 + i) if i < 64 else 63 for i in range(256))

#: One run of identical sixel characters. Finding runs with the regex engine
#: keeps the scan in C rather than comparing character by character in Python.
#:
#: Matching only runs worth compressing (``(.)\1{3,}``) and substituting them in
#: one pass was tried and is SLOWER: the backreference forces the engine to
#: attempt and back out of a match at every position a run does not start, which
#: on photographic content is nearly every position.
_RUN_RE = re.compile(rb"(.)\1*", re.DOTALL)


def _sixel_bands(raw: bytes, width: int, height: int):
    """Yield ``(top, {palette index: column bits})`` for each six-row band, where
    each byte of the column bits says which of the band's six rows that color
    occupies in that column.

    This is where sixel encoding spends its time. The obvious shape — walk the
    band again for each color — costs (colors x width x 6) reads per band, tens
    of millions for one photographic image. Accumulating every color in a single
    pass makes it (width x 6), which is the pure-Python path below.

    That is still a Python loop over every pixel: half a million iterations for a
    screenshot scaled to a terminal pane, paid again on every scroll step,
    because a partially visible picture's crop genuinely changes and so its
    encoding must. numpy removes the loop — one scatter per row into a
    (256, width) accumulator, then one slice per color present.

    The obvious numpy shape, a masked reduction per (band, color), is SLOWER than
    the Python loop it replaces: the arrays are small and per-operation overhead
    dominates at a few hundred colors a band. Scattering instead makes the work
    per band proportional to its six rows rather than to its color count.

    numpy is a win32-only dependency here, so the pure-Python path remains the
    contract and the two are verified to produce identical bytes.
    """
    try:
        import numpy as np
    except ImportError:
        for top in range(0, height, 6):
            rows = min(6, height - top)
            band: dict[int, bytearray] = {}
            for row in range(rows):
                bit = 1 << row
                y = top + row
                for x, index in enumerate(raw[y * width:(y + 1) * width]):
                    column = band.get(index)
                    if column is None:
                        column = band[index] = bytearray(width)
                    column[x] |= bit
            yield top, band
        return
    columns = np.arange(width)
    # One accumulator reused across bands: a row per possible palette entry, and
    # only the entries a band actually uses are cleared and read back.
    acc = np.zeros((256, width), dtype=np.uint8)
    for top in range(0, height, 6):
        rows = min(6, height - top)
        block = np.frombuffer(raw, dtype=np.uint8, count=rows * width,
                              offset=top * width).reshape(rows, width)
        present = np.unique(block)
        acc[present] = 0
        for row in range(rows):
            # Within one row each column appears exactly once, so this scatter
            # has no duplicate indices and the |= is well defined.
            acc[block[row], columns] |= np.uint8(1 << row)
        blob = acc[present].tobytes()
        yield top, {int(c): blob[i * width:(i + 1) * width]
                    for i, c in enumerate(present)}


def _sixel_run(char: str, count: int) -> str:
    """A run of ``count`` copies of ``char``, using sixel's ``!<n>`` repeat
    form only when it is actually shorter than spelling the run out."""
    if count > 3:
        return f"!{count}{char}"
    return char * count
