"""Widget-rendered menus — the fallback for backends without ``native_menus``.

Two widgets realize the backend-agnostic ``puikit.menu`` model in-window:

- ``MenuBar`` — a horizontal strip of top-level titles placed in the app's
  layout. On a ``native_menus`` backend it instead registers the model as the
  *OS* menu bar (``Panel.set_menu_bar``) and collapses to zero height, so the
  app places one ``MenuBar`` and never branches on the capability.
- ``MenuPopup`` — the floating list pushed as a modal Panel layer, shared by a
  bar entry dropping down and by a context menu (``Panel.popup_menu``). It
  handles separators, submenus (each opens a nested popup), live
  enabled/checked predicates, the keyboard, and the mouse.

Both reuse the same idioms as the DropDown popup: a framed, padded list on
vector backends; popup-background contrast on a character grid.
"""

from __future__ import annotations

from collections.abc import Callable

from ..backend import DEFAULT_STYLE, Style
from ..event import Event, EventType
from ..layout import LayoutContext, SizeRequest
from ..menu import Menu, MenuItem, MenuSeparator
from ..panel import DrawContext
from ..theme import DEFAULT_THEME
from .base import CONTROL_HEIGHT, Widget

# Popup row height in base units: one cell on a grid, a little taller (centered
# text + padding) on vector backends — matching the other controls.
MENU_ROW_H = CONTROL_HEIGHT
_SUBMENU_ARROW = "▸"
_CHECK_MARK = "✓"
# Columns reserved left of the label for the check/submenu marker, and right
# padding, so labels and shortcuts line up across rows.
_MARKER_W = 2
_PAD = 1
# Horizontal padding on each side of a top-level MenuBar title, in base units.
# Real padding, NOT space glyphs: a space is a full cell on a terminal but only
# ~a quarter of one in a proportional GUI font, so space-padded titles crowd
# together on GUI. One whole base unit each side reads as one cell on a grid and
# the same gap on GUI, so the bar stays evenly spaced on both.
_TITLE_PAD = 1.0


def popup_geometry(
    menu: Menu, measure: Callable[[str], float], vector: bool
) -> tuple[float, float, float]:
    """(width, height, row_h) of ``menu``'s popup in base units. Width fits the
    widest ``marker + label + shortcut`` row; height is one row per entry
    (separators included). Used by the Panel and the MenuBar to size the layer
    before it is pushed."""
    row_h = MENU_ROW_H if vector else 1.0
    text_w = 0.0
    for entry in menu.items:
        if not isinstance(entry, MenuItem):
            continue
        w = measure(entry.label)
        if entry.shortcut:
            w += 2.0 + measure(entry.shortcut)
        elif entry.submenu is not None:
            w += 2.0
        text_w = max(text_w, w)
    width = _MARKER_W + text_w + 2 * _PAD
    height = max(1, len(menu.items)) * row_h
    return (width, height, row_h)


