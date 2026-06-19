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
from dataclasses import dataclass, field, replace
from typing import Any

from AppKit import (
    NSApp,
    NSApplication,
    NSApplicationActivationPolicyRegular,
    NSBackingStoreBuffered,
    NSBezierPath,
    NSColor,
    NSDate,
    NSDefaultRunLoopMode,
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
    NSCompositingOperationSourceOver,
    NSGraphicsContext,
    NSRectClip,
    NSRectFill,
    NSRectFillUsingOperation,
    NSShadow,
    NSTextInputContext,
    NSTrackingActiveInKeyWindow,
    NSTrackingArea,
    NSTrackingInVisibleRect,
    NSTrackingMouseEnteredAndExited,
    NSTrackingMouseMoved,
    NSUnderlineStyleAttributeName,
    NSUnderlineStyleSingle,
    NSView,
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
    NSObject,
    NSString,
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

# Formal NSTextInputClient conformance: adopting the protocol makes PyObjC
# apply Apple's own method signatures (NSRange struct args, the CGRect return
# and the actualRange out-pointer), so the IME methods bridge correctly.
# Hand-written signatures got these subtly wrong, which raised exceptions in
# firstRectForCharacterRange: and mis-bridged insertText:'s range argument.
_NS_TEXT_INPUT_CLIENT = objc.protocolNamed("NSTextInputClient")

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
        )
        area = NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            self.bounds(), options, self, None
        )
        self.addTrackingArea_(area)
        objc.super(_PuiKitView, self).updateTrackingAreas()

    def mouseMoved_(self, ns_event):
        x, y = self._mouse_unit(ns_event)
        self.backend._dispatch(Event(type=EventType.MOUSE_MOVE, x=x, y=y))

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
        x, y = self._mouse_unit(ns_event)
        self.backend._dispatch(Event(type=EventType.MOUSE_CLICK, x=x, y=y, button="left"))

    def rightMouseDown_(self, ns_event):
        x, y = self._mouse_unit(ns_event)
        self.backend._dispatch(Event(type=EventType.MOUSE_CLICK, x=x, y=y, button="right"))

    def mouseDragged_(self, ns_event):
        x, y = self._mouse_unit(ns_event)
        self.backend._dispatch(Event(type=EventType.MOUSE_DRAG, x=x, y=y, button="left"))

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
            "drag_and_drop": False,
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
        # Display list double buffer: widgets fill `_back`, drawRect reads `_front`.
        self._back: list[tuple] = []
        self._front: list[tuple] = []
        self._fonts: dict[TextAttribute, Any] = {}
        # Per-Style font cache: resolved NSFonts keyed by (Font, bold, italic).
        self._style_fonts: dict[tuple, Any] = {}
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

    def resolve_font(self, font: Font, bold: bool = False, italic: bool = False) -> Any:
        """Turn a Font descriptor into a native NSFont. Shared by the base font
        and per-Style widget fonts, so both name fonts the same way.

        ``family`` is honored if installed. With no family, ``monospace``
        chooses between the system monospaced face (the base grid font, which
        must stay fixed-advance to tile the grid) and the proportional system
        UI font (``Font()`` defaults). ``bold``/``italic`` force those traits
        on top of the descriptor."""
        size = float(font.size) if font.size is not None else 14.0
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
        advance = NSString.stringWithString_("M").sizeWithAttributes_(
            {NSFontAttributeName: regular}
        ).width
        self._base_w = math.ceil(advance)
        self._base_h = math.ceil(regular.ascender() - regular.descender() + regular.leading())

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

    def measure_text(self, text: str, style: Style = DEFAULT_STYLE) -> float:
        """Displayed width in base units. Base-font (font=None) text tiles the
        grid one column per glyph; a real per-Style font is measured natively
        and divided by the base unit width, so the result stays in base units."""
        if style.font is None:
            return float(display_width(text))
        ns_font = self._resolve_style_font(style)
        width = NSString.stringWithString_(text).sizeWithAttributes_(
            {NSFontAttributeName: ns_font}
        ).width
        return width / self._base_w if self._base_w else float(len(text))

    def measure_line_height(self, style: Style = DEFAULT_STYLE) -> float:
        """Row pitch in base units. The base grid font is exactly one base unit
        (that is how the unit was derived in _init_fonts); a real per-Style font
        reports its own line height, rounded up to whole pixels so successive
        rows land on the device-pixel grid, then expressed in base units."""
        if style.font is None or not self._base_h:
            return 1.0
        ns_font = self._resolve_style_font(style)
        line_px = ns_font.ascender() - ns_font.descender() + ns_font.leading()
        return math.ceil(line_px) / self._base_h

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

    def dim_rect(self, x: int, y: int, w: int, h: int) -> None:
        self._back.append(("dim", x, y, w, h))

    def draw_shadow(self, x: int, y: int, w: int, h: int) -> None:
        self._back.append(("shadow", x, y, w, h))

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
        self._animations = {
            key: anim for key, anim in self._animations.items() if not anim.done(now)
        }
        # Layout-level animations (Panel "size" transitions) re-render the
        # display list themselves; a callback returning False is done.
        self._tick_callbacks = [cb for cb in self._tick_callbacks if cb()]
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
            self._view.setNeedsDisplay_(True)
            self._view.displayIfNeeded()

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
            # Position: start offset (in base units) decaying to the final place.
            dx = animation.hints.get("from_dx", 0.0) * self._base_w * (1.0 - eased)
            dy = animation.hints.get("from_dy", 2.0) * self._base_h * (1.0 - eased)
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

        # A per-Style font flows by its natural advances (proportional, sized,
        # or a named family); the base grid font (font=None) tiles the grid.
        if style.font is not None:
            self._render_flow_text(x, y, text, style, fg, bg, alpha)
            return

        weight = TextAttribute.BOLD if style.attr & TextAttribute.BOLD else TextAttribute.NORMAL
        attrs = {
            NSFontAttributeName: self._fonts[weight],
            NSForegroundColorAttributeName: _ns_color(fg, alpha),
        }
        if style.attr & TextAttribute.UNDERLINE:
            attrs[NSUnderlineStyleAttributeName] = NSUnderlineStyleSingle

        # The base unit width is the glyph advance rounded *up* (see _init_fonts),
        # so a run drawn as one NSString flows narrower than the base unit grid and
        # drifts left a fraction of a pixel per glyph — over a wide pane the
        # drift accumulates to whole base units, so base unit-based clipping/truncation
        # cuts text that visually still fits. Place every glyph on its own
        # base unit instead: columns stay aligned and the clip rect trims the
        # boundary glyph at the exact pane edge, matching the base-unit coordinates
        # the rest of the framework works in. The run's background fills whole
        # base units (not just glyph advances) so reversed/selected runs have no
        # sub-pixel seams.
        # Advance by each glyph's display width (East Asian wide glyphs span two
        # base units), so a wide CJK glyph gets a 2-cell slot and the next glyph
        # does not overlap it.
        runs = _glyph_runs(text)
        widths = [max(1, display_width(glyph)) for glyph in runs]
        total = sum(widths)
        if bg is not None:
            _ns_color(bg).setFill()
            NSRectFill(self._unit_rect(x, y, total, 1))
        col = 0
        for glyph, w in zip(runs, widths):
            NSString.stringWithString_(glyph).drawAtPoint_withAttributes_(
                self._unit_rect(x + col, y, w, 1).origin, attrs
            )
            col += w

    def _render_flow_text(
        self, x: int, y: int, text: str, style: Style, fg, bg, alpha: float
    ) -> None:
        """Render text with a real per-Style font: a single run drawn at its
        natural advances from the run origin (no per-glyph grid placement), so
        proportional and sized text flow continuously; the pane clip trims the
        overflow. This is the GUI "no text grid" path (docs/font_system.md §9)."""
        ns_font = self._resolve_style_font(style)
        attrs = {
            NSFontAttributeName: ns_font,
            NSForegroundColorAttributeName: _ns_color(fg, alpha),
        }
        if style.attr & TextAttribute.UNDERLINE:
            attrs[NSUnderlineStyleAttributeName] = NSUnderlineStyleSingle
        ns_text = NSString.stringWithString_(text)
        origin = self._unit_rect(x, y, 1, 1).origin
        if bg is not None:
            width = ns_text.sizeWithAttributes_({NSFontAttributeName: ns_font}).width
            _ns_color(bg).setFill()
            NSRectFill(NSMakeRect(origin.x, origin.y, width, self._base_h))
        ns_text.drawAtPoint_withAttributes_(origin, attrs)

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

    def _render_round_rect(
        self, x: float, y: float, w: float, h: float, radius, style: Style, hints: dict[str, Any]
    ) -> None:
        rect = self._unit_rect(x, y, w, h)
        r = radius if radius is not None else min(rect.size.width, rect.size.height) / 2.0
        r = max(0.0, min(r, rect.size.width / 2.0, rect.size.height / 2.0))
        if hints.get("fill") and style.bg is not None:
            # NSBezierPath.fill composites source-over by default, so an RGBA
            # fill (translucent control face) blends over what is already drawn.
            _ns_color(style.bg).setFill()
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, r, r).fill()
        if style.fg is not None:
            line = float(hints.get("line_width", 1.0))
            # Inset by half the line width so the stroke lands on the pixel grid.
            inset = NSMakeRect(
                rect.origin.x + line / 2.0, rect.origin.y + line / 2.0,
                rect.size.width - line, rect.size.height - line,
            )
            ir = max(0.0, min(r, inset.size.width / 2.0, inset.size.height / 2.0))
            _ns_color(style.fg).setStroke()
            path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(inset, ir, ir)
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

    def _render_shadow(self, x: int, y: int, w: int, h: int) -> None:
        # Fill the layer's rect with the window background while an NSShadow
        # is active; the blurred shadow remains visible around the layer
        # content drawn on top.
        NSGraphicsContext.saveGraphicsState()
        shadow = NSShadow.alloc().init()
        shadow.setShadowOffset_(NSMakeSize(4.0, 6.0))
        shadow.setShadowBlurRadius_(12.0)
        shadow.setShadowColor_(_ns_color((0, 0, 0), 0.7))
        shadow.set()
        _ns_color(_DEFAULT_BG).setFill()
        NSRectFill(self._unit_rect(x, y, w, h))
        NSGraphicsContext.restoreGraphicsState()

    def _render_scrollbar(
        self, x: int, y: int, h: int, pos: float, ratio: float, style: Style
    ) -> None:
        track = self._unit_rect(x, y, 1, h)
        _ns_color((60, 60, 60)).setFill()
        NSRectFill(track)
        # Pixel-level thumb: size and position are computed in device pixels
        # (not snapped to whole base units), so the scroll position is exact.
        track_h = track.size.height
        thumb_h = max(2.0, track_h * ratio)
        thumb_y = track.origin.y + (track_h - thumb_h) * pos
        _ns_color(style.fg or (150, 150, 150)).setFill()
        NSRectFill(NSMakeRect(track.origin.x, thumb_y, track.size.width, thumb_h))

    def _render_image(self, x: int, y: int, path: str, hints: dict[str, Any]) -> None:
        image = NSImage.alloc().initWithContentsOfFile_(path)
        if image is None:
            return
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
