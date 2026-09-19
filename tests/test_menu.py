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
from puikit.widgets import Label, MenuBar
from puikit.widgets.menu import popup_geometry


@pytest.fixture(params=[PROFILE_TUI, PROFILE_GUI_DESKTOP], ids=["tui", "gui"])
def backend(request):
    return MemoryBackend(width=40, height=16, capabilities=request.param)


def _key(name, char=None, mods=()):
    return Event(type=EventType.KEY, key=name, char=char, modifiers=frozenset(mods))


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


def test_menu_bar_is_not_a_focus_stop(backend):
    # No desktop puts the menu bar in the Tab order, and click-focus is how
    # xefm#304's stuck highlight happened: the bar took focus on the opening
    # click, the pulldown was modal, and the outside click that dismissed it
    # could never move focus back. The bar must not take part in focus at all.
    panel = Panel(backend)
    bar = MenuBar(_bar_menu([]))
    panel.add(bar, x=0, y=0, w=40, h=1)
    assert panel.get_focused() is None  # never auto-picked as first focusable
    panel.render()
    x0 = bar._entry_x[0][0]
    panel.dispatch_event(_click(x0 + 1, 0))
    assert len(panel._layers) == 1
    assert panel.get_focused() is None  # the opening click moved no focus


def test_menu_bar_highlight_clears_when_pulldown_dismissed(backend):
    # xefm#304: open [File] with the mouse, click elsewhere — the pulldown
    # closes and the title must stop reading as selected.
    panel = Panel(backend)
    bar = MenuBar(_bar_menu([]))
    panel.add(bar, x=0, y=0, w=40, h=1)
    panel.render()
    x0 = bar._entry_x[0][0]
    panel.dispatch_event(_click(x0 + 1, 0))
    panel.render()
    assert bar._open is True
    assert backend.style_at(int(x0) + 1, 0).bg == panel.theme.selection_bg
    panel.dispatch_event(_click(38, 15))  # outside click dismisses
    panel.render()
    assert panel._layers == []
    assert bar._open is False
    assert backend.style_at(int(x0) + 1, 0).bg == panel.theme.popup_bg


def test_menu_bar_left_right_walk_the_bar(backend):
    # With a pulldown open, ←/→ hop between the bar's menus (the desktop
    # convention), wrapping at the ends — xefm#304's third complaint.
    panel = Panel(backend)
    bar = MenuBar(_bar_menu([]))
    panel.add(bar, x=0, y=0, w=40, h=1)
    panel.render()
    panel.dispatch_event(_click(bar._entry_x[0][0] + 1, 0))  # open File
    panel.render()
    panel.dispatch_event(_key("right"))  # hop to Edit
    assert bar._index == 1 and bar._open is True
    assert len(panel._layers) == 1
    panel.render()
    assert panel._layers[-1].widget.menu.items[0].label == "Copy"
    panel.dispatch_event(_key("left"))   # back to File
    assert bar._index == 0
    panel.render()
    assert panel._layers[-1].widget.menu.items[0].label == "New"
    panel.dispatch_event(_key("left"))   # wraps to Edit
    assert bar._index == 1


def test_menu_bar_right_enters_submenu_before_walking(backend):
    # → on a row with a submenu opens the submenu; → inside that submenu (on a
    # plain row) walks to the next bar menu, tearing the whole chain down.
    deep = Menu(
        MenuItem("File", submenu=Menu(
            MenuItem("More", submenu=Menu(MenuItem("Child"))),
        )),
        MenuItem("Edit", submenu=Menu(MenuItem("Copy"))),
    )
    panel = Panel(backend)
    bar = MenuBar(deep)
    panel.add(bar, x=0, y=0, w=40, h=1)
    panel.render()
    panel.dispatch_event(_click(bar._entry_x[0][0] + 1, 0))  # open File
    panel.render()
    panel.dispatch_event(_key("right"))  # cursor on "More": enter its submenu
    assert len(panel._layers) == 2
    assert bar._index == 0
    panel.render()
    panel.dispatch_event(_key("right"))  # "Child" has no submenu: walk to Edit
    assert bar._index == 1
    assert len(panel._layers) == 1
    panel.render()
    assert panel._layers[-1].widget.menu.items[0].label == "Copy"


def test_menu_bar_open_menu_and_toggle_key(backend):
    # open_menu() is the keyboard activation (the app binds F10 / bare Alt to
    # it); the same key with the menu open closes the whole chain.
    panel = Panel(backend)
    fired = []
    bar = MenuBar(_bar_menu(fired))
    assert bar.open_menu() is False  # before the first draw: nowhere to open
    panel.add(bar, x=0, y=0, w=40, h=1)
    panel.render()
    assert bar.open_menu() is True
    assert len(panel._layers) == 1 and bar._open is True
    panel.render()
    panel.dispatch_event(_key("f10"))  # activation key again: toggle closed
    assert panel._layers == [] and bar._open is False
    assert bar.open_menu(1) is True   # explicit entry index
    assert bar._index == 1
    panel.render()
    panel.dispatch_event(_key("enter"))  # commit "Copy"
    assert fired == ["copy"]
    assert bar._open is False


