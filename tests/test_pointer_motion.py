"""DrawContext.request_pointer_motion: bare pointer motion handed to the
widget that asked for it, in its local coordinates, honouring modality."""

from puikit import Item, Panel, VSplit
from puikit.backends.memory_backend import MemoryBackend
from puikit.event import Event, EventType
from puikit.widgets import Label
from puikit.widgets.base import Widget


class _Tracker(Widget):
    def __init__(self):
        self.moves = []

    def draw(self, ctx):
        ctx.request_pointer_motion(self._moved)

    def _moved(self, event):
        self.moves.append((event.x, event.y))
        return False


def _panel():
    backend = MemoryBackend(width=40, height=10)
    backend.open()
    panel = Panel(backend)
    return backend, panel


def test_motion_reaches_the_widget_in_local_coordinates():
    backend, panel = _panel()
    tracker = _Tracker()
    panel.set_layout(VSplit(Item(Label("top"), size=2), Item(tracker, weight=1)))
    panel.render()
    panel.dispatch_event(Event(EventType.MOUSE_MOVE, x=5.0, y=4.0))
    assert tracker.moves == [(5.0, 2.0)]
    panel.dispatch_event(Event(EventType.MOUSE_MOVE, x=5.0, y=1.0))  # over the label
    assert tracker.moves == [(5.0, 2.0)]
    backend.close()


def test_a_modal_layer_keeps_motion_from_the_base():
    backend, panel = _panel()
    tracker = _Tracker()
    panel.set_layout(VSplit(Item(tracker, weight=1)))
    panel.push_layer(Label("dialog"), z=10, hints={"x": 0, "y": 0, "w": 10, "h": 3})
    panel.render()
    panel.dispatch_event(Event(EventType.MOUSE_MOVE, x=20.0, y=6.0))
    assert tracker.moves == []
    backend.close()


def test_a_widget_that_stops_asking_stops_receiving():
    backend, panel = _panel()
    tracker = _Tracker()
    panel.set_layout(VSplit(Item(tracker, weight=1)))
    panel.render()
    panel.set_layout(VSplit(Item(Label("gone"), weight=1)))
    panel.render()
    panel.dispatch_event(Event(EventType.MOUSE_MOVE, x=3.0, y=3.0))
    assert tracker.moves == []
    backend.close()
