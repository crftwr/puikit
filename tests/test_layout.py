"""Layout resolution: snapped to base units on TUI, pixel-fractional on GUI."""

import pytest

from puikit import HSplit, Item, Panel, PROFILE_GUI_DESKTOP, VSplit
from puikit.backends.memory_backend import MemoryBackend
from puikit.layout import LayoutContext
from puikit.widgets import Button, Label, ListView, ScrollBar, TextBlock, Widget

SNAP = LayoutContext(base_w=1, base_h=1, snap=True)
PIXEL = LayoutContext(base_w=10, base_h=20, snap=False)


def rects(placements):
    return [rect for _, rect, _ in placements]


def test_fixed_and_weighted_vsplit_snapped():
    a, b, c = Label("a"), Label("b"), Label("c")
    layout = VSplit(Item(a, size=3), Item(b), Item(c, size=1))
    (ra, rb, rc) = rects(layout.resolve(0, 0, 80, 24, SNAP))
    assert (ra.y, ra.h) == (0, 3)
    assert (rb.y, rb.h) == (3, 20)  # weighted item absorbs the rest
    assert (rc.y, rc.h) == (23, 1)


def test_snapped_boundaries_tile_exactly():
    widgets = [Label(str(i)) for i in range(3)]
    layout = HSplit(*widgets)  # equal weights over 10 base units: 3.33 each
    # Float inputs, as the Panel passes them: snap must still yield true ints.
    placements = layout.resolve(0.0, 0.0, 10.0, 5.0, SNAP)
    result = rects(placements)
    assert all(
        isinstance(v, int) for r in result for v in (r.x, r.y, r.w, r.h)
    )
    assert [r.w for r in result] == [3, 4, 3]  # boundary rounding, no gaps
    assert result[0].x + result[0].w == result[1].x
    assert result[1].x + result[1].w == result[2].x
    assert result[2].x + result[2].w == 10


def test_pixel_layout_keeps_fractional_units():
    widgets = [Label(str(i)) for i in range(3)]
    layout = HSplit(*widgets)  # 10 base units x 10px: 100px split as 33/34/33
    result = rects(layout.resolve(0.0, 0.0, 10.0, 5.0, PIXEL))
    assert result[0].w == pytest.approx(3.3)
    assert result[1].x == pytest.approx(3.3)
    assert result[1].w == pytest.approx(3.4)
    assert sum(r.w for r in result) == pytest.approx(10.0)


def test_pixel_layout_boundaries_land_on_whole_pixels():
    widgets = [Label(str(i)) for i in range(3)]
    layout = HSplit(*widgets)
    result = rects(layout.resolve(0.0, 0.0, 10.0, 5.0, PIXEL))
    for rect in result:
        # base_w is 10: every boundary must be a whole number of pixels
        assert (rect.x * 10) == pytest.approx(round(rect.x * 10))
        assert (rect.w * 10) == pytest.approx(round(rect.w * 10))
    # Adjacent rects share their boundary exactly: no gaps, no overlap.
    assert result[0].x + result[0].w == pytest.approx(result[1].x)
    assert result[1].x + result[1].w == pytest.approx(result[2].x)


def test_min_px_converted_via_base_size():
    sidebar, main = Label("side"), Label("main")
    layout = HSplit(Item(sidebar, weight=1, hints={"min_px": 55}), Item(main, weight=9))
    result = rects(layout.resolve(0.0, 0.0, 20.0, 5.0, PIXEL))
    # weight share would be 2.0 base units; min_px 55 at 10px base units means 5.5
    assert result[0].w == pytest.approx(5.5)


def test_min_px_ignored_on_whole_unit_grid_backends():
    # Regression: on TUI (base unit size 1x1) a min_px hint must not turn into a
    # huge min value that starves the other items.
    sidebar, main, inspector = Label("side"), Label("main"), Label("insp")
    layout = HSplit(
        Item(sidebar, weight=1, hints={"min_px": 220, "min": 18}),
        Item(main, weight=2),
        Item(inspector, weight=1),
    )
    rs, rm, ri = rects(layout.resolve(0, 0, 80, 20, SNAP))
    assert rs.w == 20  # weight share; min=18 not binding, min_px ignored
    assert rm.w == 40
    assert ri.w == 20
    # min still applies when it is the binding constraint.
    rs, rm, ri = rects(layout.resolve(0, 0, 40, 20, SNAP))
    assert rs.w == 18