def test_context_menu_arrows_keep_their_meaning(backend):
    # Without a bar to walk, a context menu's ← still closes and → still fires
    # the row (there is no neighbor to hop to).
    panel = Panel(backend)
    fired = []
    panel.popup_menu(_ctx_menu(fired), 2, 2)
    panel.render()
    panel.dispatch_event(_key("right"))  # cursor on "Cut": fires like enter
    assert fired == ["cut"]
    assert panel._layers == []
    panel.popup_menu(_ctx_menu(fired), 2, 2)
    panel.render()
    panel.dispatch_event(_key("left"))   # closes the root
    assert panel._layers == []
    assert fired == ["cut"]


def test_popup_letter_mnemonic_activates_unique_match(backend):
    # A plain letter with one enabled first-letter match fires the row.
    panel = Panel(backend)
    fired = []
    panel.popup_menu(_ctx_menu(fired), 2, 2)
    panel.render()
    panel.dispatch_event(_key("c"))
    assert fired == ["cut"]
    assert panel._layers == []


def test_popup_letter_mnemonic_skips_disabled_and_opens_submenu(backend):
    panel = Panel(backend)
    fired = []
    panel.popup_menu(_ctx_menu(fired), 2, 2)
    panel.render()
    panel.dispatch_event(_key("p"))  # only match "Paste" is disabled: inert
    assert fired == [] and len(panel._layers) == 1
    panel.dispatch_event(_key("m"))  # "More" is a submenu parent: opens it
    assert len(panel._layers) == 2


def test_popup_letter_mnemonic_cycles_ambiguous_matches(backend):
    # Several matches only move the cursor (wrapping), so enter commits the
    # one the user means — the Windows convention.
    menu = Menu(
        MenuItem("Copy", on_select=lambda: fired.append("copy")),
        MenuItem("Cut", on_select=lambda: fired.append("cut")),
        MenuItem("Close", on_select=lambda: fired.append("close")),
    )
    fired = []
    panel = Panel(backend)
    panel.popup_menu(menu, 2, 2)
    popup = panel._layers[-1].widget
    panel.render()
    panel.dispatch_event(_key("c"))
    assert popup.cursor == 1 and fired == []
    panel.dispatch_event(_key("c"))
    assert popup.cursor == 2
    panel.dispatch_event(_key("c"))
    assert popup.cursor == 0  # wrapped
    panel.dispatch_event(_key("enter"))
    assert fired == ["copy"]


def test_menu_bar_alt_letter_opens_matching_menu(backend):
    # The Alt+F accelerator: the title's first letter is its mnemonic.
    panel = Panel(backend)
    bar = MenuBar(_bar_menu([]))
    panel.add(bar, x=0, y=0, w=40, h=1)
    panel.render()
    assert bar.open_menu_mnemonic("z") is False
    assert panel._layers == []
    assert bar.open_menu_mnemonic("e") is True  # Edit
    assert bar._index == 1 and len(panel._layers) == 1
    panel.render()
    assert panel._layers[-1].widget.menu.items[0].label == "Copy"


def test_alt_letter_switches_the_open_pulldown(backend):
    # Alt+letter with a pulldown already open jumps to that bar entry, and an
    # unmatched letter leaves the open menu alone.
    panel = Panel(backend)
    bar = MenuBar(_bar_menu([]))
    panel.add(bar, x=0, y=0, w=40, h=1)
    panel.render()
    bar.open_menu()  # File
    panel.render()
    panel.dispatch_event(_key("z", mods=("alt",)))
    assert bar._index == 0 and len(panel._layers) == 1
    panel.dispatch_event(_key("e", mods=("alt",)))
    assert bar._index == 1 and bar._open is True
    assert len(panel._layers) == 1
    panel.render()
    assert panel._layers[-1].widget.menu.items[0].label == "Copy"


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
        self.menu_bar_gates = []
        self.popup_calls = []

    @property
    def capabilities(self):
        # Re-enable the native_menus the base class forces off for the grid, so
        # the Panel takes the native delegation path this test exercises.
        from puikit import CapabilityProfile

        return CapabilityProfile({**self._capabilities, "vector_shapes": False})

    def set_menu_bar(self, menu, is_active=None):
        self.menu_bar_calls.append(menu)
        self.menu_bar_gates.append(is_active)

    def popup_menu(self, menu, x, y, on_done=None):
        self.popup_calls.append((menu, x, y))
        if on_done is not None:
            on_done()


