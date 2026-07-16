"""Tests for the TableView widget, run against TUI and GUI memory profiles."""

import pytest

from puikit import Event, EventType, Panel, PROFILE_GUI_DESKTOP, PROFILE_TUI
from puikit.backends.memory_backend import MemoryBackend
from puikit.widgets import TableView


@pytest.fixture(params=[PROFILE_TUI, PROFILE_GUI_DESKTOP], ids=["tui", "gui"])
def backend(request):
    return MemoryBackend(width=24, height=8, capabilities=request.param)


def _key(name, mods=frozenset()):
    return Event(type=EventType.KEY, key=name, modifiers=mods)


def _table():
    return TableView(
        ["id", "name", "qty", "color"],
        [["1", "apple", "12", "red"],
         ["2", "banana", "7", "yellow"],
         ["3", "cherry", "40", "green"]],
    )


def test_header_and_rows_align(backend):
    panel = Panel(backend)
    panel.add(_table(), x=0, y=0, w=24, h=8)
    panel.render()
    snap = backend.snapshot()
    assert snap[0].startswith("id")            # frozen header on top
    assert "apple" in snap[1]
    # Numeric "qty" column is right-aligned: 12 / 7 / 40 line up on their right.
    assert snap[1].index("12") + 2 == snap[2].index("7") + 1


def test_horizontal_scroll_reveals_later_columns():
    be = MemoryBackend(width=16, height=8, capabilities=PROFILE_TUI)
    view = _table()                                # total width 23 > 16
    panel = Panel(be)
    panel.add(view, x=0, y=0, w=16, h=8)
    panel.render()
    assert "color" not in be.snapshot()[0]         # last column off-screen at rest
    view.left = 12.0
    panel.render()
    snap = be.snapshot()
    assert "color" in snap[0]                       # header scrolled with body
    assert "yellow" in "".join(snap)


def test_header_frozen_while_body_scrolls():
    be = MemoryBackend(width=24, height=6, capabilities=PROFILE_TUI)
    rows = [[str(i), f"row{i}", str(i * 2), "c"] for i in range(30)]
    view = TableView(["id", "name", "qty", "color"], rows)
    panel = Panel(be)
    panel.add(view, x=0, y=0, w=24, h=6)
    panel.render()
    view.offset = 10.0     # scroll the body down
    panel.render()
    snap = be.snapshot()
    assert snap[0].startswith("id")        # header row still shows the header
    assert "row0" not in "".join(snap)     # early rows scrolled off
    assert "row10" in "".join(snap)        # a later row is now visible


def test_keyboard_moves_current_cell(backend):
    view = _table()
    panel = Panel(backend)
    panel.add(view, x=0, y=0, w=24, h=8)
    panel.render()
    panel.dispatch_event(_key("down"))
    panel.dispatch_event(_key("right"))
    assert (view._cur_row, view._cur_col) == (1, 1)


def test_ctrl_c_copies_selection_as_tsv(backend):
    view = _table()
    panel = Panel(backend)
    panel.add(view, x=0, y=0, w=24, h=8)
    panel.render()
    view._sel_anchor = (0, 0)
    view._sel_cursor = (1, 1)
    panel.dispatch_event(_key("c", frozenset({"cmd"})))
    assert backend.get_clipboard() == "1\tapple\n2\tbanana"


def test_ctrl_a_selects_all_then_copy(backend):
    view = _table()
    panel = Panel(backend)
    panel.add(view, x=0, y=0, w=24, h=8)
    panel.render()
    panel.dispatch_event(_key("a", frozenset({"ctrl"})))
    panel.dispatch_event(_key("c", frozenset({"ctrl"})))
    clip = backend.get_clipboard()
    assert clip.splitlines()[0] == "1\tapple\t12\tred"
    assert clip.splitlines()[-1] == "3\tcherry\t40\tgreen"


def test_search_matches_navigate_and_status(backend):
    view = _table()
    panel = Panel(backend)
    panel.add(view, x=0, y=0, w=24, h=8)
    panel.render()
    view.search_begin()
    assert view.search_set("e") >= 2       # "apple", "cherry", "red", "green"...
    first = view.search_status()
    assert first[0] == 1
    view.search_navigate(1)
    assert view.search_status()[0] == 2


def test_search_no_match_restores_scroll(backend):
    view = _table()
    panel = Panel(backend)
    panel.add(view, x=0, y=0, w=24, h=8)
    panel.render()
    view.offset = 1.0
    view.search_begin()
    assert view.search_set("zzz") == 0
    assert view.offset == 1.0
