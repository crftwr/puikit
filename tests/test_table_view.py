"""Tests for the TableView widget, run against TUI and GUI memory profiles."""

import pytest

from puikit import (CapabilityProfile, Event, EventType, Panel, Style,
                   PROFILE_GUI_DESKTOP, PROFILE_TUI)
from puikit.backends.memory_backend import MemoryBackend
from puikit.widgets import TableView

_BOX_GLYPHS = "│─┼├┤┬┴┌┐└┘"


@pytest.fixture(params=[PROFILE_TUI, PROFILE_GUI_DESKTOP], ids=["tui", "gui"])
def backend(request):
    return MemoryBackend(width=24, height=8, capabilities=request.param)


class _VectorBackend(MemoryBackend):
    """A grid backend that *claims* vector_shapes so the Panel's vector path (thin
    hairline strokes instead of box-drawing glyphs) can be exercised headlessly."""

    @property
    def capabilities(self) -> CapabilityProfile:
        return CapabilityProfile({**self._capabilities, "vector_shapes": True})


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
    assert "id" in snap[0] and "name" in snap[0]   # frozen header on top
    assert any(g in snap[1] for g in "├┼─")         # box-drawing header separator
    row_apple = next(r for r in snap if "apple" in r)
    row_banana = next(r for r in snap if "banana" in r)
    # Numeric "qty" column is right-aligned: 12 and 7 share a right edge.
    assert row_apple.index("12") + len("12") == row_banana.index("7") + len("7")


def test_tui_draws_box_drawing_grid():
    # On a character grid the keisen are box-drawing glyphs: ``│`` column bars and
    # a ``├─┼─┤`` separator under the header (like MarkdownView's tables).
    be = MemoryBackend(width=26, height=10, capabilities=PROFILE_TUI)
    panel = Panel(be)
    panel.add(_table(), x=0, y=0, w=26, h=10)
    panel.render()
    snap = be.snapshot()
    assert snap[0].count("│") >= 2                      # header framed by column bars
    assert any(g in snap[1] for g in "├┼┤")             # header separator row
    assert "─" in snap[1]
    body = next(r for r in snap if "apple" in r)
    assert "│" in body                                  # body cells separated by bars


def test_gui_uses_hairlines_not_box_glyphs():
    # On a vector backend the keisen are device-thin hairline strokes, so no
    # box-drawing glyph is ever painted onto the grid.
    be = _VectorBackend(width=26, height=10, capabilities=PROFILE_GUI_DESKTOP)
    panel = Panel(be)
    panel.add(_table(), x=0, y=0, w=26, h=10)
    panel.render()
    text = "\n".join(be.snapshot())
    assert not any(g in text for g in _BOX_GLYPHS)      # hairlines, not glyphs
    assert "apple" in text                               # cells still render


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
    assert "id" in snap[0] and "name" in snap[0]   # header row still shows the header
    assert "row0 " not in "".join(snap)    # early rows scrolled off
    assert "row10" in "".join(snap)        # a later row is now visible


def test_header_survives_fractional_scroll():
    # A wheel/trackpad scroll lands the body on a fractional offset; the frozen
    # header must stay intact (a body row scrolled up beneath it must not paint
    # over it).
    be = MemoryBackend(width=24, height=6, capabilities=PROFILE_TUI)
    rows = [[str(i), f"row{i}", str(i * 2), "c"] for i in range(30)]
    view = TableView(["id", "name", "qty", "color"], rows)
    panel = Panel(be)
    panel.add(view, x=0, y=0, w=24, h=6)
    panel.render()
    view.offset = 3.5          # fractional
    panel.render()
    snap = be.snapshot()
    assert "id" in snap[0] and "name" in snap[0]   # header still whole


def test_body_does_not_overlap_horizontal_scrollbar():
    # A wide + tall table shows both bars; the last body row must stop above the
    # horizontal scroll bar's track rather than bleeding into it.
    be = MemoryBackend(width=16, height=7, capabilities=PROFILE_TUI)
    rows = [[str(i), f"row{i}", str(i * 2), "wide-col-value"] for i in range(30)]
    view = TableView(["id", "name", "qty", "notes"], rows)
    panel = Panel(be)
    panel.add(view, x=0, y=0, w=16, h=7)
    panel.render()
    snap = be.snapshot()
    assert "row" not in snap[-1]      # bottom row is the h-scrollbar, not content


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


