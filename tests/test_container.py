"""Widget tree: Container, clipping, and animation cascades."""

import pytest

from puikit import Event, EventType, Panel, PROFILE_GUI_DESKTOP
from puikit.backends.memory_backend import MemoryBackend
from puikit.widgets import Container, Label, Widget


def test_clip_primitive_restricts_drawing():
    backend = MemoryBackend(width=20, height=5)
    backend.push_clip(2, 1, 5, 2)
    backend.draw_text(0, 1, "0123456789")  # only columns 2..6 inside the clip
    backend.draw_text(0, 4, "below")       # row outside the clip
    backend.pop_clip()
    lines = backend.snapshot()
    assert lines[1] == "  23456" + " " * 13
    assert lines[4] == " " * 20


def test_nested_clips_intersect():
    backend = MemoryBackend(width=20, height=5)
    backend.push_clip(0, 0, 10, 5)
    backend.push_clip(5, 0, 10, 5)  # effective: columns 5..9
    backend.draw_text(0, 0, "0123456789ABCDEF")
    backend.pop_clip()
    backend.pop_clip()
    assert backend.snapshot()[0] == "     56789" + " " * 10


def test_child_clipped_to_container():
    backend = MemoryBackend(width=30, height=8)
    panel = Panel(backend)
    container = Container()
    # Child wider than the container: must be cut at the container edge.
    container.add(Label("ABCDEFGHIJKLMNOP"), x=1, y=1, w=20, h=1)
    panel.add(container, x=2, y=2, w=10, h=4)
    panel.render()
    line = backend.snapshot()[3]
    assert line[3:12] == "ABCDEFGHI"  # container spans columns 2..11
    assert line[12] == " "            # clipped at the container edge


def test_nested_containers_clip_recursively():
    backend = MemoryBackend(width=30, height=8)
    panel = Panel(backend)
    outer, inner = Container(), Container()
    inner.add(Label("XXXXXXXXXXXXXXXX"), x=0, y=0, w=20, h=1)
    outer.add(inner, x=2, y=1, w=20, h=2)  # inner sticks out of outer
    panel.add(outer, x=0, y=0, w=8, h=4)
    panel.render()
    line = backend.snapshot()[1]
    assert line[2:8] == "XXXXXX"
    assert line[8] == " "  # outer container ends at column 8


def test_top_level_widget_clipped_to_its_rect():
    class Overflower(Widget):
        def draw(self, ctx):
            # Draws past its own height; rows beyond the rect are dropped.
            for row in range(10):
                ctx.draw_text(0, 0, "x")
                ctx._backend.draw_text(0, row, "y")  # bypasses ctx clipping

    backend = MemoryBackend(width=10, height=10)
    panel = Panel(backend)
    panel.add(Overflower(), x=0, y=0, w=5, h=3)
    panel.render()
    lines = backend.snapshot()
    assert lines[2][0] == "y"
    assert lines[3][0] == " "  # backend clip stopped the overflow


class Recorder(Widget):
    focusable = True

    def __init__(self):
        self.events = []

    def handle_event(self, event):
        self.events.append(event)
        return True


def test_container_routes_mouse_to_children_with_local_coords():
    backend = MemoryBackend(width=30, height=10)
    panel = Panel(backend)
    container = Container()
    recorder = Recorder()
    container.add(Label("head"), x=0, y=0, w=10, h=1)
    container.add(recorder, x=2, y=1, w=8, h=3)
    panel.add(container, x=5, y=2, w=12, h=6)
    consumed = panel.dispatch_event(
        Event(type=EventType.MOUSE_CLICK, x=8, y=4, button="left")
    )
    assert consumed
    # panel(8,4) -> container-local(3,2) -> child-local(1,1)
    assert (recorder.events[0].x, recorder.events[0].y) == (1, 1)


def test_container_routes_keys_to_focused_child():
    backend = MemoryBackend(width=30, height=10)
    panel = Panel(backend)
    container = Container()
    recorder = Recorder()
    container.add(recorder, x=0, y=0, w=10, h=2)  # first focusable -> focused
    panel.add(container, x=0, y=0, w=20, h=5)
    assert panel.dispatch_event(Event(type=EventType.KEY, key="down"))
    assert recorder.events[0].key == "down"


def test_animation_groups_nest_for_cascades():
    pytest.importorskip("AppKit", reason="pyobjc not installed")
    from puikit.backends.macos_backend import MacOSBackend

    backend = MacOSBackend()  # unopened: display list only
    panel = Panel(backend)
    container = Container()
    child = Label("hi")
    container.add(child, x=1, y=1, w=5, h=1)
    panel.add(container, x=0, y=0, w=10, h=4)
    panel.render()

    kinds = [cmd[0] for cmd in backend._front]
    # The child's group and clip nest inside the container's, so a transition
    # on the container wraps (cascades to) everything the child draws.
    begins = [i for i, cmd in enumerate(backend._front) if cmd[0] == "group_begin"]
    ends = [i for i, cmd in enumerate(backend._front) if cmd[0] == "group_end"]
    assert backend._front[begins[0]][1] == id(container)
    assert backend._front[begins[1]][1] == id(child)
    assert begins[0] < begins[1] < ends[0] < ends[1]
    assert kinds.count("clip_push") == 2
    assert kinds.count("clip_pop") == 2
    backend.close()


def test_size_animation_on_container_child():
    class SizeProbe(Widget):
        def __init__(self):
            self.seen = None

        def draw(self, ctx):
            self.seen = ctx.size_units

    backend = MemoryBackend(width=40, height=20, capabilities=PROFILE_GUI_DESKTOP)
    panel = Panel(backend)
    container = Container()
    probe = SizeProbe()
    container.add(probe, x=2, y=2, w=20, h=8)
    panel.add(container, x=0, y=0, w=30, h=15)
    # Children are not Panel slots, but size animation still applies to them.
    panel.animate(probe, hints={"transition": "size", "duration_ms": 60_000, "from_w": 4, "from_h": 2})
    backend.run_animation_ticks()
    w, h = probe.seen
    assert 4 <= w < 6 and 2 <= h < 3
    panel._size_anims[probe].start -= 120.0
    backend.run_animation_ticks()
    assert probe.seen == (20, 8)
