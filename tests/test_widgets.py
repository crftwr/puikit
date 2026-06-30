"""Widget tests run identically against the TUI and GUI capability profiles."""

import pytest

from puikit import Event, EventType, Panel, PROFILE_GUI_DESKTOP, PROFILE_TUI, TextAttribute
from puikit.backends.memory_backend import MemoryBackend
from puikit.widgets import Label, ListView, ScrollBar


@pytest.fixture(params=[PROFILE_TUI, PROFILE_GUI_DESKTOP], ids=["tui", "gui"])
def backend(request):
    return MemoryBackend(width=30, height=10, capabilities=request.param)


def test_label_renders_text(backend):
    panel = Panel(backend)
    panel.add(Label("hello"), x=0, y=0, w=10, h=1)
    panel.render()
    assert backend.snapshot()[0].startswith("hello")


def test_listview_renders_visible_slice_and_selection(backend):
    panel = Panel(backend)
    listview = ListView([f"item{i}" for i in range(20)])
    panel.add(listview, x=0, y=0, w=10, h=5)
    panel.render()
    lines = backend.snapshot()
    assert lines[0].startswith("item0")
    assert lines[4].startswith("item4")
    # Selected row is drawn with the theme's active selection fill — the same
    # colored highlight on TUI and GUI, not a reverse-video swap.
    active = panel.theme.selection_active_bg
    assert backend.style_at(0, 0).bg == active
    assert backend.style_at(0, 1).bg != active
    # Long list shows a scrollbar in the last column, painted via base unit
    # background color (the theme's thumb or track) rather than a glyph.
    assert backend.style_at(9, 0).bg in {
        panel.theme.scrollbar_thumb,
        panel.theme.scrollbar_track,
    }


def test_listview_pads_rows_by_display_width(backend):
    # An item with a wide glyph (emoji) is fewer characters than display
    # columns; the row must be padded to the pane's column width, not its
    # character count, or the selection background overflows the pane edge.
    from puikit.text import display_width

    captured: list[str] = []
    orig = backend.draw_text

    def spy(x, y, text, style=None):
        captured.append(text)
        return orig(x, y, text, style) if style is not None else orig(x, y, text)

    backend.draw_text = spy
    panel = Panel(backend)
    panel.add(ListView(["🏷️ Label", "🫧 Alpha", "plain"]), x=0, y=0, w=8, h=3)
    panel.render()
    rows = [t for t in captured if t.strip()]
    assert rows, "expected the list rows to be drawn"
    for row in rows:
        assert display_width(row) == 8


def test_listview_keyboard_navigation_scrolls(backend):
    panel = Panel(backend)
    listview = ListView([f"item{i}" for i in range(20)])
    panel.add(listview, x=0, y=0, w=10, h=5)
    panel.render()
    for _ in range(7):
        panel.dispatch_event(Event(type=EventType.KEY, key="down"))
    assert listview.selected == 7
    panel.render()
    assert backend.snapshot()[4].startswith("item7")  # scrolled so selection is visible
    panel.dispatch_event(Event(type=EventType.KEY, key="end"))
    assert listview.selected == 19
    panel.dispatch_event(Event(type=EventType.KEY, key="home"))
    assert listview.selected == 0


def test_listview_mouse_click_selects(backend):
    panel = Panel(backend)
    listview = ListView([f"item{i}" for i in range(20)])
    panel.add(listview, x=2, y=1, w=10, h=5)
    panel.dispatch_event(Event(type=EventType.MOUSE_CLICK, x=3, y=3, button="left"))
    assert listview.selected == 2  # row 2 inside the widget


def test_listview_enter_triggers_on_select(backend):
    selections = []
    panel = Panel(backend)
    listview = ListView(["a", "b", "c"], on_select=lambda i, t: selections.append((i, t)))
    panel.add(listview, x=0, y=0, w=10, h=3)
    panel.dispatch_event(Event(type=EventType.KEY, key="down"))
    panel.dispatch_event(Event(type=EventType.KEY, key="enter"))
    assert selections == [(1, "b")]


