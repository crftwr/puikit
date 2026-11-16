"""Menu hover (motion with no button held): a submenu opens when the pointer
rests on its row, closes when the pointer moves to another row of the parent,
and moving over another bar title while a pulldown is open switches to it."""

import time

from puikit import Item, Menu, MenuItem, Panel, VSplit
from puikit.backends.memory_backend import MemoryBackend
from puikit.event import Event, EventType
from puikit.widgets import Label, MenuBar
from puikit.widgets.menu import MenuPopup


def _setup():
    backend = MemoryBackend(width=50, height=14)
    backend.open()
    panel = Panel(backend)
    menu = Menu(
        MenuItem("File", submenu=Menu(
            MenuItem("Open"),
            MenuItem("Recent", submenu=Menu(MenuItem("a.txt"), MenuItem("b.txt"))),
            MenuItem("Quit"))),
        MenuItem("Edit", submenu=Menu(MenuItem("Copy"), MenuItem("Paste"))),
    )
    bar = MenuBar(menu)
    panel.set_layout(VSplit(Item(bar, size="content"), Item(Label("body"), weight=1)))
    panel.render()
    return backend, panel, bar


def _find(backend, text):
    for y, row in enumerate(backend.snapshot()):
        x = "".join(row).find(text)
        if x >= 0:
            return float(x), float(y)
    raise AssertionError(f"{text!r} not on screen")


def _move(panel, at):
    panel.dispatch_event(Event(EventType.MOUSE_MOVE, x=at[0], y=at[1]))
    panel.render()


def _popups(panel):
    return [s.widget for s in panel._layers if isinstance(s.widget, MenuPopup)]


def _rest(backend, panel):
    time.sleep(MenuPopup.SUBMENU_HOVER_DELAY + 0.05)
    backend.fire_timers()
    panel.render()


def test_resting_on_a_row_opens_its_submenu_and_leaving_closes_it():
    backend, panel, bar = _setup()
    bar.open_menu(0)
    panel.render()
    _move(panel, _find(backend, "Recent"))
    assert len(_popups(panel)) == 1          # not before the pointer has rested
    _rest(backend, panel)
    assert len(_popups(panel)) == 2
    assert "a.txt" in "\n".join("".join(r) for r in backend.snapshot())
    _move(panel, _find(backend, "Quit"))     # another row of the parent
    assert len(_popups(panel)) == 1
    assert _popups(panel)[0].cursor == 2     # the cursor follows the pointer
    backend.close()


def test_a_sweep_does_not_open_a_submenu_it_passes_over():
    backend, panel, bar = _setup()
    bar.open_menu(0)
    panel.render()
    _move(panel, _find(backend, "Recent"))
    _move(panel, _find(backend, "Quit"))
    _rest(backend, panel)
    assert len(_popups(panel)) == 1
    backend.close()


def test_moving_over_another_title_switches_the_pulldown():
    backend, panel, bar = _setup()
    bar.open_menu(0)
    panel.render()
    _move(panel, _find(backend, "Edit"))
    popups = _popups(panel)
    assert len(popups) == 1 and popups[0].menu.items[0].label == "Copy"
    assert bar._index == 1
    backend.close()
