"""Tests for the menu model, the widget-rendered fallback, and native routing.

Run against the TUI and GUI memory profiles alike — the widget fallback is the
same on both (the memory backend renders to a grid either way); a separate
recording backend covers the native_menus delegation path.
"""

import pytest

from puikit import (
    Event,
    EventType,
    Menu,
    MenuItem,
    MenuSeparator,
    Panel,
    PROFILE_GUI_DESKTOP,
    PROFILE_TUI,
    SEPARATOR,
)
from puikit.backends.memory_backend import MemoryBackend
from puikit.layout import LayoutContext
from puikit.widgets import MenuBar
from puikit.widgets.menu import popup_geometry


@pytest.fixture(params=[PROFILE_TUI, PROFILE_GUI_DESKTOP], ids=["tui", "gui"])
def backend(request):
    return MemoryBackend(width=40, height=16, capabilities=request.param)


def _key(name, char=None):
    return Event(type=EventType.KEY, key=name, char=char)


def _click(x, y):
    return Event(type=EventType.MOUSE_CLICK, x=x, y=y, button="left")


# --- model -------------------------------------------------------------------


def test_menu_item_predicates_evaluated_live():
    state = {"on": False}
    item = MenuItem("Toggle", enabled=lambda: state["on"], checked=lambda: state["on"])
    assert item.is_enabled() is False and item.is_checked() is False
    state["on"] = True
    assert item.is_enabled() is True and item.is_checked() is True


def test_menu_item_activate_respects_enabled():
    fired = []
    item = MenuItem("Go", on_select=lambda: fired.append(1), enabled=False)
    item.activate()
    assert fired == []  # disabled: callback suppressed
    item.enabled = True
    item.activate()
    assert fired == [1]


def test_menu_selectable_excludes_separators():
    menu = Menu(MenuItem("a"), SEPARATOR, MenuItem("b"))
    assert [it.label for it in menu.selectable] == ["a", "b"]
    assert isinstance(menu.items[1], MenuSeparator)


# --- popup (context menu) ----------------------------------------------------


def _ctx_menu(fired):
    return Menu(
        MenuItem("Cut", on_select=lambda: fired.append("cut")),
        SEPARATOR,
        MenuItem("Paste", on_select=lambda: fired.append("paste"), enabled=False),
        MenuItem(
            "More",
            submenu=Menu(MenuItem("Child", on_select=lambda: fired.append("child"))),
        ),
    )


def test_popup_menu_pushes_layer_and_skips_separator_and_disabled(backend):
    panel = Panel(backend)
    fired = []
    panel.popup_menu(_ctx_menu(fired), 2, 2)
    assert len(panel._layers) == 1
    popup = panel._layers[-1].widget
    panel.render()
    assert popup.cursor == 0  # first selectable
    panel.dispatch_event(_key("down"))  # skip separator(1) + disabled(2) -> More(3)
    assert popup.cursor == 3


def test_popup_menu_activates_item_and_dismisses(backend):
    panel = Panel(backend)
    fired = []
    panel.popup_menu(_ctx_menu(fired), 2, 2)
    panel.render()
    panel.dispatch_event(_key("enter"))  # activate "Cut" at cursor 0
    assert fired == ["cut"]
    assert panel._layers == []  # whole chain torn down


def test_popup_menu_submenu_opens_and_commits(backend):
    panel = Panel(backend)
    fired = []
    panel.popup_menu(_ctx_menu(fired), 2, 2)
    panel.render()
    panel.dispatch_event(_key("down"))   # -> More (submenu parent)
    panel.dispatch_event(_key("right"))  # open submenu
    assert len(panel._layers) == 2
    panel.render()
    panel.dispatch_event(_key("enter"))  # commit "Child"
    assert fired == ["child"]
    assert panel._layers == []


