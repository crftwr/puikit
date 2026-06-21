"""Tests for the MessageBox modal, run against TUI and GUI memory profiles."""

import pytest

from puikit import Event, EventType, Panel, PROFILE_GUI_DESKTOP, PROFILE_TUI
from puikit.widgets import show_message_box
from puikit.backends.memory_backend import MemoryBackend


@pytest.fixture(params=[PROFILE_TUI, PROFILE_GUI_DESKTOP], ids=["tui", "gui"])
def backend(request):
    return MemoryBackend(width=60, height=20, capabilities=request.param)


def _key(name, char=None):
    return Event(type=EventType.KEY, key=name, char=char)


def test_message_box_pushes_modal_layer_and_renders(backend):
    panel = Panel(backend)
    show_message_box(panel, "Something happened.", title="Notice", buttons=("OK",))
    assert len(panel._layers) == 1
    panel.render()
    assert any("Notice" in row for row in backend.snapshot())
    assert any("Something happened." in row for row in backend.snapshot())
    assert any("OK" in row for row in backend.snapshot())


def test_message_box_enter_returns_focused_button(backend):
    results = []
    panel = Panel(backend)
    show_message_box(
        panel, "Save changes?", title="Confirm",
        buttons=("Save", "Discard", "Cancel"), default=0, on_result=results.append,
    )
    panel.render()
    panel.dispatch_event(_key("enter"))
    assert results == ["Save"]
    assert panel._layers == []


def test_message_box_arrows_move_focus(backend):
    results = []
    panel = Panel(backend)
    show_message_box(
        panel, "Pick", buttons=("A", "B", "C"), on_result=results.append,
    )
    panel.render()
    panel.dispatch_event(_key("right"))
    panel.dispatch_event(_key("right"))
    panel.dispatch_event(_key("enter"))
    assert results == ["C"]


def test_message_box_escape_picks_cancel(backend):
    results = []
    panel = Panel(backend)
    show_message_box(
        panel, "Quit?", buttons=("Yes", "No"), on_result=results.append,
    )
    panel.render()
    panel.dispatch_event(_key("escape"))  # cancel defaults to the last button
    assert results == ["No"]
    assert panel._layers == []


def test_message_box_click_activates_button(backend):
    results = []
    panel = Panel(backend)
    box = show_message_box(
        panel, "Choose", buttons=("Left", "Right"), on_result=results.append,
    )
    panel.render()
    rect = panel._layers[0].rect
    # box-local rect of the "Right" button, captured during draw
    x0, _x1, y0, _y1 = box._button_x[1]
    # Click that button in screen coords; the modal layer gets it translated.
    panel.dispatch_event(
        Event(type=EventType.MOUSE_CLICK, x=rect.x + x0, y=rect.y + y0, button="left")
    )
    assert results == ["Right"]
    assert panel._layers == []
