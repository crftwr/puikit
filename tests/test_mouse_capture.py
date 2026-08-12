"""A mouse gesture belongs to the widget it began in, at every level of the tree.

The Panel already routes a drag/release to the slot that took the press; these
cover the same rule inside the container widgets, which used to hit-test every
event on its own — so a selection dragged out of one pane silently became the
neighbor's business, and the widget the user was actually dragging in never
heard the rest of its own gesture.
"""

import pytest

from puikit import Event, EventType, Panel, PROFILE_GUI_DESKTOP, PROFILE_TUI
from puikit.backends.memory_backend import MemoryBackend
from puikit.layout import Item, VSplit
from puikit.widgets import Container, LayoutView, ScrollView
from puikit.widgets.base import Widget
from puikit.widgets.splitter import Splitter


class Spy(Widget):
    """A leaf that records the mouse events it is given, in its own coordinates."""

    focusable = True

    def __init__(self, name: str):
        self.name = name
        self.seen: list[tuple[str, float, float]] = []
        self.hints: list[dict] = []

    def draw(self, ctx) -> None:
        pass

    def handle_event(self, event: Event) -> bool:
        if event.x is not None:
            self.seen.append((event.type.name, event.x, event.y))
            self.hints.append(dict(event.hints))
        return True

    def types(self) -> list[str]:
        return [t for t, _x, _y in self.seen]


@pytest.fixture(params=[PROFILE_TUI, PROFILE_GUI_DESKTOP], ids=["tui", "gui"])
def backend(request):
    return MemoryBackend(width=20, height=10, capabilities=request.param)


def _drag(panel, x0, y0, *points, release=None):
    panel.dispatch_event(Event(type=EventType.MOUSE_DOWN, x=x0, y=y0, button="left"))
    for x, y in points:
        panel.dispatch_event(Event(type=EventType.MOUSE_DRAG, x=x, y=y, button="left"))
    if release is not None:
        panel.dispatch_event(Event(type=EventType.MOUSE_UP, x=release[0], y=release[1], button="left"))


def test_container_drag_stays_with_the_pressed_child(backend):
    panel = Panel(backend)
    top, bottom = Spy("top"), Spy("bottom")
    box = Container()
    box.add(top, x=0, y=0, w=20, h=5)
    box.add(bottom, x=0, y=5, w=20, h=5)
    panel.add(box, x=0, y=0, w=20, h=10)
    panel.render()

    _drag(panel, 2, 7, (2, 6), (2, 2), (2, 0))
    # Pressed in the lower child: every drag point reaches it, including the ones
    # over its sibling, and the sibling is left alone.
    assert bottom.types() == ["MOUSE_DOWN", "MOUSE_DRAG", "MOUSE_DRAG", "MOUSE_DRAG"]
    assert top.seen == []
    # Coordinates stay in the pressed child's frame — negative once the pointer
    # has climbed above it, which is exactly what a widget needs to tell how far
    # out the drag has gone.
    assert bottom.seen[-1] == ("MOUSE_DRAG", 2.0, -5.0)


def test_container_click_is_cancelled_when_released_off_the_pressed_child(backend):
    panel = Panel(backend)
    top, bottom = Spy("top"), Spy("bottom")
    box = Container()
    box.add(top, x=0, y=0, w=20, h=5)
    box.add(bottom, x=0, y=5, w=20, h=5)
    panel.add(box, x=0, y=0, w=20, h=10)
    panel.render()

    _drag(panel, 2, 7, (2, 2), release=(2, 2))
    # The release landed over the sibling: it must not receive a click it was
    # never pressed for, and the pressed child's gesture ends without one.
    assert top.seen == []
    assert "MOUSE_CLICK" not in bottom.types()


def test_container_click_still_fires_when_released_over_the_pressed_child(backend):
    panel = Panel(backend)
    child = Spy("only")
    box = Container()
    box.add(child, x=0, y=0, w=20, h=10)
    panel.add(box, x=0, y=0, w=20, h=10)
    panel.render()

    _drag(panel, 2, 2, (3, 3), release=(3, 3))
    assert child.types() == ["MOUSE_DOWN", "MOUSE_DRAG", "MOUSE_UP", "MOUSE_CLICK"]


