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


class BoxWidget(Widget):
    def draw(self, ctx):
        ctx.draw_box(0, 0, ctx.width, ctx.height, hints={"fill": True})


def test_dim_below_dims_underlying_content():
    backend = MemoryBackend(width=20, height=10)
    panel = Panel(backend)
    panel.add(Label("below"), x=0, y=0, w=10, h=1)
    panel.push_layer(BoxWidget(), z=10, hints={"dim_below": True, "w": 8, "h": 4})
    panel.render()
    from puikit import TextAttribute

    assert backend.style_at(0, 0).attr & TextAttribute.DIM  # under the layer
    # The layer itself is drawn after dimming, so its base units are not dimmed.
    cx, cy = (20 - 8) // 2, (10 - 4) // 2
    assert not backend.style_at(cx, cy).attr & TextAttribute.DIM


def test_shadow_hint_respects_capability():
    # TUI profile: shadow unsupported, primitive must not be called.
    backend = MemoryBackend(width=20, height=10)
    panel = Panel(backend)
    panel.push_layer(BoxWidget(), z=10, hints={"shadow": True, "w": 8, "h": 4})
    panel.render()
    assert backend.shadow_calls == []
    # GUI profile: shadow drawn at the layer's rect.
    backend = MemoryBackend(width=20, height=10, capabilities=PROFILE_GUI_DESKTOP)
    panel = Panel(backend)
    panel.push_layer(BoxWidget(), z=10, hints={"shadow": True, "w": 8, "h": 4})
    panel.render()
    assert backend.shadow_calls == [(6, 3, 8, 4)]


def test_box_fill_clears_interior():
    backend = MemoryBackend(width=20, height=10)
    panel = Panel(backend)
    panel.add(Label("below content"), x=0, y=2, w=15, h=1)
    panel.push_layer(BoxWidget(), z=10, hints={"x": 0, "y": 0, "w": 10, "h": 5})
    panel.render()
    line = backend.snapshot()[2]
    assert line[1:9] == " " * 8  # interior overwritten by the filled box
    assert line[0] == "│" and line[9] == "│"


def test_pane_background_fills_and_text_inherits():
    backend = MemoryBackend(width=20, height=6)
    panel = Panel(backend)
    panel.add(Label("hi"), x=2, y=1, w=10, h=3, hints={"bg": (10, 20, 30)})
    panel.render()
    # Empty pane base units are filled with the background...
    assert backend.style_at(8, 2).bg == (10, 20, 30)
    # ...text without an explicit bg inherits it...
    assert backend.snapshot()[1][2:4] == "hi"
    assert backend.style_at(2, 1).bg == (10, 20, 30)
    # ...and base units outside the pane are untouched.
    assert backend.style_at(0, 0).bg is None


def test_explicit_style_bg_beats_pane_bg():
    from puikit import Style

    backend = MemoryBackend(width=20, height=6)
    panel = Panel(backend)
    panel.add(Label("x", Style(bg=(99, 0, 0))), x=0, y=0, w=5, h=1, hints={"bg": (10, 20, 30)})
    panel.render()
    assert backend.style_at(0, 0).bg == (99, 0, 0)


def test_container_child_bg_overrides_and_inherits():
    from puikit.widgets import Container

    backend = MemoryBackend(width=30, height=8)
    panel = Panel(backend)
    container = Container()
    container.add(Label("a"), x=1, y=1, w=5, h=1)  # inherits container pane bg
    container.add(Label("b"), x=1, y=3, w=5, h=1, hints={"bg": (1, 2, 3)})
    panel.add(container, x=0, y=0, w=20, h=6, hints={"bg": (10, 20, 30)})
    panel.render()
    assert backend.style_at(1, 1).bg == (10, 20, 30)
    assert backend.style_at(1, 3).bg == (1, 2, 3)


def test_animate_gated_by_capability():
    widget = Label("x")
    # TUI profile: no animation capability, backend must not be called.
    backend = MemoryBackend(width=20, height=10)
    panel = Panel(backend)
    panel.animate(widget, hints={"transition": "fade", "duration_ms": 200})
    assert backend.animate_calls == []
    # GUI profile: the intent reaches the backend untouched.
    backend = MemoryBackend(width=20, height=10, capabilities=PROFILE_GUI_DESKTOP)
    panel = Panel(backend)
    panel.animate(widget, hints={"transition": "fade", "duration_ms": 200})
    assert backend.animate_calls == [
        (widget, {"transition": "fade", "duration_ms": 200})
    ]


class SizeProbe(Widget):
    def __init__(self):
        self.seen = None

    def draw(self, ctx):
        self.seen = ctx.size_units


def test_size_animation_redraws_at_intermediate_sizes():
    backend = MemoryBackend(width=40, height=20, capabilities=PROFILE_GUI_DESKTOP)
    panel = Panel(backend)
    probe = SizeProbe()
    panel.add(probe, x=2, y=2, w=30, h=10)
    panel.animate(probe, hints={"transition": "size", "duration_ms": 60_000, "from_w": 10, "from_h": 4})
    assert backend.tick_callbacks  # panel registered for animation frames
    backend.run_animation_ticks()
    w, h = probe.seen
    # Freshly started, eased progress is ~0: the rect is near (10, 4) and
    # certainly far from the final (30, 10).
    assert 10 <= w < 12 and 4 <= h < 5

    # Jump the clock to the end: the animation finishes, the widget gets its
    # assigned rect back, and the callback unregisters.
    panel._size_anims[probe].start -= 120.0
    backend.run_animation_ticks()
    assert probe.seen == (30, 10)
    assert probe not in panel._size_anims
    assert backend.tick_callbacks == []


def test_size_animation_is_layout_level_not_backend():
    backend = MemoryBackend(width=40, height=20, capabilities=PROFILE_GUI_DESKTOP)
    panel = Panel(backend)
    probe = SizeProbe()
    panel.add(probe, x=0, y=0, w=20, h=10)
    panel.animate(probe, hints={"transition": "size", "from_w": 5, "from_h": 5})
    # Handled by the Panel: the backend's render-level animate is not used.
    assert backend.animate_calls == []
    # Render-level transitions still go to the backend.
    panel.animate(probe, hints={"transition": "fade"})
    assert len(backend.animate_calls) == 1


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


def test_text_renders_in_fractional_height_pane():
    # Regression: on pixel-layout backends, rounding can squeeze a fixed
    # 1-base unit pane (e.g. a status bar) to slightly under one base unit. Its text
    # must be drawn and clipped by the backend, not silently dropped.
    from puikit import DrawContext, Rect

    backend = MemoryBackend(width=20, height=10, capabilities=PROFILE_GUI_DESKTOP)
    ctx = DrawContext(backend, Rect(0, 9, 20, 0.97), PROFILE_GUI_DESKTOP)
    ctx.draw_text(0, 0, "status")
    assert backend.snapshot()[9].startswith("status")


def test_text_extends_into_partial_last_column():
    # A pane 5.5 base units wide shows 6 columns of text (the last one partial,
    # clipped at the pane edge by the backend).
    from puikit import DrawContext, Rect

    backend = MemoryBackend(width=20, height=5, capabilities=PROFILE_GUI_DESKTOP)
    ctx = DrawContext(backend, Rect(0, 0, 5.5, 1.0), PROFILE_GUI_DESKTOP)
    ctx.draw_text(0, 0, "0123456789")
    assert backend.snapshot()[0] == "012345" + " " * 14
