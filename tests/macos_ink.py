"""Offscreen text rendering for macOS backend tests: draw through the backend's
*real* render paths into a bitmap and measure how much ink landed.

Weight is not something the font APIs will report back — ``NSFont`` happily says
"bold" for a run whose glyphs came from a Regular fallback face. The only honest
check is to draw it and look, so these helpers stand a bitmap context up in place
of a window (no ``NSWindow``, no run loop, nothing that blocks a test run) and
let ``_render_text`` draw into it exactly as it would on screen.
"""

from __future__ import annotations

from AppKit import (
    NSBitmapImageRep,
    NSColor,
    NSDeviceRGBColorSpace,
    NSGraphicsContext,
    NSMakeRect,
    NSRectFill,
)

from puikit.backend import Style
from puikit.backends.macos_backend import MacOSBackend

# Big enough for the longest specimen a test draws at the largest size it uses
# (28pt), with margin so nothing clips into the ink count.
_W, _H = 640, 120


def font_only_backend() -> MacOSBackend:
    """A backend with its fonts resolved and *nothing else* — no window, no view,
    no event loop. ``_init_fonts`` is the whole of what the text paths need
    (the base faces plus the base unit derived from them)."""
    backend = MacOSBackend()
    backend._init_fonts()
    return backend


def coverage(backend: MacOSBackend, text: str, style: Style) -> float:
    """Total inked coverage of ``text`` drawn through the backend's own render
    path, in pixels (antialiased, so a half-lit pixel counts a half). Compare two
    of these to ask whether one run is *heavier* than another."""
    rep = (
        NSBitmapImageRep.alloc()
        .initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
            None, _W, _H, 8, 4, True, False, NSDeviceRGBColorSpace, 0, 0
        )
    )
    ctx = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(ctx)
    try:
        # Black ground, white ink: one channel is then the coverage directly.
        NSColor.blackColor().setFill()
        NSRectFill(NSMakeRect(0, 0, _W, _H))
        backend._render_text(0, 0, text, _white(style))
    finally:
        NSGraphicsContext.restoreGraphicsState()
    ctx.flushGraphics()
    data = bytes(rep.bitmapData()[: _W * _H * 4])
    return sum(data[i] for i in range(0, len(data), 4)) / 255.0


def baseline_row(backend: MacOSBackend, text: str, style: Style, *, row: int = 1) -> int:
    """The bitmap row the bottom of ``text``'s ink lands on when the backend draws
    it at base-unit row ``row`` — its baseline, for a specimen with no descenders
    (``"H"``). The context is **flipped** like the backend's own view
    (``_PuiKitView.isFlipped``): where a pinned line box puts the baseline depends
    on which way is down."""
    import Quartz

    w = _W
    h = int(backend._base_h * (row + 2))
    rep = (
        NSBitmapImageRep.alloc()
        .initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
            None, w, h, 8, 4, True, False, NSDeviceRGBColorSpace, 0, 0
        )
    )
    plain = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    ctx = NSGraphicsContext.graphicsContextWithCGContext_flipped_(plain.CGContext(), True)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(ctx)
    try:
        cg = ctx.CGContext()
        Quartz.CGContextTranslateCTM(cg, 0, h)
        Quartz.CGContextScaleCTM(cg, 1, -1)
        NSColor.blackColor().setFill()
        NSRectFill(NSMakeRect(0, 0, w, h))
        backend._render_text(0, row, text, _white(style))
    finally:
        NSGraphicsContext.restoreGraphicsState()
    plain.flushGraphics()
    data = bytes(rep.bitmapData()[: w * h * 4])
    inked = [y for y in range(h) if any(data[(y * w + x) * 4] > 127 for x in range(w))]
    return max(inked)


def _white(style: Style) -> Style:
    """``style`` forced to white ink, so coverage is comparable across cases and
    a theme default can never make one specimen darker than another."""
    from dataclasses import replace

    return replace(style, fg=(255, 255, 255))