def test_minimums_shrink_other_items_to_fit():
    a, b = Label("a"), Label("b")
    layout = HSplit(Item(a, weight=1, hints={"min": 8}), Item(b, weight=1))
    ra, rb = rects(layout.resolve(0.0, 0.0, 10.0, 5.0, PIXEL))
    assert ra.w == pytest.approx(8.0)
    assert rb.w == pytest.approx(2.0)
    assert ra.w + rb.w == pytest.approx(10.0)


def test_nested_split_recurses():
    a, b, c = Label("a"), Label("b"), Label("c")
    layout = VSplit(Item(a, size=2), Item(HSplit(b, c)))
    placements = layout.resolve(0, 0, 10, 12, SNAP)
    assert len(placements) == 3
    _, rb, _ = placements[1]
    _, rc, _ = placements[2]
    assert (rb.y, rb.h) == (2, 10)
    assert (rb.x, rb.w) == (0, 5)
    assert (rc.x, rc.w) == (5, 5)


def test_divider_subtle_costs_nothing_on_whole_unit_grid():
    a, b = Label("a"), Label("b")
    layout = HSplit(Item(a), Item(b), divider="subtle")
    ctx = LayoutContext(base_w=1, base_h=1, snap=True)
    ra, rb = rects(layout.resolve(0, 0, 80, 24, ctx))
    # No base units reserved, no divider emitted: background contrast (surface
    # roles) is what separates the panes on whole-unit backends.
    assert ra.w + rb.w == 80
    assert ctx.dividers == []


def test_divider_strong_spends_one_unit_on_whole_unit_grid():
    a, b = Label("a"), Label("b")
    layout = HSplit(Item(a), Item(b), divider="strong")
    ctx = LayoutContext(base_w=1, base_h=1, snap=True)
    ra, rb = rects(layout.resolve(0, 0, 81, 24, ctx))
    assert (ra.w, rb.w) == (40, 40)
    assert rb.x == 41
    (divider,) = ctx.dividers
    assert divider.vertical and divider.level == "strong"
    assert (divider.rect.x, divider.rect.y) == (40, 0)
    assert (divider.rect.w, divider.rect.h) == (1, 24)


def test_divider_hairline_costs_one_device_pixel():
    a, b = Label("a"), Label("b")
    layout = HSplit(Item(a), Item(b), divider="subtle")
    ctx = LayoutContext(base_w=10, base_h=20, snap=False, hairline=True)
    ra, rb = rects(layout.resolve(0.0, 0.0, 10.0, 5.0, ctx))
    (divider,) = ctx.dividers
    assert divider.rect.w * 10 == pytest.approx(1)  # one device pixel
    # The divider tiles exactly between the panes: no gap, no overlap.
    assert divider.rect.x == pytest.approx(ra.x + ra.w)
    assert rb.x == pytest.approx(divider.rect.x + divider.rect.w)
    assert rb.x + rb.w == pytest.approx(10.0)


# --- intrinsic (content-driven) sizing ------------------------------------------


def test_content_item_reserves_measured_width():
    # A button placed with size="content" measures itself (label + padding)
    # and the layout reserves exactly that; the weighted item takes the rest.
    btn = Button("OK")  # width = len("OK")=2 + 2*pad_x(2) = 6
    layout = HSplit(Item(btn, size="content"), Item(Label("rest"), weight=1))
    rb, rr = rects(layout.resolve(0, 0, 20, 5, SNAP))
    assert rb.w == 6
    assert (rr.x, rr.w) == (6, 14)


def test_content_item_reserves_measured_height():
    block = TextBlock("a\nb\nc")  # three lines -> height 3
    layout = VSplit(Item(block, size="content"), Item(Label("below"), weight=1))
    rblock, rbelow = rects(layout.resolve(0, 0, 10, 12, SNAP))
    assert rblock.h == 3
    assert (rbelow.y, rbelow.h) == (3, 9)