def test_search_moves_current_cell_and_commits_on_accept(backend):
    # Like the main file manager's i-search: the current cell follows the match
    # and Enter (search_accept) leaves it on the matched row.
    view = _table()
    panel = Panel(backend)
    panel.add(view, x=0, y=0, w=24, h=8)
    panel.render()
    assert view._cur_row == 0
    view.search_begin()
    view.search_set("cherry")                # 3rd body row
    assert view._cur_row == 2                # current cell moved to the match
    assert view._sel_anchor is None          # drag selection dropped
    view.search_accept()                     # Enter
    assert view._cur_row == 2                # stays on the match


def test_search_moves_current_cell_to_matching_column():
    # The cursor lands on the matching *cell* (row and column), panning the view
    # horizontally to reveal a match in an off-screen column.
    rows = [["1", "apple", "red", "sweet"],
            ["2", "banana", "yellow", "soft"],
            ["3", "cherry", "crimson", "tart"]]
    view = TableView(["id", "name", "color", "taste"], rows)
    be = MemoryBackend(width=16, height=8, capabilities=PROFILE_TUI)  # forces h-scroll
    panel = Panel(be)
    panel.add(view, x=0, y=0, w=16, h=8)
    panel.render()
    assert (view._cur_row, view._cur_col) == (0, 0) and view.left == 0.0
    view.search_begin()
    view.search_set("crimson")               # column 2, row 2, off-screen at rest
    assert (view._cur_row, view._cur_col) == (2, 2)
    assert view.left > 0.0                    # panned horizontally to reveal it
    panel.render()
    assert "crimson" in "".join(be.snapshot())


def test_search_cancel_restores_current_cell(backend):
    view = _table()
    panel = Panel(backend)
    panel.add(view, x=0, y=0, w=24, h=8)
    panel.render()
    view.search_begin()
    view.search_set("cherry")
    assert view._cur_row == 2
    view.search_cancel()                     # Esc restores the pre-search cell
    assert (view._cur_row, view._cur_col) == (0, 0)


def test_search_no_match_restores_scroll(backend):
    view = _table()
    panel = Panel(backend)
    panel.add(view, x=0, y=0, w=24, h=8)
    panel.render()
    view.offset = 1.0
    view.search_begin()
    assert view.search_set("zzz") == 0
    assert view.offset == 1.0


class _FillRecordingBackend(MemoryBackend):
    """Records every fill_rect so a redundant page fill can be counted."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fills = []

    def fill_rect(self, x, y, w, h, style=None):
        self.fills.append((round(x), round(y), round(w), round(h),
                           getattr(style, "bg", None)))
        super().fill_rect(x, y, w, h, style if style is not None else Style())


def _page_fills(backend, bg):
    return [f for f in backend.fills if f == (0, 0, 24, 8, bg)]


@pytest.mark.parametrize(
    "profile,expected", [(PROFILE_GUI_DESKTOP, 1), (PROFILE_TUI, 2)],
    ids=["compositing-dedupes", "grid-keeps-both"])
def test_page_fill_skipped_when_it_repeats_the_inherited_background(profile, expected):
    # The view's page fill is dropped when it only repeats the surface it already
    # sits on: on a compositing backend that second blend would dissolve the same
    # surface twice under a background reveal — and paint over the scene entirely on
    # a page its owner deliberately left unpainted (a full-window viewer over a
    # wallpaper). A character grid can't composite, so both fills stay.
    backend = _FillRecordingBackend(width=24, height=8, capabilities=profile)
    panel = Panel(backend)
    content = panel.theme.surface_bg("content")
    panel.add(TableView(["id"], [["1"]], style=Style(bg=content)),
              x=0, y=0, w=24, h=8, hints={"surface": "content"})
    panel.render()
    assert len(_page_fills(backend, content)) == expected


def test_page_fill_kept_when_the_view_is_its_own_surface():
    # A view that deliberately paints a *different* surface than the page it sits on
    # still fills — the dedup is only for a repeat of the same color.
    backend = _FillRecordingBackend(width=24, height=8, capabilities=PROFILE_GUI_DESKTOP)
    panel = Panel(backend)
    own = (12, 34, 56)
    panel.add(TableView(["id"], [["1"]], style=Style(bg=own)),
              x=0, y=0, w=24, h=8, hints={"surface": "content"})
    panel.render()
    assert len(_page_fills(backend, own)) == 1
