"""Sub-cell scrollbar and drop shadow on the VT backend.

Both are visual-parity gaps against the curses backend rather than new features:
curses already slides its scrollbar thumb in eighths of a cell and paints a
stepped drop shadow, and the VT backend was drawing a whole-cell thumb and no
shadow at all. The point of these is that a moving element does not jump a whole
row at a time on a grid whose cells are the smallest thing that can be painted.
"""

import io

import pytest

from puikit import Style
from puikit.backends._textgrid import LOWER_BLOCKS, SUBCELL, vbar_cells
from puikit.backends.curses_backend import _vbar_cells
from puikit.backends.vt_backend import VTBackend, _StreamConsole


@pytest.fixture
def backend():
    con = _StreamConsole(stream=io.StringIO(), size=(30, 12))
    con.size = lambda: (30, 12)
    be = VTBackend(console=con)
    be.open()
    yield be, be._grid
    be.close()


# --- the shared decomposition ---------------------------------------------


@pytest.mark.parametrize("h", [4, 7, 13])
@pytest.mark.parametrize("pos", [0.0, 0.13, 0.5, 0.77, 1.0])
@pytest.mark.parametrize("ratio", [0.05, 0.3, 0.5, 1.0])
def test_matches_the_curses_decomposition(h, pos, ratio):
    # The helper was lifted out of the curses backend, so it must decompose a bar
    # identically — otherwise the two backends disagree about where the thumb is.
    assert list(vbar_cells(h, pos, ratio)) == list(_vbar_cells(h, pos, ratio))


def test_thumb_moves_in_sub_row_steps():
    # The whole point: a small scroll must move the thumb without waiting for a
    # full row of travel to accumulate.
    a = list(vbar_cells(10, 0.30, 0.3))
    b = list(vbar_cells(10, 0.34, 0.3))
    assert a != b


def test_both_caps_never_land_in_one_cell():
    # A cell covered only in its middle has no glyph that could draw it, which is
    # what the one-cell minimum length prevents.
    for ratio in (0.0, 0.01, 0.02):
        kinds = [k for _, k, _ in vbar_cells(20, 0.5, ratio)]
        assert not (kinds.count("top") and kinds.count("bottom") and
                    kinds.index("top") == kinds.index("bottom"))


# --- the vertical bar ------------------------------------------------------


def test_thumb_body_is_a_background_not_a_block_glyph(backend):
    # A background fills the whole cell including the terminal's line spacing, so
    # a stacked body reads as one continuous bar; stacked "█" glyphs would leave
    # inter-line gaps.
    be, grid = backend
    be.clear()
    be.draw_scrollbar(0, 0, 6, 0.0, 1.0)   # thumb covers everything
    for row in range(6):
        glyph, _fg, bg, _attr, _ul = grid.cell_at(0, row)
        assert glyph == " "
        assert bg == (150, 150, 150)


def test_partial_cap_uses_the_block_ladder(backend):
    be, grid = backend
    be.clear()
    be.draw_scrollbar(0, 0, 6, 0.33, 0.25)
    glyphs = [grid.cell_at(0, r)[0] for r in range(6)]
    assert any(g in LOWER_BLOCKS[1:-1] for g in glyphs), glyphs


def test_upper_cap_inverts_the_colors(backend):
    # Unicode has no upper-block ladder, so a thumb covering a cell's UPPER part
    # is drawn as a lower block of the track's remainder over a thumb-colored
    # cell — the colors swap rather than the glyph changing.
    be, grid = backend
    be.clear()
    be.draw_scrollbar(0, 0, 8, 0.5, 0.3)
    caps = [(r, grid.cell_at(0, r)) for r in range(8)
            if grid.cell_at(0, r)[0] in LOWER_BLOCKS[1:-1]]
    assert caps
    for _row, (_glyph, fg, bg, _attr, _ul) in caps:
        assert {fg, bg} == {(150, 150, 150), (60, 60, 60)}


def test_horizontal_bar_is_a_thin_band(backend):
    # One row, so a lower-half block reads as a bar rather than a filled cell,
    # and the cell background is the client surface behind it.
    be, grid = backend
    be.clear()
    be.draw_scrollbar(0, 0, 8, 0.0, 0.5, orientation="horizontal", surface=(9, 9, 9))
    for col in range(8):
        glyph, _fg, bg, _attr, _ul = grid.cell_at(col, 0)
        assert glyph == "▄"
        assert bg == (9, 9, 9)


# --- the drop shadow -------------------------------------------------------


def test_shadow_hugs_the_right_and_bottom_edges(backend):
    be, grid = backend
    be.clear()
    be.fill_rect(0, 0, 30, 12, style=Style(bg=(20, 40, 80)))
    be.shadow_rect(2, 2, 8, 3)
    # Right edge: a half-cell start, then full darkened cells down the column.
    assert grid.cell_at(10, 2)[0] == "▄"
    assert grid.cell_at(10, 3)[0] == " "
    # Bottom: a half-cell band, one column right of the layer's left edge.
    assert grid.cell_at(3, 5)[0] == "▄"


def test_shadow_is_the_page_beneath_darkened_not_a_flat_gray(backend):
    # A band over a blue surface must read as a dark blue-gray derived from it,
    # not a fixed slab — so a shadow over the footer and one over the file list
    # differ. The grid IS the record of what the page painted, which is why this
    # needs no side table of cell colors the way the curses backend does.
    be, grid = backend
    be.clear()
    be.fill_rect(0, 0, 30, 6, style=Style(bg=(20, 40, 80)))    # dark blue
    be.fill_rect(0, 6, 30, 6, style=Style(bg=(200, 200, 200)))  # light gray
    be.shadow_rect(2, 1, 6, 3)     # bottom band lands on the blue
    over_dark = grid.cell_at(3, 4)[2]
    be.clear()
    be.fill_rect(0, 0, 30, 6, style=Style(bg=(20, 40, 80)))
    be.fill_rect(0, 6, 30, 6, style=Style(bg=(200, 200, 200)))
    be.shadow_rect(2, 6, 6, 3)     # bottom band lands on the light gray
    over_light = grid.cell_at(3, 9)[2]
    assert over_dark != over_light
    assert over_light > over_dark  # brighter page -> brighter shade


def test_shadow_keeps_the_page_color_in_the_uncovered_half(backend):
    # The bottom band's fg carries the page color, so the lower half of the cell
    # still shows what was there — that is what makes it read as half a cell of
    # shadow rather than a whole extra row of chrome.
    be, grid = backend
    be.clear()
    be.fill_rect(0, 0, 30, 12, style=Style(bg=(20, 40, 80)))
    be.shadow_rect(2, 2, 6, 2)
    _glyph, fg, bg, _attr, _ul = grid.cell_at(3, 4)
    assert fg == (20, 40, 80)
    assert bg != fg


def test_shadow_off_screen_is_ignored(backend):
    be, grid = backend
    be.clear()
    be.shadow_rect(28, 10, 6, 6)   # runs past both edges
    assert be._grid is not None    # must not raise


def test_shadow_is_a_noop_before_open():
    be = VTBackend(console=_StreamConsole(stream=io.StringIO()))
    be.shadow_rect(0, 0, 4, 4)     # no grid yet