def test_layout_view_drag_stays_with_the_pressed_child(backend):
    panel = Panel(backend)
    body, footer = Spy("body"), Spy("footer")
    view = LayoutView(VSplit(Item(body, weight=1), Item(footer, size=2)))
    panel.add(view, x=0, y=0, w=20, h=10)
    panel.render()

    _drag(panel, 2, 2, (2, 9), (2, 40))
    assert body.types() == ["MOUSE_DOWN", "MOUSE_DRAG", "MOUSE_DRAG"]
    assert footer.seen == []


def test_splitter_drag_stays_with_the_pressed_pane(backend):
    panel = Panel(backend)
    first, second = Spy("first"), Spy("second")
    split = Splitter(first, second, orientation="vertical", fraction=0.5,
                     min_first=2, min_second=2)
    panel.add(split, x=0, y=0, w=20, h=10)
    panel.render()

    before = split.fraction
    _drag(panel, 2, 8, (2, 6), (2, 5), (2, 1))
    assert second.types() == ["MOUSE_DOWN", "MOUSE_DRAG", "MOUSE_DRAG", "MOUSE_DRAG"]
    assert first.seen == []
    # And sweeping across the handle's grab margin mid-gesture must not turn the
    # selection drag into a divider drag.
    assert split.fraction == before


def test_splitter_handle_drag_still_moves_the_divider(backend):
    panel = Panel(backend)
    split = Splitter(Spy("first"), Spy("second"), orientation="vertical",
                     fraction=0.5, min_first=2, min_second=2)
    panel.add(split, x=0, y=0, w=20, h=10)
    panel.render()

    before = split.fraction
    _drag(panel, 2, split._handle_rect.y, (2, 8))
    assert split.fraction > before


def test_scroll_view_drag_stays_with_the_pressed_child(backend):
    panel = Panel(backend)
    a, b = Spy("a"), Spy("b")
    view = ScrollView([(a, 3), (b, 3)], gap=0)
    panel.add(view, x=0, y=0, w=20, h=10)
    panel.render()

    _drag(panel, 2, 4, (2, 1), (2, 0))
    assert b.types() == ["MOUSE_DOWN", "MOUSE_DRAG", "MOUSE_DRAG"]
    assert a.seen == []


def test_panel_reports_the_unclamped_pointer_through_the_tree(backend):
    # A drag out of the *window* is clamped onto the slot's edge, so the widget
    # would otherwise see the same coordinate however far out the pointer went.
    # The pre-clamp position rides along in hints, translated into each widget's
    # own frame on the way down.
    panel = Panel(backend)
    child = Spy("child")
    view = LayoutView(VSplit(Item(Spy("head"), size=4), Item(child, weight=1)))
    panel.add(view, x=0, y=0, w=20, h=10)
    panel.render()

    _drag(panel, 2, 6, (2, 25))
    kind, _x, y = child.seen[-1]
    assert kind == "MOUSE_DRAG"
    # Clamped onto the window's bottom edge, which is 6 units into this child...
    assert y == pytest.approx(6.0, abs=0.01)
    # ...while the hint still says where the pointer really is, in this child's
    # own coordinates (25 in the window, 4 units below its own 6-unit height).
    assert child.hints[-1]["pointer_y"] == pytest.approx(21.0)
    # An in-bounds drag carries no hint at all: x/y already is the pointer.
    assert "pointer_y" not in child.hints[0]


def test_translated_event_moves_the_pointer_hint_with_it():
    event = Event(
        type=EventType.MOUSE_DRAG, x=5.0, y=9.0, button="left",
        hints={"pointer_x": 5.0, "pointer_y": 30.0, "other": "kept"},
    )
    moved = event.translated(-1.0, -4.0)
    assert (moved.x, moved.y) == (4.0, 5.0)
    assert moved.hints["pointer_x"] == 4.0
    assert moved.hints["pointer_y"] == 26.0
    assert moved.hints["other"] == "kept"
    # The original is untouched: one hints dict is shared by every translation.
    assert event.hints["pointer_y"] == 30.0
