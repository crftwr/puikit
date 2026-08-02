"""Native macOS menu support for the macOS backend (PyObjC / AppKit).

Turns a backend-agnostic ``puikit.menu.Menu`` into a real ``NSMenu`` — the app
menu bar at the top of the screen and right-click context menus — wiring each
item's callback through target/action and its live ``enabled``/``checked``
predicate through ``validateMenuItem:`` (so a custom condition is re-evaluated
every time the menu opens, the Cocoa-native way). Submenus map to nested
``NSMenu``s and separators to ``NSMenuItem.separatorItem()``.

Kept in its own module so the pure-PyObjC menu code stays out of the main
backend file and is imported lazily, only when a native menu is requested.
"""

from __future__ import annotations

from typing import Any

from AppKit import (
    NSApp,
    NSEventModifierFlagCommand,
    NSEventModifierFlagControl,
    NSEventModifierFlagOption,
    NSEventModifierFlagShift,
    NSEventTypeKeyDown,
    NSMenu,
    NSMenuItem,
)
from Foundation import NSObject
import objc

from ..menu import Menu, MenuItem, MenuSeparator


def _current_key_event():
    """The event that triggered the running dispatch, if it is a keyDown —
    the signature of a menu item fired through a key equivalent rather than
    an interactive selection. Module-level so tests can substitute it."""
    ev = NSApp.currentEvent()
    if ev is not None and ev.type() == NSEventTypeKeyDown:
        return ev
    return None


#: The modifier bits a key equivalent is matched on.
_EQUIV_MASK = (NSEventModifierFlagCommand | NSEventModifierFlagControl
               | NSEventModifierFlagOption | NSEventModifierFlagShift)


def _matches_equivalent(ns_item, ev) -> bool:
    """Whether keyDown ``ev`` is the very chord ``ns_item`` displays as its
    key equivalent — i.e. the item was fired *by that keystroke*, not by an
    interactive selection (Return/arrow keys on an open menu are keyDowns too,
    but they do not match the item's own equivalent)."""
    equivalent = str(ns_item.keyEquivalent() or "")
    if not equivalent:
        return False
    chars = str(ev.charactersIgnoringModifiers() or "").lower()
    if chars != equivalent:
        return False
    return (int(ev.modifierFlags()) & _EQUIV_MASK) == int(ns_item.keyEquivalentModifierMask())


class _MenuResponder(NSObject):
    """The target every actionable ``NSMenuItem`` points at. Maps menu items
    (by integer tag) back to their puikit ``MenuItem`` so the right callback
    fires and ``validateMenuItem:`` can answer the live enabled/checked state."""

    def init(self):
        self = objc.super(_MenuResponder, self).init()
        if self is None:
            return None
        # Plain Python attributes (not Objective-C methods): map an item's tag
        # to its puikit MenuItem. The builder fills this via _register below.
        self._by_tag = {}
        # Where a key-equivalent activation is re-routed (the backend's
        # keyDown path) instead of firing the item — see fire_. None (a
        # popup's responder) keeps every activation interactive.
        self._forward_key = None
        return self

    def fire_(self, sender) -> None:
        item = self._by_tag.get(int(sender.tag()))
        if item is None:
            return
        # _NonFiringMenu's decline covers the documented performKeyEquivalent:
        # walk, but an active app's menu-bar dispatch can match a Cmd-chord
        # against its own cached equivalent table and fire the item without
        # ever consulting that override. Honor the display-only contract here
        # too: an activation carried by the item's own chord goes back to the
        # app's key handling (which also repaints), exactly as if the menu had
        # declined. An interactive selection — mouse, or Return on an open
        # menu — activates as normal.
        if self._forward_key is not None:
            ev = _current_key_event()
            if ev is not None and _matches_equivalent(sender, ev):
                self._forward_key(ev)
                return
        item.activate()

    def validateMenuItem_(self, ns_item) -> bool:
        item = self._by_tag.get(int(ns_item.tag()))
        if item is None:
            return True
        # Reflect the live checked predicate too, so a toggle item updates its
        # checkmark each time the menu opens.
        ns_item.setState_(1 if item.is_checked() else 0)
        return item.is_enabled()


def _register(responder: _MenuResponder, item: MenuItem) -> int:
    """Assign ``item`` a unique tag in ``responder`` and return it."""
    tag = len(responder._by_tag) + 1
    responder._by_tag[tag] = item
    return tag


class _NonFiringMenu(NSMenu):
    """An ``NSMenu`` that *displays* its items' key-equivalents (so macOS draws
    native accelerator glyphs — right-aligned, greyed, auto-dimmed when the item
    is disabled) but never *fires* them: ``performKeyEquivalent:`` always
    declines, so the keystroke is not consumed and falls through to the app's
    own key handling.

    This is deliberate. A puikit ``MenuItem.shortcut`` is only a hint, and the
    app owns key dispatch (its bindings are mostly modifier-less letters). If the
    menu fired the equivalent, macOS would swallow that plain keystroke before it
    reached the view — including while a text field is focused — breaking typing
    in dialogs. Declining here keeps the native look without the hijack. Genuine
    accelerators that *should* fire (e.g. the ⌘Q in the application menu) live in
    a standard ``NSMenu`` and are unaffected.

    Declining is necessary but not sufficient: an active app's menu-bar
    dispatch can match a Cmd-chord against its own cached equivalent table and
    fire the item without consulting this override. ``_MenuResponder.fire_``
    closes that path, re-routing such an activation back to the app's key
    handling."""

    def performKeyEquivalent_(self, event) -> bool:
        return False


