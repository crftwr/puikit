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
    placements = layout.resolve(0, 0, 10, 5, SNAP)
    result = rects(placements)
    assert all(isinstance(r.x, int) and isinstance(r.w, int) for r in result)
    assert [r.w for r in result] == [3, 4, 3]  # boundary rounding, no gaps
    assert result[0].x + result[0].w == result[1].x
    assert result[1].x + result[1].w == result[2].x
    assert result[2].x + result[2].w == 10


def test_pixel_layout_keeps_fractional_cells():
    widgets = [Label(str(i)) for i in range(3)]
    layout = HSplit(*widgets)
    result = rects(layout.resolve(0.0, 0.0, 10.0, 5.0, PIXEL))
    assert result[0].w == pytest.approx(10 / 3)
    assert result[1].x == pytest.approx(10 / 3)
    assert sum(r.w for r in result) == pytest.approx(10.0)


def test_min_px_converted_via_cell_size():
    sidebar, main = Label("side"), Label("main")
    layout = HSplit(Item(sidebar, weight=1, hints={"min_px": 55}), Item(main, weight=9))
    result = rects(layout.resolve(0.0, 0.0, 20.0, 5.0, PIXEL))
    # weight share would be 2.0 cells; min_px 55 at 10px cells means 5.5
    assert result[0].w == pytest.approx(5.5)


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
    assert probes_gui[0].size_cells[0] == pytest.approx(10 / 3)

    probes_tui = [GeometryProbe() for _ in range(3)]
    backend = MemoryBackend(width=10, height=6)  # TUI profile: snap
    panel = Panel(backend)
    panel.set_layout(HSplit(*probes_tui))
    panel.render()
    assert [p.size_cells[0] for p in probes_tui] == [3, 4, 3]
