"""Widget tests run identically against the TUI and GUI capability profiles."""

import pytest

from puikit import Event, EventType, Panel, PROFILE_GUI_DESKTOP, PROFILE_TUI, TextAttribute
from puikit.backends.memory_backend import MemoryBackend
from puikit.widgets import Label, ListView, ScrollBar


@pytest.fixture(params=[PROFILE_TUI, PROFILE_GUI_DESKTOP], ids=["tui", "gui"])
def backend(request):
    return MemoryBackend(width=30, height=10, capabilities=request.param)


def test_label_renders_text(backend):
    panel = Panel(backend)
    panel.add(Label("hello"), x=0, y=0, w=10, h=1)
    panel.render()
    assert backend.snapshot()[0].startswith("hello")


def test_listview_renders_visible_slice_and_selection(backend):
    panel = Panel(backend)
    listview = ListView([f"item{i}" for i in range(20)])
    panel.add(listview, x=0, y=0, w=10, h=5)
    panel.render()
    lines = backend.snapshot()
    assert lines[0].startswith("item0")
    assert lines[4].startswith("item4")
    # Selected row is drawn reversed.
    assert backend.style_at(0, 0).attr & TextAttribute.REVERSE
    assert not backend.style_at(0, 1).attr & TextAttribute.REVERSE
    # Long list shows a scrollbar in the last column, painted via base unit
    # background color (thumb or track) rather than a glyph.
    assert backend.style_at(9, 0).bg in {(150, 150, 150), (60, 60, 60)}


def test_listview_keyboard_navigation_scrolls(backend):
    panel = Panel(backend)
    listview = ListView([f"item{i}" for i in range(20)])
    panel.add(listview, x=0, y=0, w=10, h=5)
    panel.render()
    for _ in range(7):
        panel.dispatch_event(Event(type=EventType.KEY, key="down"))
    assert listview.selected == 7
    panel.render()
    assert backend.snapshot()[4].startswith("item7")  # scrolled so selection is visible
    panel.dispatch_event(Event(type=EventType.KEY, key="end"))
    assert listview.selected == 19
    panel.dispatch_event(Event(type=EventType.KEY, key="home"))
    assert listview.selected == 0


def test_listview_mouse_click_selects(backend):
    panel = Panel(backend)
    listview = ListView([f"item{i}" for i in range(20)])
    panel.add(listview, x=2, y=1, w=10, h=5)
    panel.dispatch_event(Event(type=EventType.MOUSE_CLICK, x=3, y=3, button="left"))
    assert listview.selected == 2  # row 2 inside the widget


def test_listview_enter_triggers_on_select(backend):
    selections = []
    panel = Panel(backend)
    listview = ListView(["a", "b", "c"], on_select=lambda i, t: selections.append((i, t)))
    panel.add(listview, x=0, y=0, w=10, h=3)
    panel.dispatch_event(Event(type=EventType.KEY, key="down"))
    panel.dispatch_event(Event(type=EventType.KEY, key="enter"))
    assert selections == [(1, "b")]


def test_listview_mouse_scroll_moves_viewport_not_selection(backend):
    panel = Panel(backend)
    listview = ListView([f"item{i}" for i in range(20)])
    panel.add(listview, x=0, y=0, w=10, h=5)
    panel.render()
    panel.dispatch_event(Event(type=EventType.MOUSE_SCROLL, x=1, y=1, scroll=-3))
    assert listview.offset == 3
    assert listview.selected == 0  # selection stays put
    # The scrolled view survives a render even though the selection is off-screen.
    panel.render()
    assert listview.offset == 3
    assert backend.snapshot()[0].startswith("item3")


def test_listview_mouse_scroll_clamps_at_bounds(backend):
    panel = Panel(backend)
    listview = ListView([f"item{i}" for i in range(20)])
    panel.add(listview, x=0, y=0, w=10, h=5)
    panel.render()
    panel.dispatch_event(Event(type=EventType.MOUSE_SCROLL, x=1, y=1, scroll=5))
    assert listview.offset == 0  # already at the top
    panel.dispatch_event(Event(type=EventType.MOUSE_SCROLL, x=1, y=1, scroll=-100))
    assert listview.offset == 15  # bottom: len(items) - viewport_h


def test_listview_keyboard_pulls_selection_back_into_view(backend):
    panel = Panel(backend)
    listview = ListView([f"item{i}" for i in range(20)])
    panel.add(listview, x=0, y=0, w=10, h=5)
    panel.render()
    panel.dispatch_event(Event(type=EventType.MOUSE_SCROLL, x=1, y=1, scroll=-10))
    assert listview.offset == 10
    panel.dispatch_event(Event(type=EventType.KEY, key="down"))
    assert listview.selected == 1
    assert listview.offset == 1  # viewport follows the selection again


def test_listview_on_change_fires_only_when_selection_moves(backend):
    changes = []
    panel = Panel(backend)
    listview = ListView(["a", "b", "c"], on_change=lambda i, t: changes.append((i, t)))
    panel.add(listview, x=0, y=0, w=10, h=3)
    panel.render()  # establish the viewport
    panel.dispatch_event(Event(type=EventType.KEY, key="down"))
    assert changes == [(1, "b")]
    panel.dispatch_event(Event(type=EventType.KEY, key="enter"))  # no move
    assert changes == [(1, "b")]
    panel.dispatch_event(Event(type=EventType.MOUSE_CLICK, x=1, y=0, button="left"))
    assert changes == [(1, "b"), (0, "a")]
    panel.dispatch_event(Event(type=EventType.KEY, key="up"))  # clamped at top
    assert changes == [(1, "b"), (0, "a")]


def test_scrollbar_thumb_position(backend):
    panel = Panel(backend)
    panel.add(ScrollBar(pos=1.0, ratio=0.3), x=0, y=0, w=1, h=10)
    panel.render()
    # The bar is painted with base unit background colors; the thumb is the lighter
    # background, the track the darker one.
    thumb, track = (150, 150, 150), (60, 60, 60)
    column = [backend.style_at(0, row).bg for row in range(10)]
    assert column[0] == track
    assert column[9] == thumb  # thumb at the bottom for pos=1.0
    assert column.count(thumb) == 3  # 30% of 10 rows