def test_wrapping_block_reserves_rows_for_the_given_width():
    # A wrapping block is content-driven on both axes: the cross-axis width the
    # VSplit hands in (10) folds the 30-col line into three display rows, so it
    # reserves height 3 — not the single row an unwrapped block would.
    block = TextBlock("x" * 30, wrap=True)
    layout = VSplit(Item(block, size="content"), Item(Label("below"), weight=1))
    rblock, rbelow = rects(layout.resolve(0, 0, 10, 12, SNAP))
    assert rblock.h == 3
    assert (rbelow.y, rbelow.h) == (3, 9)


def test_intrinsic_scrollbar_coexists_with_weighted_split():
    # The worked example: a backend-fixed scrollbar takes its width first,
    # then 1:2 divides the *remainder* — no conflict with the weighted split.
    main, side, bar = Label("m"), Label("s"), ScrollBar()
    layout = HSplit(
        Item(main, weight=2), Item(side, weight=1), Item(bar, size="content")
    )
    rm, rs, rb = rects(layout.resolve(0.0, 0.0, 10.0, 5.0, PIXEL))
    assert rb.w == pytest.approx(1.0)  # scrollbar_units, font-independent
    assert rm.w == pytest.approx(6.0)  # 2/3 of the remaining 9 base units
    assert rs.w == pytest.approx(3.0)  # 1/3 of the remaining 9 base units


def test_overflow_shrinks_content_but_never_fixed():
    # Fixed chrome is incompressible; an intrinsic item shrinks toward its
    # own minimum to make the layout fit.
    top = Label("top")
    block = TextBlock("\n".join("abcdef"))  # six lines: pref 6, min 1
    layout = VSplit(Item(top, size=4), Item(block, size="content"))
    rtop, rblock = rects(layout.resolve(0, 0, 10, 5, SNAP))
    assert rtop.h == 4   # fixed: untouched
    assert rblock.h == 1  # intrinsic: shrunk to its minimum, rest clips


def test_overflow_never_shrinks_backend_fixed_scrollbar():
    # The scrollbar (min == preferred == max) has no slack; under overflow a
    # co-resident intrinsic text yields instead.
    bar = ScrollBar()
    block = TextBlock("xxxxxxxx")  # width pref 8, min 0
    layout = HSplit(Item(bar, size="content"), Item(block, size="content"))
    rbar, rblock = rects(layout.resolve(0, 0, 5, 4, SNAP))
    assert rbar.w == 1   # never yields
    assert rblock.w == 4  # absorbs all the overflow


def test_intrinsic_item_floors_at_its_own_min_on_pixel_backend():
    # An intrinsic item's hard floor is its measured req.min, even without a
    # min hint — so a fixed-width scrollbar keeps its exact width on a pixel
    # backend (where snapping would not have rounded the bug away).
    bar = ScrollBar()
    block = TextBlock("xxxxxxxx")  # width pref 8, min 0
    layout = HSplit(Item(bar, size="content"), Item(block, size="content"))
    rbar, rblock = rects(layout.resolve(0.0, 0.0, 5.0, 4.0, PIXEL))
    assert rbar.w == pytest.approx(1.0)   # not shrunk to 0.83
    assert rblock.w == pytest.approx(4.0)


def test_min_hint_number_is_base_units():
    a, b = Label("a"), Label("b")
    layout = HSplit(Item(a, weight=1, hints={"min": 8}), Item(b, weight=1))
    ra, rb = rects(layout.resolve(0.0, 0.0, 10.0, 5.0, PIXEL))
    assert ra.w == pytest.approx(8.0)  # numeric "min" floors in base units
    assert rb.w == pytest.approx(2.0)


def test_min_content_floors_a_flex_item():
    # A flex item with min="content" never shrinks below its measured size;
    # the competing flex item gives up the space instead.
    block = TextBlock("a\nb\nc")  # content height 3
    layout = VSplit(
        Item(block, weight=1, hints={"min": "content"}), Item(Label("x"), weight=1)
    )
    rblock, rother = rects(layout.resolve(0, 0, 10, 4, SNAP))
    assert rblock.h == 3  # floored at its content
    assert rother.h == 1  # the other flex yielded


