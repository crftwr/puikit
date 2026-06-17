"""Backend-agnostic menu model.

A menu is *intent*, not geometry: the same ``Menu`` tree drives a real OS menu
on ``native_menus`` backends (macOS installs an ``NSMenu``) and an in-window,
widget-rendered menu on every other backend (TUI). Apps build one model and
hand it to the Panel; the Panel resolves how it is realized per backend, so
no app branches on the capability.

The model carries everything a menu needs and nothing about how it looks:

- ``MenuItem`` — a label, an ``on_select`` callback, an optional ``submenu``,
  an optional keyboard ``shortcut`` *hint* (display only), and ``enabled`` /
  ``checked`` that are either a bool or a **predicate** (``Callable[[], bool]``)
  evaluated each time the menu opens, so items enable/disable on custom
  conditions without the app rebuilding the tree.
- ``MenuSeparator`` (and the shared ``SEPARATOR`` instance) — a divider line
  between groups of items.
- ``Menu`` — an ordered list of items and separators, with an optional title
  (the title is what a menu-bar entry or a submenu parent shows).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Union


class MenuSeparator:
    """A divider between groups of menu items. Stateless, so a single shared
    instance (``SEPARATOR``) can be reused everywhere."""

    __slots__ = ()


#: Shared separator instance — ``Menu(item_a, SEPARATOR, item_b)``.
SEPARATOR = MenuSeparator()


@dataclass
class MenuItem:
    """One selectable row in a menu.

    ``enabled`` and ``checked`` accept a bool *or* a zero-arg predicate; the
    predicate is evaluated when the menu opens (the native backend through
    ``validateMenuItem:``, the widget fallback at draw time), which is how an
    item reflects a custom, live condition. A ``submenu`` makes the item a
    parent that opens a nested menu instead of firing ``on_select``."""

    label: str
    on_select: Callable[[], None] | None = None
    enabled: bool | Callable[[], bool] = True
    checked: bool | Callable[[], bool] = False
    submenu: "Menu | None" = None
    #: Display-only accelerator hint (e.g. "Cmd+C"); the menu does not bind it.
    shortcut: str | None = None

    def is_enabled(self) -> bool:
        return self.enabled() if callable(self.enabled) else bool(self.enabled)

    def is_checked(self) -> bool:
        return self.checked() if callable(self.checked) else bool(self.checked)

    def activate(self) -> None:
        """Fire the item's callback (a no-op for a disabled item or a parent
        with no callback of its own)."""
        if self.is_enabled() and self.on_select is not None:
            self.on_select()


MenuEntry = Union[MenuItem, MenuSeparator]


@dataclass
class Menu:
    """An ordered list of items and separators, optionally titled.

    For a menu bar, build a top-level ``Menu`` whose items each carry a
    ``submenu`` — every top-level item is one bar entry, its ``submenu`` the
    list that drops down."""

    items: list[MenuEntry] = field(default_factory=list)
    title: str | None = None

    def __init__(self, *items: MenuEntry, title: str | None = None):
        self.items = list(items)
        self.title = title

    def add(self, entry: MenuEntry) -> "Menu":
        self.items.append(entry)
        return self

    @property
    def selectable(self) -> list[MenuItem]:
        """The item rows (separators excluded), in order."""
        return [it for it in self.items if isinstance(it, MenuItem)]
