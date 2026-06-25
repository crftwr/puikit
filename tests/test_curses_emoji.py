"""Color emoji are rendered independently of their row on the curses backend.

A terminal advances a color emoji by its own width table's cell count, which
disagrees with our two-column count for emoji that table predates (e.g.
U+1FAF3). Drawn inline that mismatch drifts the rest of the row; the backend
instead defers each emoji to an isolated overlay refresh in present(). These
tests pin that contract without needing a real terminal.
"""

import pytest

curses = pytest.importorskip("curses")

from puikit.backends.curses_backend import CursesBackend  # noqa: E402
from puikit.text import is_emoji_glyph  # noqa: E402


class _FakeStdscr:
    """Records the calls draw_text / present make, in order."""

    def __init__(self, w=40, h=12):
        self._w, self._h = w, h
        self.calls = []  # (op, *args)

    def getmaxyx(self):
        return (self._h, self._w)

    def erase(self):
        self.calls.append(("erase",))

    def addstr(self, y, x, text, attr=0):
        self.calls.append(("addstr", y, x, text))

    def move(self, y, x):
        self.calls.append(("move", y, x))

    def refresh(self):
        self.calls.append(("refresh",))

    def redrawwin(self):
        self.calls.append(("redrawwin",))


def _make_backend():
    backend = CursesBackend()
    backend._stdscr = _FakeStdscr()
    # Color attrs need an initscr'd terminal; the deferral logic doesn't depend
    # on the attr value, so neutralize it for the test.
    backend._to_curses_attr = lambda style: 0
    return backend


def test_emoji_deferred_not_drawn_inline():
    backend = _make_backend()
    backend.clear()
    backend.draw_text(0, 0, "\U0001FAF3 Drag")  # palm-down-hand + text

    texts = [c[3] for c in backend._stdscr.calls if c[0] == "addstr"]
    # The emoji glyph is never written inline...
    assert "\U0001FAF3" not in texts
    # ...but the text after it still goes down (each glyph placed individually).
    assert {"D", "r", "a", "g"} <= set(texts)
    # The emoji is queued for the overlay pass at the column puikit assigned it.
    assert backend._deferred_emoji == {(0, 0): ("\U0001FAF3", 0)}


def test_present_overlays_emoji_in_a_second_refresh():
    backend = _make_backend()
    backend.clear()
    backend.draw_text(0, 0, "\U0001FAF3 Drag")
    backend.present()

    calls = backend._stdscr.calls
    refreshes = [i for i, c in enumerate(calls) if c[0] == "refresh"]
    assert len(refreshes) == 2, "text and emoji must commit in separate refreshes"

    first_refresh = refreshes[0]
    emoji_writes = [
        i for i, c in enumerate(calls)
        if c[0] == "addstr" and c[3] == "\U0001FAF3"
    ]
    assert emoji_writes, "emoji must be overlaid in present()"
    # Every emoji overlay lands strictly after the first (text) refresh, so the
    # terminal's emoji advance has nothing after it to push out of column.
    assert all(i > first_refresh for i in emoji_writes)


def test_no_emoji_means_single_refresh():
    backend = _make_backend()
    backend.clear()
    backend.draw_text(0, 0, "plain text")
    backend.present()

    refreshes = [c for c in backend._stdscr.calls if c[0] == "refresh"]
    assert len(refreshes) == 1
    assert backend._deferred_emoji == {}


def test_clear_resets_deferred_emoji():
    backend = _make_backend()
    backend.clear()
    backend.draw_text(0, 0, "\U0001FAF3")
    assert backend._deferred_emoji
    backend.clear()
    assert backend._deferred_emoji == {}


def test_later_draw_over_emoji_evicts_it():
    # An opaque layer above (a Drawer fill, a dialog) covers the cell a lower
    # layer deferred an emoji to. The covering draw must evict it, or present()'s
    # overlay pass would paint the emoji back on top of the layer.
    backend = _make_backend()
    backend.clear()
    backend.draw_text(0, 0, "\U0001FAF3 Drag")        # nav row, emoji at (0, 0)
    assert (0, 0) in backend._deferred_emoji
    backend.fill_rect(0, 0, 10, 1)                     # a layer fills over it
    assert (0, 0) not in backend._deferred_emoji

    backend.present()
    overlaid = [c for c in backend._stdscr.calls
                if c[0] == "addstr" and c[3] == "\U0001FAF3"]
    assert not overlaid, "an occluded emoji must not be overlaid in present()"


def test_emoji_outside_the_covering_rect_survives():
    backend = _make_backend()
    backend.clear()
    backend.draw_text(0, 0, "\U0001FAF3 A")            # emoji at (0, 0)
    backend.draw_text(0, 1, "\U0001F6AA B")            # emoji at (1, 0)
    backend.fill_rect(0, 0, 10, 1)                     # covers row 0 only
    assert (0, 0) not in backend._deferred_emoji
    assert (1, 0) in backend._deferred_emoji           # row 1 untouched


def test_classifier_excludes_cjk_text():
    # CJK wide text never drifts (all terminals count it two columns), so it must
    # not be deferred — only color emoji are.
    assert is_emoji_glyph("\U0001FAF3")
    assert not is_emoji_glyph("漢")  # 漢
    assert not is_emoji_glyph(chr(0x20000))  # CJK Extension B
