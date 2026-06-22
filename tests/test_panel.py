from puikit import (
    CapabilityProfile,
    DEFAULT_STYLE,
    Event,
    EventType,
    Font,
    Panel,
    PROFILE_GUI_DESKTOP,
    PROFILE_TUI,
    Style,
    TextAttribute,
)
from puikit.backends.memory_backend import MemoryBackend
from puikit.text import display_width
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


class _ProportionalBackend(MemoryBackend):
    """A fonts-capable grid backend whose per-Style font measures at half a
    base unit per glyph, so a proportional run packs more characters than its
    base-unit width. It records the exact text each draw_text receives."""

    def __init__(self, **kw):
        super().__init__(capabilities=PROFILE_GUI_DESKTOP, **kw)
        self.calls: list[str] = []

    def measure_text(self, text, style=DEFAULT_STYLE):
        if style.font is not None:
            return 0.5 * len(text)
        return float(display_width(text))

    def draw_text(self, x, y, text, style=DEFAULT_STYLE):
        self.calls.append(text)
        super().draw_text(x, y, text, style)


class _Para(Widget):
    def draw(self, ctx):
        ctx.draw_text(0, 0, "x" * 10, Style(font=Font()))


def test_proportional_text_is_not_truncated_by_column_count():
    # The 10-glyph run measures 5.0 base units and fits a width-5 pane, but a
    # column count would cap it at ceil(5)=5 characters. Flow text must reach
    # the backend whole and let the pane clip rect trim any overflow instead.
    backend = _ProportionalBackend(width=20, height=3)
    panel = Panel(backend)
    panel.add(_Para(), x=0, y=0, w=5, h=1)
    render(panel, backend)
    assert "x" * 10 in backend.calls  # full run, not sliced to 5 chars
    # (Grid font slicing stays covered by test_text_is_clipped_to_widget_rect.)


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
    assert backend.shadow_calls == [(6, 3, 8, 4, None, None)]


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


def test_slide_animation_runs_on_tui_via_rect_interpolation():
    # A terminal cannot composite a transition, but it *can* slide a region by
    # interpolating its rect — so a "slide" animates at the Panel level (not the
    # backend) on the TUI profile, exactly the drawer's intent.
    backend = MemoryBackend(width=40, height=20, capabilities=PROFILE_TUI)
    panel = Panel(backend)

    class RectProbe(Widget):
        def __init__(self):
            self.rect = None

        def draw(self, ctx):
            self.rect = (ctx._rect.x, ctx._rect.y)

    probe = RectProbe()
    panel.add(probe, x=10, y=4, w=12, h=3)
    panel.animate(
        probe,
        hints={"transition": "slide", "duration_ms": 60_000, "from_dx": -12.0},
    )
    # The Panel drives it (a geometry transition), not the backend.
    assert backend.animate_calls == []
    assert backend.tick_callbacks
    backend.run_animation_ticks()
    # Freshly started (eased ~0): the rect sits ~12 units left of its anchor.
    assert probe.rect[0] < 10

    # Run it to completion: the rect settles at the anchored x and the tick
    # unregisters.
    panel._size_anims[probe].start -= 120.0
    backend.run_animation_ticks()
    assert probe.rect == (10, 4)
    assert probe not in panel._size_anims
    assert backend.tick_callbacks == []


def test_slide_on_compositing_backend_goes_to_backend():
    # A compositing backend slides via its own sub-unit transform, so a "slide"
    # is handed to the backend there (and the Panel does not also interpolate).
    backend = MemoryBackend(width=40, height=20, capabilities=PROFILE_GUI_DESKTOP)
    panel = Panel(backend)
    probe = SizeProbe()
    panel.add(probe, x=0, y=0, w=20, h=10)
    panel.animate(probe, hints={"transition": "slide", "from_dx": -8})
    assert backend.animate_calls == [(probe, {"transition": "slide", "from_dx": -8})]
    assert probe not in panel._size_anims


class RectProbe(Widget):
    def __init__(self):
        self.rect = None

    def draw(self, ctx):
        self.rect = (ctx._rect.x, ctx._rect.y, ctx._rect.w, ctx._rect.h)


