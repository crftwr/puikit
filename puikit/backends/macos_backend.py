"""macOS native GUI backend built on PyObjC.

Uses whatever macOS frameworks fit the job — AppKit/Cocoa for windows and
events today; CoreGraphics, CoreText, and others as rendering grows.

The backend keeps a display list of drawing intents (text runs, boxes,
scrollbars, icons, images) in base-unit coordinates; a custom NSView renders the
list in pixels on each draw pass, so the same widget code that runs on
curses gets real rectangles, color text, and emoji icons here.

A compiled C++ CoreText extension is planned for the hot rendering path
(see CLAUDE.md, Multi-Language Policy); this pure-PyObjC renderer is the
reference implementation and the graceful fallback when the extension is
unavailable.
"""

from __future__ import annotations

import math
import os
import random
import sys
import time
import warnings
from dataclasses import dataclass, field, replace
from collections.abc import Callable
from typing import Any

from AppKit import (
    NSApp,
    NSApplication,
    NSApplicationActivationPolicyRegular,
    NSAttributedString,
    NSBackingStoreBuffered,
    NSBezierPath,
    NSColor,
    NSCursor,
    NSDate,
    NSDefaultRunLoopMode,
    NSDragOperationCopy,
    NSDragOperationLink,
    NSDragOperationMove,
    NSDragOperationNone,
    NSDraggingItem,
    NSEvent,
    NSEventMaskAny,
    NSEventModifierFlagCommand,
    NSEventModifierFlagControl,
    NSEventModifierFlagOption,
    NSEventModifierFlagShift,
    NSEventTypeApplicationDefined,
    NSPasteboard,
    NSPasteboardTypeFileURL,
    NSPasteboardTypeHTML,
    NSPasteboardTypeRTF,
    NSPasteboardTypeString,
    NSTimer,
    NSBoldFontMask,
    NSFont,
    NSFontAttributeName,
    NSFontManager,
    NSFontWeightBold,
    NSFontWeightRegular,
    NSForegroundColorAttributeName,
    NSItalicFontMask,
    NSImage,
    NSKernAttributeName,
    NSShadowAttributeName,
    NSMutableParagraphStyle,
    NSParagraphStyleAttributeName,
    NSCompositingOperationCopy,
    NSCompositingOperationSourceOver,
    NSGraphicsContext,
    NSRectClip,
    NSRectFill,
    NSRectFillUsingOperation,
    NSShadow,
    NSTextInputContext,
    NSTrackingActiveInKeyWindow,
    NSTrackingArea,
    NSTrackingCursorUpdate,
    NSTrackingInVisibleRect,
    NSTrackingMouseEnteredAndExited,
    NSTrackingMouseMoved,
    NSStrikethroughStyleAttributeName,
    NSUnderlineStyleAttributeName,
    NSUnderlineStyleSingle,
    NSUnderlineStyleThick,
    NSView,
    NSWorkspace,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskResizable,
    NSWindowStyleMaskTitled,
)
from Foundation import (
    NSAffineTransform,
    NSMakeRange,
    NSMakeRect,
    NSMakeSize,
    NSNotFound,
    NSNumber,
    NSObject,
    NSURL,
    NSZeroPoint,
)
import objc
from PyObjCTools import AppHelper

from ..background import Shader, Wallpaper
from ._metal import HAVE_METAL as _HAS_METAL, MetalBackground, PIXEL_FORMAT as _METAL_PIXEL_FORMAT

try:
    from Quartz import CAMetalLayer
except ImportError:  # pragma: no cover - older/partial PyObjC
    CAMetalLayer = None
from ..backend import Backend, DEFAULT_STYLE, EventHandler, Style, TextAttribute, is_transparent
from ..capability import PROFILE_GUI_DESKTOP, CapabilityProfile
from ..easing import resolve as _resolve_easing
from ..event import Event, EventType, char_key_event
from ..font import Font, FontMetrics, FontWeight
from ..text import display_width, glyph_runs as _glyph_runs

try:
    from Quartz import (
        CGColorSpaceCreateDeviceRGB,
        CGContextBeginTransparencyLayer,
        CGContextDrawLinearGradient,
        CGContextDrawRadialGradient,
        CGContextEndTransparencyLayer,
        CGContextRestoreGState,
        CGContextSaveGState,
        CGContextScaleCTM,
        CGContextSetAlpha,
        CGContextTranslateCTM,
        CGGradientCreateWithColorComponents,
        kCGGradientDrawsAfterEndLocation,
        kCGGradientDrawsBeforeStartLocation,
    )

    _HAS_QUARTZ = True
    _DEVICE_RGB = CGColorSpaceCreateDeviceRGB()
except ImportError:  # animation gracefully degrades to immediate switches
    _HAS_QUARTZ = False
    _DEVICE_RGB = None

#: Vignette falloff, in units of the rect's half-extent after an aspect-correct
#: CTM scale (see _render_vignette): clear out to _INNER, fully dark by _OUTER.
#: Edge midpoints sit at radius 1.0, corners at ~1.41 — so _OUTER just past the
#: corners darkens them most while leaving the mid-edges only partly dimmed.
_VIGNETTE_INNER = 0.55
_VIGNETTE_OUTER = 1.45

#: Rolling "vertical hold" band (see _render_roll_band / _crt_roll_tick). A roll
#: fires every _ROLL_GAP seconds (randomized in that range) and sweeps a band of
#: height _ROLL_BAND_H points down the screen over _ROLL_DUR seconds.
_ROLL_GAP = (5.0, 13.0)
_ROLL_DUR = (1.6, 3.2)
_ROLL_BAND_H = 144.0
#: Rolls only fire while the app is being used: its window is key AND the last
#: key/mouse input was within this many seconds. Keeps the 60fps sweep (and its
#: redraw cost) off when the user is away or in another app. An in-flight roll
#: always finishes regardless.
_ROLL_IDLE_TIMEOUT = 60.0
#: Where the band is brightest, as a fraction from its top (0) to bottom (1): the
#: leading (lower) edge is the bright head, with the intensity ramping up from the
#: weak upper trail to the peak, then a short fade over the final stretch.
_ROLL_PEAK = 0.85


def _roll_band_top(progress: float, view_h: float, band_h: float) -> float:
    """Top-edge y (flipped coords: 0 = screen top) of the rolling band at
    ``progress`` 0..1. At 0 the band sits just above the top; at 1 just below the
    bottom — so it sweeps fully through the screen. Pure, for unit tests."""
    return progress * (view_h + band_h) - band_h


def _roll_falloff(pos: float) -> float:
    """Bottom-weighted intensity across the band at fractional position ``pos``
    (0 = band top / trailing, 1 = leading/bottom edge): ramps up from the weak top
    to a peak at ``_ROLL_PEAK``, then a short fade to the leading edge. Pure."""
    if pos <= _ROLL_PEAK:
        return pos / _ROLL_PEAK
    return (1.0 - pos) / (1.0 - _ROLL_PEAK)

try:
    # Core Image drives the color side of the post-processing effect (tint / glow
    # / bloom / vignette). Kept in its own guard: without it the post_effects
    # capability is declared off and set_post_effect no-ops, exactly like a
    # backend that never had it. NOTE scanlines are NOT a CIFilter — AppKit's
    # layer content filters only honor Apple's built-in CIFilters, silently
    # dropping a custom CIFilter/CIKernel subclass — so scanlines are drawn in the
    # render pass instead (see _render_scanlines).
    from Quartz import CIColor, CIFilter

    _HAS_COREIMAGE = True
except ImportError:
    _HAS_COREIMAGE = False

#: Scanline pitch in points (one dark + one light row). Must stay LARGER than the
#: CIBloom radius (see _build_ci_filters): the lines are painted in the render
#: pass, then bloom composites over them as a content filter, so too small a pitch
#: against too wide a bloom washes them out. A follow-up could derive this from
#: the display's device scale for pixel-exact lines.
_SCANLINE_PERIOD = 4.0

try:
    import CoreText

    _HAS_CORETEXT = True
except ImportError:  # bundled-font registration unavailable; use OS system fonts
    _HAS_CORETEXT = False

_DEFAULT_FG = (230, 230, 230)
_DEFAULT_BG = (24, 24, 24)

#: An animated background is the one thing that keeps a file manager redrawing
#: while nobody is using it, so it coasts to a stop when the app goes idle or
#: loses focus and spins back up on the next input — the same park/re-arm shape
#: as the CRT roll ticker (see ``_ensure_roll_ticker``), which is re-armed from
#: ``_dispatch``.
#:
#: The *ramp* matters as much as the parking. Cutting the motion dead would be as
#: noticeable as the motion itself, so the rate eases between 0 and 1 over these
#: spans rather than switching: the scene visibly coasts to a halt and glides back
#: up. Resuming is quicker than stopping so input feels answered, and both ends of
#: each ramp are smoothed (see ``_smoothstep``) so the speed never changes abruptly.
#: Seconds of no input before the animation starts slowing.
_BG_IDLE_TIMEOUT = 15.0
#: Seconds the coast-down and the spin-up take. Long on purpose: at these spans the
#: rate changes by under 0.2% of full speed per frame, which puts the change below
#: the threshold where the eye reads it as the animation "doing" something. The
#: cost is that the background keeps running for the ramp's length after you stop
#: — parking is deferred, not skipped.
_BG_RAMP_DOWN = 40.0
_BG_RAMP_UP = 15.0
#: Below this rate the scene is close enough to still to be parked outright — the
#: remaining motion is imperceptible and not worth a 60Hz timer.
_BG_RATE_FLOOR = 0.02

#: NSView autoresizing masks (AppKit constants, spelled out so the import block
#: does not have to carry two more names): the UI view tracks its container's size.
_NS_VIEW_WIDTH_SIZABLE = 2
_NS_VIEW_HEIGHT_SIZABLE = 16

def _smoothstep(t: float) -> float:
    """Ease ``t`` (0..1) so it leaves 0 and arrives at 1 with zero slope.

    Applied to the background's rate: a linear ramp would start and stop the
    motion with a visible kick at each end, which defeats the point of ramping at
    all. This makes the change in speed itself gradual.
    """
    t = 0.0 if t < 0.0 else 1.0 if t > 1.0 else t
    return t * t * (3.0 - 2.0 * t)


def _approach(current: float, target: float, dt: float,
              up: float, down: float) -> float:
    """Move ``current`` toward ``target`` (both 0..1) by one ``dt`` step.

    Separate spans for rising and falling, so a background can coast to a stop
    slowly while still answering input briskly. A zero-length span snaps, which
    keeps the helper total for degenerate configuration.
    """
    span = up if target > current else down
    if span <= 0.0:
        return target
    step = dt / span
    if target > current:
        return min(target, current + step)
    return max(target, current - step)


#: ``PUIKIT_BG_PROFILE=1`` prints a timing summary for the animated background
#: every :data:`_BG_PROFILE_FRAMES` frames: how long the scene's generator ran, how
#: long stroking it took, and the whole frame for comparison. Off by default and
#: costs nothing when off — the timing calls are behind this flag, not merely
#: discarded — so it can be left in place. Aimed at answering "is a dense scene
#: affordable on the real window context?", which an offscreen bitmap benchmark
#: cannot: this measures the layer-backed view the app actually draws into.
_BG_PROFILE = bool(os.environ.get("PUIKIT_BG_PROFILE"))
_BG_PROFILE_FRAMES = 60

# Bundled default fonts (puikit/fonts): Noto Sans + Noto Sans Mono — the same
# metrics-matched superfamily the Windows backend bundles, so an unnamed default
# Font() resolves to the same faces on both platforms and the base unit (derived
# from the mono face) fits the UI face without text clipping. Registered with
# Core Text at process scope so they render without being installed; the backend
# falls back to the OS system mono/UI fonts if the files are absent or Core Text
# is unavailable. The files are fetched at build time, not committed.
_FONT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "fonts")
_BUNDLED_MONO = "Noto Sans Mono"
_BUNDLED_UI = "Noto Sans"
_BUNDLED_FONT_FILES = (
    "NotoSans-Regular.ttf", "NotoSans-Bold.ttf",
    "NotoSansMono-Regular.ttf", "NotoSansMono-Bold.ttf",
)

# Registration is a process-wide side effect, so it runs once and the result is
# cached: None = not yet attempted, True/False = whether the bundled families
# are usable.
_bundled_fonts_registered: bool | None = None


def _ensure_bundled_fonts() -> bool:
    """Register the bundled Noto faces with Core Text (process scope) on first
    use so they resolve by family name without being installed, and report
    whether they are usable. Idempotent and process-global; returns False (so
    callers fall back to the OS system fonts) when Core Text is unavailable, a
    font file is missing, or registration fails. Success is confirmed by
    resolving the families rather than by the API return, so an 'already
    registered' error on a re-run in the same process still counts as usable."""
    global _bundled_fonts_registered
    if _bundled_fonts_registered is not None:
        return _bundled_fonts_registered
    _bundled_fonts_registered = False
    if not _HAS_CORETEXT:
        return False
    paths = [os.path.join(_FONT_DIR, f) for f in _BUNDLED_FONT_FILES]
    if not all(os.path.exists(p) for p in paths):
        return False
    try:
        scope = CoreText.kCTFontManagerScopeProcess
        for path in paths:
            CoreText.CTFontManagerRegisterFontsForURL(NSURL.fileURLWithPath_(path), scope, None)
    except Exception:  # any Core Text failure -> OS system fonts
        return False
    if (NSFont.fontWithName_size_(_BUNDLED_UI, 12.0) is None
            or NSFont.fontWithName_size_(_BUNDLED_MONO, 12.0) is None):
        return False
    _bundled_fonts_registered = True
    return True


# Slant used to synthesize an italic (oblique) for a face that ships no real
# italic member — 12 degrees, the conventional synthetic-italic angle.
_OBLIQUE_SHEAR = math.tan(math.radians(12.0))


def _oblique(ns: Any) -> Any:
    """A synthetic oblique of ``ns``: the same face sheared by a fixed slant, for
    an italic request against a family with no real italic member (the bundled
    Noto faces ship upright + bold only). The shear is horizontal, so advances
    are unchanged and grid / proportional layout is unaffected — matching the
    Windows backend, where DirectWrite obliques a face that lacks an italic.
    Returns ``ns`` unchanged if the transform can't be built."""
    size = ns.pointSize()
    transform = NSAffineTransform.transform()
    transform.setTransformStruct_((size, 0.0, _OBLIQUE_SHEAR * size, size, 0.0, 0.0))
    return NSFont.fontWithDescriptor_textTransform_(ns.fontDescriptor(), transform) or ns

# Upper bound on the attributed-string / measured-width caches. Picked well
# above the run count of any single frame so steady-state UIs never evict, yet
# small enough that a flood of unique text stays a bounded blip.
_ATTR_CACHE_MAX = 8192

# Formal NSTextInputClient conformance: adopting the protocol makes PyObjC
# apply Apple's own method signatures (NSRange struct args, the CGRect return
# and the actualRange out-pointer), so the IME methods bridge correctly.
# Hand-written signatures got these subtly wrong, which raised exceptions in
# firstRectForCharacterRange: and mis-bridged insertText:'s range argument.
_NS_TEXT_INPUT_CLIENT = objc.protocolNamed("NSTextInputClient")

# The protocol declares the actualRange argument of firstRectForCharacterRange:
# and attributedSubstringForProposedRange: as a bare NSRange* pointer, not an
# OUT pointer — and PyObjC enforces that exact signature for protocol
# conformance (it rejects an `o^{_NSRange}` override and ignores per-class
# metadata for it). So when the IME passes a non-NULL pointer, PyObjC bridges it
# into an opaque PyObjCPointer and emits an ObjCPointerWarning on every call
# (issue #60). The pointer is an optional out-param both methods already handle
# defensively, so the warning is pure noise. Silence just this NSRange pointer
# warning (other opaque-pointer warnings still surface) while keeping the formal
# conformance, which is what fixed the real firstRect/insertText bridging bugs.
warnings.filterwarnings(
    "ignore",
    category=objc.ObjCPointerWarning,
    message=r"PyObjCPointer created.*_NSRange",
)

# Cocoa function-key code points -> PuiKit symbolic key names.
_FUNCTION_KEYS = {
    0xF700: "up",
    0xF701: "down",
    0xF702: "left",
    0xF703: "right",
    0xF728: "delete",
    0xF729: "home",
    0xF72B: "end",
    0xF72C: "pageup",
    0xF72D: "pagedown",
    # Function keys F1-F12 (NSF1FunctionKey = 0xF704 .. F12 = 0xF70F).
    **{0xF704 + i: f"f{i + 1}" for i in range(12)},
}

_CONTROL_KEYS = {
    "\r": "enter",
    "\n": "enter",
    "\x03": "enter",  # numeric keypad enter
    "\t": "tab",
    "\x19": "tab",    # Shift+Tab: AppKit sends NSBackTabCharacter (0x19), with
                      # the shift flag set, so it resolves to a shift-modified tab
    "\x1b": "escape",
    "\x7f": "backspace",
}

