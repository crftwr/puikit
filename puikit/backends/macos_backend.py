"""macOS native GUI backend built on PyObjC.

Uses whatever macOS frameworks fit the job — AppKit/Cocoa for windows and
events today; CoreGraphics, CoreText, and others as rendering grows.

The backend keeps a display list of drawing intents (text runs, boxes,
scrollbars, icons, images) in cell coordinates; a custom NSView renders the
list in pixels on each draw pass, so the same widget code that runs on
curses gets real rectangles, color text, and emoji icons here.

A compiled C++ CoreText extension is planned for the hot rendering path
(see CLAUDE.md, Multi-Language Policy); this pure-PyObjC renderer is the
reference implementation and the graceful fallback when the extension is
unavailable.
"""

from __future__ import annotations

import math
from typing import Any

from AppKit import (
    NSApp,
    NSApplication,
    NSApplicationActivationPolicyRegular,
    NSBackingStoreBuffered,
    NSBezierPath,
    NSBackgroundColorAttributeName,
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
    NSFont,
    NSFontAttributeName,
    NSFontWeightRegular,
    NSForegroundColorAttributeName,
    NSImage,
    NSCompositingOperationSourceOver,
    NSGraphicsContext,
    NSRectFill,
    NSRectFillUsingOperation,
    NSShadow,
    NSUnderlineStyleAttributeName,
    NSUnderlineStyleSingle,
    NSView,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskResizable,
    NSWindowStyleMaskTitled,
)
from Foundation import NSMakeRect, NSMakeSize, NSObject, NSString, NSZeroPoint

from ..backend import Backend, DEFAULT_STYLE, EventHandler, Style, TextAttribute
from ..capability import PROFILE_GUI_DESKTOP, CapabilityProfile
from ..event import Event, EventType

_DEFAULT_FG = (230, 230, 230)
_DEFAULT_BG = (24, 24, 24)

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


def _ns_color(rgb: tuple[int, int, int], alpha: float = 1.0):
    r, g, b = rgb
    return NSColor.colorWithSRGBRed_green_blue_alpha_(r / 255, g / 255, b / 255, alpha)


