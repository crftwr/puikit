from puikit import Event, EventType, Panel, PROFILE_GUI_DESKTOP
from puikit.backends.memory_backend import MemoryBackend
from puikit.widgets import Label, Widget


def render(panel: Panel, backend: MemoryBackend) -> list[str]:
    panel.render()
    return backend.snapshot()


def test_widget_draws_at_panel_position():
    backend = MemoryBackend(width=20, height=5)
    panel = Panel(backend)
    panel.add(Label("hi"), x=3, y=2, w=10, h=1)
    lines = render(panel, backend)
    assert lines[2][3:5] == "hi"


def test_text_is_clipped_to_widget_rect():
    backend = MemoryBackend(width=20, height=5)
    panel = Panel(backend)
    panel.add(Label("0123456789"), x=0, y=0, w=4, h=1)
    lines = render(panel, backend)
    assert lines[0] == "0123" + " " * 16


class IconWidget(Widget):
    def draw(self, ctx):
        ctx.draw_icon(0, 0, "folder")


def test_icon_falls_back_to_text_without_capability():
    backend = MemoryBackend(width=10, height=3)  # TUI profile: no icons
    panel = Panel(backend)
    panel.add(IconWidget(), x=0, y=0, w=5, h=1)
    lines = render(panel, backend)
    assert lines[0].startswith("📁")
    assert backend.icon_calls == []


def test_icon_uses_backend_primitive_with_capability():
    backend = MemoryBackend(width=10, height=3, capabilities=PROFILE_GUI_DESKTOP)
    panel = Panel(backend)
    panel.add(IconWidget(), x=2, y=1, w=5, h=1)
    panel.render()
    assert backend.icon_calls == [(2, 1, "folder")]


class Recorder(Widget):
    focusable = True

    def __init__(self):
        self.events = []

    def handle_event(self, event):
        self.events.append(event)
        return True


def test_mouse_event_is_routed_and_translated():
    backend = MemoryBackend(width=20, height=10)
    panel = Panel(backend)
    recorder = Recorder()
    panel.add(recorder, x=5, y=2, w=10, h=5)
    consumed = panel.dispatch_event(
        Event(type=EventType.MOUSE_CLICK, x=7, y=4, button="left")
    )
    assert consumed
    assert recorder.events[0].x == 2
    assert recorder.events[0].y == 2


def test_key_event_goes_to_focused_widget():
    backend = MemoryBackend(width=20, height=10)
    panel = Panel(backend)
    recorder = Recorder()
    panel.add(Label("x"), x=0, y=0, w=5, h=1)
    panel.add(recorder, x=0, y=2, w=5, h=1)
    assert panel.focused is recorder  # first focusable widget gets focus
    assert panel.dispatch_event(Event(type=EventType.KEY, key="down"))
    assert recorder.events[0].key == "down"


def test_topmost_layer_receives_events_exclusively():
    backend = MemoryBackend(width=20, height=10)
    panel = Panel(backend)
    below = Recorder()
    dialog = Recorder()
    panel.add(below, x=0, y=0, w=20, h=10)
    panel.push_layer(dialog, z=10, hints={"w": 10, "h": 4})
    assert panel.dispatch_event(Event(type=EventType.KEY, key="enter"))
    assert dialog.events and not below.events


def test_pop_layer_restores_event_flow():
    backend = MemoryBackend(width=20, height=10)
    panel = Panel(backend)
    below = Recorder()
    dialog = Recorder()
    panel.add(below, x=0, y=0, w=20, h=10)
    panel.push_layer(dialog, z=10)
    assert panel.pop_layer() is dialog
    panel.dispatch_event(Event(type=EventType.KEY, key="enter"))
    assert below.events and not dialog.events