def test_geometry_animation_is_two_frame_and_cell_snapped_on_tui():
    # A terminal cannot draw smooth motion, so a slide plays as exactly two
    # frames — one intermediate, then the target — snapped to whole cells.
    backend = MemoryBackend(width=40, height=20, capabilities=PROFILE_TUI)
    panel = Panel(backend)
    probe = RectProbe()
    panel.add(probe, x=10, y=4, w=12, h=3)
    panel.animate(probe, hints={"transition": "slide", "from_dx": -12.0})
    # Frame 1 = the single intermediate (progress 0.5): x = 10 + (-12)*0.5 = 4,
    # every component snapped to a whole cell.
    backend.run_animation_ticks()
    x, y, w, h = probe.rect
    assert x == 4
    assert all(float(v).is_integer() for v in (x, y, w, h))
    # Frame 2 = the target; the animation is then finished and dropped.
    backend.run_animation_ticks()
    assert probe.rect == (10, 4, 12, 3)
    assert probe not in panel._size_anims
    assert backend.tick_callbacks == []


def test_scale_is_two_frame_rect_inset_on_tui():
    # A grid cannot sub-scale glyphs, so "scale" is expressed as a real rect
    # inset toward the center (then full) — still the 2-frame policy.
    backend = MemoryBackend(width=40, height=20, capabilities=PROFILE_TUI)
    panel = Panel(backend)
    probe = RectProbe()
    panel.add(probe, x=10, y=4, w=12, h=4)
    panel.animate(probe, hints={"transition": "scale", "from_scale": 0.5})
    backend.run_animation_ticks()  # intermediate: inset toward the center
    x, y, w, h = probe.rect
    assert w < 12 and h < 4 and x > 10
    assert all(float(v).is_integer() for v in (x, y, w, h))
    backend.run_animation_ticks()  # target: the full rect
    assert probe.rect == (10, 4, 12, 4)
    assert probe not in panel._size_anims


def test_geometry_animation_is_subunit_on_pixel_backend():
    # A pixel-layout backend keeps fractional positions (smooth sub-unit slide).
    backend = MemoryBackend(width=40, height=20, capabilities=PROFILE_GUI_DESKTOP)
    panel = Panel(backend)
    probe = RectProbe()
    panel.add(probe, x=0, y=0, w=10, h=5)
    panel.animate(
        probe, hints={"transition": "size", "duration_ms": 60_000, "from_w": 2.0}
    )
    panel._size_anims[probe].start -= 20.0  # p = 1/3
    panel.render()
    _, _, w, _ = probe.rect
    # Linear at p=1/3: w = 2 + (10-2)/3 = 4.667 — kept fractional (no cell snap),
    # which a character backend would instead round to a whole base unit.
    assert abs(w - 4.667) < 0.1
    assert not float(w).is_integer()


class ColorProbe(Widget):
    def __init__(self):
        self.seen = None

    def draw(self, ctx):
        # Resting color is the tween's destination, so completion is seamless.
        self.seen = ctx.animated_color(default=(30, 30, 30))


def test_color_animation_is_two_frame_on_tui():
    backend = MemoryBackend(width=20, height=10, capabilities=PROFILE_TUI)
    panel = Panel(backend)
    probe = ColorProbe()  # resting color (30, 30, 30) == the tween destination
    panel.add(probe, x=0, y=0, w=10, h=5)
    panel.animate(
        probe,
        hints={"transition": "color", "from": (200, 0, 0), "to": (30, 30, 30)},
    )
    # A Panel-level transition (no backend compositing), driven by ticks.
    assert backend.animate_calls == []
    assert backend.tick_callbacks
    # Frame 1 = the intermediate (progress 0.5): linear midpoint ~ (115, 15, 15).
    backend.run_animation_ticks()
    r, g, b = probe.seen
    assert 95 < r < 135 and 5 < g < 25

    # Frame 2 = target: the finished tween is dropped, so the widget shows its
    # resting color (== the destination, no visible jump) and ticking stops.
    backend.run_animation_ticks()
    assert probe.seen == (30, 30, 30)
    assert (probe, None) not in panel._color_anims
    assert backend.tick_callbacks == []