# --- cross-axis alignment ----------------------------------------------------------


def test_cross_axis_align_centers_shrink_to_content():
    lbl = Label("hi")  # intrinsic height 1
    layout = HSplit(Item(lbl, align="center"))
    (r,) = rects(layout.resolve(0, 0, 10, 5, SNAP))
    assert (r.h, r.y) == (1, 2)  # centered in the 5-base unit slot
    assert (r.x, r.w) == (0, 10)  # main axis still fills


def test_cross_axis_align_end():
    layout = HSplit(Item(Label("hi"), align="end"))
    (r,) = rects(layout.resolve(0, 0, 10, 5, SNAP))
    assert (r.h, r.y) == (1, 4)


def test_no_align_fills_cross_axis():
    layout = HSplit(Item(Label("hi")))
    (r,) = rects(layout.resolve(0, 0, 10, 5, SNAP))
    assert (r.h, r.y) == (5, 0)  # default: stretch to the slot


# --- size_px companion (pixel-only fixed length) -----------------------------------


def test_size_px_overrides_size_on_pixel_only():
    a, b = Label("a"), Label("b")
    layout = HSplit(Item(a, size=3, size_px=20), Item(b, weight=1))
    ra, _ = rects(layout.resolve(0.0, 0.0, 10.0, 5.0, PIXEL))
    assert ra.w == pytest.approx(2.0)  # 20px at 10px base units
    ra2, _ = rects(layout.resolve(0, 0, 10, 5, SNAP))
    assert ra2.w == 3  # whole-unit: px ignored, falls back to size


class FakeGuiBackend(MemoryBackend):
    """Memory backend that claims pixel layout and a real base unit size."""

    def __init__(self, **kwargs):
        super().__init__(capabilities=PROFILE_GUI_DESKTOP, **kwargs)

    @property
    def base_size(self):
        return (10, 20)


def test_panel_set_layout_places_widgets():
    backend = MemoryBackend(width=20, height=10)
    panel = Panel(backend)
    top, bottom = Label("top"), Label("bottom")
    panel.set_layout(VSplit(Item(top, size=2), Item(bottom)))
    panel.render()
    lines = backend.snapshot()
    assert lines[0].startswith("top")
    assert lines[2].startswith("bottom")


def test_panel_layout_assigns_focus_and_routes_keys():
    from puikit import Event, EventType

    backend = MemoryBackend(width=20, height=10)
    panel = Panel(backend)
    listview = ListView(["a", "b", "c"])
    panel.set_layout(VSplit(Item(Label("head"), size=1), Item(listview)))
    panel.render()
    assert panel.focused is listview
    panel.dispatch_event(Event(type=EventType.KEY, key="down"))
    assert listview.selected == 1


def test_panel_layout_fills_fractional_backend_size():
    # A window of 105px at 10px base units is 10.5 base units: the layout must extend
    # to the exact window edge, not stop at the last whole base unit.
    class OddSizeBackend(FakeGuiBackend):
        @property
        def size_units(self):
            return (10.5, 6.0)

    backend = OddSizeBackend(width=10, height=6)
    panel = Panel(backend)
    left, right = Label("l"), Label("r")
    panel.set_layout(HSplit(left, right))
    panel.render()
    right_rect = panel._children[-1].rect
    assert right_rect.x + right_rect.w == pytest.approx(10.5)


def test_layout_margin_px_insets_on_pixel_backends():
    backend = FakeGuiBackend(width=10, height=6)  # 100 x 120 px
    panel = Panel(backend)
    left, right = Label("l"), Label("r")
    panel.set_layout(HSplit(left, right), margin_px=8)
    panel.render()
    first = panel._children[0].rect
    last = panel._children[-1].rect
    assert first.x == pytest.approx(0.8)  # 8px at 10px base units
    assert first.y == pytest.approx(0.4)  # 8px at 20px base units
    assert last.x + last.w == pytest.approx(10 - 0.8)
    assert first.y + first.h == pytest.approx(6 - 0.4)