def test_popup_menu_escape_backs_out_one_level(backend):
    panel = Panel(backend)
    fired = []
    panel.popup_menu(_ctx_menu(fired), 2, 2)
    panel.render()
    panel.dispatch_event(_key("down"))
    panel.dispatch_event(_key("right"))  # open submenu -> 2 layers
    assert len(panel._layers) == 2
    panel.render()  # the child popup captures its panel on draw (like DropDown)
    panel.dispatch_event(_key("escape"))  # back to parent
    assert len(panel._layers) == 1
    panel.dispatch_event(_key("escape"))  # close root
    assert panel._layers == []
    assert fired == []


def test_popup_menu_outside_click_cancels(backend):
    panel = Panel(backend)
    fired = []
    panel.popup_menu(_ctx_menu(fired), 2, 2)
    panel.render()
    # A click far outside the popup rows dismisses without firing.
    panel.dispatch_event(_click(38, 15))
    assert panel._layers == []
    assert fired == []


def test_popup_geometry_sizes_to_widest_row():
    menu = Menu(MenuItem("Short"), MenuItem("A much longer label", shortcut="Cmd+L"))
    w, h, row_h = popup_geometry(menu, lambda s: float(len(s)), vector=False)
    assert row_h == 1.0
    assert h == 2.0
    assert w >= len("A much longer label") + len("Cmd+L")


# --- menu bar ----------------------------------------------------------------


def _bar_menu(fired):
    return Menu(
        MenuItem("File", submenu=Menu(MenuItem("New", on_select=lambda: fired.append("new")))),
        MenuItem("Edit", submenu=Menu(MenuItem("Copy", on_select=lambda: fired.append("copy")))),
    )


def test_menu_bar_renders_titles_and_opens_popup(backend):
    panel = Panel(backend)
    fired = []
    bar = MenuBar(_bar_menu(fired))
    panel.add(bar, x=0, y=0, w=40, h=1)
    panel.render()
    assert "File" in backend.snapshot()[0]
    assert "Edit" in backend.snapshot()[0]
    # Click on the "File" title opens its submenu popup as a layer.
    x0 = bar._entry_x[0][0]
    panel.dispatch_event(_click(x0 + 1, 0))
    assert len(panel._layers) == 1
    panel.render()
    panel.dispatch_event(_key("enter"))  # commit "New"
    assert fired == ["new"]


def test_menu_bar_collapses_to_zero_height_when_native():
    bar = MenuBar(Menu(MenuItem("File", submenu=Menu(MenuItem("New")))))
    native = LayoutContext(1, 1, snap=True, native_menus=True)
    plain = LayoutContext(1, 1, snap=True, native_menus=False)
    assert bar.measure(native, "y", 0.0).preferred == 0.0
    assert bar.measure(plain, "y", 0.0).preferred == 1.0


# --- native delegation -------------------------------------------------------


class _NativeBackend(MemoryBackend):
    """A memory backend that claims native_menus and records the calls."""

    def __init__(self, **kwargs):
        super().__init__(capabilities=PROFILE_GUI_DESKTOP, **kwargs)
        self.menu_bar_calls = []
        self.popup_calls = []

    @property
    def capabilities(self):
        # Re-enable the native_menus the base class forces off for the grid, so
        # the Panel takes the native delegation path this test exercises.
        from puikit import CapabilityProfile

        return CapabilityProfile({**self._capabilities, "vector_shapes": False})

    def set_menu_bar(self, menu):
        self.menu_bar_calls.append(menu)

    def popup_menu(self, menu, x, y, on_done=None):
        self.popup_calls.append((menu, x, y))
        if on_done is not None:
            on_done()


def test_native_backend_receives_menu_bar_and_popup():
    backend = _NativeBackend(width=40, height=16)
    assert backend.capabilities.supports("native_menus")
    panel = Panel(backend)
    menu = _bar_menu([])
    # MenuBar on a native backend registers the OS bar and draws no strip.
    bar = MenuBar(menu)
    panel.add(bar, x=0, y=0, w=40, h=1)
    panel.render()
    assert backend.menu_bar_calls == [menu]

    done = []
    panel.popup_menu(menu, 3, 4, on_done=lambda: done.append(True))
    assert backend.popup_calls == [(menu, 3, 4)]
    assert done == [True]
    assert panel._layers == []  # native path pushes no widget layer
