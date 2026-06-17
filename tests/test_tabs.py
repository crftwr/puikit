"""Tests for the Tabs widget, run against TUI and GUI memory profiles."""

import pytest

from puikit import Event, EventType, Panel, PROFILE_GUI_DESKTOP, PROFILE_TUI
from puikit.widgets import Label, Tabs
from puikit.backends.memory_backend import MemoryBackend


@pytest.fixture(params=[PROFILE_TUI, PROFILE_GUI_DESKTOP], ids=["tui", "gui"])
def backend(request):
    return MemoryBackend(width=40, height=10, capabilities=request.param)


def _key(name):
    return Event(type=EventType.KEY, key=name)


def _tabs(changes=None):
    on_change = (lambda i, t: changes.append(t)) if changes is not None else None
    return Tabs(
        [
            ("One", Label("first body")),
            ("Two", Label("second body")),
            ("Three", Label("third body")),
        ],
        on_change=on_change,
    )


def test_tabs_render_strip_and_active_content(backend):
    panel = Panel(backend)
    panel.add(_tabs(), x=0, y=0, w=40, h=10)
    panel.render()
    snap = backend.snapshot()
    assert "One" in snap[0] and "Two" in snap[0] and "Three" in snap[0]
    # The active (first) tab's content shows below the strip.
    assert any("first body" in row for row in snap[1:])


def test_tabs_switch_with_arrows(backend):
    changes = []
    panel = Panel(backend)
    tabs = _tabs(changes)
    panel.add(tabs, x=0, y=0, w=40, h=10)
    panel.render()
    panel.dispatch_event(_key("right"))
    assert tabs.selected == 1 and changes == ["Two"]
    panel.render()
    assert any("second body" in row for row in backend.snapshot()[1:])
    panel.dispatch_event(_key("left"))
    assert tabs.selected == 0 and changes == ["Two", "One"]


def test_tabs_clamp_at_ends(backend):
    panel = Panel(backend)
    tabs = _tabs()
    panel.add(tabs, x=0, y=0, w=40, h=10)
    panel.render()
    panel.dispatch_event(_key("left"))  # already at 0
    assert tabs.selected == 0
    tabs.selected = 2
    panel.dispatch_event(_key("right"))  # already at last
    assert tabs.selected == 2


def test_tabs_click_selects_tab(backend):
    panel = Panel(backend)
    tabs = _tabs()
    panel.add(tabs, x=0, y=0, w=40, h=10)
    panel.render()
    x0 = tabs._tab_x[1][0]
    panel.dispatch_event(Event(type=EventType.MOUSE_CLICK, x=x0 + 1, y=0, button="left"))
    assert tabs.selected == 1


def test_tabs_forward_event_to_active_content(backend):
    seen = []

    class Probe(Label):
        def handle_event(self, event):
            seen.append(event.key)
            return True

    panel = Panel(backend)
    tabs = Tabs([("A", Probe("a")), ("B", Probe("b"))])
    panel.add(tabs, x=0, y=0, w=40, h=10)
    panel.render()
    panel.dispatch_event(_key("enter"))  # not left/right -> forwarded
    assert seen == ["enter"]