# Icon names -> emoji glyphs. macOS renders these as full-color glyphs, which
# serves as the MVP icon implementation; SF Symbols can replace this later.
_ICON_GLYPHS = {
    "folder": "📁",
    "file": "📄",
    "warning": "⚠️",
    "error": "❌",
    "info": "ℹ️",
    "check": "✅",
}


# PuiKit drag-operation names <-> AppKit NSDragOperation bits. Move is checked
# before copy when naming the *result*, since a session that ended as a move
# also implies the bytes were copied to the receiver — the move is the stronger
# intent and the one the app must act on (delete the originals).
_DRAG_OP_BITS = {
    "copy": NSDragOperationCopy,
    "move": NSDragOperationMove,
    "link": NSDragOperationLink,
}


def _drag_mask(operations: tuple[str, ...]) -> int:
    """OR the named operations into an NSDragOperation mask (copy if empty)."""
    mask = 0
    for op in operations:
        mask |= _DRAG_OP_BITS.get(op, 0)
    return mask or NSDragOperationCopy


def _drag_op_name(operation: int) -> str:
    """Name the operation a finished drag session settled on."""
    for name in ("move", "copy", "link"):  # priority: report move over copy
        if operation & _DRAG_OP_BITS[name]:
            return name
    return "none"


def _modifier_names(modifier_flags: int) -> frozenset[str]:
    """Decode a Cocoa modifier-flag bitmask into contract modifier names."""
    return frozenset(
        name
        for flag, name in (
            (NSEventModifierFlagShift, "shift"),
            (NSEventModifierFlagControl, "ctrl"),
            (NSEventModifierFlagOption, "alt"),
            (NSEventModifierFlagCommand, "cmd"),
        )
        if modifier_flags & flag
    )


def translate_key(characters: str, modifier_flags: int = 0) -> Event | None:
    """Translate a Cocoa key event payload into a PuiKit Event.

    Module-level so the mapping is testable without opening a window."""
    if not characters:
        return None
    modifiers = _modifier_names(modifier_flags)
    ch = characters[0]
    code = ord(ch)
    if code in _FUNCTION_KEYS:
        return Event(type=EventType.KEY, key=_FUNCTION_KEYS[code], modifiers=modifiers)
    if ch in _CONTROL_KEYS:
        return Event(type=EventType.KEY, key=_CONTROL_KEYS[ch], modifiers=modifiers)
    if ch.isprintable():
        # The shared contract helper names space, lowercases letters (keeping
        # Shift), and drops the now-redundant Shift from a shifted glyph (Rule 3):
        # charactersIgnoringModifiers already baked Shift into ``ch`` while
        # dropping Cmd/Ctrl/Option, so Cmd/Ctrl/Alt survive in ``modifiers``.
        return char_key_event(ch, modifiers)
    return None




def _ns_color(color: tuple[int, ...], alpha: float = 1.0):
    # An RGBA 4-tuple folds its alpha channel into the opacity; a 3-tuple is
    # opaque. ``alpha`` multiplies on top (e.g. a dim or shadow tint).
    if len(color) == 4:
        r, g, b, a = color
        alpha = alpha * (a / 255.0)
    else:
        r, g, b = color
    return NSColor.colorWithSRGBRed_green_blue_alpha_(r / 255, g / 255, b / 255, alpha)


def _is_grid_font(font: Any) -> bool:
    """Whether ``font`` lays out one glyph per base-unit column, so it must be
    drawn through the grid path (kerned to base_w) rather than flowed at its
    natural advance. True for the base grid font (``None``) and for an unsized,
    unnamed monospace request — both resolve to the same fixed-advance face the
    base unit was derived from. This mirrors ``log_view._grid_aligned`` exactly:
    LogView wraps such a font by counting columns (one column == base_w), so it
    must also *render* at base_w, or the rendered line falls short of the wrap
    width and wraps early (issue #62)."""
    return font is None or (
        font.monospace and font.family is None and font.size is None
    )


def _attr_string(text: str, attrs) -> Any:
    """Build an NSAttributedString for drawing or measuring.

    The convenience methods -[NSString drawAtPoint:withAttributes:],
    -drawInRect:withAttributes:, and -sizeWithAttributes: each leak ~1.5 KB
    *per call* in this AppKit/PyObjC stack (verified in isolation), independent
    of the string or whether an autorelease pool wraps the call. Text-heavy
    frames issue hundreds to thousands of such calls per render, so the leak
    accumulated into the hundreds of MB / GB as the view redrew. The equivalent
    NSAttributedString methods (-drawAtPoint: / -size) do not leak, so every
    text draw and measurement routes through this helper instead.

    A second, subtler leak motivates the cache in front of this builder (see
    MacOSBackend._cached_attr_string): drawing an attributed string built from a
    *fresh* Python str into a layer-backed view leaves the bridged
    OC_PythonUnicode proxy retained by the layer's display list. Widgets that
    rebuild their text every render (e.g. a re-wrapping MarkdownView) thus
    minted a new proxy per run per frame that never came back. Reusing one
    immutable NSAttributedString per (text, style) keeps the proxy count bounded
    by distinct content rather than growing with frame count."""
    return NSAttributedString.alloc().initWithString_attributes_(text, attrs)


def _fill_rect(rect, color) -> None:
    """Fill ``rect`` with ``color``, compositing over what is already drawn
    when the color is translucent (an RGBA channel below 255)."""
    _ns_color(color).setFill()
    if len(color) == 4 and color[3] < 255:
        NSRectFillUsingOperation(rect, NSCompositingOperationSourceOver)
    else:
        NSRectFill(rect)


def _wallpaper_rect(bounds, image_size, fit: str):
    """Destination rect for a wallpaper image drawn into ``bounds``, centered and
    scaled per ``fit``: ``"stretch"`` ignores aspect (fills exactly), ``"center"``
    keeps native size, ``"fit"`` contains (letterboxed), and ``"fill"`` (the default,
    and any unknown value) covers (cropping overflow)."""
    bx, by = bounds.origin.x, bounds.origin.y
    bw, bh = bounds.size.width, bounds.size.height
    iw, ih = image_size.width, image_size.height
    if fit == "stretch" or iw <= 0 or ih <= 0:
        return NSMakeRect(bx, by, bw, bh)
    if fit == "center":
        w, h = iw, ih
    elif fit == "fit":
        scale = min(bw / iw, bh / ih)
        w, h = iw * scale, ih * scale
    else:  # "fill" — cover
        scale = max(bw / iw, bh / ih)
        w, h = iw * scale, ih * scale
    return NSMakeRect(bx + (bw - w) / 2.0, by + (bh - h) / 2.0, w, h)


def _ci_filter(name: str, **inputs: "Any") -> "Any":
    """A defaulted CIFilter with ``inputs`` (input key WITHOUT the ``input``
    prefix -> value) set. Content filters must leave ``inputImage`` unset; AppKit
    wires each filter's input to the previous stage's output at composite time."""
    f = CIFilter.filterWithName_(name)
    f.setDefaults()
    for key, value in inputs.items():
        f.setValue_forKey_(value, "input" + key)
    return f


def _build_ci_filters(effect) -> "list[Any]":
    """Translate a ``PostEffect`` into an ordered Core Image *content-filter*
    chain (each filter takes the previous one's output as its input image).

    Pure — takes no view and touches no window — so the mapping is unit-testable
    without opening a screen. Returns ``[]`` for a cleared / no-op effect or when
    Core Image is unavailable.

    Covers the *color* side of the look: ``tint`` (CIColorMonochrome), ``glow``
    (CIColorControls), ``bloom`` (a tight CIBloom halo). ``scanline`` and
    ``vignette`` are drawn in the render pass (_render_scanlines /
    _render_vignette): scanlines because AppKit content filters ignore custom
    CIFilters, and vignette because CIVignette's fixed circular falloff portholes
    a non-square window — the render-pass version fits the live bounds. ``tint``
    stays here (a content filter recolors the drawn scanlines/vignette too).
    ``curvature`` / ``flicker`` are carried by the model but need a geometry warp
    / a per-frame param update on the animation timer — the next increment.
    """
    if not _HAS_COREIMAGE or effect is None or effect.is_noop:
        return []
    filters: list[Any] = []
    if effect.tint is not None:
        r, g, b = effect.tint[:3]
        filters.append(_ci_filter(
            "CIColorMonochrome",
            Color=CIColor.colorWithRed_green_blue_(r / 255.0, g / 255.0, b / 255.0),
            Intensity=1.0,
        ))
    if effect.glow > 0:
        filters.append(_ci_filter(
            "CIColorControls",
            Saturation=1.0 + effect.glow * 0.4,
            Brightness=effect.glow * 0.12,
            Contrast=1.0 + effect.glow * 0.15,
        ))
    if effect.bloom > 0:
        # Radius grows with the strength for a wide, soft halo. It is capped below
        # _SCANLINE_PERIOD *only when scanlines are drawn*, so the halo can't wash the
        # painted lines out; with no scanlines a theme can ask for a broad glow.
        radius = effect.bloom * 18.0
        if effect.scanline > 0:
            radius = min(radius, _SCANLINE_PERIOD * 0.5)
        filters.append(_ci_filter("CIBloom", Radius=radius, Intensity=effect.bloom))
    return filters


@dataclass
class Animation:
    """One running transition; progress is derived from wall-clock time.

    Kinds: "fade" (alpha), "slide" (position, hints from_dx/from_dy in
    base units), "scale" (size, hints from_scale), "highlight" (color overlay,
    hints color/strength)."""

    kind: str
    duration: float  # seconds
    start: float     # time.monotonic() timestamp
    hints: dict[str, Any] = field(default_factory=dict)

    def progress(self, now: float) -> float:
        if self.duration <= 0:
            return 1.0
        return min(1.0, max(0.0, (now - self.start) / self.duration))

    def eased(self, now: float) -> float:
        """Progress shaped by the transition's timing curve. The ``easing`` hint
        names one from ``puikit.easing``; with none named this stays
        ``ease_out_quad`` — the curve that was hardcoded here before easing became
        selectable — so an existing transition is unchanged."""
        return _resolve_easing(self.hints.get("easing"))(self.progress(now))

    def done(self, now: float) -> bool:
        return self.progress(now) >= 1.0


