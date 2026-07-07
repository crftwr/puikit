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


def test_file_drop_is_routed_to_widget_under_pointer():
    # A FILE_DROP is a positioned event: it routes to the widget under its point
    # like a mouse event, translated to widget-local coordinates, carrying the
    # dropped paths in hints.
    backend = MemoryBackend(width=20, height=10)
    panel = Panel(backend)
    recorder = Recorder()
    panel.add(recorder, x=5, y=2, w=10, h=5)
    consumed = panel.dispatch_event(
        Event(type=EventType.FILE_DROP, x=7, y=4, hints={"paths": ["/a/b.txt"]})
    )
    assert consumed
    assert recorder.events[0].type is EventType.FILE_DROP
    assert recorder.events[0].x == 2 and recorder.events[0].y == 2
    assert recorder.events[0].hints["paths"] == ["/a/b.txt"]


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


def test_per_cell_dim_keeps_surfaces_distinct():
    # The per-cell dim composites a single veil over each cell's own color, so
    # two different surfaces stay distinct (faintly) instead of collapsing to one
    # pair — the TUI stand-in for a translucent overlay.
    backend = MemoryBackend(width=4, height=1)
    backend.draw_text(0, 0, "AB", Style(fg=(255, 255, 255), bg=(200, 30, 30)))
    backend.draw_text(2, 0, "CD", Style(fg=(255, 255, 255), bg=(30, 30, 200)))
    veil = (20, 20, 20)
    backend.dim_rect(0, 0, 4, 1, scrim=((120, 120, 120), veil), per_cell=True)
    red = backend.style_at(0, 0)
    blue = backend.style_at(2, 0)
    assert red.attr & TextAttribute.DIM and blue.attr & TextAttribute.DIM
    # The dim is grayscale: each composited color is a neutral gray (r==g==b).
    assert red.bg[0] == red.bg[1] == red.bg[2]
    assert blue.bg[0] == blue.bg[1] == blue.bg[2]
    # The two surfaces still differ (by brightness) and both moved off their
    # original color toward the veil.
    assert red.bg != blue.bg
    assert red.bg != (200, 30, 30) and blue.bg != (30, 30, 200)


def test_uniform_dim_collapses_surfaces():
    # Without per_cell, every cell flattens to the one scrim pair (the fade
    # stand-in / colorless fallback behavior).
    backend = MemoryBackend(width=4, height=1)
    backend.draw_text(0, 0, "AB", Style(fg=(255, 255, 255), bg=(200, 30, 30)))
    backend.draw_text(2, 0, "CD", Style(fg=(255, 255, 255), bg=(30, 30, 200)))
    backend.dim_rect(0, 0, 4, 1, scrim=((120, 120, 120), (20, 20, 20)))
    assert backend.style_at(0, 0).bg == backend.style_at(2, 0).bg == (20, 20, 20)


def test_shadow_hint_respects_capability():
    # TUI profile: no real compositing, so the Panel uses the stepped stand-in
    # (shadow_rect, the darkened halo), not the GUI draw_shadow.
    backend = MemoryBackend(width=20, height=10)
    panel = Panel(backend)
    panel.push_layer(BoxWidget(), z=10, hints={"shadow": True, "w": 8, "h": 4})
    panel.render()
    assert backend.shadow_calls == []
    assert backend.shadow_rect_calls == [(6, 3, 8, 4)]
    # GUI profile: real shadow drawn at the layer's rect; no stand-in.
    backend = MemoryBackend(width=20, height=10, capabilities=PROFILE_GUI_DESKTOP)
    panel = Panel(backend)
    panel.push_layer(BoxWidget(), z=10, hints={"shadow": True, "w": 8, "h": 4})
    panel.render()
    # The trailing element is the caster fill color: a bare modal has no surface
    # hint, so the Panel passes the theme popup surface (see draw_shadow).
    assert backend.shadow_calls == [(6, 3, 8, 4, None, None, panel.theme.popup_bg)]
    assert backend.shadow_rect_calls == []


class FillWidget(Widget):
    def draw(self, ctx):
        ctx.fill_rect(0, 0, ctx.width, ctx.height, Style(bg=(200, 200, 200)))


class FillWithGlyph(Widget):
    def draw(self, ctx):
        ctx.fill_rect(0, 0, ctx.width, ctx.height, Style(bg=(200, 200, 200)))
        # A text cell that lands in the shadow's bottom strip (col 6, row 5).
        ctx.draw_text(6, 5, "Z", Style(fg=(10, 10, 10), bg=(200, 200, 200)))