class _PuiKitView(NSView):
    """Renders the backend's display list; forwards input to the backend."""

    backend = None  # set right after alloc/init

    def isFlipped(self):
        return True  # top-left origin, matching cell coordinates

    def acceptsFirstResponder(self):
        return True

    def drawRect_(self, rect):
        if self.backend is not None:
            self.backend._render_into_view()

    def keyDown_(self, ns_event):
        event = translate_key(
            ns_event.charactersIgnoringModifiers(), ns_event.modifierFlags()
        )
        if event is not None:
            self.backend._dispatch(event)

    def _mouse_cell(self, ns_event) -> tuple[int, int]:
        point = self.convertPoint_fromView_(ns_event.locationInWindow(), None)
        cw, ch = self.backend.cell_size
        return (int(point.x // cw), int(point.y // ch))

    def mouseDown_(self, ns_event):
        x, y = self._mouse_cell(ns_event)
        self.backend._dispatch(Event(type=EventType.MOUSE_CLICK, x=x, y=y, button="left"))

    def rightMouseDown_(self, ns_event):
        x, y = self._mouse_cell(ns_event)
        self.backend._dispatch(Event(type=EventType.MOUSE_CLICK, x=x, y=y, button="right"))

    def mouseDragged_(self, ns_event):
        x, y = self._mouse_cell(ns_event)
        self.backend._dispatch(Event(type=EventType.MOUSE_DRAG, x=x, y=y, button="left"))

    def scrollWheel_(self, ns_event):
        delta = ns_event.scrollingDeltaY()
        if delta == 0:
            return
        x, y = self._mouse_cell(ns_event)
        scroll = 1 if delta > 0 else -1
        self.backend._dispatch(Event(type=EventType.MOUSE_SCROLL, x=x, y=y, scroll=scroll))


class _PuiKitWindowDelegate(NSObject):
    backend = None

    def windowWillClose_(self, notification):
        if self.backend is not None:
            self.backend.quit()

    def windowDidResize_(self, notification):
        if self.backend is not None:
            self.backend._on_resize()


class MacOSBackend(Backend):
    """macOS GUI backend (PyObjC). Coordinates stay cell-based; this backend
    owns the cell size and converts to pixels at render time."""

    PROFILE = CapabilityProfile(
        {
            **PROFILE_GUI_DESKTOP,
            # Not implemented yet in the MVP; flip these on as features land.
            "animation": False,
            "drag_and_drop": False,
            "ime": False,
            "clipboard_rich": False,
            "native_file_dialog": False,
            "system_tray": False,
            "media_keys": False,
            "gpu_acceleration": False,
        }
    )

    def __init__(self, width: int = 100, height: int = 30, title: str = "PuiKit",
                 font_size: float = 14.0):
        self._initial_cells = (width, height)
        self._title = title
        self._font_size = font_size
        self._window = None
        self._view = None
        self._delegate = None
        self._handler: EventHandler | None = None
        self._quit_requested = False
        # Display list double buffer: widgets fill `_back`, drawRect reads `_front`.
        self._back: list[tuple] = []
        self._front: list[tuple] = []
        self._fonts: dict[TextAttribute, Any] = {}
        self._cell_w = 1.0
        self._cell_h = 1.0

    # --- lifecycle ---------------------------------------------------------

    def open(self) -> None:
        app = NSApplication.sharedApplication()
        app.setActivationPolicy_(NSApplicationActivationPolicyRegular)

        self._init_fonts()
        w_px = self._initial_cells[0] * self._cell_w
        h_px = self._initial_cells[1] * self._cell_h
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
        self._window.setContentView_(self._view)
        self._window.makeFirstResponder_(self._view)

        self._delegate = _PuiKitWindowDelegate.alloc().init()
        self._delegate.backend = self
        self._window.setDelegate_(self._delegate)

        self._window.makeKeyAndOrderFront_(None)
        app.activateIgnoringOtherApps_(True)

    def close(self) -> None:
        if self._window is not None:
            self._window.setDelegate_(None)
            self._window.orderOut_(None)
            self._window = None
            self._view = None
            self._delegate = None

    def _init_fonts(self) -> None:
        regular = NSFont.monospacedSystemFontOfSize_weight_(
            self._font_size, NSFontWeightRegular
        )
        if regular is None:
            regular = NSFont.fontWithName_size_("Menlo", self._font_size)
        bold = NSFont.fontWithName_size_(
            regular.fontName() + "-Bold", self._font_size
        ) or NSFont.boldSystemFontOfSize_(self._font_size)
        self._fonts = {TextAttribute.NORMAL: regular, TextAttribute.BOLD: bold}
        size = NSString.stringWithString_("M").sizeWithAttributes_(
            {NSFontAttributeName: regular}
        )
        self._cell_w = math.ceil(size.width)
        self._cell_h = math.ceil(regular.ascender() - regular.descender() + regular.leading())

    # --- geometry ----------------------------------------------------------

    @property
    def size(self) -> tuple[int, int]:
        if self._view is None:
            return self._initial_cells
        bounds = self._view.bounds()
        return (int(bounds.size.width // self._cell_w), int(bounds.size.height // self._cell_h))

    @property
    def size_cells(self) -> tuple[float, float]:
        if self._view is None:
            return (float(self._initial_cells[0]), float(self._initial_cells[1]))
        bounds = self._view.bounds()
        return (bounds.size.width / self._cell_w, bounds.size.height / self._cell_h)

    @property
    def cell_size(self) -> tuple[int, int]:
        return (int(self._cell_w), int(self._cell_h))

    # --- drawing (display list, cell coordinates) ----------------------------

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

    def dim_rect(self, x: int, y: int, w: int, h: int) -> None:
        self._back.append(("dim", x, y, w, h))

    def draw_shadow(self, x: int, y: int, w: int, h: int) -> None:
        self._back.append(("shadow", x, y, w, h))

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
            elif kind == "dim":
                self._render_dim(*command[1:])
            elif kind == "shadow":
                self._render_shadow(*command[1:])

    def _cell_rect(self, x: int, y: int, w_cells: int, h_cells: int):
        return NSMakeRect(
            x * self._cell_w, y * self._cell_h, w_cells * self._cell_w, h_cells * self._cell_h
        )

    def _render_text(self, x: int, y: int, text: str, style: Style) -> None:
        fg = style.fg or _DEFAULT_FG
        bg = style.bg
        if style.attr & TextAttribute.REVERSE:
            fg, bg = (bg or _DEFAULT_BG), (style.fg or _DEFAULT_FG)
        alpha = 0.55 if style.attr & TextAttribute.DIM else 1.0

        font = self._fonts[
            TextAttribute.BOLD if style.attr & TextAttribute.BOLD else TextAttribute.NORMAL
        ]
        attrs = {
            NSFontAttributeName: font,
            NSForegroundColorAttributeName: _ns_color(fg, alpha),
        }
        if bg is not None:
            attrs[NSBackgroundColorAttributeName] = _ns_color(bg)
        if style.attr & TextAttribute.UNDERLINE:
            attrs[NSUnderlineStyleAttributeName] = NSUnderlineStyleSingle

        point = self._cell_rect(x, y, 1, 1).origin
        NSString.stringWithString_(text).drawAtPoint_withAttributes_(point, attrs)

    def _render_box(
        self, x: int, y: int, w: int, h: int, style: Style, hints: dict[str, Any]
    ) -> None:
        rect = self._cell_rect(x, y, w, h)
        if hints.get("fill"):
            _ns_color(style.bg or _DEFAULT_BG).setFill()
            NSRectFill(rect)
        # Inset by half the line width so the 1px stroke lands on the pixel grid.
        rect = NSMakeRect(
            rect.origin.x + 0.5, rect.origin.y + 0.5, rect.size.width - 1, rect.size.height - 1
        )
        _ns_color(style.fg or _DEFAULT_FG).setStroke()
        path = NSBezierPath.bezierPathWithRect_(rect)
        path.setLineWidth_(1.0)
        path.stroke()

    def _render_dim(self, x: int, y: int, w: int, h: int) -> None:
        # Real transparency: a translucent dark overlay on whatever was
        # already drawn below.
        _ns_color((0, 0, 0), 0.45).setFill()
        NSRectFillUsingOperation(self._cell_rect(x, y, w, h), NSCompositingOperationSourceOver)

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
        NSRectFill(self._cell_rect(x, y, w, h))
        NSGraphicsContext.restoreGraphicsState()

    def _render_scrollbar(
        self, x: int, y: int, h: int, pos: float, ratio: float, style: Style
    ) -> None:
        track = self._cell_rect(x, y, 1, h)
        _ns_color((60, 60, 60)).setFill()
        NSRectFill(track)
        thumb_h = max(1, round(h * ratio))
        thumb_y = round((h - thumb_h) * pos)
        _ns_color(style.fg or (150, 150, 150)).setFill()
        NSRectFill(self._cell_rect(x, y + thumb_y, 1, thumb_h))

    def _render_image(self, x: int, y: int, path: str, hints: dict[str, Any]) -> None:
        image = NSImage.alloc().initWithContentsOfFile_(path)
        if image is None:
            return
        w_cells = hints.get("w", max(1, round(image.size().width / self._cell_w)))
        h_cells = hints.get("h", max(1, round(image.size().height / self._cell_h)))
        image.drawInRect_fromRect_operation_fraction_respectFlipped_hints_(
            self._cell_rect(x, y, w_cells, h_cells),
            NSMakeRect(0, 0, 0, 0),
            2,  # NSCompositingOperationSourceOver
            1.0,
            True,
            None,
        )

    # --- event loop ----------------------------------------------------------

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
