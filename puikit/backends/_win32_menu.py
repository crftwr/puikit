"""Native Windows menu support for the Windows backend.

Turns a backend-agnostic ``puikit.menu.Menu`` into a real Win32 ``HMENU`` —
the window's menu bar and right-click context menus — mirroring
``_macos_menu.py``'s responder/tag pattern: every actionable item gets a
unique command id, and ``WM_COMMAND``/``WM_INITMENUPOPUP`` route back to it.

One difference from the macOS version drives the design here: AppKit fires a
menu item's callback synchronously while a popup is tracking, but
``TrackPopupMenu`` only *posts* the resulting ``WM_COMMAND`` — it arrives on
the message queue after ``TrackPopupMenu`` has already returned. A fresh
per-popup responder would risk losing that command if it were torn down
before the posted message is pumped, so the backend keeps a single
``MenuResponder`` for its whole lifetime (menu bar and every popup share one
growing command-id space) instead of building a new one per call.
"""

from __future__ import annotations

from . import _win32_native as w
from ..menu import Menu, MenuItem, MenuSeparator


class MenuResponder:
    """Owns the command-id <-> MenuItem mapping for every menu this backend
    has ever built, plus enough bookkeeping to re-validate a popup's
    enabled/checked state right before it opens (``WM_INITMENUPOPUP``)."""

    def __init__(self) -> None:
        self._items_by_tag: dict[int, MenuItem] = {}
        self._tag_by_item: dict[int, int] = {}  # id(item) -> tag
        self._popups: dict[int, Menu] = {}  # HMENU address -> the Menu shown there
        self._next_tag = 1

    def register(self, item: MenuItem) -> int:
        tag = self._tag_by_item.get(id(item))
        if tag is None:
            tag = self._next_tag
            self._next_tag += 1
            self._tag_by_item[id(item)] = tag
            self._items_by_tag[tag] = item
        return tag

    def register_popup(self, hmenu: int, menu: Menu) -> None:
        self._popups[hmenu] = menu

    def fire(self, tag: int) -> None:
        item = self._items_by_tag.get(tag)
        if item is not None:
            item.activate()

    def revalidate(self, hmenu: int) -> None:
        """Reflect each item's live enabled/checked predicate just before the
        popup at ``hmenu`` is shown — the Win32 analogue of ``validateMenuItem:``."""
        menu = self._popups.get(hmenu)
        if menu is None:
            return
        for entry in menu.items:
            if not isinstance(entry, MenuItem) or entry.submenu is not None:
                continue
            tag = self._tag_by_item.get(id(entry))
            if tag is None:
                continue
            w.user32.EnableMenuItem(
                hmenu, tag, w.MF_BYCOMMAND | (w.MF_ENABLED if entry.is_enabled() else w.MF_GRAYED)
            )
            w.user32.CheckMenuItem(
                hmenu, tag, w.MF_BYCOMMAND | (w.MF_CHECKED if entry.is_checked() else w.MF_UNCHECKED)
            )


def _build_menu(menu: Menu, responder: MenuResponder) -> int:
    hmenu = w.user32.CreatePopupMenu()
    responder.register_popup(hmenu, menu)
    for entry in menu.items:
        if isinstance(entry, MenuSeparator):
            w.user32.AppendMenuW(hmenu, w.MF_SEPARATOR, 0, None)
            continue
        if not isinstance(entry, MenuItem):
            continue
        label = f"{entry.label}\t{entry.shortcut}" if entry.shortcut else entry.label
        if entry.submenu is not None:
            submenu_hmenu = _build_menu(entry.submenu, responder)
            w.user32.AppendMenuW(hmenu, w.MF_POPUP, submenu_hmenu, label)
        else:
            tag = responder.register(entry)
            w.user32.AppendMenuW(hmenu, w.MF_STRING, tag, label)
    return hmenu


def build_menu_bar(menu: Menu, responder: MenuResponder) -> int:
    """Build the HMENU for a window's menu bar: one top-level entry per item
    in ``menu``, each carrying its own dropdown submenu."""
    hmenu = w.user32.CreateMenu()
    for entry in menu.items:
        if not isinstance(entry, MenuItem):
            continue
        submenu = entry.submenu if entry.submenu is not None else Menu(title=entry.label)
        submenu_hmenu = _build_menu(submenu, responder)
        w.user32.AppendMenuW(hmenu, w.MF_POPUP, submenu_hmenu, entry.label)
    return hmenu


def build_popup_menu(menu: Menu, responder: MenuResponder) -> int:
    """Build a standalone HMENU for a context-menu popup."""
    return _build_menu(menu, responder)


def destroy_menu_recursive(hmenu: int) -> None:
    """Destroy ``hmenu`` and every submenu it owns.

    ``DestroyMenu`` does not reliably cascade into ``MF_POPUP`` submenus on
    its own, so submenus are walked and destroyed bottom-up first.
    """
    if not hmenu:
        return
    count = w.user32.GetMenuItemCount(hmenu)
    for i in range(max(count, 0)):
        sub = w.user32.GetSubMenu(hmenu, i)
        if sub:
            destroy_menu_recursive(sub)
    w.user32.DestroyMenu(hmenu)