def test_tui_shadow_bottom_halfblock_right_wholecell():
    # The TUI shadow hugs the layer's right + bottom edges. The bottom edge is a ▄
    # half-block on blank cells (page color in the lower half via fg, shadow in the
    # upper half via bg); the right edge is a whole-cell darken (no glyph). Both at
    # one weak strength (0.8 kept).
    backend = MemoryBackend(width=12, height=8)
    panel = Panel(backend)
    panel.add(FillWidget(), x=0, y=0, w=12, h=8)
    panel.push_layer(BoxWidget(), z=10, hints={"shadow": True, "w": 6, "h": 3})
    panel.render()
    # Layer centered: cols 3..8, rows 2..4. Shadow: right col 9 rows 3..4
    # (whole-cell), bottom row 5 cols 4..9 (▄, incl. corner).
    snap = backend.snapshot()
    # Bottom edge: ▄ half-block, fg = page color (lower half), bg = shadow (top).
    assert snap[5][6] == "▄" and snap[5][9] == "▄"
    bottom = backend.style_at(6, 5)
    assert bottom.fg == (200, 200, 200) and bottom.bg == (160, 160, 160)
    # Right edge: whole-cell darken, no glyph, bg fully shaded.
    assert snap[3][9] == " "
    assert backend.style_at(9, 3).bg == (160, 160, 160)
    # No vertical half-block anywhere.
    assert all("▌" not in row for row in snap)
    # Shifted half a cell down: the right-edge shadow starts in the lower half of
    # the top-right cell — a ▄ with the shade in the lower half (fg) and the page
    # kept in the upper half (bg).
    assert snap[2][9] == "▄"
    top = backend.style_at(9, 2)
    assert top.fg == (160, 160, 160) and top.bg == (200, 200, 200)
    # Down-right only, light from top-left: left edge untouched.
    assert backend.style_at(1, 3).bg == (200, 200, 200)


def test_tui_shadow_text_cells_keep_glyph():
    # A shadow cell that already holds TEXT can't use a half-block (it would erase
    # the glyph), so it keeps the glyph and darkens the whole cell instead.
    backend = MemoryBackend(width=12, height=8)
    panel = Panel(backend)
    panel.add(FillWithGlyph(), x=0, y=0, w=12, h=8)
    panel.push_layer(BoxWidget(), z=10, hints={"shadow": True, "w": 6, "h": 3})
    panel.render()
    assert backend.snapshot()[5][6] == "Z"          # glyph preserved
    cell = backend.style_at(6, 5)
    assert cell.fg == (8, 8, 8)        # text darkened (10 * 0.8)
    assert cell.bg == (160, 160, 160)  # bg darkened (200 * 0.8)


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
    # Frame 1 (intermediate): the group is dimmed. A fade washes the group
    # toward its own background (not the fixed dark modal scrim), so the
    # intermediate cell carries the theme's fade scrim, not near-black — this is
    # what keeps a fade on a light theme from flashing black.
    backend.run_animation_ticks()
    assert backend.style_at(0, 0).attr & TextAttribute.DIM
    fade_fg, fade_bg = panel.theme.fade_scrim()
    assert backend.style_at(0, 0).bg == fade_bg
    assert backend.style_at(0, 0).fg == fade_fg
    # Frame 2 (target): clean, effect dropped, ticking stops.
    backend.run_animation_ticks()
    assert not (backend.style_at(0, 0).attr & TextAttribute.DIM)
    assert probe not in panel._effect_anims
    assert backend.tick_callbacks == []


def test_fade_scrim_follows_theme_polarity_not_fixed_dark():
    # Regression: a fade on a light theme used to flash near-black because the
    # TUI stand-in reused the fixed dark modal scrim. The fade scrim now tracks
    # the theme background, so a light theme washes toward near-white.
    from puikit import derive_theme

    light = derive_theme(
        background=(240, 240, 240),
        foreground=(30, 30, 30),
        muted=(120, 120, 120),
        accent=(0, 122, 204),
        surface=(225, 225, 228),
        selection=(180, 205, 240),
    )
    backend = MemoryBackend(width=20, height=10, capabilities=PROFILE_TUI)
    panel = Panel(backend, theme=light)
    probe = Label("hello")
    panel.add(probe, x=0, y=0, w=10, h=1)
    panel.animate(probe, hints={"transition": "fade"})
    backend.run_animation_ticks()
    bg = backend.style_at(0, 0).bg
    # Bright background, emphatically not the (21, 22, 30) dark modal scrim.
    assert sum(bg) > 600