def test_layout_margin_px_ignored_on_whole_unit_grid():
    # A pixel margin would cost whole base units on TUI; like min_px, it only
    # applies on pixel-layout backends.
    backend = MemoryBackend(width=20, height=10)
    panel = Panel(backend)
    panel.set_layout(HSplit(Label("l"), Label("r")), margin_px=8)
    panel.render()
    first = panel._children[0].rect
    last = panel._children[-1].rect
    assert (first.x, first.y) == (0, 0)
    assert last.x + last.w == 20


def test_layout_margin_backgrounds_bleed_to_window_edges():
    # The margin must read as pane padding, not as a bare frame: edge panes
    # fill across the margin while their content rect stays inset.
    backend = FakeGuiBackend(width=10, height=6)  # 100 x 120 px
    panel = Panel(backend)
    left, right = Label("l"), Label("r")
    panel.set_layout(
        HSplit(
            Item(left, hints={"surface": "content"}),
            Item(right, hints={"surface": "sidebar"}),
        ),
        margin_px=8,
    )
    panel.render()
    first, last = panel._children[0], panel._children[-1]
    assert (first.fill.x, first.fill.y) == (0, 0)
    assert first.fill.y + first.fill.h == pytest.approx(6.0)
    assert last.fill.x + last.fill.w == pytest.approx(10.0)
    # The interior boundary between the panes is not extended.
    assert first.fill.x + first.fill.w == pytest.approx(first.rect.x + first.rect.w)
    assert last.fill.x == pytest.approx(last.rect.x)


def test_layout_margin_dividers_extend_to_window_edges():
    backend = FakeGuiBackend(width=10, height=6)
    panel = Panel(backend)
    panel.set_layout(HSplit(Label("l"), Label("r"), divider="strong"), margin_px=8)
    panel.render()
    (divider,) = panel._dividers
    assert divider.rect.y == 0
    assert divider.rect.y + divider.rect.h == pytest.approx(6.0)


def test_margin_clicks_route_to_edge_pane_with_clamped_coords():
    from puikit import Event, EventType

    class ClickProbe(Widget):
        focusable = True

        def __init__(self):
            self.events = []

        def draw(self, ctx):
            pass

        def handle_event(self, event):
            self.events.append(event)
            return True

    backend = FakeGuiBackend(width=10, height=6)  # 100 x 120 px
    panel = Panel(backend)
    left, right = ClickProbe(), ClickProbe()
    panel.set_layout(HSplit(left, right), margin_px=8)
    panel.render()
    # Base unit (0, 0) lies in the bled margin (content starts at 0.8, 0.4): the
    # click must reach the left pane, clamped to its first content base unit.
    assert panel.dispatch_event(Event(type=EventType.MOUSE_CLICK, x=0, y=0, button="left"))
    assert panel.focused is left
    (event,) = left.events
    assert (event.x, event.y) == (0, 0)


def test_layout_margin_units_applies_on_whole_unit_grid():
    backend = MemoryBackend(width=20, height=10)
    panel = Panel(backend)
    panel.set_layout(HSplit(Label("l"), Label("r")), margin_units=1)
    panel.render()
    first = panel._children[0].rect
    last = panel._children[-1].rect
    assert (first.x, first.y) == (1, 1)
    assert last.x + last.w == 19
    assert first.y + first.h == 9


class GeometryProbe(Widget):
    def __init__(self):
        self.size_units = None

    def draw(self, ctx):
        self.size_units = ctx.size_units


def test_panel_layout_pixel_vs_snap():
    probes_gui = [GeometryProbe() for _ in range(3)]
    backend = FakeGuiBackend(width=10, height=6)
    panel = Panel(backend)
    panel.set_layout(HSplit(*probes_gui))
    panel.render()
    assert probes_gui[0].size_units[0] == pytest.approx(3.3)  # 33 of 100px

    probes_tui = [GeometryProbe() for _ in range(3)]
    backend = MemoryBackend(width=10, height=6)  # TUI profile: snap
    panel = Panel(backend)
    panel.set_layout(HSplit(*probes_tui))
    panel.render()
    assert [p.size_units[0] for p in probes_tui] == [3, 4, 3]