def test_activation_key_is_offered_until_the_os_bar_takes_over():
    # What an app asks instead of asking the backend what menus it has: the bar
    # reports whether its activation key (F10, a bare Alt tap) does anything, so
    # a help screen can leave out a key that would answer nothing.
    for backend, expected in ((MemoryBackend(width=40, height=16), True),
                              (_NativeBackend(width=40, height=16), False)):
        bar = MenuBar(_bar_menu([]))
        assert bar.takes_activation_key, "an undrawn bar is the in-window one"
        panel = Panel(backend)
        panel.add(bar, x=0, y=0, w=40, h=1)
        panel.render()
        assert bar.takes_activation_key is expected
        # And it agrees with what opening actually does.
        assert bar.open_menu() is expected


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
    # Keyboard activation is the OS bar's own job here — open_menu declines
    # rather than popping a widget context menu over a native bar.
    assert bar.open_menu() is False
    assert backend.popup_calls == []

    done = []
    panel.popup_menu(menu, 3, 4, on_done=lambda: done.append(True))
    assert backend.popup_calls == [(menu, 3, 4)]
    assert done == [True]
    assert panel._layers == []  # native path pushes no widget layer


def test_native_menu_bar_goes_inert_while_a_modal_layer_is_up():
    # xefm#388: an OS bar lives outside the layer stack — a click on it never
    # passes through dispatch_event, so without this gate every item would go
    # on driving the surface under an open dialog. The in-window bar needs no
    # such gate: there the top interactive layer swallows the click on the
    # strip, which is the behaviour this makes the OS bar agree with.
    backend = _NativeBackend(width=40, height=16)
    panel = Panel(backend)
    bar = MenuBar(_bar_menu([]))
    panel.add(bar, x=0, y=0, w=40, h=1)
    panel.render()
    is_active = backend.menu_bar_gates[-1]
    assert is_active is not None
    assert is_active() is True

    panel.push_layer(Label("DIALOG"), z=10, hints={"w": 10, "h": 4})
    assert is_active() is False
    panel.pop_layer()
    assert is_active() is True


def test_native_menu_bar_stays_active_under_a_non_interactive_overlay():
    # What makes the bar inert is a layer owning the keyboard, not a layer
    # existing: a completion popup floats above the surface without taking it.
    backend = _NativeBackend(width=40, height=16)
    panel = Panel(backend)
    bar = MenuBar(_bar_menu([]))
    panel.add(bar, x=0, y=0, w=40, h=1)
    panel.render()
    is_active = backend.menu_bar_gates[-1]
    panel.push_layer(Label("HINT"), z=11, hints={"w": 10, "h": 3},
                     interactive=False)
    assert is_active() is True


def test_set_menu_redraws_the_bar_from_the_new_menu(backend):
    # xefm#382: an app whose menu labels quote the live keymap rebuilds the menu
    # when the user rebinds a key. The bar must show what it was just handed.
    panel = Panel(backend)
    fired = []
    bar = MenuBar(_bar_menu(fired))
    panel.add(bar, x=0, y=0, w=40, h=1)
    panel.render()
    assert "Edit" in backend.snapshot()[0]

    bar.set_menu(Menu(
        MenuItem("File", submenu=Menu(
            MenuItem("New", on_select=lambda: fired.append("new2")))),
        MenuItem("View", submenu=Menu(MenuItem("Zoom"))),
    ))
    panel.render()
    row = backend.snapshot()[0]
    assert "View" in row and "Edit" not in row

    # And the entry opens the submenu it now carries, not the one it replaced.
    panel.dispatch_event(_click(bar._entry_x[1][0] + 1, 0))
    panel.render()
    assert panel._layers[-1].widget.menu.items[0].label == "Zoom"


def test_set_menu_reinstalls_the_os_bar():
    # The OS bar is installed once, on the first draw; replacing the menu is the
    # only way to change it, so a swap must re-register rather than leave the
    # platform showing the menu built at startup (xefm#382).
    backend = _NativeBackend(width=40, height=16)
    panel = Panel(backend)
    first = _bar_menu([])
    bar = MenuBar(first)
    panel.add(bar, x=0, y=0, w=40, h=1)
    panel.render()
    assert backend.menu_bar_calls == [first]

    second = _bar_menu([])
    bar.set_menu(second)
    assert backend.menu_bar_calls == [first, second]
    panel.render()
    assert backend.menu_bar_calls == [first, second], "the draw re-installs nothing"


def test_set_menu_before_the_first_draw_installs_only_the_new_one():
    # Nothing is registered yet, so there is nothing to replace — the first draw
    # installs the menu the bar holds by then, exactly once.
    backend = _NativeBackend(width=40, height=16)
    panel = Panel(backend)
    bar = MenuBar(_bar_menu([]))
    second = _bar_menu([])
    bar.set_menu(second)
    panel.add(bar, x=0, y=0, w=40, h=1)
    panel.render()
    assert backend.menu_bar_calls == [second]