class MenuPopup(Widget):
    """A floating menu list pushed as a modal Panel layer.

    Modal: it owns events while open. Up/down move the cursor (skipping
    separators and disabled rows), enter fires the item and dismisses the whole
    chain, right enters the cursor row's submenu, escape (and left, inside a
    submenu) backs out one level, and an outside click cancels. Submenus open
    as nested popups to the right of their row.

    A pulldown hanging off a ``MenuBar`` carries ``bar``: there ←/→ with no
    submenu to enter hop to the adjacent bar entry's pulldown, and Alt+letter
    jumps straight to the bar entry with that mnemonic — the desktop menu
    convention (xefm#304). A plain letter is an item mnemonic in any menu:
    a unique first-letter match on an enabled row activates it, several
    matches cycle the cursor. A context menu has no bar to walk, so without
    it ← keeps closing and → keeps firing like enter."""

    def __init__(
        self,
        menu: Menu,
        row_h: float = 1.0,
        parent: "MenuPopup | None" = None,
        on_close: Callable[[], None] | None = None,
        bar: "MenuBar | None" = None,
    ):
        self.menu = menu
        self._row_h = row_h
        self.parent = parent
        # Root popup only: called once the whole chain is torn down (e.g. the
        # Panel's on_done for a context menu).
        self.on_close = on_close
        # Root popup only, set when this is a MenuBar pulldown: the bar to walk
        # with ←/→ and Alt+letter once this chain has dismissed itself.
        self.bar = bar
        self._panel = None
        self._width = 0.0
        self._abs: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
        self._child: "MenuPopup | None" = None
        self.cursor = self._first_selectable()

    # --- selection helpers ----------------------------------------------------

    def _selectable(self, index: int) -> bool:
        entry = self.menu.items[index]
        return isinstance(entry, MenuItem) and entry.is_enabled()

    def _first_selectable(self) -> int:
        for i in range(len(self.menu.items)):
            if self._selectable(i):
                return i
        return -1

    def _step_cursor(self, direction: int) -> None:
        n = len(self.menu.items)
        if n == 0:
            return
        i = self.cursor
        for _ in range(n):
            i = (i + direction) % n
            if self._selectable(i):
                self.cursor = i
                return

    # --- drawing -------------------------------------------------------------

    def _hover_row(self, ctx: DrawContext) -> int | None:
        panel = ctx.panel
        if panel is None or panel.pointer is None:
            return None
        px, py = panel.pointer
        rx, ry, rw, rh = ctx.screen_rect
        if not (rx <= px < rx + rw and ry <= py < ry + rh):
            return None
        row = int((py - ry) / self._row_h)
        return row if 0 <= row < len(self.menu.items) else None

    def draw(self, ctx: DrawContext) -> None:
        self._panel = ctx.panel
        self._abs = ctx.screen_rect
        theme = ctx.theme or DEFAULT_THEME
        wu, hu = ctx.size_units
        self._width = wu
        row_h = self._row_h
        text_dy = (row_h - 1.0) / 2.0
        ctx.fill_rect(0, 0, wu, hu, Style(bg=theme.popup_bg))
        hover = self._hover_row(ctx)
        # A pointing hand over an actionable row (an enabled item, not a
        # separator or disabled row); one intent, resolved per backend.
        if hover is not None:
            entry = self.menu.items[hover]
            if isinstance(entry, MenuItem) and entry.is_enabled():
                ctx.set_cursor("pointer")

        for i, entry in enumerate(self.menu.items):
            top = i * row_h
            if top >= hu:
                break
            if isinstance(entry, MenuSeparator):
                # A hairline on GUI, a ─ run on grid — the Panel layer picks.
                ctx.draw_hairline(
                    _PAD, top + row_h / 2.0, max(0.0, wu - 2 * _PAD),
                    style=Style(fg=theme.popup_border, bg=theme.popup_bg),
                )
                continue
            enabled = entry.is_enabled()
            if i == self.cursor:
                row_bg = theme.selection_bg
            elif i == hover and enabled:
                row_bg = theme.hover_bg
            else:
                row_bg = theme.popup_bg
            if row_bg != theme.popup_bg:
                ctx.fill_rect(0, top, wu, row_h, Style(bg=row_bg))
            fg = theme.text if enabled else theme.muted_text
            # Check marker in the reserved left column.
            if entry.is_checked():
                ctx.draw_text(_PAD, top + text_dy, _CHECK_MARK, Style(fg=fg, bg=row_bg))
            label_x = _MARKER_W
            avail = max(0, int(wu) - label_x - _PAD)
            ctx.draw_text(label_x, top + text_dy, entry.label[:avail], Style(fg=fg, bg=row_bg))
            # Right-aligned shortcut hint, or the submenu arrow.
            if entry.submenu is not None:
                ctx.draw_text(wu - _PAD - 1, top + text_dy, _SUBMENU_ARROW, Style(fg=fg, bg=row_bg))
            elif entry.shortcut:
                sx = wu - _PAD - ctx.measure_text(entry.shortcut)
                ctx.draw_text(sx, top + text_dy, entry.shortcut, Style(fg=theme.muted_text, bg=row_bg))

        if ctx.vector_shapes:
            ctx.round_rect(0, 0, wu, hu, Style(fg=theme.popup_border), radius=5.0)

    # --- chain teardown -------------------------------------------------------

    def _pop_if_top(self) -> None:
        panel = self._panel
        if panel is not None and panel._layers and panel._layers[-1].widget is self:
            panel.pop_layer()

    def _back(self) -> None:
        """Close just this level, returning to the parent (or finishing the
        whole menu when this is the root)."""
        self._pop_if_top()
        if self.parent is not None:
            self.parent._child = None
        elif self.on_close is not None:
            self.on_close()

    def _dismiss(self) -> None:
        """Tear down the whole chain from this (active) popup up to the root."""
        self._pop_if_top()
        if self.parent is not None:
            self.parent._dismiss()
        elif self.on_close is not None:
            self.on_close()

    def _open_submenu(self, index: int) -> None:
        entry = self.menu.items[index]
        if not isinstance(entry, MenuItem) or entry.submenu is None or self._panel is None:
            return
        rx, ry, rw, _rh = self._abs
        vector = self._panel.backend.capabilities.supports("vector_shapes")
        w, h, row_h = popup_geometry(entry.submenu, self._panel.backend.measure_text, vector)
        child = MenuPopup(entry.submenu, row_h=row_h, parent=self)
        self._child = child
        # Open to the right of this row; the Panel nudges it on-screen.
        sw, sh = self._panel.backend.size_units
        x = min(rx + rw, max(0.0, sw - w))
        y = min(ry + index * self._row_h, max(0.0, sh - h))
        self._panel.push_layer(
            child, z=61 + self._depth(),
            hints={"shadow": True, "x": x, "y": y, "w": w, "h": h},
        )

    def _depth(self) -> int:
        d, p = 0, self.parent
        while p is not None:
            d, p = d + 1, p.parent
        return d

    def _bar(self) -> "MenuBar | None":
        root = self
        while root.parent is not None:
            root = root.parent
        return root.bar

    def _navigate_bar(self, direction: int) -> bool:
        """When this chain hangs off a MenuBar, close the whole chain and ask
        the bar to open the adjacent pulldown. False when there is no bar."""
        bar = self._bar()
        if bar is None:
            return False
        self._dismiss()
        bar._navigate(direction)
        return True

    def _jump_bar_mnemonic(self, letter: str) -> bool:
        """Alt+letter while open: close the chain and open the bar entry with
        that mnemonic. False (nothing closed) when no bar entry matches."""
        bar = self._bar()
        index = bar.mnemonic_index(letter) if bar is not None else None
        if index is None:
            return False
        self._dismiss()
        bar._do_open(index)
        return True

    # --- item mnemonics -------------------------------------------------------

    def _jump_mnemonic(self, letter: str) -> None:
        """A plain letter is an item mnemonic, resolved the way Windows menus
        do: a unique first-letter match among the enabled rows activates it
        outright (a submenu parent opens); several matches only cycle the
        cursor through them, so enter commits the one the user means."""
        matches = [
            i for i in range(len(self.menu.items))
            if self._selectable(i)
            and self.menu.items[i].label.lstrip()[:1].lower() == letter
        ]
        if not matches:
            return
        if len(matches) == 1:
            self.cursor = matches[0]
            self._activate(matches[0])
            return
        after = [i for i in matches if i > self.cursor]
        self.cursor = after[0] if after else matches[0]

    # --- events --------------------------------------------------------------

    def _activate(self, index: int) -> None:
        entry = self.menu.items[index]
        if not isinstance(entry, MenuItem) or not entry.is_enabled():
            return
        if entry.submenu is not None:
            self._open_submenu(index)
            return
        # Tear the menu chain down *before* firing the callback: an action may
        # itself push a layer (a message box, a dialog), and once it does this
        # popup is no longer the top layer, so a later _dismiss() would skip it
        # (see _pop_if_top) and leave the menu open behind the new overlay.
        self._dismiss()
        entry.activate()

    def handle_event(self, event: Event) -> bool:
        if event.type is EventType.KEY:
            key = event.key
            in_range = 0 <= self.cursor < len(self.menu.items)
            if key == "up":
                self._step_cursor(-1)
            elif key == "down":
                self._step_cursor(1)
            elif key == "enter" or event.char == " ":
                if in_range:
                    self._activate(self.cursor)
            elif key == "right":
                # → enters the cursor row's submenu; with none to enter, a bar
                # pulldown hops to the next menu, a context menu fires the row.
                entry = self.menu.items[self.cursor] if in_range else None
                if (isinstance(entry, MenuItem) and entry.is_enabled()
                        and entry.submenu is not None):
                    self._open_submenu(self.cursor)
                elif not self._navigate_bar(1) and in_range:
                    self._activate(self.cursor)
            elif key == "left":
                # ← backs out of a submenu; at the root of a bar pulldown it
                # hops to the previous menu instead of closing outright.
                if self.parent is not None or not self._navigate_bar(-1):
                    self._back()
            elif key == "escape":
                self._back()
            elif key in ("f10", "alt"):
                # The menu-activation key toggles: pressed with the menu
                # already open, it closes the whole chain (as on Windows).
                self._dismiss()
            elif isinstance(key, str) and len(key) == 1 and key.isalpha():
                mods = frozenset(event.modifiers or ())
                if mods == frozenset({"alt"}):
                    self._jump_bar_mnemonic(key)
                elif not mods & {"ctrl", "cmd", "alt"}:
                    self._jump_mnemonic(key)
            return True
        if event.type is EventType.MOUSE_CLICK:
            row = int(event.y / self._row_h) if event.y is not None else -1
            inside_x = event.x is not None and 0 <= event.x < self._width
            if inside_x and 0 <= row < len(self.menu.items) and self._selectable(row):
                self.cursor = row
                self._activate(row)
            else:
                self._dismiss()  # click outside cancels the whole menu
            return True
        return True  # modal: swallow everything else