class _PuiKitView(NSView, protocols=[_NS_TEXT_INPUT_CLIENT]):
    """Renders the backend's display list; forwards input to the backend.

    Implements the NSTextInputClient protocol so macOS IME (e.g. Japanese)
    works: every keyDown is routed through the text input system, committed
    characters arrive via insertText: (delivered as KEY events with a char),
    in-progress composition arrives via setMarkedText: (delivered as
    IME_COMPOSITION events), and non-text commands (arrows, enter, ...) arrive
    via doCommandBySelector: (re-translated from the current key event). This
    mirrors the ttk CoreGraphics backend's input pipeline."""

    backend = None  # set right after alloc/init

    def isFlipped(self):
        return True  # top-left origin, matching base-unit coordinates

    def acceptsFirstResponder(self):
        return True

    def drawRect_(self, rect):
        if self.backend is not None:
            self.backend._render_into_view()

    # --- IME state (set by the backend right after the view is created) -------

    def _ensure_ime_state(self) -> None:
        # Lazily initialize composition state, so the protocol methods are safe
        # even if called before the backend wired the view up.
        if not hasattr(self, "marked_text"):
            self.marked_text = ""
            self.marked_range = NSMakeRange(NSNotFound, 0)
            self.selected_range = NSMakeRange(0, 0)

    def keyDown_(self, ns_event):
        # Stash the event so doCommandBySelector: can re-translate it (more
        # reliable than NSApp.currentEvent()).
        self._last_key_event = ns_event
        if self.backend is not None and not self.backend._text_input_active:
            # No text widget focused: deliver a plain command KEY event and do
            # NOT engage the input context. interpretKeyEvents would otherwise
            # feed every keystroke to the IME, so with a CJK input source a
            # single-letter binding ('j', 'f', ...) would start composition
            # instead of dispatching. translate_key reads the layout's base
            # character (charactersIgnoringModifiers) so shortcuts stay stable.
            event = translate_key(
                ns_event.charactersIgnoringModifiers(), ns_event.modifierFlags()
            )
            if event is not None:
                self.backend._dispatch(event)
            return
        # A text widget holds focus: hand the key to the input context. It either
        # composes (setMarkedText:), commits text (insertText:), or issues an
        # editing command (doCommandBySelector:) — never a raw key here.
        self.interpretKeyEvents_([ns_event])

    def inputContext(self):
        # Expose the input context ONLY while a text widget holds focus; return
        # nil in command mode. This is the canonical Cocoa way to disable text
        # input for a view, and — unlike becomeFirstResponder — the system queries
        # inputContext on app/window *activation* and when the IME hotkey is
        # pressed. becomeFirstResponder fires only on a responder *change*, so on
        # a plain app reactivation (the sole view is already first responder) it
        # never runs; macOS would then auto-activate the context and inline the
        # IME/input-source UI on our window. Gating here keeps the IME truly
        # disengaged in command mode (system-centered switcher), consistently —
        # at launch, on reactivation, and after a text field closes.
        if self.backend is not None and not self.backend._text_input_active:
            return None
        return getattr(self, "_input_context", None)

    def _sync_input_context(self) -> None:
        """Mirror the OS input context to the backend's text-input flag: engage
        the IME only while a text widget holds focus, disengage it otherwise.
        Reads the context directly (not via ``inputContext``, which reports nil in
        command mode) so it can *deactivate* on the way out. ``begin_text_input`` /
        ``end_text_input`` flip the flag on focus and call this to apply the
        transition immediately; ``becomeFirstResponder`` calls it too so the state
        is re-established when the view regains first responder. The nil-gating in
        ``inputContext`` is what covers plain app reactivation (no responder
        change); this handles the immediate, explicit transitions."""
        ctx = getattr(self, "_input_context", None)
        if ctx is None:
            return
        if self.backend is not None and self.backend._text_input_active:
            ctx.activate()
        else:
            ctx.deactivate()

    def becomeFirstResponder(self):
        result = objc.super(_PuiKitView, self).becomeFirstResponder()
        if result:
            self._sync_input_context()
        return result

    def resignFirstResponder(self):
        ctx = self.inputContext()
        if ctx is not None:
            ctx.deactivate()
        if self.hasMarkedText():
            self.unmarkText()
        return objc.super(_PuiKitView, self).resignFirstResponder()

    # --- NSTextInputClient ---------------------------------------------------

    def hasMarkedText(self) -> bool:
        self._ensure_ime_state()
        return self.marked_range.location != NSNotFound

    def markedRange(self):
        self._ensure_ime_state()
        return self.marked_range

    def selectedRange(self):
        self._ensure_ime_state()
        return self.selected_range

    def validAttributesForMarkedText(self):
        return []

    def setMarkedText_selectedRange_replacementRange_(
        self, string, selected_range, replacement_range
    ):
        self._ensure_ime_state()
        text = str(string.string()) if hasattr(string, "string") else str(string)
        self.marked_text = text
        if len(text) > 0:
            self.marked_range = NSMakeRange(0, len(text))
        else:
            self.marked_range = NSMakeRange(NSNotFound, 0)
        self.selected_range = selected_range
        # Tell the widget about the in-progress composition (preedit).
        caret = selected_range.location if selected_range.location != NSNotFound else len(text)
        # A nonzero-length selectedRange is Cocoa's signal that this is a real
        # highlighted *clause* (multi-segment kanji conversion, cycled with
        # left/right) rather than just the raw input cursor advancing as kana
        # is typed (selectedRange.length == 0 then) — mirrors Windows'
        # GCS_COMPATTR target-run check (see _win32_ime's module docstring).
        # The candidate window should track the former and ignore the latter,
        # or it would crawl rightward on every keystroke of untouched input.
        # ``target_end`` bounds that clause so the widget can underline it thickly
        # (native IMEs mark the selected clause with a heavier rule); it collapses
        # onto the start while there's no highlighted clause, so nothing is marked.
        if selected_range.length > 0:
            target_start = int(selected_range.location)
            target_end = int(selected_range.location + selected_range.length)
        else:
            target_start = target_end = 0
        self.backend._dispatch(
            Event(
                type=EventType.IME_COMPOSITION,
                hints={
                    "preedit": text,
                    "caret": int(caret),
                    "target_start": target_start,
                    "target_end": target_end,
                },
            )
        )

    def unmarkText(self):
        self._ensure_ime_state()
        self.marked_text = ""
        self.marked_range = NSMakeRange(NSNotFound, 0)
        self.selected_range = NSMakeRange(0, 0)
        self.backend._dispatch(
            Event(type=EventType.IME_COMPOSITION, hints={"preedit": "", "caret": 0})
        )

    def insertText_replacementRange_(self, string, replacement_range):
        self._ensure_ime_state()
        self.marked_text = ""
        self.marked_range = NSMakeRange(NSNotFound, 0)
        self.selected_range = NSMakeRange(0, 0)
        text = str(string.string()) if hasattr(string, "string") else str(string)
        if not text:
            return
        # A commit ends composition; clear any lingering preedit in the widget.
        self.backend._dispatch(
            Event(type=EventType.IME_COMPOSITION, hints={"preedit": "", "caret": 0})
        )
        # Each committed character is delivered as a KEY event through the shared
        # contract helper — the same normalization translate_key uses — so direct
        # typing matches every backend: Shift+A is key='a' + {shift} (not 'A'),
        # space is the named key, a shifted symbol drops shift. The modifiers come
        # from the originating key event (interpretKeyEvents stashed it); an
        # IME-committed glyph is non-ASCII, so it ignores shift/alt regardless.
        ns_event = getattr(self, "_last_key_event", None)
        mods = _modifier_names(ns_event.modifierFlags()) if ns_event is not None else frozenset()
        for ch in text:
            self.backend._dispatch(char_key_event(ch, mods))

    def doCommandBySelector_(self, selector):
        # Non-text keys (arrows, enter, tab, delete, escape, ...). Re-translate
        # the originating key event rather than mapping every selector by hand.
        ns_event = getattr(self, "_last_key_event", None) or NSApp.currentEvent()
        if ns_event is not None:
            event = translate_key(
                ns_event.charactersIgnoringModifiers(), ns_event.modifierFlags()
            )
            if event is not None:
                self.backend._dispatch(event)

    def firstRectForCharacterRange_actualRange_(self, char_range, actual_range):
        # Position the candidate window at the widget's reported caret. This is
        # called by the IME during composition; ANY exception here aborts the
        # composition session (and macOS logs "Exception raised accessing first
        # rect"), so the whole body is defensive and always returns a rect.
        try:
            # Position for the exact character the IME asks about (char_range),
            # not just the single reported anchor: the composition's glyph layout
            # is known, so the candidate window follows the clause being converted
            # even when the platform re-queries with a stale caret.
            cx = self.backend._ime_caret_x(char_range.location)
            cy = self.backend._input_caret[1]
            bw, bh = self.backend._base_w, self.backend._base_h
            rect = NSMakeRect(cx * bw, cy * bh, bw, bh)
            window = self.window()
            if window is None:
                return NSMakeRect(0, 0, 0, 0)
            window_rect = self.convertRect_toView_(rect, None)
            screen_rect = window.convertRectToScreen_(window_rect)
        except Exception:
            return NSMakeRect(0, 0, 0, 0)
        # Report the range we answered for, when the IME provided an out-pointer.
        if actual_range is not None:
            try:
                actual_range[0] = char_range
            except (TypeError, AttributeError, IndexError):
                pass
        return screen_rect

    def attributedSubstringForProposedRange_actualRange_(self, proposed_range, actual_range):
        return None

    def characterIndexForPoint_(self, point):
        return NSNotFound

    # --- hover ---------------------------------------------------------------

    def updateTrackingAreas(self):
        for area in list(self.trackingAreas()):
            self.removeTrackingArea_(area)
        options = (
            NSTrackingMouseMoved
            | NSTrackingMouseEnteredAndExited
            | NSTrackingActiveInKeyWindow
            | NSTrackingInVisibleRect
            # Let AppKit ask us (cursorUpdate_) what pointer to show over the
            # view, so our per-region shape survives AppKit's own cursor passes.
            | NSTrackingCursorUpdate
        )
        area = NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            self.bounds(), options, self, None
        )
        self.addTrackingArea_(area)
        objc.super(_PuiKitView, self).updateTrackingAreas()

    def mouseMoved_(self, ns_event):
        x, y = self._mouse_unit(ns_event)
        self.backend._dispatch(Event(type=EventType.MOUSE_MOVE, x=x, y=y))

    def cursorUpdate_(self, ns_event):
        # AppKit's chance to set the pointer as it enters/moves over the view.
        # Re-assert the shape the Panel last requested (set_pointer_shape) so it
        # is not reset to the default arrow between renders; None falls through
        # to the default.
        cursor = self.backend._pointer_cursor
        if cursor is not None:
            cursor.set()
        else:
            objc.super(_PuiKitView, self).cursorUpdate_(ns_event)

    def mouseExited_(self, ns_event):
        # Move the pointer off-canvas so nothing reads as hovered.
        self.backend._dispatch(Event(type=EventType.MOUSE_MOVE, x=-1.0, y=-1.0))

    def _mouse_unit(self, ns_event) -> tuple[float, float]:
        # Carry the *fractional* base-unit position. Flooring here would quantize
        # the click at the window origin, but panes are laid out at pixel-snapped,
        # fractional base-unit origins (the layout margin offsets them). Let the
        # single floor in Event.translated happen after the pane origin is
        # subtracted, so the click grid stays aligned with the rendered grid.
        point = self.convertPoint_fromView_(ns_event.locationInWindow(), None)
        cw, ch = self.backend.base_size
        return (point.x / cw, point.y / ch)

    def mouseDown_(self, ns_event):
        # Keep the live NSEvent: a native drag session (begin_file_drag) must be
        # started from the mouse event that produced it.
        self._last_mouse_event = ns_event
        x, y = self._mouse_unit(ns_event)
        # Press and release are reported separately; the Panel decides when the
        # gesture becomes a click (release over the same widget).
        self.backend._dispatch(Event(type=EventType.MOUSE_DOWN, x=x, y=y, button="left"))

    def mouseUp_(self, ns_event):
        x, y = self._mouse_unit(ns_event)
        self.backend._dispatch(Event(type=EventType.MOUSE_UP, x=x, y=y, button="left"))

    def rightMouseDown_(self, ns_event):
        # Right-click acts on press (context menus), so it stays an atomic click.
        x, y = self._mouse_unit(ns_event)
        self.backend._dispatch(Event(type=EventType.MOUSE_CLICK, x=x, y=y, button="right"))

    def mouseDragged_(self, ns_event):
        self._last_mouse_event = ns_event
        x, y = self._mouse_unit(ns_event)
        self.backend._dispatch(Event(type=EventType.MOUSE_DRAG, x=x, y=y, button="left"))

    # NSDraggingSource: the operations this view offers as a drag source, set
    # per session by begin_file_drag (copy / move / link). The receiver picks
    # one from this mask.
    def draggingSession_sourceOperationMaskForDraggingContext_(self, session, context):
        return getattr(self, "_drag_mask", NSDragOperationCopy)

    # NSDraggingSource: the session finished. ``operation`` is what the receiver
    # settled on; report it to the app so it performs a move (PuiKit never
    # deletes files itself) or undo bookkeeping.
    def draggingSession_endedAtPoint_operation_(self, session, point, operation):
        callback = getattr(self, "_drag_on_complete", None)
        self._drag_on_complete = None
        self._drag_mask = NSDragOperationNone
        if callback is not None:
            callback(_drag_op_name(operation))

    # NSDraggingDestination: accept files dropped onto the view from other apps
    # (drop-IN). The view was registered for file-URL drags in open(); a drop is
    # turned into a positioned FILE_DROP event carrying the dropped paths, which
    # the Panel routes to the widget under the drop point (e.g. a file pane).

    def _dropped_paths(self, sender) -> list:
        """The local filesystem paths a dragging session carries, in order (empty
        if it holds no file URLs — a text-only or unsupported drag)."""
        urls = sender.draggingPasteboard().readObjectsForClasses_options_([NSURL], None)
        paths = []
        for url in urls or ():
            if url.isFileURL() and url.path():
                paths.append(str(url.path()))
        return paths

    def _drop_unit(self, sender) -> tuple[float, float]:
        point = self.convertPoint_fromView_(sender.draggingLocation(), None)
        cw, ch = self.backend.base_size
        return (point.x / cw, point.y / ch)

    def draggingEntered_(self, sender):
        # Offer a copy (the green "+" badge) only for a drag that carries files;
        # reject anything else so the cursor shows it won't be accepted.
        return NSDragOperationCopy if self._dropped_paths(sender) else NSDragOperationNone

    def draggingUpdated_(self, sender):
        return NSDragOperationCopy if self._dropped_paths(sender) else NSDragOperationNone

    def prepareForDragOperation_(self, sender):
        return bool(self._dropped_paths(sender))

    def performDragOperation_(self, sender) -> bool:
        paths = self._dropped_paths(sender)
        if not paths:
            return False
        x, y = self._drop_unit(sender)
        self.backend._dispatch(
            Event(type=EventType.FILE_DROP, x=x, y=y, hints={"paths": paths})
        )
        return True

    def scrollWheel_(self, ns_event):
        delta = ns_event.scrollingDeltaY()
        delta_x = ns_event.scrollingDeltaX()
        if delta == 0 and delta_x == 0:
            return
        # Axis lock: a scroll event drives only its dominant (faster) axis, so a
        # slightly diagonal trackpad swipe scrolls cleanly one way instead of
        # creeping on both axes at once. A tie keeps the vertical axis.
        if abs(delta_x) > abs(delta):
            delta = 0
        else:
            delta_x = 0
        x, y = self._mouse_unit(ns_event)
        scroll = 1 if delta > 0 else (-1 if delta < 0 else 0)
        # A trackpad / precise wheel reports pixel-resolution deltas; convert them
        # to base units so widgets can scroll at pixel granularity, on both axes
        # (a two-finger horizontal swipe drives ``scroll_units_x``). A classic line
        # wheel reports whole lines, so it stays a discrete notch (no unit hints)
        # and widgets fall back to ``scroll``.
        hints = {}
        if ns_event.hasPreciseScrollingDeltas():
            if delta != 0:
                hints["scroll_units"] = delta / self.backend._base_h
            if delta_x != 0:
                hints["scroll_units_x"] = delta_x / self.backend._base_w
        self.backend._dispatch(
            Event(type=EventType.MOUSE_SCROLL, x=x, y=y, scroll=scroll, hints=hints)
        )


class _PuiKitWindowDelegate(NSObject):
    backend = None

    def windowWillClose_(self, notification):
        if self.backend is not None:
            self.backend.quit()

    def windowDidResize_(self, notification):
        if self.backend is not None:
            self.backend._on_resize()


