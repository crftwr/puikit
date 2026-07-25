"""A vertical scrollbar's thumb lands on 1/8-cell boundaries on a character grid.

The thumb *body* stays a background fill (seamless across the terminal's line
spacing), while its two end caps are drawn from the lower-block ladder — so the
thumb slides in eighth-row steps instead of jumping a whole row at a time. The
bottom cap inverts fg/bg, Unicode having no upper-block ladder to match.

Two backends render this: CursesBackend for the real TUI and MemoryBackend for
every widget test. They carry separate copies of the geometry (the headless
backend must never import curses), so the last test here pins them together.
"""

import pytest

from puikit.backend import Style
from puikit.backends.memory_backend import (
    MemoryBackend, _LOWER_BLOCKS, _SUBCELL, _vbar_cells,
)

THUMB = (200, 200, 200)
TRACK = (40, 40, 40)
STYLE = Style(fg=THUMB, bg=TRACK)


def _column(backend, h, x=0):
    """The bar column as (glyph, fg, bg) per row."""
    return [
        (backend.snapshot()[row][x], backend.style_at(x, row).fg,
         backend.style_at(x, row).bg)
        for row in range(h)
    ]


def test_thumb_body_is_a_background_fill():
    # A thumb covering whole cells draws no glyph at all: the fill covers the
    # inter-line spacing that a stacked block glyph would leave gapped.
    backend = MemoryBackend(4, 10)
    backend.clear()
    backend.draw_scrollbar(0, 0, 10, 0.0, 0.5, STYLE)
    column = _column(backend, 10)
    assert [c[0] for c in column] == [" "] * 10
    assert [c[2] for c in column] == [THUMB] * 5 + [TRACK] * 5


# h=8, ratio=0.5 -> a 32-eighth thumb in a 64-eighth track; pos=0.625 starts it
# at eighth 20 — halfway into row 2 — and ends it halfway into row 6.
_HALF_BAR = dict(h=8, pos=0.625, ratio=0.5)


def test_top_cap_is_a_lower_block_in_the_thumb_color():
    # Row 2 is the top cap: the thumb covers its lower half.
    backend = MemoryBackend(4, 8)
    backend.clear()
    backend.draw_scrollbar(0, 0, _HALF_BAR["h"], _HALF_BAR["pos"],
                           _HALF_BAR["ratio"], STYLE)
    glyph, fg, bg = _column(backend, 8)[2]
    assert glyph == _LOWER_BLOCKS[4] == "▄"  # lower 4/8 of the cell
    assert (fg, bg) == (THUMB, TRACK)        # thumb below, track above


def test_bottom_cap_inverts_foreground_and_background():
    # Same bar: the thumb ends halfway into row 6, whose *upper* half it covers.
    # No upper-block ladder exists, so the cap is the track's remaining lower
    # 4/8 painted in the track color over a thumb-colored cell.
    backend = MemoryBackend(4, 8)
    backend.clear()
    backend.draw_scrollbar(0, 0, _HALF_BAR["h"], _HALF_BAR["pos"],
                           _HALF_BAR["ratio"], STYLE)
    glyph, fg, bg = _column(backend, 8)[6]
    assert glyph == _LOWER_BLOCKS[4] == "▄"
    assert (fg, bg) == (TRACK, THUMB)  # inverted: thumb above, track below


def test_thumb_moves_in_sub_row_steps():
    # The whole point: two positions a fraction of a row apart must render
    # differently. With whole-cell rounding both of these snapped to row 1.
    def render(pos):
        backend = MemoryBackend(4, 8)
        backend.clear()
        backend.draw_scrollbar(0, 0, 8, pos, 0.25, STYLE)
        return _column(backend, 8)

    assert render(0.20) != render(0.25)


def test_ends_are_flush_at_the_extremes():
    # pos=0 starts flush with the top edge, pos=1 ends flush with the bottom —
    # no cap glyph at either, so the bar never looks short of its track.
    for pos, expected in ((0.0, [THUMB] * 3 + [TRACK] * 9),
                          (1.0, [TRACK] * 9 + [THUMB] * 3)):
        backend = MemoryBackend(4, 12)
        backend.clear()
        backend.draw_scrollbar(0, 0, 12, pos, 0.25, STYLE)
        column = _column(backend, 12)
        assert [c[0] for c in column] == [" "] * 12
        assert [c[2] for c in column] == expected