def test_listview_mouse_scroll_moves_viewport_not_selection(backend):
    panel = Panel(backend)
    listview = ListView([f"item{i}" for i in range(20)])
    panel.add(listview, x=0, y=0, w=10, h=5)
    panel.render()
    panel.dispatch_event(Event(type=EventType.MOUSE_SCROLL, x=1, y=1, scroll=-3))
    assert listview.offset == 3
    assert listview.selected == 0  # selection stays put
    # The scrolled view survives a render even though the selection is off-screen.
    panel.render()
    assert listview.offset == 3
    assert backend.snapshot()[0].startswith("item3")


def test_listview_mouse_scroll_clamps_at_bounds(backend):
    panel = Panel(backend)
    listview = ListView([f"item{i}" for i in range(20)])
    panel.add(listview, x=0, y=0, w=10, h=5)
    panel.render()
    panel.dispatch_event(Event(type=EventType.MOUSE_SCROLL, x=1, y=1, scroll=5))
    assert listview.offset == 0  # already at the top
    panel.dispatch_event(Event(type=EventType.MOUSE_SCROLL, x=1, y=1, scroll=-100))
    assert listview.offset == 15  # bottom: len(items) - viewport_h


def test_listview_smooth_scroll_accumulates_fractional_offset(backend):
    # A backend whose scroll event carries a precise sub-unit delta (in hints)
    # scrolls the viewport by that fraction of a row, not a whole row.
    panel = Panel(backend)
    listview = ListView([f"item{i}" for i in range(20)])
    panel.add(listview, x=0, y=0, w=10, h=5)
    panel.render()
    panel.dispatch_event(
        Event(type=EventType.MOUSE_SCROLL, x=1, y=1, scroll=-1, hints={"scroll_units": -0.5})
    )
    assert listview.offset == pytest.approx(0.5)
    panel.dispatch_event(
        Event(type=EventType.MOUSE_SCROLL, x=1, y=1, scroll=-1, hints={"scroll_units": -1.25})
    )
    assert listview.offset == pytest.approx(1.75)


def test_listview_smooth_scroll_clamps_to_content(backend):
    panel = Panel(backend)
    listview = ListView([f"item{i}" for i in range(20)])
    panel.add(listview, x=0, y=0, w=10, h=5)
    panel.render()
    # Overscroll past the bottom: clamped to content_h - viewport_h, even
    # though the requested fractional delta runs well past it.
    panel.dispatch_event(
        Event(type=EventType.MOUSE_SCROLL, x=1, y=1, scroll=-1, hints={"scroll_units": -99.5})
    )
    assert listview.offset == pytest.approx(15.0)
    # And back up past the top.
    panel.dispatch_event(
        Event(type=EventType.MOUSE_SCROLL, x=1, y=1, scroll=1, hints={"scroll_units": 99.5})
    )
    assert listview.offset == pytest.approx(0.0)


def test_listview_keyboard_pulls_selection_back_into_view(backend):
    panel = Panel(backend)
    listview = ListView([f"item{i}" for i in range(20)])
    panel.add(listview, x=0, y=0, w=10, h=5)
    panel.render()
    panel.dispatch_event(Event(type=EventType.MOUSE_SCROLL, x=1, y=1, scroll=-10))
    assert listview.offset == 10
    panel.dispatch_event(Event(type=EventType.KEY, key="down"))
    assert listview.selected == 1
    assert listview.offset == 1  # viewport follows the selection again


def test_listview_on_change_fires_only_when_selection_moves(backend):
    changes = []
    panel = Panel(backend)
    listview = ListView(["a", "b", "c"], on_change=lambda i, t: changes.append((i, t)))
    panel.add(listview, x=0, y=0, w=10, h=3)
    panel.render()  # establish the viewport
    panel.dispatch_event(Event(type=EventType.KEY, key="down"))
    assert changes == [(1, "b")]
    panel.dispatch_event(Event(type=EventType.KEY, key="enter"))  # no move
    assert changes == [(1, "b")]
    panel.dispatch_event(Event(type=EventType.MOUSE_CLICK, x=1, y=0, button="left"))
    assert changes == [(1, "b"), (0, "a")]
    panel.dispatch_event(Event(type=EventType.KEY, key="up"))  # clamped at top
    assert changes == [(1, "b"), (0, "a")]


