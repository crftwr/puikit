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
    NSMenu,
    NSMenuItem,
)
from Foundation import NSObject
import objc

from ..menu import Menu, MenuItem, MenuSeparator


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
        return self

    def fire_(self, sender) -> None:
        item = self._by_tag.get(int(sender.tag()))
        if item is not None:
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


def _build_menu(menu: Menu, responder: _MenuResponder) -> Any:
    ns_menu = NSMenu.alloc().initWithTitle_(menu.title or "")
    ns_menu.setAutoenablesItems_(True)  # consult validateMenuItem:
    for entry in menu.items:
        if isinstance(entry, MenuSeparator):
            ns_menu.addItem_(NSMenuItem.separatorItem())
            continue
        if not isinstance(entry, MenuItem):
            continue
        key = ""
        ns_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            entry.label, None, key
        )
        if entry.submenu is not None:
            ns_item.setSubmenu_(_build_menu(entry.submenu, responder))
        else:
            tag = _register(responder, entry)
            ns_item.setTag_(tag)
            ns_item.setTarget_(responder)
            ns_item.setAction_("fire:")
        ns_menu.addItem_(ns_item)
    return ns_menu


def build_menu_bar(menu: Menu, app_title: str) -> tuple[Any, _MenuResponder]:
    """Build the NSMenu main menu: a standard application menu (with Quit)
    followed by one bar entry per top-level item in ``menu``."""
    responder = _MenuResponder.alloc().init()
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


def install_menu_bar(menu: Menu | None, app_title: str) -> _MenuResponder | None:
    """Set (or clear) the application main menu. Returns the responder, which
    the caller must retain so the item callbacks survive."""
    if menu is None:
        NSApp.setMainMenu_(NSMenu.alloc().init())
        return None
    main, responder = build_menu_bar(menu, app_title)
    NSApp.setMainMenu_(main)
    return responder
