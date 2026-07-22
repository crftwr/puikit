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
    separators and disabled rows), enter/right open a submenu or fire the item
    and dismiss the whole chain, left/escape back out one level, and an outside
    click cancels. Submenus open as nested popups to the right of their row."""

    def __init__(
        self,
        menu: Menu,
        row_h: float = 1.0,
        parent: "MenuPopup | None" = None,
        on_close: Callable[[], None] | None = None,
    ):
        self.menu = menu
        self._row_h = row_h
        self.parent = parent
        # Root popup only: called once the whole chain is torn down (e.g. the
        # Panel's on_done for a context menu).
        self.on_close = on_close
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
            if key == "up":
                self._step_cursor(-1)
            elif key == "down":
                self._step_cursor(1)
            elif key in ("enter", "right") or event.char == " ":
                if 0 <= self.cursor < len(self.menu.items):
                    self._activate(self.cursor)
            elif key in ("escape", "left"):
                self._back()
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
    of the top-level titles, each opening a ``MenuPopup`` below it."""

    focusable = True

    def __init__(self, menu: Menu, style: Style = DEFAULT_STYLE):
        # A Menu whose items each carry a submenu (their labels are the bar
        # entries). Plain items without a submenu still work (they fire on click).
        self.menu = menu
        self.style = style
        self.highlight = 0
        self._panel = None
        self._installed_native = False
        self._abs: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 1.0)
        self._entry_x: list[tuple[int, int, MenuItem]] = []  # (x0, x1, item) per render

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
        if entries:
            self.highlight = max(0, min(self.highlight, len(entries) - 1))
        self._entry_x = []
        x = float(_PAD)
        for i, item in enumerate(entries):
            # Pad with real base units, not surrounding spaces (which collapse to
            # a thin gap under a proportional font); the span the title occupies
            # is the padding plus the measured label.
            w = ctx.measure_text(item.label)
            span = _TITLE_PAD + w + _TITLE_PAD
            focused_here = ctx.focused and i == self.highlight
            if focused_here:
                ctx.fill_rect(x, 0, span, hu, Style(bg=theme.selection_bg))
            fg = theme.text if item.is_enabled() else theme.muted_text
            bg = theme.selection_bg if focused_here else theme.popup_bg
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

    def _open(self, item: MenuItem, x0: int) -> None:
        if self._panel is None:
            return
        menu = item.submenu if item.submenu is not None else Menu(item)
        rx, ry, _rw, rh = self._abs
        self._panel.popup_menu(menu, rx + x0, ry + rh)

    # --- events --------------------------------------------------------------

    def handle_event(self, event: Event) -> bool:
        entries = self._entries()
        if not entries:
            return False
        if event.type is EventType.KEY:
            if event.key == "left":
                self.highlight = (self.highlight - 1) % len(entries)
                return True
            if event.key == "right":
                self.highlight = (self.highlight + 1) % len(entries)
                return True
            if event.key in ("enter", "down") or event.char == " ":
                item = entries[self.highlight]
                x0 = self._entry_x[self.highlight][0] if self.highlight < len(self._entry_x) else _PAD
                self._open(item, x0)
                return True
            return False
        if event.type is EventType.MOUSE_CLICK and event.x is not None:
            for i, (x0, x1, item) in enumerate(self._entry_x):
                if x0 <= event.x < x1:
                    self.highlight = i
                    self._open(item, x0)
                    return True
        return False