# Modifier token (as it appears in a puikit shortcut hint) -> NSEvent mask.
_EQUIV_MOD = {
    "CMD": NSEventModifierFlagCommand, "COMMAND": NSEventModifierFlagCommand,
    "CTRL": NSEventModifierFlagControl, "CONTROL": NSEventModifierFlagControl,
    "ALT": NSEventModifierFlagOption, "OPT": NSEventModifierFlagOption,
    "OPTION": NSEventModifierFlagOption,
    "SHIFT": NSEventModifierFlagShift,
}

# Named base key -> the character macOS uses as that key's keyEquivalent
# (function-key private-use area for the navigation keys). Any base not listed
# that is a single character is used literally (letters lowercased).
_EQUIV_CHAR = {
    "ENTER": "\r", "RETURN": "\r",
    "TAB": "\t",
    "SPACE": " ",
    "BACKSPACE": "",
    "DELETE": "",
    "ESC": "", "ESCAPE": "",
    "UP": "", "↑": "",
    "DOWN": "", "↓": "",
    "LEFT": "", "←": "",
    "RIGHT": "", "→": "",
    "HOME": "",
    "END": "",
    "PGUP": "", "PAGE_UP": "",
    "PGDN": "", "PAGE_DOWN": "",
}


def _key_equivalent(shortcut: str) -> tuple[str, int] | None:
    """Parse a puikit shortcut hint (``"Cmd-Shift-C"``, ``"Enter"``, ``"="``)
    into ``(keyEquivalent_char, modifier_mask)`` for native NSMenuItem display,
    or ``None`` when it can't be represented — so an item shows no accelerator
    rather than a wrong one."""
    parts = shortcut.split("-")
    base, mod_parts = parts[-1], parts[:-1]
    mask = 0
    for part in mod_parts:
        flag = _EQUIV_MOD.get(part.upper())
        if flag is None:
            return None
        mask |= flag
    char = _EQUIV_CHAR.get(base.upper())
    if char is None:
        if len(base) != 1:
            return None
        char = base.lower()  # macOS shows Shift via the mask, not an upper char
    return char, mask


def _build_menu(menu: Menu, responder: _MenuResponder) -> Any:
    ns_menu = _NonFiringMenu.alloc().initWithTitle_(menu.title or "")
    ns_menu.setAutoenablesItems_(True)  # consult validateMenuItem:
    for entry in menu.items:
        if isinstance(entry, MenuSeparator):
            ns_menu.addItem_(NSMenuItem.separatorItem())
            continue
        if not isinstance(entry, MenuItem):
            continue
        ns_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            entry.label, None, ""
        )
        # Display-only accelerator: set the key-equivalent so macOS renders it
        # natively; _NonFiringMenu keeps it from actually firing.
        if entry.shortcut and entry.submenu is None:
            equiv = _key_equivalent(entry.shortcut)
            if equiv is not None:
                ns_item.setKeyEquivalent_(equiv[0])
                ns_item.setKeyEquivalentModifierMask_(equiv[1])
        if entry.submenu is not None:
            ns_item.setSubmenu_(_build_menu(entry.submenu, responder))
        else:
            tag = _register(responder, entry)
            ns_item.setTag_(tag)
            ns_item.setTarget_(responder)
            ns_item.setAction_("fire:")
        ns_menu.addItem_(ns_item)
    return ns_menu


def build_menu_bar(
    menu: Menu, app_title: str, forward_key: Any | None = None
) -> tuple[Any, _MenuResponder]:
    """Build the NSMenu main menu: a standard application menu (with Quit)
    followed by one bar entry per top-level item in ``menu``. ``forward_key``
    receives the NSEvent of a key-equivalent activation instead of the item
    firing (see ``_MenuResponder.fire_``)."""
    responder = _MenuResponder.alloc().init()
    responder._forward_key = forward_key
    main = NSMenu.alloc().init()

    # The application menu (its title is ignored; macOS shows the app name).
    app_item = NSMenuItem.alloc().init()
    main.addItem_(app_item)
    app_menu = NSMenu.alloc().init()
    quit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        f"Quit {app_title}", "terminate:", "q"
    )
    quit_item.setKeyEquivalentModifierMask_(NSEventModifierFlagCommand)
    app_menu.addItem_(quit_item)
    app_item.setSubmenu_(app_menu)

    # One bar entry per top-level item; each carries its own submenu.
    for entry in menu.items:
        if not isinstance(entry, MenuItem):
            continue
        bar_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            entry.label, None, ""
        )
        submenu = entry.submenu if entry.submenu is not None else Menu(title=entry.label)
        bar_item.setSubmenu_(_build_menu(submenu, responder))
        main.addItem_(bar_item)
    return main, responder


def build_popup_menu(menu: Menu) -> tuple[Any, _MenuResponder]:
    """Build a standalone NSMenu for a context-menu popup."""
    responder = _MenuResponder.alloc().init()
    return _build_menu(menu, responder), responder


def install_menu_bar(
    menu: Menu | None, app_title: str, forward_key: Any | None = None
) -> _MenuResponder | None:
    """Set (or clear) the application main menu. Returns the responder, which
    the caller must retain so the item callbacks survive. ``forward_key`` is
    passed through to ``build_menu_bar``."""
    if menu is None:
        NSApp.setMainMenu_(NSMenu.alloc().init())
        return None
    main, responder = build_menu_bar(menu, app_title, forward_key)
    NSApp.setMainMenu_(main)
    return responder