def test_fade_intermediate_follows_each_cells_own_surface():
    # Regression: a fade's intermediate frame used to flatten every cell to the
    # one theme (content-surface) scrim pair, so a group drawn on a *different*
    # surface (e.g. a MessageBox on the popup surface) faded toward the wrong
    # color — the intermediate did not follow the actual grid cells. The fade is
    # now per-cell opacity: each cell's own fg sinks toward its OWN bg, keeping
    # the bg, so a popup-surface cell stays popup-colored.
    class _SurfaceProbe(Widget):
        def __init__(self, fg, bg):
            self._fg, self._bg = fg, bg

        def draw(self, ctx):
            ctx.draw_text(0, 0, "X", Style(fg=self._fg, bg=self._bg))

    surface_bg = (200, 60, 60)  # a vivid, non-content surface color
    text_fg = (250, 250, 250)
    backend = MemoryBackend(width=20, height=10, capabilities=PROFILE_TUI)
    panel = Panel(backend)
    probe = _SurfaceProbe(text_fg, surface_bg)
    panel.add(probe, x=0, y=0, w=4, h=1)
    panel.animate(probe, hints={"transition": "fade"})
    backend.run_animation_ticks()
    cell = backend.style_at(0, 0)
    # The cell keeps its own surface background (not the theme content scrim bg).
    assert cell.bg == surface_bg
    assert cell.bg != panel.theme.fade_scrim()[1]
    # Its foreground sank toward that same surface bg (opacity), so it lies
    # strictly between the original text and the surface — never the content scrim.
    assert surface_bg[0] < cell.fg[0] < text_fg[0]


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


class _CursorBackend(MemoryBackend):
    """A pointer_shape-capable backend that records each set_pointer_shape."""

    def __init__(self, **kw):
        caps = CapabilityProfile({**PROFILE_TUI, "pointer_shape": True})
        super().__init__(capabilities=caps, **kw)
        self.shapes: list[str | None] = []

    def set_pointer_shape(self, shape):
        self.shapes.append(shape)


class _CursorWidget(Widget):
    """Requests a cursor only while the pointer is over it."""

    def draw(self, ctx):
        if ctx.hovered:
            ctx.set_cursor("text")


def test_cursor_intent_reaches_capable_backend_on_hover():
    backend = _CursorBackend(width=20, height=5)
    panel = Panel(backend)
    panel.add(_CursorWidget(), x=0, y=0, w=10, h=3)

    # Pointer outside the widget: the frame resolves to no shape (default).
    panel.dispatch_event(Event(type=EventType.MOUSE_MOVE, x=15.0, y=0.0))
    panel.render()
    assert backend.shapes[-1] is None

    # Pointer over the widget: the requested shape is pushed once per frame.
    panel.dispatch_event(Event(type=EventType.MOUSE_MOVE, x=2.0, y=1.0))
    panel.render()
    assert backend.shapes[-1] == "text"


def test_cursor_intent_is_not_pushed_without_capability():
    # A TUI-profile backend (pointer_shape False) never gets the call, even
    # though the widget issues the intent — the Panel gates it.
    backend = MemoryBackend(width=20, height=5)
    calls = []
    backend.set_pointer_shape = lambda shape: calls.append(shape)  # type: ignore[method-assign]
    panel = Panel(backend)
    panel.add(_CursorWidget(), x=0, y=0, w=10, h=3)
    panel.dispatch_event(Event(type=EventType.MOUSE_MOVE, x=2.0, y=1.0))
    panel.render()
    assert calls == []


class _AlwaysCursorWidget(Widget):
    """An underlying widget that claims a cursor on every frame regardless of
    the pointer (e.g. a pane splitter's resize affordance)."""

    def draw(self, ctx):
        ctx.set_cursor("col-resize")


