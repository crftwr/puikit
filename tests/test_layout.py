"""Layout resolution: snapped to cells on TUI, pixel-fractional on GUI."""

import pytest

from puikit import HSplit, Item, Panel, PROFILE_GUI_DESKTOP, VSplit
from puikit.backends.memory_backend import MemoryBackend
from puikit.layout import LayoutContext
from puikit.widgets import Label, ListView, Widget

SNAP = LayoutContext(cell_w=1, cell_h=1, snap=True)
PIXEL = LayoutContext(cell_w=10, cell_h=20, snap=False)


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
    layout = HSplit(*widgets)  # equal weights over 10 cells: 3.33 each
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


def test_pixel_layout_keeps_fractional_cells():
    widgets = [Label(str(i)) for i in range(3)]
    layout = HSplit(*widgets)  # 10 cells x 10px: 100px split as 33/34/33
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
        # cell_w is 10: every boundary must be a whole number of pixels
        assert (rect.x * 10) == pytest.approx(round(rect.x * 10))
        assert (rect.w * 10) == pytest.approx(round(rect.w * 10))
    # Adjacent rects share their boundary exactly: no gaps, no overlap.
    assert result[0].x + result[0].w == pytest.approx(result[1].x)
    assert result[1].x + result[1].w == pytest.approx(result[2].x)


def test_min_px_converted_via_cell_size():
    sidebar, main = Label("side"), Label("main")
    layout = HSplit(Item(sidebar, weight=1, hints={"min_px": 55}), Item(main, weight=9))
    result = rects(layout.resolve(0.0, 0.0, 20.0, 5.0, PIXEL))
    # weight share would be 2.0 cells; min_px 55 at 10px cells means 5.5
    assert result[0].w == pytest.approx(5.5)


def test_min_px_ignored_on_cell_grid_backends():
    # Regression: on TUI (cell size 1x1) a min_px hint must not turn into a
    # huge min_cells value that starves the other items.
    sidebar, main, inspector = Label("side"), Label("main"), Label("insp")
    layout = HSplit(
        Item(sidebar, weight=1, hints={"min_px": 220, "min_cells": 18}),
        Item(main, weight=2),
        Item(inspector, weight=1),
    )
    rs, rm, ri = rects(layout.resolve(0, 0, 80, 20, SNAP))
    assert rs.w == 20  # weight share; min_cells=18 not binding, min_px ignored
    assert rm.w == 40
    assert ri.w == 20
    # min_cells still applies when it is the binding constraint.
    rs, rm, ri = rects(layout.resolve(0, 0, 40, 20, SNAP))
    assert rs.w == 18


def test_minimums_shrink_other_items_to_fit():
    a, b = Label("a"), Label("b")
    layout = HSplit(Item(a, weight=1, hints={"min_cells": 8}), Item(b, weight=1))
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


def test_divider_subtle_costs_nothing_on_cell_grid():
    a, b = Label("a"), Label("b")
    layout = HSplit(Item(a), Item(b), divider="subtle")
    ctx = LayoutContext(cell_w=1, cell_h=1, snap=True)
    ra, rb = rects(layout.resolve(0, 0, 80, 24, ctx))
    # No cells reserved, no divider emitted: background contrast (surface
    # roles) is what separates the panes on cell-grid backends.
    assert ra.w + rb.w == 80
    assert ctx.dividers == []


def test_divider_strong_spends_one_cell_on_cell_grid():
    a, b = Label("a"), Label("b")
    layout = HSplit(Item(a), Item(b), divider="strong")
    ctx = LayoutContext(cell_w=1, cell_h=1, snap=True)
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
    ctx = LayoutContext(cell_w=10, cell_h=20, snap=False, hairline=True)
    ra, rb = rects(layout.resolve(0.0, 0.0, 10.0, 5.0, ctx))
    (divider,) = ctx.dividers
    assert divider.rect.w * 10 == pytest.approx(1)  # one device pixel
    # The divider tiles exactly between the panes: no gap, no overlap.
    assert divider.rect.x == pytest.approx(ra.x + ra.w)
    assert rb.x == pytest.approx(divider.rect.x + divider.rect.w)
    assert rb.x + rb.w == pytest.approx(10.0)


class FakeGuiBackend(MemoryBackend):
    """Memory backend that claims pixel layout and a real cell size."""

    def __init__(self, **kwargs):
        super().__init__(capabilities=PROFILE_GUI_DESKTOP, **kwargs)

    @property
    def cell_size(self):
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
    # A window of 105px at 10px cells is 10.5 cells: the layout must extend
    # to the exact window edge, not stop at the last whole cell.
    class OddSizeBackend(FakeGuiBackend):
        @property
        def size_cells(self):
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
    assert first.x == pytest.approx(0.8)  # 8px at 10px cells
    assert first.y == pytest.approx(0.4)  # 8px at 20px cells
    assert last.x + last.w == pytest.approx(10 - 0.8)
    assert first.y + first.h == pytest.approx(6 - 0.4)


def test_layout_margin_px_ignored_on_cell_grid():
    # A pixel margin would cost whole cells on TUI; like min_px, it only
    # applies on pixel-layout backends.
    backend = MemoryBackend(width=20, height=10)
    panel = Panel(backend)
    panel.set_layout(HSplit(Label("l"), Label("r")), margin_px=8)
    panel.render()
    first = panel._children[0].rect
    last = panel._children[-1].rect
    assert (first.x, first.y) == (0, 0)
    assert last.x + last.w == 20


def test_layout_margin_cells_applies_on_cell_grid():
    backend = MemoryBackend(width=20, height=10)
    panel = Panel(backend)
    panel.set_layout(HSplit(Label("l"), Label("r")), margin_cells=1)
    panel.render()
    first = panel._children[0].rect
    last = panel._children[-1].rect
    assert (first.x, first.y) == (1, 1)
    assert last.x + last.w == 19
    assert first.y + first.h == 9


class GeometryProbe(Widget):
    def __init__(self):
        self.size_cells = None

    def draw(self, ctx):
        self.size_cells = ctx.size_cells


def test_panel_layout_pixel_vs_snap():
    probes_gui = [GeometryProbe() for _ in range(3)]
    backend = FakeGuiBackend(width=10, height=6)
    panel = Panel(backend)
    panel.set_layout(HSplit(*probes_gui))
    panel.render()
    assert probes_gui[0].size_cells[0] == pytest.approx(3.3)  # 33 of 100px

    probes_tui = [GeometryProbe() for _ in range(3)]
    backend = MemoryBackend(width=10, height=6)  # TUI profile: snap
    panel = Panel(backend)
    panel.set_layout(HSplit(*probes_tui))
    panel.render()
    assert [p.size_cells[0] for p in probes_tui] == [3, 4, 3]
