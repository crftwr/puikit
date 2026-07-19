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
import sys
from typing import Any

KITTY = "kitty"
ITERM2 = "iterm2"
SIXEL = "sixel"

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
    protocol = _TERM_PROGRAM.get((env.get("TERM_PROGRAM") or "").strip().lower())
    if protocol is not None:
        return protocol
    term = (env.get("TERM") or "").lower()
    for needle, found in _TERM_HINTS:
        if needle in term:
            return found
    return None


def cell_pixels(fd: int | None = None) -> tuple[int, int] | None:
    """Pixel size of one character cell as ``(w, h)``, from the kernel's window
    size (``TIOCGWINSZ``'s ``ws_xpixel``/``ws_ypixel``), or ``None`` when the
    terminal does not report it. Needed to turn a cell box into the pixel box
    an image should be scaled to; callers fall back to a nominal cell."""
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
    return (xpixel // cols, ypixel // rows)


def render(
    path: str,
    px_w: int,
    px_h: int,
    src: tuple[float, float, float, float] | None = None,
) -> tuple[Any, bytes] | None:
    """Crop ``path`` to ``src`` (normalized ``(x, y, w, h)`` fractions of the
    image, top-left origin — the pan/zoom window) and scale it to fit ``px_w`` x
    ``px_h`` preserving aspect ratio.

    Returns ``(image, png_bytes)`` — the Pillow image for the sixel encoder,
    and PNG bytes for the two transmit-a-file protocols — or ``None`` if the
    file cannot be read. Scaling happens *here* rather than in the emulator so
    the payload stays proportional to the screen box, not the source file: a
    24-megapixel photo ships as a few hundred KB, and zooming re-crops from
    the original rather than upscaling an already-downscaled copy."""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        image = Image.open(path)
        image.load()
    except Exception:
        return None
    if src is not None:
        # Scale the normalized crop by Pillow's true pixel size (the same
        # fractions the GUI backends scale by their own image size).
        fx, fy, fw, fh = src
        sx, sy = int(round(fx * image.width)), int(round(fy * image.height))
        sw, sh = max(1, int(round(fw * image.width))), max(1, int(round(fh * image.height)))
        box = (
            max(0, sx), max(0, sy),
            min(image.width, sx + sw), min(image.height, sy + sh),
        )
        if box[2] > box[0] and box[3] > box[1]:
            image = image.crop(box)
    # An animated GIF / multi-frame TIFF renders its first frame; a paletted or
    # CMYK source becomes RGB(A) so both encoders see a uniform pixel format.
    if image.mode not in ("RGB", "RGBA"):
        image = image.convert("RGBA" if "A" in image.mode or image.mode == "P" else "RGB")
    px_w, px_h = max(1, int(px_w)), max(1, int(px_h))
    scale = min(px_w / image.width, px_h / image.height)
    if scale < 1.0:  # only ever downscale; magnification is the emulator's job
        image = image.resize(
            (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
            Image.LANCZOS,
        )
    import io

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return image, buffer.getvalue()


def encode(
    protocol: str, image: Any, png: bytes, cols: int, rows: int, image_id: int = 1
) -> str:
    """The escape sequence that draws ``image`` at the cursor in a ``cols`` x
    ``rows`` cell box. The caller positions the cursor first; every protocol
    here is told (or asked) to leave it where it found it, so the sequence does
    not disturb the grid curses believes it is managing."""
    if protocol == KITTY:
        return _kitty(png, cols, rows, image_id)
    if protocol == ITERM2:
        return _iterm2(png, cols, rows)
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


def _iterm2(png: bytes, cols: int, rows: int) -> str:
    """iTerm2 inline image: OSC 1337 with the file inline. ``width``/``height``
    are given in cells (bare integers) and aspect ratio is preserved, so the
    picture letterboxes inside the box instead of stretching."""
    payload = base64.b64encode(png).decode("ascii")
    args = (
        f"inline=1;size={len(png)};width={cols};height={rows};"
        "preserveAspectRatio=1;doNotMoveCursor=1"
    )
    return f"\x1b]1337;File={args}:{payload}\a"


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

    for top in range(0, height, 6):
        band_colors = set()
        for y in range(top, min(top + 6, height)):
            for x in range(width):
                band_colors.add(pixels[x, y])
        for position, index in enumerate(sorted(band_colors)):
            out.append(f"#{index}")
            run_char, run_len = None, 0
            for x in range(width):
                bits = 0
                for row in range(6):
                    y = top + row
                    if y < height and pixels[x, y] == index:
                        bits |= 1 << row
                char = chr(63 + bits)
                if char == run_char:
                    run_len += 1
                    continue
                if run_char is not None:
                    out.append(_sixel_run(run_char, run_len))
                run_char, run_len = char, 1
            if run_char is not None:
                out.append(_sixel_run(run_char, run_len))
            # "$" returns to column 0 to overlay the next color on this same
            # band; "-" after the last one advances to the next band.
            out.append("$" if position < len(band_colors) - 1 else "-")
    out.append("\x1b\\")
    return "".join(out)


def _sixel_run(char: str, count: int) -> str:
    """A run of ``count`` copies of ``char``, using sixel's ``!<n>`` repeat
    form only when it is actually shorter than spelling the run out."""
    if count > 3:
        return f"!{count}{char}"
    return char * count