class MenuBar(Widget):
    """A top-level menu bar. Placed once in the app's layout (a content-sized
    row). On a ``native_menus`` backend it registers ``menu`` as the OS menu
    bar and claims no in-window space; otherwise it renders an in-window strip
    of the top-level titles, each opening a ``MenuPopup`` below it.

    Deliberately NOT a focus stop: no desktop puts the menu bar in the Tab
    order, and taking focus on click is exactly how a bar entry stayed
    highlighted after its pulldown closed (xefm#304 — the pulldown is modal,
    so the outside click that dismissed it could never move focus back). The
    open entry's title is highlighted from the pulldown's open state instead.
    Keyboard access is the app binding its menu-activation key (F10, a bare
    Alt tap) to :meth:`open_menu`, and Alt+letter to :meth:`open_menu_mnemonic`
    (Alt+F → File, by title first letter); once a pulldown is open, ←/→ and
    Alt+letter walk the bar and plain letters pick items (``MenuPopup``,
    which receives this bar as its ``bar``)."""

    def __init__(self, menu: Menu, style: Style = DEFAULT_STYLE):
        # A Menu whose items each carry a submenu (their labels are the bar
        # entries). Plain items without a submenu still work (they fire on click).
        self.menu = menu
        self.style = style
        self._panel = None
        self._installed_native = False
        self._abs: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 1.0)
        self._entry_x: list[tuple[int, int, MenuItem]] = []  # (x0, x1, item) per render
        self._index = 0      # entry of the open (or last-open) pulldown
        self._open = False   # whether that pulldown is up right now

    # --- geometry -------------------------------------------------------------

    def measure(self, ctx: LayoutContext, axis: str, available: float) -> SizeRequest:
        if axis == "y":
            if ctx.native_menus:
                return SizeRequest(min=0.0, preferred=0.0, max=0.0)
            h = 1.0 if ctx.snap else CONTROL_HEIGHT
            return SizeRequest(min=h, preferred=h, max=h)
        return SizeRequest()

    def _entries(self) -> list[MenuItem]:
        return self.menu.selectable

    # --- drawing -------------------------------------------------------------

    def draw(self, ctx: DrawContext) -> None:
        self._panel = ctx.panel
        self._abs = ctx.screen_rect
        if ctx.native_menus:
            # The OS owns the bar; register it once and take no in-window space.
            if not self._installed_native and ctx.panel is not None:
                ctx.panel.set_menu_bar(self.menu)
                self._installed_native = True
            return

        theme = ctx.theme or DEFAULT_THEME
        wu, hu = ctx.size_units
        ty = (hu - 1.0) / 2.0
        ctx.fill_rect(0, 0, wu, hu, Style(bg=theme.popup_bg))
        entries = self._entries()
        self._entry_x = []
        x = float(_PAD)
        for i, item in enumerate(entries):
            # Pad with real base units, not surrounding spaces (which collapse to
            # a thin gap under a proportional font); the span the title occupies
            # is the padding plus the measured label.
            w = ctx.measure_text(item.label)
            span = _TITLE_PAD + w + _TITLE_PAD
            # The title reads as selected exactly while its pulldown is open —
            # never from focus, which the bar does not take (xefm#304).
            open_here = self._open and i == self._index
            if open_here:
                ctx.fill_rect(x, 0, span, hu, Style(bg=theme.selection_bg))
            fg = theme.text if item.is_enabled() else theme.muted_text
            bg = theme.selection_bg if open_here else theme.popup_bg
            ctx.draw_text(x + _TITLE_PAD, ty, item.label, Style(fg=fg, bg=bg))
            self._entry_x.append((x, x + span, item))
            x += span

        # A pointing hand over an enabled top-level title, so the bar reads as
        # clickable. Pointer taken in widget-local coords (screen pointer minus
        # this widget's origin), tested against the title spans built above.
        if ctx.panel is not None and ctx.panel.pointer is not None:
            rx, ry, _rw, rh = self._abs
            lx, ly = ctx.panel.pointer[0] - rx, ctx.panel.pointer[1] - ry
            if 0 <= ly < rh and any(
                x0 <= lx < x1 and item.is_enabled() for x0, x1, item in self._entry_x
            ):
                ctx.set_cursor("pointer")

    # --- opening --------------------------------------------------------------

    def open_menu(self, index: int = 0) -> bool:
        """Open the pulldown of bar entry ``index`` — what the app's
        menu-activation key (F10, a bare Alt tap) calls. Once open, ←/→ walk
        the bar and escape closes. Returns False without opening on a
        ``native_menus`` backend (the OS bar owns activation there), before
        the first draw, and while a pulldown is already up (the open pulldown
        is modal, so the activation key reaching the *bar* again means a
        programmatic double call, not the user)."""
        if self._installed_native or self._panel is None or self._open:
            return False
        entries = self._entries()
        if not entries:
            return False
        self._do_open(index % len(entries))
        return True

    def mnemonic_index(self, letter: str) -> int | None:
        """The enabled bar entry whose title starts with ``letter``
        (case-insensitive), or None. The auto-mnemonic is the title's first
        letter — File is Alt+F without any markup in the label."""
        letter = letter.lower()
        for i, item in enumerate(self._entries()):
            if item.label.lstrip()[:1].lower() == letter and item.is_enabled():
                return i
        return None

    def open_menu_mnemonic(self, letter: str) -> bool:
        """Open the pulldown whose title starts with ``letter`` — what the
        app's Alt+letter accelerator calls (Alt+F → File). Same refusals as
        :meth:`open_menu`, plus False when no enabled title matches. With a
        pulldown already open, Alt+letter reaches the modal popup instead,
        which jumps between bar entries itself."""
        if self._installed_native or self._panel is None or self._open:
            return False
        index = self.mnemonic_index(letter)
        if index is None:
            return False
        self._do_open(index)
        return True

    def _do_open(self, index: int) -> None:
        entries = self._entries()
        if self._panel is None or not (0 <= index < len(entries)):
            return
        item = entries[index]
        menu = item.submenu if item.submenu is not None else Menu(item)
        # The entry's left edge from the last render; a pre-render open (no
        # spans yet) falls back to the bar's own left edge.
        x0 = self._entry_x[index][0] if index < len(self._entry_x) else float(_PAD)
        rx, ry, _rw, rh = self._abs
        self._index = index
        self._open = True
        self._panel.popup_menu(
            menu, rx + x0, ry + rh,
            on_done=self._pulldown_closed, bar=self,
        )

    def _pulldown_closed(self) -> None:
        self._open = False

    def _navigate(self, direction: int) -> None:
        """←/→ handed up from the open pulldown, which has already dismissed
        itself: open the neighboring entry's pulldown, wrapping at the ends."""
        entries = self._entries()
        if entries:
            self._do_open((self._index + direction) % len(entries))

    # --- events --------------------------------------------------------------

    def handle_event(self, event: Event) -> bool:
        if event.type is EventType.MOUSE_CLICK and event.x is not None:
            for i, (x0, x1, _item) in enumerate(self._entry_x):
                if x0 <= event.x < x1:
                    self._do_open(i)
                    return True
        return False