def test_short_thumb_keeps_its_one_cell_minimum():
    # A tiny ratio still yields a full cell, which is also what keeps both caps
    # out of the same cell — a cell covered only in its middle has no glyph.
    for pos in (0.0, 0.37, 0.5, 1.0):
        cells = list(_vbar_cells(20, pos, 0.001))
        painted = sum(n for _, kind, n in cells if kind != "track")
        assert painted == _SUBCELL


def test_full_ratio_fills_the_track():
    cells = list(_vbar_cells(6, 0.0, 1.0))
    assert all(kind == "thumb" for _, kind, _ in cells)


def test_no_color_falls_back_to_whole_cells():
    # A cap paints two colors into one cell; without color there is nothing to
    # invert, so the mono fallback yields whole cells only.
    for pos in (0.0, 0.33, 0.5, 0.9, 1.0):
        kinds = {kind for _, kind, _ in _vbar_cells(10, pos, 0.37, False)}
        assert kinds <= {"thumb", "track"}


@pytest.mark.parametrize("h", [1, 2, 5, 10, 37])
def test_geometry_is_total_and_contiguous(h):
    # Every row gets exactly one cell, and the thumb is one unbroken run.
    for pos in (0.0, 0.1, 0.5, 0.83, 1.0):
        for ratio in (0.01, 0.2, 0.5, 0.99, 1.0):
            cells = list(_vbar_cells(h, pos, ratio))
            assert [row for row, _, _ in cells] == list(range(h))
            covered = [i for i, (_, kind, _) in enumerate(cells) if kind != "track"]
            assert covered == list(range(covered[0], covered[-1] + 1))
            # Caps only ever sit at the ends of that run, pointing outward.
            for i, (_, kind, _) in enumerate(cells):
                if kind == "top":
                    assert i == covered[0]
                elif kind == "bottom":
                    assert i == covered[-1]


def test_curses_and_memory_geometry_agree():
    # The two backends duplicate _vbar_cells (memory_backend must not import
    # curses); a change to one that skips the other would be invisible to every
    # widget test, which only ever sees the memory copy.
    curses_backend = pytest.importorskip("puikit.backends.curses_backend")
    for h in (1, 3, 8, 25):
        for pos in (0.0, 0.17, 0.5, 0.75, 1.0):
            for ratio in (0.02, 0.3, 0.66, 1.0):
                for subcell in (True, False):
                    assert list(_vbar_cells(h, pos, ratio, subcell)) == \
                        list(curses_backend._vbar_cells(h, pos, ratio, subcell))


def test_curses_backend_draws_the_caps():
    """The real TUI path: same glyphs and the same fg/bg inversion."""
    curses = pytest.importorskip("curses")
    from puikit.backends.curses_backend import CursesBackend

    class _FakeStdscr:
        def __init__(self):
            self.calls = []

        def getmaxyx(self):
            return (8, 4)

        def erase(self):
            pass

        def addstr(self, y, x, text, attr=0):
            self.calls.append((y, x, text))

        def move(self, y, x):
            pass

        def refresh(self):
            pass

    backend = CursesBackend()
    backend._stdscr = _FakeStdscr()
    # Color pairs need an initscr'd terminal; the cap logic only needs colors to
    # *exist*, and the styles it chose are recorded per cell either way.
    backend._to_curses_attr = lambda style: 0
    backend._color_pair = lambda fg, bg: 0
    backend.clear()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(curses, "has_colors", lambda: True)
        backend.draw_scrollbar(0, 0, _HALF_BAR["h"], _HALF_BAR["pos"],
                               _HALF_BAR["ratio"], STYLE)
    glyphs = {y: text for y, x, text in backend._stdscr.calls if x == 0}
    assert glyphs[2] == "▄" and backend._cell_color[(2, 0)] == (THUMB, TRACK)
    assert glyphs[6] == "▄" and backend._cell_color[(6, 0)] == (TRACK, THUMB)
    assert glyphs[4] == " " and backend._cell_color[(4, 0)] == (None, THUMB)