def test_modal_layer_owns_cursor_no_leak_from_beneath():
    # A modal layer owns events exclusively, so it must own the pointer shape
    # too: a cursor requested by a widget *underneath* it (even where a
    # non-fullscreen dialog does not cover) must not leak through.
    backend = _CursorBackend(width=40, height=12)
    panel = Panel(backend)
    panel.add(_AlwaysCursorWidget(), x=0, y=0, w=40, h=12)
    # A small, centered (non-fullscreen) modal that only claims a cursor while
    # hovered over itself.
    panel.push_layer(_CursorWidget(), z=10, hints={"x": 15, "y": 4, "w": 10, "h": 4})

    # Pointer outside the dialog, over the underlying always-cursor widget: the
    # modal owns the frame, so the leaked "col-resize" is discarded.
    panel.dispatch_event(Event(type=EventType.MOUSE_MOVE, x=2.0, y=1.0))
    panel.render()
    assert backend.shapes[-1] is None

    # Pointer inside the dialog: its own hovered request applies.
    panel.dispatch_event(Event(type=EventType.MOUSE_MOVE, x=18.0, y=5.0))
    panel.render()
    assert backend.shapes[-1] == "text"


class _HairlineBackend(MemoryBackend):
    """A grid backend that keeps ``vector_shapes`` on (the base MemoryBackend
    forces it off) and records fill_rect / draw_text, so ``draw_hairline``'s
    stroke-vs-glyph resolution can be inspected off-screen."""

    def __init__(self, **kw):
        super().__init__(capabilities=PROFILE_GUI_DESKTOP, **kw)
        self.fills: list[tuple] = []
        self.texts: list[str] = []

    @property
    def capabilities(self):
        return self._capabilities  # unmodified: vector_shapes stays True

    @property
    def base_size(self):
        return (8, 16)

    def fill_rect(self, x, y, w, h, style=DEFAULT_STYLE):
        self.fills.append((x, y, w, h))
        super().fill_rect(x, y, w, h, style)

    def draw_text(self, x, y, text, style=DEFAULT_STYLE):
        self.texts.append(text)
        super().draw_text(x, y, text, style)


class _Hairliner(Widget):
    def draw(self, ctx):
        ctx.draw_hairline(2.0, 1.5, 4.0, style=Style(fg=(200, 60, 60)))                # horizontal
        ctx.draw_hairline(3.5, 0.0, 3.0, vertical=True, style=Style(fg=(200, 60, 60)))  # vertical


def test_draw_hairline_strokes_thin_rects_on_vector():
    # On a vector backend the line is a device-pixel-thin fill_rect, never a box
    # glyph — the visible-vs-grid choice is resolved in the Panel layer.
    backend = _HairlineBackend(width=10, height=5)
    panel = Panel(backend)
    panel.add(_Hairliner(), x=0, y=0, w=10, h=5)
    panel.render()
    assert all("─" not in t and "│" not in t for t in backend.texts)
    assert any(0 < h < 1.0 for _, _, _, h in backend.fills)  # thin horizontal stroke
    assert any(0 < w < 1.0 for _, _, w, _ in backend.fills)  # thin vertical stroke


def test_draw_hairline_uses_box_glyphs_on_grid():
    backend = MemoryBackend(width=10, height=5)  # TUI profile: vector_shapes off
    panel = Panel(backend)
    panel.add(_Hairliner(), x=0, y=0, w=10, h=5)
    panel.render()
    snap = "".join(backend.snapshot())
    assert "─" in snap and "│" in snap


class _DispatchBackend(MemoryBackend):
    """Native-desktop-capable memory backend that records main-thread hops."""

    def __init__(self, **kw):
        super().__init__(capabilities=PROFILE_GUI_DESKTOP, **kw)
        self.dispatched = []

    def call_on_main_thread(self, callback):
        self.dispatched.append(callback)


def test_panel_forwards_main_thread_dispatch_when_supported():
    backend = _DispatchBackend(width=10, height=3)
    panel = Panel(backend)
    assert panel.dispatches_to_main_thread is True
    sentinel = lambda: None  # noqa: E731
    assert panel.call_on_main_thread(sentinel) is True
    assert backend.dispatched == [sentinel]


def test_panel_main_thread_dispatch_is_noop_without_capability():
    # A TUI backend can't dispatch; the Panel returns False and never calls the
    # backend method (whose base raises), so callers stay branch-free.
    backend = MemoryBackend(width=10, height=3, capabilities=PROFILE_TUI)
    panel = Panel(backend)
    assert panel.dispatches_to_main_thread is False
    assert panel.call_on_main_thread(lambda: None) is False
