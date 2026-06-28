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
    NSUnderlineStyleAttributeName,
    NSUnderlineStyleSingle,
    NSView,
    NSWorkspace,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskResizable,
    NSWindowStyleMaskTitled,
)
from Foundation import (
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

from ..backend import Backend, DEFAULT_STYLE, EventHandler, Style, TextAttribute
from ..capability import PROFILE_GUI_DESKTOP, CapabilityProfile
from ..event import Event, EventType
from ..font import Font, FontWeight
from ..text import display_width, glyph_runs as _glyph_runs

try:
    from Quartz import (
        CGContextBeginTransparencyLayer,
        CGContextEndTransparencyLayer,
        CGContextRestoreGState,
        CGContextSaveGState,
        CGContextScaleCTM,
        CGContextSetAlpha,
        CGContextTranslateCTM,
    )

    _HAS_QUARTZ = True
except ImportError:  # animation gracefully degrades to immediate switches
    _HAS_QUARTZ = False

_DEFAULT_FG = (230, 230, 230)
_DEFAULT_BG = (24, 24, 24)

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


def translate_key(characters: str, modifier_flags: int = 0) -> Event | None:
    """Translate a Cocoa key event payload into a PuiKit Event.

    Module-level so the mapping is testable without opening a window."""
    if not characters:
        return None
    modifiers = frozenset(
        name
        for flag, name in [
            (NSEventModifierFlagShift, "shift"),
            (NSEventModifierFlagControl, "ctrl"),
            (NSEventModifierFlagOption, "alt"),
            (NSEventModifierFlagCommand, "cmd"),
        ]
        if modifier_flags & flag
    )
    ch = characters[0]
    code = ord(ch)
    if code in _FUNCTION_KEYS:
        return Event(type=EventType.KEY, key=_FUNCTION_KEYS[code], modifiers=modifiers)
    if ch in _CONTROL_KEYS:
        return Event(type=EventType.KEY, key=_CONTROL_KEYS[ch], modifiers=modifiers)
    if ch.isprintable():
        return Event(type=EventType.KEY, key=ch, char=ch, modifiers=modifiers)
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
        p = self.progress(now)
        return 1.0 - (1.0 - p) ** 2  # ease-out

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
        # Standard Cocoa text input: hand every key to the input context. It
        # either composes (setMarkedText:), commits text (insertText:), or
        # issues a command (doCommandBySelector:) — never a raw key here.
        # Stash the event so doCommandBySelector: can re-translate it (more
        # reliable than NSApp.currentEvent()).
        self._last_key_event = ns_event
        self.interpretKeyEvents_([ns_event])

    def inputContext(self):
        return getattr(self, "_input_context", None)

    def becomeFirstResponder(self):
        result = objc.super(_PuiKitView, self).becomeFirstResponder()
        if result:
            ctx = self.inputContext()
            if ctx is not None:
                ctx.activate()
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
        self.backend._dispatch(
            Event(
                type=EventType.IME_COMPOSITION,
                hints={"preedit": text, "caret": int(caret)},
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
        # Each committed character is delivered as a KEY event with a char, the
        # same shape ordinary typing produces.
        for ch in text:
            self.backend._dispatch(Event(type=EventType.KEY, key=ch, char=ch))

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
            cx, cy = self.backend._input_caret
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

    def scrollWheel_(self, ns_event):
        delta = ns_event.scrollingDeltaY()
        if delta == 0:
            return
        x, y = self._mouse_unit(ns_event)
        scroll = 1 if delta > 0 else -1
        # A trackpad / precise wheel reports pixel-resolution deltas; convert
        # them to base units so widgets can scroll at pixel granularity. A
        # classic line wheel reports whole lines, so it stays a discrete notch
        # (no scroll_units hint) and widgets fall back to ``scroll``.
        hints = {}
        if ns_event.hasPreciseScrollingDeltas():
            hints["scroll_units"] = delta / self.backend._base_h
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
            "drag_and_drop": False,  # drop-IN not wired up yet
            "os_drag_drop": True,    # drag-OUT: native NSDraggingSource (below)
            "ime": True,
            "clipboard_rich": False,
            "native_file_dialog": False,
            "system_tray": False,
            "media_keys": False,
            "gpu_acceleration": False,
        }
    )

    @property
    def capabilities(self) -> CapabilityProfile:
        if _HAS_QUARTZ:
            return self.PROFILE
        # Without Quartz the fade effect cannot be rendered; declare honestly
        # so the Panel falls back to immediate switches.
        return CapabilityProfile({**self.PROFILE, "animation": False})

    def __init__(self, width: int = 100, height: int = 30, title: str = "PuiKit",
                 base_font: Font | None = None):
        self._initial_size = (width, height)
        self._title = title
        # The base font is the monospaced grid font, named with the same Font
        # descriptor a text widget uses. The base unit (the layout's length
        # unit) is derived from this font's glyph box on open (base font ->
        # base unit); per-Style proportional fonts never affect it.
        self._base_font = base_font or Font(size=14.0, monospace=True)
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
        self._tick_callbacks: list[Any] = []
        # On-screen caret position (base units) reported by the focused text
        # widget; positions the IME candidate window.
        self._input_caret: tuple[float, float] = (0.0, 0.0)
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
        self._window.setContentView_(self._view)
        self._window.setAcceptsMouseMovedEvents_(True)
        self._window.makeFirstResponder_(self._view)

        self._delegate = _PuiKitWindowDelegate.alloc().init()
        self._delegate.backend = self
        self._window.setDelegate_(self._delegate)

        self._window.makeKeyAndOrderFront_(None)
        app.activateIgnoringOtherApps_(True)

    def close(self) -> None:
        if self._anim_timer is not None:
            self._anim_timer.invalidate()
            self._anim_timer = None
        self._animations.clear()
        self._tick_callbacks.clear()
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

        ``family`` is honored if installed. With no family, ``monospace``
        chooses between the system monospaced face (the base grid font, which
        must stay fixed-advance to tile the grid) and the proportional system
        UI font (``Font()`` defaults). ``bold``/``italic`` force those traits
        on top of the descriptor.

        A font that names no size inherits the **base font's** size — the same
        size the base grid unit is derived from — so an unsized ``Font()`` (the
        markdown body, a default label) scales with ``base_font`` / --font-size
        instead of pinning to a constant."""
        size = float(font.size) if font.size is not None else self._base_size_pt()
        want_bold = bold or font.bold
        weight = NSFontWeightBold if want_bold else NSFontWeightRegular
        ns = None
        if font.family:
            ns = NSFont.fontWithName_size_(font.family, size)
        if ns is None:
            if font.monospace:
                ns = (
                    NSFont.monospacedSystemFontOfSize_weight_(size, weight)
                    or NSFont.fontWithName_size_("Menlo", size)
                )
            else:
                ns = NSFont.systemFontOfSize_weight_(size, weight)
        # Apply traits a named family does not already encode.
        mask = 0
        if want_bold:
            mask |= NSBoldFontMask
        if italic or font.italic:
            mask |= NSItalicFontMask
        if mask:
            ns = NSFontManager.sharedFontManager().convertFont_toHaveTrait_(ns, mask) or ns
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
        rows land on the device-pixel grid, then expressed in base units."""
        if _is_grid_font(style.font) or not self._base_h:
            return 1.0
        ns_font = self._resolve_style_font(style)
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
    ) -> None:
        self._back.append(("shadow", x, y, w, h, radius, corners))

    def begin_group(self, key: Any, rect: Any = None) -> None:
        self._back.append(("group_begin", id(key), rect))

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

    def _ensure_animation_timer(self) -> None:
        if self._anim_timer is not None:
            return
        self._anim_timer = NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
            1 / 60.0, True, self._on_animation_tick
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
        if self._view is not None:
            self._view.setNeedsDisplay_(True)
        if not self._animations and not self._tick_callbacks:
            # One last redraw at the final state has been requested above.
            timer.invalidate()
            self._anim_timer = None

    def draw_scrollbar(
        self, x: int, y: int, h: int, pos: float, ratio: float, style: Style = DEFAULT_STYLE
    ) -> None:
        self._back.append(("scrollbar", x, y, h, pos, ratio, style))

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
        _ns_color(_DEFAULT_BG).setFill()
        NSRectFill(self._view.bounds())
        now = time.monotonic()
        group_stack: list[tuple] = []  # (anim, rect, gstate_saved, layer_opened)
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
            elif kind == "dim":
                self._render_dim(*command[1:])
            elif kind == "shadow":
                self._render_shadow(*command[1:])
            elif kind == "group_begin":
                group_stack.append(self._begin_group_render(command[1], command[2], now))
            elif kind == "group_end":
                if group_stack:
                    self._end_group_render(group_stack.pop(), now)
            elif kind == "clip_push":
                # NSRectClip works in the current transform, so a clip set
                # inside an animated group travels with the transition.
                NSGraphicsContext.saveGraphicsState()
                NSRectClip(self._unit_rect(*command[1:]))
            elif kind == "clip_pop":
                NSGraphicsContext.restoreGraphicsState()

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
            CGContextTranslateCTM(cg, cx, cy)
            CGContextScaleCTM(cg, scale, scale)
            CGContextTranslateCTM(cg, -cx, -cy)
            return (animation, rect, True, False)
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
            attrs[NSUnderlineStyleAttributeName] = NSUnderlineStyleSingle

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
        if bg is not None:
            _ns_color(bg).setFill()
            NSRectFill(self._unit_rect(x, y, total, 1))
        kerned = dict(attrs)
        kerned[NSKernAttributeName] = self._grid_kern
        # id(ns_font) keys the cache by the exact resolved face, covering both
        # the NORMAL/BOLD base faces and any grid-aligned per-Style monospace.
        sig = (id(ns_font), fg, alpha, underline)
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
        attrs = {
            NSFontAttributeName: ns_font,
            NSForegroundColorAttributeName: _ns_color(fg, alpha),
        }
        if underline:
            attrs[NSUnderlineStyleAttributeName] = NSUnderlineStyleSingle
        key = ("f", text, id(ns_font), tuple(fg) if fg else None, alpha, underline)
        ns_text = self._cached_attr_string(key, text, attrs)
        origin = self._unit_rect(x, y, 1, 1).origin
        if bg is not None:
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

    def _render_fill(self, x: float, y: float, w: float, h: float, style: Style) -> None:
        _fill_rect(self._unit_rect(x, y, w, h), style.bg or _DEFAULT_BG)

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

    def _render_dim(self, x: int, y: int, w: int, h: int) -> None:
        # Real transparency: a translucent dark overlay on whatever was
        # already drawn below.
        _ns_color((0, 0, 0), 0.45).setFill()
        NSRectFillUsingOperation(self._unit_rect(x, y, w, h), NSCompositingOperationSourceOver)

    def _render_shadow(
        self, x: int, y: int, w: int, h: int,
        radius: float | None = None, corners: tuple[str, ...] | None = None,
    ) -> None:
        # Fill the layer's silhouette with the window background while an
        # NSShadow is active; the blurred shadow remains visible around the
        # layer content drawn on top. A rounded panel (a Drawer) passes a radius
        # and a corner subset so the shadow follows the rounded outline.
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
        _ns_color(_DEFAULT_BG).setFill()
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
        self, x: int, y: int, h: int, pos: float, ratio: float, style: Style
    ) -> None:
        track = self._unit_rect(x, y, 1, h)
        _ns_color(style.bg or (60, 60, 60)).setFill()
        NSRectFill(track)
        # Pixel-level thumb: size and position are computed in device pixels
        # (not snapped to whole base units), so the scroll position is exact.
        track_h = track.size.height
        thumb_h = max(2.0, track_h * ratio)
        thumb_y = track.origin.y + (track_h - thumb_h) * pos
        _ns_color(style.fg or (150, 150, 150)).setFill()
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

    def request_text_input(self, x: int, y: int, hints: dict[str, Any] | None = None) -> None:
        """Record where the focused text widget's caret is (base units), so the
        IME candidate window (firstRectForCharacterRange) appears next to it."""
        self._input_caret = (float(x), float(y))
        if self._view is not None:
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
        if self._handler is not None:
            self._handler(event)

    def _on_resize(self) -> None:
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