def test_listview_row_factory_draws_a_widget_per_item(backend):
    # With a row_factory each item becomes a widget; the rows draw their own
    # content (here a Checkbox's label) instead of the item string.
    from puikit.widgets import Checkbox

    panel = Panel(backend)
    listview = ListView(
        [f"opt{i}" for i in range(8)],
        row_factory=lambda item: Checkbox(item),
    )
    panel.add(listview, x=0, y=0, w=12, h=4)
    panel.render()
    line = backend.snapshot()[0]
    assert "opt0" in line  # the row widget rendered its label


def test_listview_row_factory_routes_click_to_inner_widget(backend):
    from puikit.widgets import Checkbox

    toggled: list[bool] = []
    panel = Panel(backend)
    listview = ListView(
        ["a", "b", "c"],
        row_factory=lambda item: Checkbox(item, on_change=toggled.append),
    )
    panel.add(listview, x=0, y=0, w=12, h=3)
    panel.render()
    # A click on row 1 selects it and toggles that row's checkbox.
    panel.dispatch_event(Event(type=EventType.MOUSE_CLICK, x=1, y=1, button="left"))
    assert listview.selected == 1
    assert listview.row_widget(1).checked is True
    assert toggled == [True]


def test_listview_row_factory_space_activates_selected_row(backend):
    from puikit.widgets import Checkbox

    panel = Panel(backend)
    listview = ListView(
        ["a", "b", "c"],
        row_factory=lambda item: Checkbox(item),
    )
    panel.add(listview, x=0, y=0, w=12, h=3)
    panel.render()
    panel.dispatch_event(Event(type=EventType.KEY, key="down"))
    panel.dispatch_event(Event(type=EventType.KEY, key="space"))
    assert listview.selected == 1
    assert listview.row_widget(1).checked is True
    assert listview.row_widget(0).checked is False


def test_listview_row_factory_caches_row_widgets(backend):
    calls: list[str] = []

    def factory(item):
        calls.append(item)
        return Label(item)

    panel = Panel(backend)
    listview = ListView(["a", "b"], row_factory=factory)
    panel.add(listview, x=0, y=0, w=12, h=3)
    panel.render()
    panel.render()
    # Two visible rows, built once each and reused across renders.
    assert calls == ["a", "b"]
    # set_items discards the cache so the factory rebuilds.
    listview.set_items(["x", "y"])
    panel.render()
    assert calls == ["a", "b", "x", "y"]


def test_listview_row_height_scales_scroll_geometry(backend):
    # Rows two units tall: ten items make a 20-unit content height, so a
    # 6-unit pane scrolls and the offset clamps in base units, not item counts.
    panel = Panel(backend)
    listview = ListView(
        [f"r{i}" for i in range(10)],
        row_factory=lambda item: Label(item),
        row_height=2,
    )
    panel.add(listview, x=0, y=0, w=12, h=6)
    panel.render()
    panel.dispatch_event(Event(type=EventType.MOUSE_SCROLL, x=1, y=1, scroll=-100))
    assert listview.offset == 14  # 10*2 - 6


def test_scrollbar_thumb_position(backend):
    panel = Panel(backend)
    panel.add(ScrollBar(pos=1.0, ratio=0.3), x=0, y=0, w=1, h=10)
    panel.render()
    # The bar is painted with base unit background colors from the theme: the
    # thumb on the opposite-brightness side, the track close to the background.
    thumb, track = panel.theme.scrollbar_thumb, panel.theme.scrollbar_track
    column = [backend.style_at(0, row).bg for row in range(10)]
    assert column[0] == track
    assert column[9] == thumb  # thumb at the bottom for pos=1.0
    assert column.count(thumb) == 3  # 30% of 10 rows


def test_scrollbar_horizontal_orientation(backend):
    # A horizontal bar lays the thumb out along a row (one cell tall on the grid).
    from puikit.backends.memory_backend import _SCROLLBAR_THUMB, _SCROLLBAR_TRACK
    backend.draw_scrollbar(0, 0, 10, 1.0, 0.3, orientation="horizontal")
    row = [backend.style_at(col, 0).bg for col in range(10)]
    assert row[0] == _SCROLLBAR_TRACK
    assert row[9] == _SCROLLBAR_THUMB        # thumb at the right for pos=1.0
    assert row.count(_SCROLLBAR_THUMB) == 3  # 30% of 10 columns
