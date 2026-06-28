"""A wide (full-width / CJK) glyph straddling a layer edge is split cleanly.

A wide glyph is one ``addstr`` spanning two terminal cells. When an opaque
upper layer — a dialog fill, the drop shadow — covers only one of those cells,
the orphaned half would render as a broken glyph spilling past the covering
edge. The curses backend tracks each wide glyph's lead cell and replaces the
orphaned half with a background space so only a clean left/right half remains.
These tests pin that contract without needing a real terminal.
"""

import pytest

curses = pytest.importorskip("curses")

from puikit.backends.curses_backend import CursesBackend  # noqa: E402


class _FakeStdscr:
    def __init__(self, w=40, h=12):
        self._w, self._h = w, h
        self.calls = []  # (op, *args)

    def getmaxyx(self):
        return (self._h, self._w)

    def erase(self):
        self.calls.append(("erase",))

    def addstr(self, y, x, text, attr=0):
        self.calls.append(("addstr", y, x, text))

    def chgat(self, y, x, n, attr=0):
        self.calls.append(("chgat", y, x, n))

    def redrawln(self, top, count):
        self.calls.append(("redrawln", top, count))

    def move(self, y, x):
        self.calls.append(("move", y, x))

    def refresh(self):
        self.calls.append(("refresh",))


@pytest.fixture
def backend(monkeypatch):
    # Color attrs / pairs need an initscr'd terminal; neutralize them so the
    # split logic (which does not depend on the attr value) runs headless.
    monkeypatch.setattr(curses, "has_colors", lambda: False)
    b = CursesBackend()
    b._stdscr = _FakeStdscr()
    b._to_curses_attr = lambda style: 0
    b.clear()
    return b


def _single_space_cols(backend, y):
    """Columns where a lone ' ' was written on row ``y`` (a _blank_cell_bg)."""
    return [
        c[2] for c in backend._stdscr.calls
        if c[0] == "addstr" and c[1] == y and c[3] == " "
    ]


def test_left_edge_blanks_the_orphaned_lead(backend):
    # 漢 occupies cols 2,3 (lead at col 2). A higher layer starting at col 3 (its
    # trail) covers only the right half — the lead at col 2 must be blanked.
    backend.draw_text(0, 0, "AB漢CD")
    assert (0, 2) in backend._wide_lead

    backend.draw_text(3, 0, "XY")
    assert 2 in _single_space_cols(backend, 0)
    assert (0, 2) not in backend._wide_lead


def test_right_edge_blanks_the_orphaned_trail(backend):
    # 漢 occupies cols 2,3 (lead at col 2). A higher layer covering cols 0..2
    # takes the lead but not the trail at col 3 — col 3 must be blanked.
    backend.draw_text(0, 0, "AB漢CD")
    backend.draw_text(0, 0, "XYZ")
    assert 3 in _single_space_cols(backend, 0)


def test_full_cover_leaves_no_orphan(backend):
    # A higher layer covering both of the glyph's cells needs no blank fixup.
    backend.draw_text(0, 0, "AB漢CD")
    before = len(_single_space_cols(backend, 0))
    backend.draw_text(2, 0, "WXYZ")  # covers cols 2,3 (and beyond)
    assert len(_single_space_cols(backend, 0)) == before


def test_ascii_only_tracks_no_wide_glyphs(backend):
    backend.draw_text(0, 0, "plainascii")  # no spaces: any lone ' ' is a fixup
    assert backend._wide_lead == set()
    backend.draw_text(3, 0, "over")  # no orphan work to do
    assert _single_space_cols(backend, 0) == []


def test_clear_resets_wide_tracking(backend):
    backend.draw_text(0, 0, "漢字")
    assert backend._wide_lead
    backend.clear()
    assert backend._wide_lead == set()


def test_shadow_over_a_wide_half_replaces_the_glyph(backend):
    # 漢 occupies cols 2,3. A drop shadow whose bottom row lands on col 3 (the
    # trail only) must replace the whole glyph with background spaces, then
    # darken its covered cell — never split-darken a half-glyph.
    backend.draw_text(0, 0, "AB漢CD")
    # shadow_rect bottom row sits at y+h; with y=-1,h=1 it lands on row 0, and
    # x=2,w=1 makes its cols span exactly col 3 (the glyph's trail).
    backend.shadow_rect(2, -1, 1, 1)
    spaces = _single_space_cols(backend, 0)
    assert 2 in spaces and 3 in spaces           # whole glyph blanked
    assert any(c[0] == "chgat" and c[1] == 0 and c[2] == 3
               for c in backend._stdscr.calls)   # covered cell still darkened