def test_fade_is_two_frame_dim_effect_on_tui():
    # A terminal cannot composite alpha, so a fade shows one dim intermediate
    # frame over the whole group, then the clean target frame.
    backend = MemoryBackend(width=20, height=10, capabilities=PROFILE_TUI)
    panel = Panel(backend)
    probe = Label("hello")
    panel.add(probe, x=0, y=0, w=10, h=1)
    panel.animate(probe, hints={"transition": "fade"})
    # Played by the Panel, not handed to the backend's compositor.
    assert backend.animate_calls == []
    assert probe in panel._effect_anims
    # Frame 1 (intermediate): the group is dimmed.
    backend.run_animation_ticks()
    assert backend.style_at(0, 0).attr & TextAttribute.DIM
    # Frame 2 (target): clean, effect dropped, ticking stops.
    backend.run_animation_ticks()
    assert not (backend.style_at(0, 0).attr & TextAttribute.DIM)
    assert probe not in panel._effect_anims
    assert backend.tick_callbacks == []


def test_highlight_is_two_frame_flash_on_tui():
    # Highlight has no alpha on a terminal: a one-frame color flash over the
    # group, then the clean target frame.
    backend = MemoryBackend(width=20, height=10, capabilities=PROFILE_TUI)
    panel = Panel(backend)
    probe = Label("hi")
    panel.add(probe, x=0, y=0, w=10, h=1)
    panel.animate(probe, hints={"transition": "highlight", "color": (229, 229, 16)})
    assert backend.animate_calls == []
    # Frame 1 (intermediate): a flash in the requested color.
    backend.run_animation_ticks()
    assert backend.flash_calls and backend.flash_calls[-1][4] == (229, 229, 16)
    # Frame 2 (target): no further flash, effect dropped.
    before = len(backend.flash_calls)
    backend.run_animation_ticks()
    assert len(backend.flash_calls) == before
    assert probe not in panel._effect_anims


def test_optical_transitions_go_to_backend_on_gui():
    # A compositing backend realizes fade/scale/highlight itself (real alpha /
    # sub-unit transforms); the Panel does not play a stepped stand-in.
    backend = MemoryBackend(width=40, height=20, capabilities=PROFILE_GUI_DESKTOP)
    panel = Panel(backend)
    probe = Label("x")
    panel.add(probe, x=0, y=0, w=10, h=1)
    for kind in ("fade", "scale", "highlight"):
        panel.animate(probe, hints={"transition": kind})
    assert [h["transition"] for _, h in backend.animate_calls] == ["fade", "scale", "highlight"]
    assert panel._effect_anims == {}
    assert probe not in panel._size_anims


def test_color_animation_immediate_on_still_backend():
    # No animation and no animation_ticks: the tween never registers and the
    # widget simply renders its resting color.
    still = CapabilityProfile({**PROFILE_TUI, "animation_ticks": False})
    backend = MemoryBackend(width=20, height=10, capabilities=still)
    panel = Panel(backend)
    probe = ColorProbe()
    panel.add(probe, x=0, y=0, w=10, h=5)
    panel.animate(
        probe,
        hints={"transition": "color", "from": (200, 0, 0), "to": (30, 30, 30)},
    )
    assert panel._color_anims == {}
    assert backend.tick_callbacks == []
    panel.render()
    assert probe.seen == (30, 30, 30)


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


def test_default_font_flows_proportionally_on_gui():
    # With no explicit font, GUI text uses the proportional UI font, not the
    # monospaced grid (docs/font_system.md §5). A proportional run is handed to
    # the backend whole and trimmed by the pane clip rect, never column-sliced —
    # so a width-5 pane still receives the full 10-glyph run. (Grid-font column
    # slicing on whole-unit backends stays covered by
    # test_text_is_clipped_to_widget_rect.)
    class _Plain(Widget):
        def draw(self, ctx):
            ctx.draw_text(0, 0, "0123456789")  # no font -> GUI default proportional

    backend = _ProportionalBackend(width=20, height=3)
    panel = Panel(backend)
    panel.add(_Plain(), x=0, y=0, w=5, h=1)
    render(panel, backend)
    assert "0123456789" in backend.calls