class MacOSBackend(Backend):
    """macOS GUI backend (PyObjC). Coordinates stay base unit-based; this backend
    owns the base unit size and converts to pixels at render time."""

    PROFILE = CapabilityProfile(
        {
            **PROFILE_GUI_DESKTOP,
            # Not implemented yet in the MVP; flip these on as features land.
            "drag_and_drop": True,   # drop-IN: NSDraggingDestination (FILE_DROP)
            "os_drag_drop": True,    # drag-OUT: native NSDraggingSource (below)
            "ime": True,
            "clipboard_rich": True,   # NSPasteboard multi-type write (HTML + plain)
            "native_file_dialog": False,
            "system_tray": False,
            "media_keys": False,
            "gpu_acceleration": False,
            "post_effects": True,    # Core Image content-filter composite; gated
                                     # on _HAS_COREIMAGE in `capabilities` below.
            "background": True,      # a background (a wallpaper image) drawn under
                                     # the UI in the render pass (see set_background).
            "background_shader": True,  # GPU fragment-shader background; gated on
                                        # _HAS_METAL in `capabilities` below.
        }
    )

    @property
    def capabilities(self) -> CapabilityProfile:
        overrides: dict[str, bool] = {}
        if not _HAS_QUARTZ:
            # Without Quartz the fade effect cannot be rendered; declare honestly
            # so the Panel falls back to immediate switches.
            overrides["animation"] = False
        if not _HAS_COREIMAGE:
            overrides["post_effects"] = False
        if not _HAS_METAL or CAMetalLayer is None:
            # No Metal bindings (or no CAMetalLayer): a shader background cannot be
            # composited, so declare it unsupported and let the app fall back.
            overrides["background_shader"] = False
        if overrides:
            return CapabilityProfile({**self.PROFILE, **overrides})
        return self.PROFILE

    def __init__(self, width: int = 100, height: int = 30, title: str = "PuiKit",
                 base_font: Font | None = None, ui_font: Font | None = None,
                 frame_autosave_name: str | None = None):
        self._initial_size = (width, height)
        self._title = title
        # When set, AppKit persists this window's frame (position + size) to the
        # user defaults under this name and restores it on the next launch — the
        # standard NSWindow frame-autosave feature. None keeps the default of
        # opening at the initial size with no restore.
        self._frame_autosave_name = frame_autosave_name
        # The base font is the monospaced grid font, named with the same Font
        # descriptor a text widget uses. The base unit (the layout's length
        # unit) is derived from this font's glyph box on open (base font ->
        # base unit); per-Style proportional fonts never affect it.
        self._base_font = base_font or Font(size=14.0, monospace=True)
        # The UI font is the default *proportional* face: what an unnamed,
        # non-monospace Font() resolves to (markdown prose, message-box text, a
        # plain label). None keeps the OS system UI font. Only its family is read
        # — size comes from the base font (both share one size), so a Font() still
        # scales with base_font. (base_font above is likewise the default mono
        # face an unnamed Font(monospace=True) resolves to.)
        self._ui_font = ui_font
        self._base_w = 1.0
        self._base_h = 1.0
        self._window = None
        self._view = None
        self._delegate = None
        self._handler: EventHandler | None = None
        self._quit_requested = False
        # Pointer shape requested by the Panel (set_pointer_shape): the resolved
        # NSCursor (None = default arrow) and the name it was resolved from, so a
        # repeat request is a no-op. The view's cursorUpdate_ re-asserts it.
        self._pointer_cursor: Any | None = None
        self._pointer_shape: str | None = None
        # Active post-processing effect (set_post_effect); re-applied to the view
        # on open() and kept across resizes because it lives on the layer.
        self._post_effect: Any | None = None
        # Cached NSShadow for the effect's ``drop_shadow`` (rebuilt when the effect
        # changes); None when the active effect has none.
        self._drop_shadow_obj: Any | None = None
        # Rolling-band ("vertical hold") animation state, or None when the active
        # effect has no roll. Keys: active(bool), start, duration, next(start time).
        self._crt_roll: dict | None = None
        # Active background behind the UI (set_background): a Shader (GPU), a
        # Wallpaper (static image), or None (solid). A Shader drives a per-frame
        # tick and paints its own layer; a Wallpaper is drawn by the render pass.
        self._background: Any | None = None
        # The animation's own clock, in seconds of *animated* time. Deliberately not
        # wall-clock: it advances only by what was actually drawn (dt x the current
        # rate), so a background that coasted to a stop and parked resumes exactly
        # where it left off instead of jumping ahead by the idle stretch.
        self._bg_clock: float = 0.0
        # Current speed, 0..1, eased between by _background_tick; and whether the
        # tick is registered (False once parked, until _dispatch re-arms it).
        self._bg_rate: float = 1.0
        self._bg_running: bool = False
        self._bg_last_tick: float = 0.0
        # Loaded NSImage per wallpaper path, so the file is decoded once (not every
        # frame); keyed by the expanded path.
        self._wallpaper_images: dict[str, Any] = {}
        # GPU background (Shader kind): the container view holding the UI view, the
        # Metal renderer, and the CAMetalLayer composited beneath the UI. All three
        # stay None until a shader background is actually set — see
        # _sync_shader_layer — so an app that never uses one creates no Metal
        # objects, and a machine without Metal never gets past the guard.
        self._container: Any = None
        self._metal: Any = None
        self._metal_layer: Any = None
        # Opacity of UI surface fills (1 = opaque UI); lower composites them
        # translucently so a wallpaper behind them shows through. Backend-wide and
        # wallpaper-agnostic — set from the active theme via set_surface_opacity,
        # independent of which wallpaper (the 3D scene, a future static image).
        self._surface_opacity: float = 1.0
        # Nesting depth of reveal-exempt (opaque overlay) groups during the render
        # pass; while > 0 the background does not dissolve surface fills, so
        # an overlay layer occludes the base instead of showing it through.
        self._reveal_exempt_depth: int = 0
        # Wall-clock (monotonic) of the last user input, for the roll's active-use
        # gate (see _roll_user_active). Seeded to launch time so a roll can fire
        # right away if the window opens focused.
        self._last_input_time: float = time.monotonic()
        # Display list double buffer: widgets fill `_back`, drawRect reads `_front`.
        self._back: list[tuple] = []
        self._front: list[tuple] = []
        self._fonts: dict[TextAttribute, Any] = {}
        # Per-Style font cache: resolved NSFonts keyed by (Font, bold, italic).
        self._style_fonts: dict[tuple, Any] = {}
        # Immutable NSAttributedStrings keyed by (text, style-signature), and
        # measured widths keyed the same way. Both bound the per-render text
        # work AND the OC_PythonUnicode proxy retention described in
        # _attr_string; cleared wholesale when they exceed _ATTR_CACHE_MAX so a
        # stream of unique text (a busy log) can never grow them without bound.
        self._attr_cache: dict[tuple, Any] = {}
        self._width_cache: dict[tuple, float] = {}
        # Decoded images keyed by path. Without this, _render_image decoded a
        # fresh NSImage from disk on every frame for every visible image; the
        # AppKit/CoreGraphics backing-store caches behind those accumulate as
        # animations drive repeated redraws (a native memory leak invisible to
        # Python object counts). Bounded by the number of distinct image paths.
        self._image_cache: dict[str, Any] = {}
        self._animations: dict[int, Animation] = {}  # keyed by id(widget)
        self._anim_timer = None
        self._anim_timer_interval: float | None = None  # rate of the live timer
        self._tick_callbacks: list[Any] = []
        # On-screen caret position (base units) reported by the focused text
        # widget; positions the IME candidate window.
        self._input_caret: tuple[float, float] = (0.0, 0.0)
        # Base-unit x of each composition-character boundary (absolute), reported
        # alongside the caret so firstRectForCharacterRange: can answer for the
        # exact clause the IME is converting. None while there's no composition.
        self._input_char_xs: list[float] | None = None
        # Whether a text widget holds focus. While False, keyDown delivers plain
        # command KEY events and never engages the IME (so single-letter
        # bindings work under any input source); while True it routes through
        # the OS text-input services (insertText / IME composition).
        self._text_input_active = False
        # Retained NSMenu target for the installed app menu bar, so item
        # callbacks survive (an NSMenuItem does not retain its target).
        self._menu_responder: Any = None

    # --- lifecycle ---------------------------------------------------------

    def open(self) -> None:
        app = NSApplication.sharedApplication()
        app.setActivationPolicy_(NSApplicationActivationPolicyRegular)

        self._init_fonts()
        w_px = self._initial_size[0] * self._base_w
        h_px = self._initial_size[1] * self._base_h
        style = (
            NSWindowStyleMaskTitled
            | NSWindowStyleMaskClosable
            | NSWindowStyleMaskMiniaturizable
            | NSWindowStyleMaskResizable
        )
        self._window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(120, 120, w_px, h_px), style, NSBackingStoreBuffered, False
        )
        self._window.setTitle_(self._title)

        self._view = _PuiKitView.alloc().initWithFrame_(NSMakeRect(0, 0, w_px, h_px))
        self._view.backend = self
        # IME composition state + the text input context used by becomeFirstResponder.
        self._view.marked_text = ""
        self._view.marked_range = NSMakeRange(NSNotFound, 0)
        self._view.selected_range = NSMakeRange(0, 0)
        self._view._input_context = NSTextInputContext.alloc().initWithClient_(self._view)
        # A container holds the UI view so a shader background can be composited
        # *under* it. A CALayer draws its sublayers above its own contents, so the
        # GPU layer cannot live inside _view — it has to be a sibling behind it,
        # which is what the container provides. _view fills the container and
        # autoresizes with it, so every bounds-based call site is unaffected (they
        # all read _view.bounds(), never the content view).
        container = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, w_px, h_px))
        container.setAutoresizesSubviews_(True)
        self._view.setAutoresizingMask_(_NS_VIEW_WIDTH_SIZABLE | _NS_VIEW_HEIGHT_SIZABLE)
        container.addSubview_(self._view)
        self._container = container
        self._window.setContentView_(container)
        self._window.setAcceptsMouseMovedEvents_(True)
        self._window.makeFirstResponder_(self._view)
        # A post effect set before open() (e.g. from the launch theme) now has a
        # view to attach to; re-assert it (no-op when none / unsupported).
        self._apply_post_effect()
        # Accept files dropped onto the window from other apps (drop-IN): the view
        # becomes an NSDraggingDestination for file URLs and turns a drop into a
        # FILE_DROP event (see the NSDraggingDestination methods on _PuiKitView).
        self._view.registerForDraggedTypes_([NSPasteboardTypeFileURL])

        # Restore the saved frame (if any) and enable ongoing autosave. Setting
        # the frame from the saved name resizes the content view, which size()
        # reads live, so the restored geometry is in effect before the first
        # paint.
        if self._frame_autosave_name:
            self._window.setFrameUsingName_(self._frame_autosave_name)
            self._window.setFrameAutosaveName_(self._frame_autosave_name)

        self._delegate = _PuiKitWindowDelegate.alloc().init()
        self._delegate.backend = self
        self._window.setDelegate_(self._delegate)

        self._window.makeKeyAndOrderFront_(None)
        app.activateIgnoringOtherApps_(True)

    def close(self) -> None:
        if self._anim_timer is not None:
            self._anim_timer.invalidate()
            self._anim_timer = None
        self._anim_timer_interval = None
        self._animations.clear()
        self._tick_callbacks.clear()
        self._crt_roll = None
        self._background = None
        if self._window is not None:
            self._window.setDelegate_(None)
            self._window.orderOut_(None)
            self._window = None
            self._view = None
            self._delegate = None

    def _base_size_pt(self) -> float:
        """Point size an unsized font inherits — the base font's size, the same
        size the base grid unit is derived from."""
        return float(self._base_font.size) if self._base_font.size is not None else 14.0

    def resolve_font(self, font: Font, bold: bool = False, italic: bool = False) -> Any:
        """Turn a Font descriptor into a native NSFont. Shared by the base font
        and per-Style widget fonts, so both name fonts the same way.

        ``family`` is honored if installed. With no family, the request falls
        back to the backend's configured **default face** for its role: the base
        (mono/grid) font for a ``monospace`` request, the ``ui_font`` for a
        proportional one — so widgets share one configurable pair of fonts
        instead of each hardcoding the OS system face. When that default itself
        names no family, it drops to the bundled Noto pair (metrics-matched, so
        text does not clip — the same default the Windows backend uses), or to
        the OS system monospaced / UI font if the bundled files are unavailable
        (the base grid font must stay fixed-advance to tile the grid).
        ``bold``/``italic`` force those traits on top of the descriptor.

        A font that names no size inherits the **base font's** size — the same
        size the base grid unit is derived from — so an unsized ``Font()`` (the
        markdown body, a default label) scales with ``base_font`` / --font-size
        instead of pinning to a constant."""
        size = float(font.size) if font.size is not None else self._base_size_pt()
        want_bold = bold or font.bold
        weight = NSFontWeightBold if want_bold else NSFontWeightRegular
        family = font.family
        if family is None:
            default = self._base_font if font.monospace else self._ui_font
            family = default.family if default is not None else None
        # A default that still names no family drops to the bundled Noto pair
        # (metrics-matched, so text does not clip) — the same default the Windows
        # backend uses — or to the OS system fonts below if the bundled files are
        # unavailable.
        if family is None and _ensure_bundled_fonts():
            family = _BUNDLED_MONO if font.monospace else _BUNDLED_UI
        ns = None
        if family:
            ns = NSFont.fontWithName_size_(family, size)
        if ns is None:
            if font.monospace:
                ns = (
                    NSFont.monospacedSystemFontOfSize_weight_(size, weight)
                    or NSFont.fontWithName_size_("Menlo", size)
                )
            else:
                ns = NSFont.systemFontOfSize_weight_(size, weight)
        # Apply traits a named family does not already encode. Bold and italic
        # are applied separately (not as one combined mask): a family may have a
        # bold face but no bold-italic member — asking for both at once can drop
        # the bold, so bold is applied first and kept, then italic is layered on.
        want_italic = italic or font.italic
        mgr = NSFontManager.sharedFontManager()
        if want_bold:
            ns = mgr.convertFont_toHaveTrait_(ns, NSBoldFontMask) or ns
        if want_italic:
            slanted = mgr.convertFont_toHaveTrait_(ns, NSItalicFontMask)
            if slanted is not None and (mgr.traitsOfFont_(slanted) & NSItalicFontMask):
                ns = slanted  # a real italic member
            else:
                # No real italic (the bundled Noto faces ship upright + bold
                # only) — synthesize an oblique so italic prose is still slanted,
                # keeping any bold already applied above.
                ns = _oblique(ns)
        return ns

    def _init_fonts(self) -> None:
        # Build the base font from its Font descriptor, then DERIVE the base
        # unit from its glyph box (base font -> base unit). The base font is
        # monospaced, so its advance and line height are canonical.
        regular = self.resolve_font(self._base_font)
        bold = self.resolve_font(self._base_font, bold=True)
        self._fonts = {TextAttribute.NORMAL: regular, TextAttribute.BOLD: bold}
        advance = _attr_string("M", {NSFontAttributeName: regular}).size().width
        self._base_w = math.ceil(advance)
        self._base_h = math.ceil(regular.ascender() - regular.descender() + regular.leading())
        # Natural advance of the monospaced base font, before the base unit was
        # rounded up. A constant kern of (base_w - grid_advance) added after each
        # glyph makes a run advance exactly one base unit per glyph, so a whole
        # run can be drawn in a single call yet still land on the grid columns
        # (see _render_text).
        self._grid_advance = advance
        # Pre-bridge the kern as ONE NSNumber, reused for every text run. The
        # kern goes into the id-typed NSKernAttributeName slot, so PyObjC has to
        # wrap the Python number in an ObjC object; a *fresh* Python float per
        # call (e.g. recomputing base_w - grid_advance inline) makes a new
        # wrapper each time that is then retained for the process lifetime — a
        # ~16 byte-per-text-run leak that, across redraws, grew RSS without
        # bound (verified in isolation). One shared NSNumber bridges once.
        self._grid_kern = NSNumber.numberWithDouble_(self._base_w - advance)
        # Pin the line box to the base unit so drawAtPoint places the baseline
        # deterministically. The text engine's default line height for a font
        # varies by launch/link context: a bundled .app resolves Noto's oversized
        # usWin vertical metrics (a ~30pt box for an 18pt font) where a terminal
        # run uses the ~24pt typographic box, which drops the baseline and spills
        # descenders past the cell. A fixed line height keeps every row on the
        # grid regardless of context.
        para = NSMutableParagraphStyle.alloc().init()
        para.setMinimumLineHeight_(float(self._base_h))
        para.setMaximumLineHeight_(float(self._base_h))
        self._grid_para = para
        # Per-line-height paragraph styles for the flow (proportional/sized) path,
        # cached so a redraw does not allocate. Same purpose as _grid_para but the
        # height tracks each font's own typographic box instead of the base unit.
        self._flow_para_cache: dict[int, Any] = {}

    def _line_height_para(self, ns_font: Any) -> Any:
        """A paragraph style pinning the line box to ``ns_font``'s typographic
        height (ascent + descent + leading), so drawAtPoint places the baseline
        the same way regardless of the launch context's default line-height
        metric — see _init_fonts for why that default is unreliable for Noto."""
        lh = math.ceil(ns_font.ascender() - ns_font.descender() + ns_font.leading())
        para = self._flow_para_cache.get(lh)
        if para is None:
            para = NSMutableParagraphStyle.alloc().init()
            para.setMinimumLineHeight_(float(lh))
            para.setMaximumLineHeight_(float(lh))
            self._flow_para_cache[lh] = para
        return para

    def _resolve_style_font(self, style: Style) -> Any:
        """The NSFont for a Style carrying a per-widget font, cached by request.
        Attribute bold/italic compose with the font's own weight/slant (the
        stronger wins; either italic source makes it italic — see §5)."""
        font = style.font
        bold = bool(style.attr & TextAttribute.BOLD)
        italic = bool(style.attr & TextAttribute.ITALIC)
        key = (font, bold, italic)
        ns = self._style_fonts.get(key)
        if ns is None:
            ns = self.resolve_font(font, bold=bold, italic=italic)
            self._style_fonts[key] = ns
        return ns

    def _cached_attr_string(self, key: tuple, text: str, attrs) -> Any:
        """An NSAttributedString for (text, style ``key``), reused across frames.
        ``attrs`` is only built by the caller on a miss. See _attr_string for the
        leak this prevents and _ATTR_CACHE_MAX for the bound."""
        s = self._attr_cache.get(key)
        if s is None:
            s = _attr_string(text, attrs)
            if len(self._attr_cache) >= _ATTR_CACHE_MAX:
                self._attr_cache.clear()
            self._attr_cache[key] = s
        return s

    def measure_text(self, text: str, style: Style = DEFAULT_STYLE) -> float:
        """Displayed width in base units. A grid font (font=None or an unsized,
        unnamed monospace face) tiles the grid one column per glyph; a real
        per-Style font is measured natively and divided by the base unit width,
        so the result stays in base units."""
        if _is_grid_font(style.font):
            return float(display_width(text))
        ns_font = self._resolve_style_font(style)
        key = (text, id(ns_font))
        width = self._width_cache.get(key)
        if width is None:
            width = _attr_string(text, {NSFontAttributeName: ns_font}).size().width
            if len(self._width_cache) >= _ATTR_CACHE_MAX:
                self._width_cache.clear()
            self._width_cache[key] = width
        return width / self._base_w if self._base_w else float(len(text))

    def measure_line_height(self, style: Style = DEFAULT_STYLE) -> float:
        """Row pitch in base units. A grid font is exactly one base unit (that
        is how the unit was derived in _init_fonts); a real per-Style font
        reports its own line height, rounded up to whole pixels so successive
        rows land on the device-pixel grid, then expressed in base units.

        font=None is NOT a grid font here: Panel's _resolve() draws it as the
        proportional UI font (_DEFAULT_UI_FONT), so it is measured as that font
        too — measuring it as one mono row under-sizes a content-sized
        default-font container and its clip trims the taller UI font's
        descenders. Only an explicit unsized/unnamed monospace request stays a
        true grid row."""
        if not self._base_h:
            return 1.0
        font = style.font if style.font is not None else Font()
        if _is_grid_font(font):
            return 1.0
        ns_font = self._resolve_style_font(Style(attr=style.attr, font=font))
        line_px = ns_font.ascender() - ns_font.descender() + ns_font.leading()
        return math.ceil(line_px) / self._base_h

    def measure_font_size(self, style: Style = DEFAULT_STYLE) -> float:
        """Resolved point size in points. A Style that names no size folds to the
        base grid font, so its size is the base font's — the size a relative
        heading scales off of (docs/font_system.md §7)."""
        font = style.font
        if font is None or font.size is None:
            return self._base_size_pt()
        return float(font.size)

    def font_metrics(self, style: Style = DEFAULT_STYLE) -> FontMetrics:
        if not self._base_h:
            return FontMetrics(ascent=1.0, descent=0.0)
        # font=None is drawn as the UI font (see measure_line_height); measure
        # that. NSFont's descender is negative (below the baseline), so negate
        # it. ascent + descent matches measure_line_height's line box, split at
        # the baseline for mixed-font alignment (draw_text_baseline).
        font = style.font if style.font is not None else Font()
        ns_font = self._resolve_style_font(Style(attr=style.attr, font=font))
        ascent_px = ns_font.ascender() + ns_font.leading()
        descent_px = -ns_font.descender()
        return FontMetrics(ascent=ascent_px / self._base_h, descent=descent_px / self._base_h)

    # --- geometry ----------------------------------------------------------

    @property
    def size(self) -> tuple[int, int]:
        if self._view is None:
            return self._initial_size
        bounds = self._view.bounds()
        return (int(bounds.size.width // self._base_w), int(bounds.size.height // self._base_h))

    @property
    def size_units(self) -> tuple[float, float]:
        if self._view is None:
            return (float(self._initial_size[0]), float(self._initial_size[1]))
        bounds = self._view.bounds()
        return (bounds.size.width / self._base_w, bounds.size.height / self._base_h)

    @property
    def base_size(self) -> tuple[int, int]:
        return (int(self._base_w), int(self._base_h))

    # --- drawing (display list, base-unit coordinates) ----------------------------

    def clear(self) -> None:
        self._back = []

    def draw_text(self, x: int, y: int, text: str, style: Style = DEFAULT_STYLE) -> None:
        self._back.append(("text", x, y, text, style))

    def draw_box(
        self,
        x: int,
        y: int,
        w: int,
        h: int,
        style: Style = DEFAULT_STYLE,
        hints: dict[str, Any] | None = None,
    ) -> None:
        self._back.append(("box", x, y, w, h, style, hints or {}))

    def fill_rect(self, x: float, y: float, w: float, h: float, style: Style = DEFAULT_STYLE) -> None:
        self._back.append(("fill", x, y, w, h, style))

    def draw_round_rect(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        radius: float | None,
        style: Style = DEFAULT_STYLE,
        hints: dict[str, Any] | None = None,
    ) -> None:
        self._back.append(("round_rect", x, y, w, h, radius, style, hints or {}))

    def draw_check(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        style: Style = DEFAULT_STYLE,
        hints: dict[str, Any] | None = None,
    ) -> None:
        self._back.append(("check", x, y, w, h, style))

    def draw_chevron(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        expanded: bool,
        style: Style = DEFAULT_STYLE,
        hints: dict[str, Any] | None = None,
    ) -> None:
        self._back.append(("chevron", x, y, w, h, expanded, style))

    def dim_rect(
        self, x: int, y: int, w: int, h: int, scrim: Any = None, per_cell: bool = False,
        fade: bool = False,
    ) -> None:
        # Compositing backend: the dim is a real translucent overlay, correct on
        # any theme, so the whole-cell ``scrim``/``per_cell``/``fade`` hints are
        # ignored.
        self._back.append(("dim", x, y, w, h))

    def draw_shadow(
        self,
        x: int,
        y: int,
        w: int,
        h: int,
        radius: float | None = None,
        corners: tuple[str, ...] | None = None,
        bg: tuple[int, ...] | None = None,
    ) -> None:
        self._back.append(("shadow", x, y, w, h, radius, corners, bg))

    def begin_group(self, key: Any, rect: Any = None, opaque: bool = False) -> None:
        self._back.append(("group_begin", id(key), rect, opaque))

    def end_group(self, key: Any) -> None:
        self._back.append(("group_end", id(key)))

    def push_clip(self, x: float, y: float, w: float, h: float) -> None:
        self._back.append(("clip_push", x, y, w, h))

    def pop_clip(self) -> None:
        self._back.append(("clip_pop",))

    # --- animation -------------------------------------------------------------

    def animate(self, widget: Any, hints: dict[str, Any] | None = None) -> None:
        hints = hints or {}
        self._animations[id(widget)] = Animation(
            kind=hints.get("transition", "fade"),
            duration=hints.get("duration_ms", 200) / 1000.0,
            start=time.monotonic(),
            hints=hints,
        )
        self._ensure_animation_timer()

    def request_animation_ticks(self, callback) -> None:
        if callback not in self._tick_callbacks:
            self._tick_callbacks.append(callback)
        self._ensure_animation_timer()

    def call_on_main_thread(self, callback) -> None:
        # callAfter posts via performSelectorOnMainThread, which signals a run-loop
        # source and wakes a loop blocked in nextEventMatchingMask; the callback
        # then runs on the main (UI) thread. This is what lets an app be fully
        # event-driven — a worker enqueues, wakes the UI thread to drain, and no
        # idle polling timer is needed.
        AppHelper.callAfter(callback)

    #: Frame-timer rates. A live animation wants smooth 60fps; a permanent tick
    #: callback with no animation (e.g. TFM's idle filesystem-monitoring pump)
    #: only polls queues, so it runs far slower — 10Hz keeps reload/loading
    #: latency imperceptible while cutting idle CPU wakeups 6x, letting macOS
    #: coalesce timers and nap.
    _ANIM_INTERVAL = 1 / 60.0
    _IDLE_TICK_INTERVAL = 1 / 10.0

    def _ensure_animation_timer(self) -> None:
        """(Re)create the frame timer at the rate the current work needs: 60fps
        while any animation is live, the slow idle-poll rate when only permanent
        tick callbacks remain. Recreates the NSTimer only when the required rate
        actually changes, so steady state costs nothing."""
        if not self._animations and not self._tick_callbacks:
            return
        # A live widget animation, an in-flight roll sweep, OR a continuously
        # animating background wants smooth 60fps; otherwise the slow idle-poll
        # rate (a roll only waiting to fire, a bare filesystem pump, or a *static*
        # wallpaper needs nothing faster).
        # Both animated background kinds count. A shader is easy to forget here
        # because it does not repaint the UI — but the tick is now the *only* thing
        # advancing it, so leaving it out drops it to the idle rate and it animates
        # at 10fps.
        fast = bool(self._animations) or self._roll_active() or self._bg_running
        interval = self._ANIM_INTERVAL if fast else self._IDLE_TICK_INTERVAL
        if self._anim_timer is not None and self._anim_timer_interval == interval:
            return
        if self._anim_timer is not None:
            self._anim_timer.invalidate()
        self._anim_timer_interval = interval
        self._anim_timer = NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
            interval, True, self._on_animation_tick
        )

    def _on_animation_tick(self, timer) -> None:
        now = time.monotonic()
        finished = [anim for anim in self._animations.values() if anim.done(now)]
        self._animations = {
            key: anim for key, anim in self._animations.items() if not anim.done(now)
        }
        # Layout-level animations (Panel "size" transitions) re-render the
        # display list themselves; a callback returning False is done.
        self._tick_callbacks = [cb for cb in self._tick_callbacks if cb()]
        # Fire each finished transition's completion hook (a drawer slide-out pops
        # its layer here) BEFORE the redraw below, so the hook's re-render rebuilds
        # the display list without the popped layer — otherwise the now-untransformed
        # group would flash back at its rest position for one frame.
        for anim in finished:
            on_complete = anim.hints.get("on_complete")
            if on_complete is not None:
                on_complete()
        # Only repaint per-frame when something is actually animating. A permanent
        # tick callback (e.g. an idle filesystem-monitoring pump) keeps this timer
        # alive but must NOT drag the whole window through a 60fps re-rasterization
        # while nothing moves — such callbacks request their own redraw via
        # render()/present() when they genuinely change state. ``finished`` gets one
        # last frame so a just-completed animation lands at its rest position.
        if self._view is not None and (self._animations or finished):
            self._view.setNeedsDisplay_(True)
        if not self._animations and not self._tick_callbacks:
            # One last redraw at the final state has been requested above.
            timer.invalidate()
            self._anim_timer = None
            self._anim_timer_interval = None
        else:
            # Animations that just finished leave only the idle pump behind; drop
            # back to the slow rate so we stop waking at 60fps to poll empty
            # queues. No-op (same rate) in steady state.
            self._ensure_animation_timer()

    def draw_scrollbar(
        self, x: int, y: int, h: int, pos: float, ratio: float,
        style: Style = DEFAULT_STYLE, orientation: str = "vertical",
        surface: tuple[int, int, int] | None = None,
    ) -> None:
        # ``surface`` is only meaningful to the character-grid half-block bar; the
        # vector render paints just the thin bar, so the row already shows the
        # surface around it. Accepted for signature parity, not recorded.
        self._back.append(("scrollbar", x, y, h, pos, ratio, style, orientation))

    def draw_icon(self, x: int, y: int, icon_name: str, style: Style = DEFAULT_STYLE) -> None:
        glyph = _ICON_GLYPHS.get(icon_name, "❓")
        self._back.append(("text", x, y, glyph, style))

    def draw_image(self, x: int, y: int, path: str, hints: dict[str, Any] | None = None) -> None:
        self._back.append(("image", x, y, path, hints or {}))

    def present(self) -> None:
        self._front = self._back
        self._back = []
        if self._view is not None:
            # Mark the view dirty and let AppKit coalesce the actual redraw to
            # one drawRect_ per display refresh. Do NOT force a synchronous draw
            # here (displayIfNeeded): a trackpad emits a flood of precise scroll
            # events, and rasterizing the whole window in CoreText on every one
            # cannot keep pace, so scrolling stutters. Because each present()
            # overwrites _front, a burst of events collapses to a single
            # rasterization of the latest frame — the intermediate frames'
            # expensive draw work is skipped. The display list itself is rebuilt
            # per event, but that is cheap; the cost was always the rasterization.
            self._view.setNeedsDisplay_(True)

    # --- pixel rendering (called from the view's drawRect) --------------------

    def _render_into_view(self) -> None:
        frame_start = time.perf_counter() if _BG_PROFILE else 0.0
        # Clear the frame. When the background names a backdrop, the opacity-dissolved
        # surface fills (and any bare gaps) fall back to *that* color instead of the
        # neutral dark default — so a light-theme app stays light where dissolved and
        # a dark scene line reads against it, rather than muddying toward near-black.
        # No effect when no background / no backdrop.
        clear = _DEFAULT_BG
        bg = self._background
        if bg is not None and getattr(bg, "backdrop", None) is not None:
            clear = bg.backdrop
        if isinstance(bg, Shader) and self._metal_layer is not None:
            # A shader paints the GPU layer *behind* this view, so the view must
            # not lay down an opaque backdrop over it. Clear to transparent with
            # Copy (not the default source-over, which would leave the previous
            # frame's pixels), then let the UI composite onto the layer showing
            # through. The shader itself clears to the backdrop on its side.
            NSGraphicsContext.currentContext().setCompositingOperation_(
                NSCompositingOperationCopy)
            NSColor.clearColor().setFill()
            NSRectFill(self._view.bounds())
            NSGraphicsContext.currentContext().setCompositingOperation_(
                NSCompositingOperationSourceOver)
        else:
            _ns_color(clear).setFill()
            NSRectFill(self._view.bounds())
        now = time.monotonic()
        # The background is drawn *before* the display list, so every widget paints
        # over it — it shows through only where the UI leaves the cleared background
        # bare (or paints a translucent fill). Dispatch on the kind.
        if isinstance(bg, Wallpaper):
            self._render_wallpaper(bg)
        # A Shader is deliberately absent here: it paints its own layer from the
        # frame tick (see _background_tick), not from the UI's render pass, so a
        # UI repaint neither needs nor triggers one. The transparent clear above is
        # all this pass owes it.
        group_stack: list[tuple] = []  # (group_state, reveal_exempt)
        # An overlay layer (modal viewer, dialog, menu) marks its group opaque so
        # its surface fills occlude rather than dissolve under an active reveal —
        # otherwise the base file manager behind it would show through. Counts
        # nesting so a widget's own groups inside the layer inherit the exemption.
        self._reveal_exempt_depth = 0
        for command in self._front:
            kind = command[0]
            if kind == "text":
                self._render_text(*command[1:])
            elif kind == "box":
                self._render_box(*command[1:])
            elif kind == "scrollbar":
                self._render_scrollbar(*command[1:])
            elif kind == "image":
                self._render_image(*command[1:])
            elif kind == "fill":
                self._render_fill(*command[1:])
            elif kind == "round_rect":
                self._render_round_rect(*command[1:])
            elif kind == "check":
                self._render_check(*command[1:])
            elif kind == "chevron":
                self._render_chevron(*command[1:])
            elif kind == "dim":
                self._render_dim(*command[1:])
            elif kind == "shadow":
                self._render_shadow(*command[1:])
            elif kind == "group_begin":
                exempt = len(command) > 3 and command[3]
                if exempt:
                    self._reveal_exempt_depth += 1
                group_stack.append(
                    (self._begin_group_render(command[1], command[2], now), exempt))
            elif kind == "group_end":
                if group_stack:
                    state, exempt = group_stack.pop()
                    self._end_group_render(state, now)
                    if exempt:
                        self._reveal_exempt_depth -= 1
            elif kind == "clip_push":
                # NSRectClip works in the current transform, so a clip set
                # inside an animated group travels with the transition.
                NSGraphicsContext.saveGraphicsState()
                NSRectClip(self._unit_rect(*command[1:]))
            elif kind == "clip_pop":
                NSGraphicsContext.restoreGraphicsState()
        # Scanlines are painted here (not as a CIFilter): AppKit's content filters
        # drop custom CIFilters, so the color chain (tint/glow/bloom/vignette) runs
        # as content filters over this whole frame — these dark rows included.
        effect = self._post_effect
        if effect is not None and effect.scanline > 0:
            self._render_scanlines(effect.scanline)
        if effect is not None and effect.vignette > 0 and _HAS_QUARTZ:
            self._render_vignette(effect.vignette)
        # Drawn last in the render pass, so the band sits on top of the scanlines
        # and vignette (a roll won't be dimmed at the screen edges). It still
        # passes through the color content filters (tint / glow / bloom) applied
        # to the whole layer afterward.
        if effect is not None and effect.roll > 0 and self._roll_active():
            self._render_roll_band(effect, now)
        if _BG_PROFILE:
            self._bg_profile_frame(time.perf_counter() - frame_start)

    def _bg_profile_stats(self) -> dict:
        """The profiling accumulator, created on first use."""
        stats = getattr(self, "_bg_stats", None)
        if stats is None:
            stats = self._bg_stats = {"n": 0, "frame": 0.0, "gen": 0.0,
                                      "stroke": 0.0, "segs": 0, "paths": 0,
                                      "ui": 0, "kind": "none",
                                      "t0": time.perf_counter()}
        return stats

    def _bg_profile_frame(self, frame_seconds: float) -> None:
        """Record one UI repaint. Only called while PUIKIT_BG_PROFILE is set.

        For a segment background this *is* the animation's frame — the scene is
        drawn inside this pass — so it also drives the report. A shader's animation
        is decoupled from UI repaints, so there it only counts how often the UI
        actually redrew, and the tick drives the report instead.
        """
        stats = self._bg_profile_stats()
        stats["ui"] += 1
        stats["frame"] += frame_seconds
        if stats["kind"] != "shader":
            stats["n"] += 1
            if stats["n"] >= _BG_PROFILE_FRAMES:
                self._bg_profile_report()

    def _bg_profile_tick(self, seconds: float) -> None:
        """Record one shader tick — the background's real per-frame CPU cost."""
        stats = self._bg_profile_stats()
        stats["kind"] = "shader"
        stats["stroke"] += seconds
        stats["n"] += 1
        if stats["n"] >= _BG_PROFILE_FRAMES:
            self._bg_profile_report()

    def _bg_profile_report(self) -> None:
        """Print the averages since the last report and start a fresh window."""
        stats = self._bg_stats
        n = max(1, stats["n"])
        # Measured, not assumed: the achieved rate is the number that says whether
        # the animation is actually smooth. Cost per frame looks identical whether
        # the timer is running at 60Hz or 10Hz, which is exactly how a background
        # stuck at the idle rate goes unnoticed.
        elapsed = max(1e-6, time.perf_counter() - stats["t0"])
        fps = n / elapsed
        if stats["kind"] == "shader":
            # Two separate numbers, because they are no longer the same thing: what
            # the background costs per animated frame, and how often the UI had to
            # repaint at all. A healthy idle app shows a near-zero repaint count.
            ui = stats["ui"]
            ui_ms = stats["frame"] / ui * 1000 if ui else 0.0
            sys.stderr.write(
                "[puikit bg] shader   {:5.1f} fps | rate {:4.2f} | tick {:5.2f}ms | "
                "UI repaints {:3d} ({:5.2f}ms each)\n"
                .format(fps, self._bg_rate, stats["stroke"] / n * 1000, ui, ui_ms))
        else:
            scene = (stats["gen"] + stats["stroke"]) / n * 1000
            sys.stderr.write(
                "[puikit bg] segments {:5.1f} fps | rate {:4.2f} | frame {:5.2f}ms | "
                "scene {:5.2f}ms (generate {:5.2f} + stroke {:5.2f}) | {:5.0f} segments "
                "in {:3.0f} paths\n"
                .format(fps, self._bg_rate, stats["frame"] / n * 1000, scene,
                        stats["gen"] / n * 1000, stats["stroke"] / n * 1000,
                        stats["segs"] / n, stats["paths"] / n))
        sys.stderr.flush()
        stats.update({"n": 0, "frame": 0.0, "gen": 0.0, "stroke": 0.0,
                      "segs": 0, "paths": 0, "ui": 0,
                      "t0": time.perf_counter()})

    def _render_shader(self, bg: Any, now: float) -> None:
        """Draw one frame of the shader background onto its GPU layer.

        Called from the render pass so the GPU frame is produced in step with the
        UI frame that composites over it, rather than on a timer of its own (which
        would let the two drift and tear). The whole cost here is packing 64 bytes
        of uniforms and encoding a three-vertex draw — it does not scale with what
        the shader actually paints, which is the entire reason this kind exists.
        """
        if self._metal is None or self._metal_layer is None:
            return
        mark = time.perf_counter() if _BG_PROFILE else 0.0
        self._metal.render_to_layer(self._metal_layer, self._bg_clock)
        if _BG_PROFILE:
            self._bg_profile_tick(time.perf_counter() - mark)

    def _render_wallpaper(self, bg: Any) -> None:
        """Draw the wallpaper image under the display list, scaled into the live
        bounds per its ``fit``. The decoded NSImage is cached by path so only the
        first frame pays the load; a path that fails to load draws nothing (the
        backdrop clear already painted), so a bad path degrades gracefully."""
        image = self._wallpaper_image(bg.image)
        if image is None:
            return
        bounds = self._view.bounds()
        dst = _wallpaper_rect(bounds, image.size(), bg.fit)
        image.drawInRect_fromRect_operation_fraction_(
            dst, NSMakeRect(0, 0, 0, 0), NSCompositingOperationSourceOver, bg.opacity
        )

    def _wallpaper_image(self, path: str) -> Any:
        """The decoded, cached NSImage for ``path`` (``~`` expanded), or ``None`` if
        it cannot be loaded. Cached (including the ``None`` miss) so a missing or
        broken file is not re-read every frame."""
        if path not in self._wallpaper_images:
            expanded = os.path.expanduser(path)
            self._wallpaper_images[path] = NSImage.alloc().initWithContentsOfFile_(expanded)
        return self._wallpaper_images[path]

    def _render_scanlines(self, strength: float) -> None:
        """Paint dark horizontal rows every ``_SCANLINE_PERIOD`` points over the
        whole view — the CRT scanline texture. ``strength`` (0..1) is used almost
        directly as the dark rows' opacity: on a near-black phosphor background a
        low opacity is invisible (near-black over near-black), so the line must
        genuinely dim the row for the banding to read. Drawn source-over so it
        darkens the content; the color CIFilter chain then composites over the
        result."""
        bounds = self._view.bounds()
        w = bounds.size.width
        h = bounds.size.height
        _ns_color((0, 0, 0), alpha=min(strength, 0.7)).setFill()
        line_h = _SCANLINE_PERIOD / 2.0  # half dark, half light
        y = 0.0
        while y < h:
            NSRectFillUsingOperation(
                NSMakeRect(0.0, y, w, line_h), NSCompositingOperationSourceOver
            )
            y += _SCANLINE_PERIOD

    def _render_vignette(self, strength: float) -> None:
        """Darken the frame toward its edges with a radial falloff that fits the
        live view bounds. ``strength`` (0..1) is the corner darkness.

        Drawn in the render pass rather than as CIVignette because a content
        filter's circular falloff can't adapt to the window's aspect ratio — on a
        wide/short window it collapses into a porthole. Here a CTM scale maps a
        unit circle onto an ellipse fitting the rect, so distance is normalized
        per axis: every edge midpoint dims equally and the corners most,
        regardless of shape, and it re-fits on every resize."""
        bounds = self._view.bounds()
        w = bounds.size.width
        h = bounds.size.height
        if w <= 0 or h <= 0:
            return
        cg = NSGraphicsContext.currentContext().CGContext()
        alpha = min(strength, 1.0)
        gradient = CGGradientCreateWithColorComponents(
            _DEVICE_RGB, [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, alpha], [0.0, 1.0], 2
        )
        CGContextSaveGState(cg)
        CGContextTranslateCTM(cg, w * 0.5, h * 0.5)
        CGContextScaleCTM(cg, w * 0.5, h * 0.5)  # unit circle -> rect-fitting ellipse
        CGContextDrawRadialGradient(
            cg, gradient, (0.0, 0.0), _VIGNETTE_INNER, (0.0, 0.0), _VIGNETTE_OUTER,
            kCGGradientDrawsBeforeStartLocation | kCGGradientDrawsAfterEndLocation,
        )
        CGContextRestoreGState(cg)

    def _render_roll_band(self, effect: Any, now: float) -> None:
        """Paint the rolling "vertical hold" band at its current sweep position: a
        smooth bright vertical gradient whose opacity follows the bottom-weighted
        ``_roll_falloff`` profile (transparent at the top edge, peak at
        ``_ROLL_PEAK``, transparent at the bottom edge). Drawn as one CGGradient
        rather than stacked rows so it reads as a smooth glow with no banding.
        Brightening is source-over so it lifts the phosphor; the tint filter then
        recolors it on-palette. Position comes from how far into the sweep we are."""
        roll = self._crt_roll
        bounds = self._view.bounds()
        w = bounds.size.width
        h = bounds.size.height
        if w <= 0 or h <= 0:
            return
        progress = (now - roll["start"]) / max(roll["duration"], 1e-6)
        progress = 0.0 if progress < 0.0 else 1.0 if progress > 1.0 else progress
        top = _roll_band_top(progress, h, _ROLL_BAND_H)
        # Bright, phosphor-leaning color; the CIColorMonochrome tint (if any) maps
        # it back onto the theme hue, so a neutral bright works for tinted or not.
        r, g, b = (min(255, c + 110) / 255.0 for c in (effect.tint or (170, 255, 185))[:3])
        peak = effect.roll * 0.9
        # Three stops reproduce the piecewise-linear falloff exactly: 0 at the top
        # edge, peak at _ROLL_PEAK, 0 at the bottom edge — a clean two-segment ramp.
        gradient = CGGradientCreateWithColorComponents(
            _DEVICE_RGB,
            [r, g, b, 0.0, r, g, b, peak, r, g, b, 0.0],
            [0.0, _ROLL_PEAK, 1.0],
            3,
        )
        cg = NSGraphicsContext.currentContext().CGContext()
        CGContextSaveGState(cg)
        CGContextDrawLinearGradient(
            cg, gradient, (0.0, top), (0.0, top + _ROLL_BAND_H), 0
        )
        CGContextRestoreGState(cg)

    def _begin_group_render(self, key: int, rect: Any, now: float) -> tuple:
        """Set up the group's transition effect (alpha or CTM transform).
        Returns state for _end_group_render."""
        animation = self._animations.get(key)
        if animation is None or not _HAS_QUARTZ:
            return (None, rect, False, False)
        cg = NSGraphicsContext.currentContext().CGContext()
        eased = animation.eased(now)
        if animation.kind == "fade":
            CGContextSaveGState(cg)
            CGContextSetAlpha(cg, eased)
            CGContextBeginTransparencyLayer(cg, None)
            return (animation, rect, True, True)
        if animation.kind == "slide":
            # Position: an offset (in base units) interpolated against the rest
            # place. Slide in decays it to zero (1 - p); slide out ("out") grows it
            # from zero (a drawer sliding back off its edge). Linear (constant
            # velocity), matching the Panel's geometry transitions, so a slide
            # reads the same on GUI and TUI.
            lin = animation.progress(now)
            slide_p = lin if animation.hints.get("out") else (1.0 - lin)
            dx = animation.hints.get("from_dx", 0.0) * self._base_w * slide_p
            dy = animation.hints.get("from_dy", 2.0) * self._base_h * slide_p
            CGContextSaveGState(cg)
            CGContextTranslateCTM(cg, dx, dy)
            return (animation, rect, True, False)
        if animation.kind == "scale" and rect is not None:
            # Size: grow from from_scale to full size around the rect center.
            from_scale = animation.hints.get("from_scale", 0.7)
            scale = from_scale + (1.0 - from_scale) * eased
            cx = (rect.x + rect.w / 2.0) * self._base_w
            cy = (rect.y + rect.h / 2.0) * self._base_h
            CGContextSaveGState(cg)
            # A scale may opt into fading in as it grows ("fade": True) — the
            # "materialize" look a modal wants, where scaling alone reads as a
            # fully-opaque box lurching to size. It needs the SAME offscreen
            # transparency layer a plain fade does (see animation_compositing.md):
            # compositing the group once at the group alpha, rather than
            # multiplying each primitive's alpha, which double-blends where the
            # dialog's own fills overlap. Opened before the CTM so the layer
            # covers the transformed content.
            fade = bool(animation.hints.get("fade", False))
            if fade:
                CGContextSetAlpha(cg, eased)
                CGContextBeginTransparencyLayer(cg, None)
            CGContextTranslateCTM(cg, cx, cy)
            CGContextScaleCTM(cg, scale, scale)
            CGContextTranslateCTM(cg, -cx, -cy)
            return (animation, rect, True, fade)
        # "highlight" draws its color overlay at group end; unknown kinds no-op.
        return (animation, rect, False, False)

    def _end_group_render(self, state: tuple, now: float) -> None:
        animation, rect, gstate_saved, layer_opened = state
        if not _HAS_QUARTZ:
            return
        if layer_opened or gstate_saved:
            cg = NSGraphicsContext.currentContext().CGContext()
            if layer_opened:
                CGContextEndTransparencyLayer(cg)
            if gstate_saved:
                CGContextRestoreGState(cg)
        if animation is not None and animation.kind == "highlight" and rect is not None:
            # Color: a tint over the widget that fades back to normal.
            strength = animation.hints.get("strength", 0.45)
            color = animation.hints.get("color", (229, 229, 16))
            alpha = strength * (1.0 - animation.eased(now))
            if alpha > 0:
                _ns_color(color, alpha).setFill()
                NSRectFillUsingOperation(
                    self._unit_rect(rect.x, rect.y, rect.w, rect.h),
                    NSCompositingOperationSourceOver,
                )

    def _unit_rect(self, x: int, y: int, w_units: int, h_units: int):
        return NSMakeRect(
            x * self._base_w, y * self._base_h, w_units * self._base_w, h_units * self._base_h
        )

    def _drop_shadow_ns(self) -> "Any | None":
        """The NSShadow for the active effect's ``drop_shadow`` (a soft shadow under
        the drawn glyphs — the reflective-LCD "segments cast a shadow" look), or
        ``None`` when the effect has none. Applied only to *text* (the ink), as an
        NSAttributedString attribute — NOT as a whole-context shadow: a context
        shadow also shadows the background/row/selection *fills*, and a filled rect's
        rectangular shadow reads as ugly boxes/underlines behind the text. So the
        drop shadow is scoped to the ink here, in the one place that draws it. Built
        once per effect and cached; the offset is down-right (negative y is down in
        this view, as for the popup shadow) and grows with the strength."""
        eff = self._post_effect
        strength = getattr(eff, "drop_shadow", 0.0) if eff is not None else 0.0
        if strength <= 0:
            return None
        if self._drop_shadow_obj is None:
            depth = 0.6 + strength * 1.4  # points
            sh = NSShadow.alloc().init()
            sh.setShadowOffset_(NSMakeSize(depth * 0.5, -depth))
            sh.setShadowBlurRadius_(strength * 1.6)
            sh.setShadowColor_(_ns_color((0, 0, 0), min(0.6, 0.2 + strength * 0.45)))
            self._drop_shadow_obj = sh
        return self._drop_shadow_obj

    def _render_text(self, x: int, y: int, text: str, style: Style) -> None:
        fg = style.fg or _DEFAULT_FG
        bg = style.bg
        if style.attr & TextAttribute.REVERSE:
            fg, bg = (bg or _DEFAULT_BG), (style.fg or _DEFAULT_FG)
        alpha = 0.55 if style.attr & TextAttribute.DIM else 1.0

        # A per-Style font that names a family, size, or is non-monospace flows
        # by its natural advances; the base grid font (font=None) AND an unsized,
        # unnamed monospace request — the very face the base unit was derived
        # from — tile the grid one glyph per base unit column. Routing the latter
        # through the flow path (its natural advance is a hair under the
        # rounded-up base unit) drifts it left of the grid and made LogView's
        # column-counted wrap fall short of the pane, wrapping early (#62).
        if not _is_grid_font(style.font):
            self._render_flow_text(x, y, text, style, fg, bg, alpha)
            return

        underline = bool(style.attr & TextAttribute.UNDERLINE)
        thick = bool(style.attr & TextAttribute.UNDERLINE_THICK)
        strike = bool(style.attr & TextAttribute.STRIKETHROUGH)
        # font=None uses the prebuilt NORMAL/BOLD base faces; a grid-aligned
        # per-Style monospace font resolves to the same face honoring its own
        # weight/slant (a monospaced bold/italic keeps the base advance, so the
        # column grid and the kern below stay exact).
        if style.font is None:
            weight = (
                TextAttribute.BOLD if style.attr & TextAttribute.BOLD else TextAttribute.NORMAL
            )
            ns_font = self._fonts[weight]
        else:
            ns_font = self._resolve_style_font(style)
        attrs = {
            NSFontAttributeName: ns_font,
            NSForegroundColorAttributeName: _ns_color(fg, alpha),
        }
        if underline:
            attrs[NSUnderlineStyleAttributeName] = (
                NSUnderlineStyleThick if thick else NSUnderlineStyleSingle
            )
        if strike:
            attrs[NSStrikethroughStyleAttributeName] = NSUnderlineStyleSingle
        shadow = self._drop_shadow_ns()
        if shadow is not None:
            attrs[NSShadowAttributeName] = shadow

        # Lock each glyph to its base unit column without drawing it in its own
        # call. The base unit width is the monospaced advance rounded *up*, so a
        # run drawn as one string would drift left a fraction of a pixel per
        # glyph; a constant kern of (base_w - grid_advance) added after each
        # glyph cancels that drift exactly, so columns stay aligned and the clip
        # rect still trims the boundary glyph at the pane edge. Drawing each
        # contiguous single-width segment in one call (rather than per glyph)
        # also collapses the draw count; see _attr_string for why these go
        # through NSAttributedString rather than -[NSString draw…WithAttributes:].
        # Wide glyphs (East Asian, display width 2) break a segment: they get a
        # 2-cell slot drawn on their own so the next glyph does not overlap.
        runs = _glyph_runs(text)
        widths = [max(1, display_width(glyph)) for glyph in runs]
        total = sum(widths)
        if bg is not None and not is_transparent(bg):
            _ns_color(bg).setFill()
            NSRectFill(self._unit_rect(x, y, total, 1))
        kerned = dict(attrs)
        kerned[NSKernAttributeName] = self._grid_kern
        kerned[NSParagraphStyleAttributeName] = self._grid_para
        # id(ns_font) keys the cache by the exact resolved face, covering both
        # the NORMAL/BOLD base faces and any grid-aligned per-Style monospace.
        sig = (id(ns_font), fg, alpha, underline, thick, strike, shadow is not None)
        col = 0
        i = 0
        n = len(runs)
        while i < n:
            if widths[i] == 1:
                j = i
                while j < n and widths[j] == 1:
                    j += 1
                seg = "".join(runs[i:j])
                self._cached_attr_string(("g1", seg, *sig), seg, kerned).drawAtPoint_(
                    self._unit_rect(x + col, y, 1, 1).origin
                )
                col += j - i
                i = j
            else:
                self._cached_attr_string(("g2", runs[i], *sig), runs[i], attrs).drawAtPoint_(
                    self._unit_rect(x + col, y, widths[i], 1).origin
                )
                col += widths[i]
                i += 1

    def _render_flow_text(
        self, x: int, y: int, text: str, style: Style, fg, bg, alpha: float
    ) -> None:
        """Render text with a real per-Style font: a single run drawn at its
        natural advances from the run origin (no per-glyph grid placement), so
        proportional and sized text flow continuously; the pane clip trims the
        overflow. This is the GUI "no text grid" path (docs/font_system.md §9)."""
        ns_font = self._resolve_style_font(style)
        underline = bool(style.attr & TextAttribute.UNDERLINE)
        thick = bool(style.attr & TextAttribute.UNDERLINE_THICK)
        strike = bool(style.attr & TextAttribute.STRIKETHROUGH)
        attrs = {
            NSFontAttributeName: ns_font,
            NSForegroundColorAttributeName: _ns_color(fg, alpha),
            NSParagraphStyleAttributeName: self._line_height_para(ns_font),
        }
        if underline:
            attrs[NSUnderlineStyleAttributeName] = (
                NSUnderlineStyleThick if thick else NSUnderlineStyleSingle
            )
        if strike:
            attrs[NSStrikethroughStyleAttributeName] = NSUnderlineStyleSingle
        shadow = self._drop_shadow_ns()
        if shadow is not None:
            attrs[NSShadowAttributeName] = shadow
        key = ("f", text, id(ns_font), tuple(fg) if fg else None, alpha,
               underline, thick, strike, shadow is not None)
        ns_text = self._cached_attr_string(key, text, attrs)
        origin = self._unit_rect(x, y, 1, 1).origin
        if bg is not None and not is_transparent(bg):
            width = ns_text.size().width
            _ns_color(bg).setFill()
            NSRectFill(NSMakeRect(origin.x, origin.y, width, self._base_h))
        ns_text.drawAtPoint_(origin)

    def _render_box(
        self, x: int, y: int, w: int, h: int, style: Style, hints: dict[str, Any]
    ) -> None:
        rect = self._unit_rect(x, y, w, h)
        if hints.get("fill"):
            _fill_rect(rect, style.bg or _DEFAULT_BG)
        # Inset by half the line width so the 1px stroke lands on the pixel grid.
        rect = NSMakeRect(
            rect.origin.x + 0.5, rect.origin.y + 0.5, rect.size.width - 1, rect.size.height - 1
        )
        _ns_color(style.fg or _DEFAULT_FG).setStroke()
        path = NSBezierPath.bezierPathWithRect_(rect)
        path.setLineWidth_(1.0)
        path.stroke()

    def _ui_fill_alpha(self) -> float:
        """Opacity for UI *surface* fills (pane / row backgrounds). ``1.0`` in
        normal rendering; when the surface opacity is lowered (set_surface_opacity,
        so a wallpaper shows through) the surfaces composite translucently at that
        value. Text, strokes, and framed dialog boxes are unaffected — only the flat
        surface fills routed through _render_fill — so the UI stays legible. Fills
        inside a reveal-exempt (opaque) group — an overlay layer such as a
        full-window viewer or a modal dialog — stay opaque so they occlude the base
        UI instead of dissolving it into view."""
        if self._reveal_exempt_depth > 0:
            return 1.0
        return self._surface_opacity

    def _render_fill(self, x: float, y: float, w: float, h: float, style: Style) -> None:
        rect = self._unit_rect(x, y, w, h)
        color = style.bg or _DEFAULT_BG
        alpha = self._ui_fill_alpha()
        if alpha >= 1.0:
            _fill_rect(rect, color)  # opaque fast path (also handles RGBA colors)
            return
        # Reveal the 3D background: composite the surface over what is already
        # drawn (the scene) at the reduced opacity.
        _ns_color(color, alpha=alpha).setFill()
        NSRectFillUsingOperation(rect, NSCompositingOperationSourceOver)

    def _rounded_path(self, rect, r: float, corners: tuple[str, ...] | None):
        """An NSBezierPath around ``rect`` with corner radius ``r`` on the named
        ``corners`` (``"tl"``/``"tr"``/``"br"``/``"bl"``, screen-oriented; the
        view is flipped so top = smaller y). ``corners is None`` rounds all four,
        matching the stock rounded-rect path."""
        if corners is None:
            return NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, r, r)
        minx, miny = rect.origin.x, rect.origin.y
        maxx, maxy = minx + rect.size.width, miny + rect.size.height

        def rad(name: str) -> float:
            return r if name in corners else 0.0

        path = NSBezierPath.bezierPath()
        path.moveToPoint_((minx + rad("tl"), miny))
        path.lineToPoint_((maxx - rad("tr"), miny))
        if rad("tr"):
            path.appendBezierPathWithArcFromPoint_toPoint_radius_((maxx, miny), (maxx, maxy), r)
        path.lineToPoint_((maxx, maxy - rad("br")))
        if rad("br"):
            path.appendBezierPathWithArcFromPoint_toPoint_radius_((maxx, maxy), (minx, maxy), r)
        path.lineToPoint_((minx + rad("bl"), maxy))
        if rad("bl"):
            path.appendBezierPathWithArcFromPoint_toPoint_radius_((minx, maxy), (minx, miny), r)
        path.lineToPoint_((minx, miny + rad("tl")))
        if rad("tl"):
            path.appendBezierPathWithArcFromPoint_toPoint_radius_((minx, miny), (maxx, miny), r)
        path.closePath()
        return path

    def _render_round_rect(
        self, x: float, y: float, w: float, h: float, radius, style: Style, hints: dict[str, Any]
    ) -> None:
        rect = self._unit_rect(x, y, w, h)
        r = radius if radius is not None else min(rect.size.width, rect.size.height) / 2.0
        r = max(0.0, min(r, rect.size.width / 2.0, rect.size.height / 2.0))
        # A subset of corners to round (the rest stay square), e.g. a Drawer's
        # inner edge; absent means a uniformly rounded rect.
        corners = hints.get("corners")
        if hints.get("fill") and style.bg is not None:
            # NSBezierPath.fill composites source-over by default, so an RGBA
            # fill (translucent control face) blends over what is already drawn.
            _ns_color(style.bg).setFill()
            self._rounded_path(rect, r, corners).fill()
        if style.fg is not None:
            line = float(hints.get("line_width", 1.0))
            # Inset by half the line width so the stroke lands on the pixel grid.
            inset = NSMakeRect(
                rect.origin.x + line / 2.0, rect.origin.y + line / 2.0,
                rect.size.width - line, rect.size.height - line,
            )
            ir = max(0.0, min(r, inset.size.width / 2.0, inset.size.height / 2.0))
            _ns_color(style.fg).setStroke()
            path = self._rounded_path(inset, ir, corners)
            path.setLineWidth_(line)
            path.stroke()

    def _render_check(self, x: float, y: float, w: float, h: float, style: Style) -> None:
        rect = self._unit_rect(x, y, w, h)
        ox, oy = rect.origin.x, rect.origin.y
        pw, ph = rect.size.width, rect.size.height
        # A check stroked across the box (view is flipped: larger y is lower).
        path = NSBezierPath.bezierPath()
        path.moveToPoint_((ox + pw * 0.24, oy + ph * 0.52))
        path.lineToPoint_((ox + pw * 0.42, oy + ph * 0.70))
        path.lineToPoint_((ox + pw * 0.78, oy + ph * 0.30))
        path.setLineWidth_(max(1.4, ph * 0.13))
        path.setLineCapStyle_(1)   # NSLineCapStyleRound
        path.setLineJoinStyle_(1)  # NSLineJoinStyleRound
        _ns_color(style.fg or _DEFAULT_FG).setStroke()
        path.stroke()

    def _render_chevron(
        self, x: float, y: float, w: float, h: float, expanded: bool, style: Style
    ) -> None:
        rect = self._unit_rect(x, y, w, h)
        cx = rect.origin.x + rect.size.width / 2.0
        cy = rect.origin.y + rect.size.height / 2.0
        # Both arms run at 45° from the apex (equal x/y reach in device pixels),
        # so the two arms meet at exactly 90°. `k` is the short half-extent,
        # sized to fill the box (capped by the smaller of width/height); the mark
        # keeps the same size when it rotates open, pivoting about the center.
        # View is flipped (larger y is lower).
        k = min(rect.size.width, rect.size.height) * 0.24
        path = NSBezierPath.bezierPath()
        if expanded:   # ⌄ apex at bottom-center, arms up-left / up-right
            path.moveToPoint_((cx - 2 * k, cy - k))
            path.lineToPoint_((cx,         cy + k))
            path.lineToPoint_((cx + 2 * k, cy - k))
        else:          # › apex at right-center, arms up-left / down-left
            path.moveToPoint_((cx - k, cy - 2 * k))
            path.lineToPoint_((cx + k, cy))
            path.lineToPoint_((cx - k, cy + 2 * k))
        path.setLineWidth_(max(1.4, k * 0.5))
        path.setLineCapStyle_(1)   # NSLineCapStyleRound
        path.setLineJoinStyle_(1)  # NSLineJoinStyleRound
        _ns_color(style.fg or _DEFAULT_FG).setStroke()
        path.stroke()

    def _render_dim(self, x: int, y: int, w: int, h: int) -> None:
        # Real transparency: a translucent dark overlay on whatever was
        # already drawn below.
        _ns_color((0, 0, 0), 0.45).setFill()
        NSRectFillUsingOperation(self._unit_rect(x, y, w, h), NSCompositingOperationSourceOver)

    def _render_shadow(
        self, x: int, y: int, w: int, h: int,
        radius: float | None = None, corners: tuple[str, ...] | None = None,
        bg: tuple[int, ...] | None = None,
    ) -> None:
        # Fill the layer's silhouette while an NSShadow is active; the blurred
        # shadow remains visible around the layer content drawn on top. A rounded
        # panel (a Drawer) passes a radius and a corner subset so the shadow
        # follows the rounded outline. The caster is filled with the layer's own
        # surface color (``bg``), not the window-dark default: the content on top
        # snaps to whole base units and can leave a sub-unit sliver of the caster
        # exposed at the edge, which reads as a hard dark fringe (a "TUI" shadow)
        # if the caster does not match the surface. ``bg`` is None only for a
        # backend/caller that predates the themed caster; keep the old default.
        NSGraphicsContext.saveGraphicsState()
        shadow = NSShadow.alloc().init()
        # The view is flipped (top-left origin), so a positive Y offset casts the
        # shadow upward. macOS panels/menus drop their shadow straight down with no
        # horizontal bias, so use a negative Y offset and zero X. Mimic the native
        # look: a wide, soft blur at low opacity rather than a tight dark edge.
        shadow.setShadowOffset_(NSMakeSize(0.0, -8.0))
        shadow.setShadowBlurRadius_(24.0)
        shadow.setShadowColor_(_ns_color((0, 0, 0), 0.33))
        shadow.set()
        _ns_color(bg if bg is not None else _DEFAULT_BG).setFill()
        rect = self._unit_rect(x, y, w, h)
        if radius:
            r = max(0.0, min(radius, rect.size.width / 2.0, rect.size.height / 2.0))
            self._rounded_path(rect, r, corners).fill()
        else:
            # Source-over, not bare NSRectFill: NSRectFill composites with COPY,
            # which replaces pixels directly and so casts no shadow — the blurred
            # edge composited against the empty (white) backing showed as a white
            # halo instead of a soft dark shadow (#48). Source-over blends the
            # silhouette and lets the active NSShadow render, matching the
            # rounded path above.
            NSRectFillUsingOperation(rect, NSCompositingOperationSourceOver)
        NSGraphicsContext.restoreGraphicsState()

    def _render_scrollbar(
        self, x: int, y: int, h: int, pos: float, ratio: float, style: Style,
        orientation: str = "vertical",
    ) -> None:
        track_color = _ns_color(style.bg or (60, 60, 60))
        thumb_color = _ns_color(style.fg or (150, 150, 150))
        if orientation == "horizontal":
            # Match the vertical bar's px thickness (one base-unit *width*); a full
            # base-unit row would be base_h tall — too thick. Centered in the row.
            thick = self._base_w
            top = y * self._base_h + (self._base_h - thick) / 2.0
            track_w = h * self._base_w
            left = x * self._base_w
            track_color.setFill()
            NSRectFill(NSMakeRect(left, top, track_w, thick))
            thumb_w = max(2.0, track_w * ratio)
            thumb_x = left + (track_w - thumb_w) * pos
            thumb_color.setFill()
            NSRectFill(NSMakeRect(thumb_x, top, thumb_w, thick))
            return
        track = self._unit_rect(x, y, 1, h)
        track_color.setFill()
        NSRectFill(track)
        # Pixel-level thumb: size and position are computed in device pixels
        # (not snapped to whole base units), so the scroll position is exact.
        track_h = track.size.height
        thumb_h = max(2.0, track_h * ratio)
        thumb_y = track.origin.y + (track_h - thumb_h) * pos
        thumb_color.setFill()
        NSRectFill(NSMakeRect(track.origin.x, thumb_y, track.size.width, thumb_h))

    def _render_image(self, x: int, y: int, path: str, hints: dict[str, Any]) -> None:
        image = self._image_cache.get(path)
        if image is None:
            image = NSImage.alloc().initWithContentsOfFile_(path)
            if image is None:
                return
            self._image_cache[path] = image
        iw, ih = image.size().width, image.size().height
        w_units = hints.get("w", max(1, round(iw / self._base_w)))
        h_units = hints.get("h", max(1, round(ih / self._base_h)))
        target = self._unit_rect(x, y, w_units, h_units)
        dest, source = self._fit_rects(hints.get("fit", "fill"), target, iw, ih)
        # SourceOver already honors the image's own per-pixel alpha (an RGBA
        # PNG); the "alpha" hint is an extra global opacity (the fraction).
        opacity = float(hints.get("alpha", 1.0))
        image.drawInRect_fromRect_operation_fraction_respectFlipped_hints_(
            dest,
            source,
            2,  # NSCompositingOperationSourceOver
            opacity,
            True,
            None,
        )

    def _fit_rects(self, fit: str, target, iw: float, ih: float):
        """Destination and source rects for an object-fit. The geometry lives
        in puikit.image (shared with the TUI placeholder); here it is mapped
        onto the pixel target rect. A zero source rect means the whole image."""
        from ..image import CONTAIN, COVER, contain_box, cover_source

        whole = NSMakeRect(0, 0, 0, 0)
        tw, th = target.size.width, target.size.height
        if fit == CONTAIN:
            ox, oy, bw, bh = contain_box(tw, th, iw, ih)
            return NSMakeRect(target.origin.x + ox, target.origin.y + oy, bw, bh), whole
        if fit == COVER:
            sx, sy, sw, sh = cover_source(iw, ih, tw, th)
            return target, NSMakeRect(sx, sy, sw, sh)
        # FILL (and the aspect modes, whose rect is already aspect-correct):
        # stretch the whole image across the whole target.
        return target, whole

    # --- event loop ----------------------------------------------------------

    def begin_text_input(self) -> None:
        """A text widget took focus: route keys through the OS text-input system
        (keyDown reads this flag) and activate the input context so IME works."""
        self._text_input_active = True
        if self._view is not None:
            self._view._sync_input_context()  # flag True -> activate

    def end_text_input(self) -> None:
        """Focus left the text widget: drop back to plain command KEY events and
        tear down any in-progress composition so it can't leak into the next
        field."""
        self._text_input_active = False
        if self._view is not None:
            if self._view.hasMarkedText():
                self._view.unmarkText()
            # Read the context directly: inputContext() now reports nil (the flag
            # just went False), but we still need to tear down any composition.
            ctx = getattr(self._view, "_input_context", None)
            if ctx is not None:
                ctx.discardMarkedText()
            self._view._sync_input_context()  # flag False -> deactivate

    def request_text_input(self, x: float, y: float, hints: dict[str, Any] | None = None) -> None:
        """Record where the focused text widget's caret is (base units, possibly
        fractional), so the IME candidate window (firstRectForCharacterRange)
        appears next to it — aligned with the field's bottom edge even when the
        field sits at a fractional row origin."""
        moved = (float(x), float(y)) != self._input_caret
        self._input_caret = (float(x), float(y))
        char_xs = (hints or {}).get("ime_char_xs")
        self._input_char_xs = [float(v) for v in char_xs] if char_xs else None
        if self._view is not None:
            ctx = self._view.inputContext()
            if ctx is not None:
                ctx.invalidateCharacterCoordinates()
                # macOS is a *pull* model: the IME re-reads firstRectForCharacterRange
                # only when it decides its coordinates are stale. When the anchor
                # moves mid-composition — the user cycles conversion clauses with
                # left/right, so the widget re-reports the caret from *inside* the
                # setMarkedText: callback that is delivering that change — the
                # invalidate above lands while the IME is still mid-update and is
                # swallowed: it has already positioned its candidate panel for this
                # keystroke and won't re-query until its own geometry next changes
                # (pressing space to cycle candidates resizes the list, which does).
                # So the panel lags a keystroke behind the selected clause. Re-issue
                # the invalidate once the callback has unwound, on the next run-loop
                # turn, so the panel follows the clause immediately. Gated on an
                # actual move so the per-frame caret re-assertion (caret blink,
                # raw kana typing with a fixed anchor) schedules no needless work.
                if moved:
                    AppHelper.callAfter(self._reinvalidate_ime_coordinates)

    def _ime_caret_x(self, location: int) -> float:
        """Base-unit x of the caret rect for composition offset ``location`` (the
        location of a firstRectForCharacterRange: query). Uses the per-character
        layout the widget reported when it's available — so the candidate window
        anchors under the exact clause the IME is converting — and falls back to
        the single reported caret x otherwise (no composition, or a location the
        IME never gave a layout for)."""
        xs = self._input_char_xs
        if xs and location != NSNotFound:
            return xs[max(0, min(int(location), len(xs) - 1))]
        return self._input_caret[0]

    def _reinvalidate_ime_coordinates(self) -> None:
        """Deferred companion to ``request_text_input``'s invalidate (see there):
        forces the IME to re-query the caret rect on a later run-loop turn. Runs
        after the composition callback has unwound, so guard against teardown."""
        if self._view is None:
            return
        ctx = self._view.inputContext()
        if ctx is not None:
            ctx.invalidateCharacterCoordinates()

    # --- clipboard -----------------------------------------------------------

    def get_clipboard(self) -> str:
        """Plain-text contents of the real system pasteboard."""
        text = NSPasteboard.generalPasteboard().stringForType_(NSPasteboardTypeString)
        return str(text) if text is not None else ""

    def set_clipboard(self, text: str) -> None:
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        pb.setString_forType_(text, NSPasteboardTypeString)

    def set_clipboard_rich(
        self, text: str, *, html: str | None = None, rtf: str | None = None
    ) -> None:
        """Write plain ``text`` and any ``rtf`` / ``html`` reps to the system
        pasteboard as separate types, so a rich editor pastes with formatting
        while a plain target reads the string. Types are declared richest-first
        (RTF, then HTML, then plain); a reader picks the best it supports."""
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        types = []
        if rtf is not None:
            types.append(NSPasteboardTypeRTF)
        if html is not None:
            types.append(NSPasteboardTypeHTML)
        types.append(NSPasteboardTypeString)
        pb.declareTypes_owner_(types, None)
        if rtf is not None:
            pb.setString_forType_(rtf, NSPasteboardTypeRTF)
        if html is not None:
            pb.setString_forType_(html, NSPasteboardTypeHTML)
        pb.setString_forType_(text, NSPasteboardTypeString)

    # --- pointer shape -------------------------------------------------------

    # CSS/X cursor name -> NSCursor factory selector. Names AppKit has no cursor
    # for (e.g. "wait") fall through to the default arrow rather than guess.
    _CURSORS = {
        "text": "IBeamCursor",
        "vertical-text": "IBeamCursorForVerticalLayout",
        "pointer": "pointingHandCursor",
        "crosshair": "crosshairCursor",
        "not-allowed": "operationNotAllowedCursor",
        "no-drop": "operationNotAllowedCursor",
        "grab": "openHandCursor",
        "grabbing": "closedHandCursor",
        "context-menu": "contextualMenuCursor",
        "col-resize": "resizeLeftRightCursor",
        "ew-resize": "resizeLeftRightCursor",
        "row-resize": "resizeUpDownCursor",
        "ns-resize": "resizeUpDownCursor",
    }

    def set_pointer_shape(self, shape: str | None) -> None:
        """Set the real OS pointer (NSCursor) under the mouse. ``shape`` is a
        CSS/X cursor name resolved via ``_CURSORS``; ``None`` (or an unknown
        name) resets to the default arrow. The resolved cursor is applied now
        and re-asserted from the view's ``cursorUpdate_`` so AppKit's own cursor
        passes keep it. The Panel gates this on the ``pointer_shape``
        capability."""
        if shape == self._pointer_shape:
            return
        self._pointer_shape = shape
        selector = self._CURSORS.get(shape) if shape else None
        self._pointer_cursor = getattr(NSCursor, selector)() if selector else None
        # The named cursors are only available once NSApplication is up; guard
        # so a missing one resets to the arrow (or no-ops) rather than raising.
        cursor = self._pointer_cursor or NSCursor.arrowCursor()
        if cursor is not None:
            cursor.set()

    def set_post_effect(self, effect: Any | None) -> None:
        """Composite a CRT / phosphor effect over the whole window, or clear it
        with ``None`` (see ``puikit.posteffect.PostEffect``). Realized as Core
        Image *content filters* on the layer-backed view: the frame the display
        list rasterizes is run through the filter chain by AppKit before it hits
        the screen, so nothing in the render path changes. Cheap to toggle — the
        app calls this once when a theme recommends an effect. Stored so open()
        can re-attach it, and the layer keeps it across window resizes."""
        self._post_effect = None if (effect is None or effect.is_noop) else effect
        self._drop_shadow_obj = None  # rebuilt lazily for the new effect
        self._apply_post_effect()

    def set_background(self, background: Any | None) -> None:
        """Set the background behind the display list (see ``Backend.set_background``):
        a ``Shader`` (GPU fragment shader), a ``Wallpaper`` (static image), or
        ``None`` (solid). A shader registers a permanent redraw tick; a
        wallpaper draws once per frame (no
        tick). Idempotent to re-set (e.g. on a theme switch): a no-op / cleared
        background drops any tick and the next frame paints without it. Survives
        window resizes — the background re-fits to the live bounds."""
        self._background = None if (background is None or background.is_noop) else background
        if isinstance(self._background, Shader):
            # A new background starts from the beginning of its own clock, at full
            # rate — switching theme is itself user activity, so it should not
            # arrive mid-coast.
            self._bg_clock = 0.0
            self._bg_rate = 1.0
            self._bg_running = False
            self._ensure_background_ticker()
        # A shader draws on the GPU into its own layer beneath the UI, so it needs
        # that layer attached (and the UI view made transparent so it shows
        # through); anything else needs it gone so the UI is opaque again.
        self._sync_shader_layer()
        if self._view is not None:
            self._view.setNeedsDisplay_(True)

    def _sync_shader_layer(self) -> None:
        """Attach or detach the GPU background layer to match the active background.

        Attaching is lazy — the Metal device, renderer and layer are created the
        first time a shader background is actually set, so an app that never uses
        one pays nothing and a machine without Metal simply never gets here. On
        detach the layer is removed and the UI view goes back to being opaque, so
        the ordinary render path is byte-for-byte what it was before.
        """
        shader = self._background if isinstance(self._background, Shader) else None
        if shader is None:
            if self._metal_layer is not None:
                self._metal_layer.removeFromSuperlayer()
                self._metal_layer = None
                # Also give up layer backing. Hosting the GPU layer requires it,
                # but leaving it on afterwards makes every *other* background pay
                # whatever it costs for the rest of the session — and quietly
                # skews any before/after comparison between kinds.
                if self._container is not None:
                    self._container.setWantsLayer_(False)
            return
        if not _HAS_METAL:
            return
        if self._metal is None:
            self._metal = MetalBackground()
            if not self._metal.available:
                return
        if not self._metal.set_shader(shader):
            # Compile failure: report once and leave the layer off, so the frame
            # shows the plain backdrop rather than a stale or garbage scene.
            sys.stderr.write(f"[puikit] background shader failed to compile: "
                             f"{self._metal.error}\n")
            if self._metal_layer is not None:
                self._metal_layer.removeFromSuperlayer()
                self._metal_layer = None
            return
        if self._metal_layer is None and self._container is not None:
            layer = CAMetalLayer.layer()
            layer.setDevice_(self._metal.device)
            layer.setPixelFormat_(_METAL_PIXEL_FORMAT)
            layer.setFramebufferOnly_(True)
            # The compositor, not a timer, decides when this is presented; drawing
            # is driven from the same frame tick as the UI so the two stay in step.
            layer.setPresentsWithTransaction_(False)
            self._container.setWantsLayer_(True)
            self._container.layer().insertSublayer_atIndex_(layer, 0)
            self._metal_layer = layer
        self._fit_shader_layer()

    def _fit_shader_layer(self) -> None:
        """Match the GPU layer to the container's size in *pixels* (the layer is
        addressed in device pixels, the view in points), so the shader gets the
        real resolution on a Retina display and re-fits across a resize."""
        if self._metal_layer is None or self._container is None:
            return
        bounds = self._container.bounds()
        scale = self._window.backingScaleFactor() if self._window is not None else 1.0
        # A shader's cost is per pixel, so a scene that declares itself soft enough
        # renders below native resolution and lets the compositor scale it up — on a
        # Retina display that is the difference between affordable and not.
        shader_scale = getattr(self._background, "resolution_scale", 1.0)
        self._metal_layer.setFrame_(bounds)
        self._metal_layer.setContentsScale_(scale)
        self._metal_layer.setDrawableSize_(
            NSMakeSize(max(1.0, bounds.size.width * scale * shader_scale),
                       max(1.0, bounds.size.height * scale * shader_scale)))

    def set_surface_opacity(self, opacity: float) -> None:
        """Set the opacity of UI surface fills so a wallpaper shows through (see the
        base ``Backend.set_surface_opacity``). Clamped to ``0``..``1`` and read by
        ``_ui_fill_alpha`` per fill. Background-agnostic: it does not start or stop an
        animation's tick (``set_background`` owns that) — a static wallpaper needs no
        tick — so a lone repaint applies the new value."""
        self._surface_opacity = 0.0 if opacity < 0.0 else 1.0 if opacity > 1.0 else float(opacity)
        if self._view is not None:
            self._view.setNeedsDisplay_(True)

    @property
    def has_wallpaper(self) -> bool:
        """True while any background (animation or image) is set (see
        ``Backend.has_wallpaper``) — a ``reveal_mode="transparent"`` pane then drops
        its fill regardless of the surface opacity, so the background shows at full
        strength even at ``opacity == 1``."""
        return self._background is not None

    def _background_tick(self) -> bool:
        """Frame callback for an animation background: request a redraw each frame
        while one is active, else return False to unregister (a bare tick callback
        does not itself trigger a repaint — see _on_animation_tick — so it must ask).
        A static wallpaper never registers this, so it does not spin the timer."""
        background = self._background
        if not isinstance(background, Shader):
            self._bg_running = False
            return False
        now = time.monotonic()
        # Clamp dt so a stalled main thread (a modal drag, a slow directory read)
        # resumes by continuing the motion rather than lurching forward by the
        # whole stall.
        dt = min(max(0.0, now - self._bg_last_tick), 0.25)
        self._bg_last_tick = now

        self._bg_rate = _approach(self._bg_rate, self._bg_target(now), dt,
                                  _BG_RAMP_UP, _BG_RAMP_DOWN)
        eased = _smoothstep(self._bg_rate)
        # The scene's clock advances only by what was actually animated, so it
        # never jumps: park for ten minutes and it resumes exactly where it
        # stopped, rather than teleporting ten minutes into the scene.
        self._bg_clock += dt * eased

        if eased <= _BG_RATE_FLOOR and self._bg_rate <= 0.0:
            # Fully coasted: drop the callback so the frame timer can fall back to
            # the idle rate (or stop). The last frame drawn stays on screen — a
            # shader's layer keeps its last drawable, a segment scene its last
            # paint — so parking freezes the scene rather than clearing it.
            # _dispatch re-arms on the next input.
            self._bg_running = False
            return False

        if isinstance(background, Shader):
            # A shader owns its own layer *behind* the UI, so advancing it does not
            # touch a single UI pixel — draw straight into that layer and leave the
            # view alone. Marking the view dirty instead (as the segment kinds must)
            # would repaint the entire file manager 60 times a second purely to move
            # a background, which measured ~8ms/frame of pure waste.
            self._render_shader(background, now)
        elif self._view is not None:
            # A segment scene is drawn *in* the UI's render pass, so it can only
            # advance by repainting the view.
            self._view.setNeedsDisplay_(True)
        return True

    def _bg_target(self, now: float) -> float:
        """The rate the background should be heading toward: full while the app is
        being used, zero once it is not.

        "Being used" is the window holding focus *and* recent input — an animation
        the user cannot see (another app is front) or has walked away from is pure
        battery drain. Mirrors ``_roll_user_active``.

        Reduced motion targets zero for the same reason idleness does, and reuses
        the same ramp: the scene *decelerates* to a stop and holds its last frame
        as a still backdrop, rather than cutting out mid-motion. An endlessly
        looping ambient scene has no "final frame" to resolve to, so its rest
        state is a still one.
        """
        if self.reduced_motion:
            return 0.0
        window = self._window
        if window is not None and not window.isKeyWindow():
            return 0.0
        return 1.0 if (now - self._last_input_time) < _BG_IDLE_TIMEOUT else 0.0

    def _on_reduced_motion_changed(self) -> None:
        """Re-arm the ticker so the change is acted on now: turning reduced motion
        *on* needs a tick to ramp the scene down (it may have been parked), and
        turning it *off* needs one to ramp back up. Re-applying the post effect
        starts or stops its rolling band to match."""
        self._ensure_background_ticker()
        self._apply_post_effect()

    @property
    def _live_post_effect(self) -> Any | None:
        """The stored effect as it should actually be composited *right now* —
        stripped of its moving parts while reduced motion is on (see
        ``PostEffect.without_motion``). The stored value is left untouched, so
        turning the setting back off restores the full effect without the app
        re-issuing it."""
        effect = self._post_effect
        if effect is None or not self.reduced_motion:
            return effect
        return effect.without_motion()

    def _ensure_background_ticker(self) -> None:
        """Re-arm the background tick if it parked itself while idle. Cheap to call
        on every input: returns immediately once running."""
        if self._bg_running or not isinstance(self._background, Shader):
            return
        self._bg_running = True
        # Reset the tick baseline, or the first dt would be the whole idle stretch.
        self._bg_last_tick = time.monotonic()
        self.request_animation_ticks(self._background_tick)

    def _apply_post_effect(self) -> None:
        """(Re)attach the stored effect's filter chain to the view. Safe to call
        with no view yet (open() calls it again) or without Core Image."""
        self._sync_roll()  # start/stop the rolling-band animation (view-independent)
        if self._view is None or not _HAS_COREIMAGE:
            return
        filters = _build_ci_filters(self._live_post_effect)
        # layerUsesCoreImageFilters lets a layer-backed NSView run CI filters over
        # its own drawn content; contentFilters is that chain (empty = cleared).
        self._view.setWantsLayer_(True)
        self._view.setLayerUsesCoreImageFilters_(True)
        self._view.setContentFilters_(filters)
        self._view.setNeedsDisplay_(True)

    def _sync_roll(self) -> None:
        """Start or stop the rolling-band animation to match the active effect.
        The ticker (see _crt_roll_tick) schedules rolls and drives the per-frame
        redraw while one sweeps; it also parks itself when the app goes idle and
        is re-armed from _dispatch on the next input (see _ensure_roll_ticker)."""
        effect = self._live_post_effect  # no roll at all under reduced motion
        wants = effect is not None and effect.roll > 0
        if wants:
            if self._crt_roll is None:
                self._crt_roll = {"active": False, "start": 0.0, "duration": 0.0,
                                  "next": 0.0}
            self._ensure_roll_ticker()
        else:
            # The tick callback unregisters itself once _crt_roll is None; clearing
            # it also stops _render_into_view from drawing the band.
            self._crt_roll = None

    def _ensure_roll_ticker(self) -> None:
        """Register the roll frame-callback if the effect wants a roll and it is
        not already running. Reschedules the next roll from now, so resuming after
        an idle stretch doesn't fire one instantly. A no-op (returns early) while
        the ticker is already registered, so it's cheap to call on every input."""
        if self._crt_roll is None or self._crt_roll_tick in self._tick_callbacks:
            return
        self._crt_roll["next"] = time.monotonic() + random.uniform(*_ROLL_GAP)
        self.request_animation_ticks(self._crt_roll_tick)

    def _roll_user_active(self, now: float) -> bool:
        """Whether the app is actively in use: its window is key AND the last input
        was recent. Gates *starting* a roll, not finishing one."""
        win = self._window
        if win is None or not win.isKeyWindow():
            return False
        return (now - self._last_input_time) < _ROLL_IDLE_TIMEOUT

    def _crt_roll_tick(self) -> bool:
        """Frame callback: advance an in-flight roll to completion, else start one
        only while the app is actively used. Returns False to unregister — when the
        effect no longer wants a roll, or when idle (to drop the timer; _dispatch
        re-arms on the next input)."""
        roll = self._crt_roll
        if roll is None:
            return False
        now = time.monotonic()
        if roll["active"]:
            # An in-flight roll always finishes its sweep, even if the user goes
            # idle or the window deactivates mid-roll.
            if now - roll["start"] >= roll["duration"]:
                roll["active"] = False
                roll["next"] = now + random.uniform(*_ROLL_GAP)
                if self._view is not None:
                    self._view.setNeedsDisplay_(True)  # one clean frame without the band
            elif self._view is not None:
                self._view.setNeedsDisplay_(True)      # animate the sweep
            return True
        if not self._roll_user_active(now):
            return False  # park the ticker while idle/inactive; _dispatch re-arms
        if now >= roll["next"]:
            roll["active"] = True
            roll["start"] = now
            roll["duration"] = random.uniform(*_ROLL_DUR)
            if self._view is not None:
                self._view.setNeedsDisplay_(True)
        return True

    def _roll_active(self) -> bool:
        return self._crt_roll is not None and self._crt_roll["active"]

    def open_url(self, url: str) -> bool:
        """Open ``url`` via the workspace: an http(s) URL in the default browser,
        anything else (a bare path) as a file URL in its default app."""
        ns = NSURL.URLWithString_(url)
        if ns is None or ns.scheme() is None:
            ns = NSURL.fileURLWithPath_(url)
        return bool(NSWorkspace.sharedWorkspace().openURL_(ns))

    # --- drag source ---------------------------------------------------------

    def begin_file_drag(
        self,
        paths: list[str],
        event: Event | None = None,
        operations: tuple[str, ...] = ("copy",),
        on_complete: Callable[[str], None] | None = None,
    ) -> bool:
        """Begin a native file-drag session from the content view.

        The view adopts ``NSDraggingSource``; each path becomes an
        ``NSDraggingItem`` whose pasteboard writer is a file ``NSURL`` (so the
        receiver gets real files), imaged with the file's Finder icon. The
        session starts from the live mouse ``NSEvent`` last seen by the view —
        AppKit requires a drag to begin from the originating mouse event, which
        is why we cache it rather than synthesize one from ``event``.

        ``operations`` becomes the session's source mask; the chosen operation
        is reported to ``on_complete`` when the session ends (the view's
        ``draggingSession:endedAtPoint:operation:``). PuiKit never deletes the
        originals on a move — the app does, from ``on_complete``."""
        if self._view is None or not paths:
            return False
        ns_event = getattr(self._view, "_last_mouse_event", None) or NSApp.currentEvent()
        if ns_event is None:
            return False
        origin = self._view.convertPoint_fromView_(ns_event.locationInWindow(), None)
        workspace = NSWorkspace.sharedWorkspace()
        side = 32.0  # icon box in points; stagger stacked items so each shows
        items = []
        for i, path in enumerate(paths):
            path = str(path)
            url = NSURL.fileURLWithPath_(path)
            item = NSDraggingItem.alloc().initWithPasteboardWriter_(url)
            frame = NSMakeRect(
                origin.x - side / 2, origin.y - side / 2 - i * 6.0, side, side
            )
            item.setDraggingFrame_contents_(frame, workspace.iconForFile_(path))
            items.append(item)
        self._view._drag_mask = _drag_mask(operations)
        self._view._drag_on_complete = on_complete
        self._view.beginDraggingSessionWithItems_event_source_(
            items, ns_event, self._view
        )
        return True

    # --- native menus --------------------------------------------------------

    def set_menu_bar(self, menu: Any) -> None:
        from ._macos_menu import install_menu_bar

        self._menu_responder = install_menu_bar(menu, self._title)

    def popup_menu(
        self, menu: Any, x: float, y: float, on_done: Any | None = None
    ) -> None:
        from Foundation import NSMakePoint

        from ._macos_menu import build_popup_menu

        ns_menu, responder = build_popup_menu(menu)
        # `responder` stays referenced here through the synchronous popup loop,
        # so its item callbacks survive the tracking session.
        point = NSMakePoint(x * self._base_w, y * self._base_h)
        ns_menu.popUpMenuPositioningItem_atLocation_inView_(None, point, self._view)
        if on_done is not None:
            on_done()

    def _dispatch(self, event: Event) -> None:
        # Every key/mouse/resize event flows through here — record it as user
        # activity and re-arm the roll ticker if it parked itself while idle.
        self._last_input_time = time.monotonic()
        self._ensure_roll_ticker()
        self._ensure_background_ticker()
        if self._handler is not None:
            self._handler(event)

    def _on_resize(self) -> None:
        # The GPU background layer does not autoresize with the container (it is a
        # CALayer, not a view), so it is refitted here — otherwise the shader would
        # keep rendering at the old resolution and stretch.
        self._fit_shader_layer()
        w, h = self.size
        self._dispatch(Event(type=EventType.RESIZE, hints={"w": w, "h": h}))

    def run_event_loop(self, handler: EventHandler) -> None:
        self._handler = handler
        self._quit_requested = False
        NSApp.run()
        self._handler = None

    def run_event_loop_iteration(self, handler: EventHandler, timeout_ms: int = 0) -> bool:
        if self._quit_requested:
            return False
        self._handler = handler
        until = NSDate.dateWithTimeIntervalSinceNow_(timeout_ms / 1000.0)
        ns_event = NSApp.nextEventMatchingMask_untilDate_inMode_dequeue_(
            NSEventMaskAny, until, NSDefaultRunLoopMode, True
        )
        if ns_event is not None:
            NSApp.sendEvent_(ns_event)
        self._handler = None
        return not self._quit_requested

    def quit(self) -> None:
        self._quit_requested = True
        NSApp.stop_(None)
        # stop() only takes effect after an event is processed; post a wake-up.
        wake = NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
            NSEventTypeApplicationDefined, NSZeroPoint, 0, 0.0, 0, None, 0, 0, 0
        )
        NSApp.postEvent_atStart_(wake, True)
