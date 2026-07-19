"""Color mapping for the curses backend (no terminal needed)."""

import pytest

curses = pytest.importorskip("curses")

from puikit.backends.curses_backend import CursesBackend, _TUI_PALETTE  # noqa: E402
from puikit.theme import THEME_TUI  # noqa: E402


def test_curated_palette_includes_theme_surface_colors_exactly():
    # The built-in theme's surface colors must be present verbatim so the
    # default chrome renders without quantization drift.
    backend = CursesBackend()
    for role in ("content", "sidebar", "header", "status"):
        color = THEME_TUI.surface_bg(role)
        assert _TUI_PALETTE[backend._quantize(color)] == color


def test_curated_palette_keeps_surface_roles_distinct():
    # The theme separates regions with contrasting backgrounds on TUI; the
    # curated palette must not collapse adjacent roles onto one color.
    backend = CursesBackend()
    idxs = [backend._quantize(THEME_TUI.surface_bg(r))
            for r in ("content", "sidebar", "header", "status")]
    assert len(set(idxs)) == 4


def test_quantize_snaps_arbitrary_color_and_caches():
    backend = CursesBackend()
    idx = backend._quantize((7, 249, 9))
    assert 0 <= idx < len(_TUI_PALETTE)
    assert (7, 249, 9) in backend._quant_cache
    assert backend._quantize((7, 249, 9)) == idx  # served from cache


def test_term_index_maps_directly_before_open():
    # With no palette bound yet (open() not run), colors map straight to the
    # terminal so the backend still works in isolation.
    backend = CursesBackend()
    assert backend._palette_term == []
    assert backend._term_index((255, 0, 0)) == CursesBackend._nearest_color((255, 0, 0))


def test_bind_palette_redefines_slots_on_ccc_terminal(monkeypatch):
    # On a can-change-color terminal we must NOT trust the existing palette
    # (a ccc terminal owns indices >= 16, e.g. macOS Terminal.app does not hold
    # the standard xterm cube there). Each curated color is written to its own
    # slot above the 16 ANSI colors, so rendering is exact. The whole palette
    # ships as ONE OSC-4 escape (not one init_color per color) so a terminal
    # that re-renders on palette changes (iTerm2) pays a single invalidation.
    import io

    monkeypatch.setattr(curses, "can_change_color", lambda: True)
    monkeypatch.setattr(curses, "COLORS", 256, raising=False)
    # A stray init_color would defeat the batching — make it fail loudly.
    monkeypatch.setattr(
        curses, "init_color",
        lambda *a: pytest.fail("init_color must not be called"),
    )

    backend = CursesBackend()
    out = io.StringIO()
    backend._raw_out = out
    backend._bind_palette()

    assert backend._palette_term == list(range(16, 16 + len(_TUI_PALETTE)))
    written = out.getvalue()
    # Exactly one OSC-4 sequence: ESC ] 4 ; <index;rgb pairs> ESC \
    assert written.startswith("\x1b]4;")
    assert written.endswith("\x1b\\")
    assert written.count("\x1b]4;") == 1
    assert written.count(";rgb:") == len(_TUI_PALETTE)
    # Slot 0's color appears at index 16, hex straight from its 0-255 channels.
    r, g, b = _TUI_PALETTE[0]
    assert f"16;rgb:{r:02x}/{g:02x}/{b:02x}" in written


def test_bind_palette_falls_back_to_existing_without_ccc(monkeypatch):
    # Terminals that cannot redefine colors map onto the existing palette.
    monkeypatch.setattr(curses, "can_change_color", lambda: False)
    monkeypatch.setattr(curses, "COLORS", 256, raising=False)

    backend = CursesBackend()
    backend._bind_palette()

    assert backend._palette_term == [CursesBackend._nearest_color(c) for c in _TUI_PALETTE]


def test_bind_palette_falls_back_when_too_few_slots(monkeypatch):
    # Not enough slots to hold the curated palette -> map onto what exists.
    monkeypatch.setattr(curses, "can_change_color", lambda: True)
    monkeypatch.setattr(curses, "COLORS", 64, raising=False)

    backend = CursesBackend()
    backend._bind_palette()

    assert backend._palette_term == [CursesBackend._nearest_color(c) for c in _TUI_PALETTE]


def test_color_pair_falls_back_to_nearest_when_exhausted(monkeypatch):
    # Regression: on a pair-heavy screen (the demo's 400-swatch hue table plus a
    # dialog + dim) curses' COLOR_PAIRS limit is reached. The allocator used to
    # return pair 0 (the terminal's fixed white-on-black default), which punched
    # undimmed blocks through the dimmed page. It now degrades to the nearest
    # already-allocated pair so a dimmed cell still reads as a near color.
    monkeypatch.setattr(curses, "init_pair", lambda *a: None)
    monkeypatch.setattr(curses, "COLOR_PAIRS", 4, raising=False)
    backend = CursesBackend()
    # Fill the (tiny) pair table: pairs 1..3 for three distinct dark grays.
    near_dark = backend._color_pair((20, 20, 20), (30, 30, 38))
    mid = backend._color_pair((120, 120, 120), (140, 140, 140))
    light = backend._color_pair((240, 240, 240), (246, 246, 246))
    assert len({near_dark, mid, light}) == 3
    assert backend._next_pair_id >= 4  # exhausted
    # A new request now reuses the closest existing pair, never 0.
    pair = backend._color_pair((22, 22, 22), (31, 31, 40))
    assert pair == near_dark
    assert pair != 0
    # And the resolution is memoized (no further growth).
    before = dict(backend._color_pairs)
    assert backend._color_pair((22, 22, 22), (31, 31, 40)) == near_dark
    assert backend._color_pairs == before


def test_pair_capacity_capped_at_legacy_limit_despite_advertised(monkeypatch):
    # macOS Terminal.app advertises COLOR_PAIRS=32767, but curses.color_pair(n)
    # packs n into the 8-bit A_COLOR field, so only 256 pairs are actually usable
    # — past that, cells render wrong colors. The allocator must cap at 256
    # regardless of the advertised (inflated) ceiling, and switch to graceful
    # fallback there, which is exactly what broke when a dialog's dim/fade pushed
    # the pair count past 256.
    monkeypatch.setattr(curses, "init_pair", lambda *a: None)
    monkeypatch.setattr(curses, "COLOR_PAIRS", 32767, raising=False)
    backend = CursesBackend()
    assert backend._pair_capacity() == 256  # capped, not 32767

    # Simulate a screen that has already allocated every usable pair (1..255):
    # two real pairs recorded for the nearest-fallback to pick from, and the
    # allocator advanced to the cap.
    backend._pair_rgb = {1: ((20, 20, 20), (30, 30, 38)), 2: ((230, 230, 230), (0, 0, 0))}
    backend._next_pair_id = 256

    # The next distinct (fg, bg) must NOT allocate pair 256 (curses.color_pair
    # cannot address it — that overflow is what corrupted colors when a dialog's
    # dim/fade pushed the count past 256). It degrades to the nearest in-range
    # pair and counts the overflow.
    pair = backend._color_pair((22, 22, 22), (31, 31, 40))
    assert pair == 1  # nearest of the two recorded pairs (the dark one)
    assert 0 < pair < 256
    assert backend._next_pair_id == 256  # never advanced past the cap
    assert backend.color_pair_stats() == (255, 256, 1)


def test_recolored_pair_forces_full_repaint(monkeypatch):
    # Per-frame pair recycling can give a pair NUMBER a new color between frames.
    # curses' diff refresh would leave a cell that kept the same (glyph, pair#)
    # showing the stale color (a lone out-of-place cell in a gradient), so
    # present() forces a full repaint (redrawwin) whenever any pair's color
    # changed since the previous frame — and skips it for static content so the
    # cheap diff path is kept.
    class _Scr:
        def __init__(self):
            self.calls = []

        def getmaxyx(self):
            return (12, 40)

        def erase(self):
            pass

        def refresh(self):
            self.calls.append("refresh")

        def redrawwin(self):
            self.calls.append("redrawwin")

        def move(self, *a):
            pass

    monkeypatch.setattr(curses, "init_pair", lambda *a: None)
    monkeypatch.setattr(curses, "curs_set", lambda *a: None, raising=False)
    backend = CursesBackend()
    backend._stdscr = _Scr()

    # Frame 1: first paint (prev map empty) -> repaint, as expected.
    backend.clear()
    backend._color_pair((10, 20, 30), (40, 50, 60))
    backend.present()
    assert "redrawwin" in backend._stdscr.calls

    # Frame 2: identical content -> pair #1 keeps its color -> no forced repaint.
    backend._stdscr.calls.clear()
    backend.clear()
    backend._color_pair((10, 20, 30), (40, 50, 60))
    backend.present()
    assert "redrawwin" not in backend._stdscr.calls

    # Frame 3: pair #1 is now a different color -> forced repaint.
    backend._stdscr.calls.clear()
    backend.clear()
    backend._color_pair((200, 100, 0), (0, 0, 0))
    backend.present()
    assert "redrawwin" in backend._stdscr.calls


def test_xterm256_grayscale_ramp_distinguishes_dark_panes():
    # Two subtly different dark grays must land on different palette slots,
    # otherwise pane backgrounds are indistinguishable on TUI.
    a = CursesBackend._xterm256_index((26, 28, 34))
    b = CursesBackend._xterm256_index((52, 62, 88))
    assert a != b


def test_xterm256_extremes_and_colors():
    assert CursesBackend._xterm256_index((0, 0, 0)) == 16
    assert CursesBackend._xterm256_index((255, 255, 255)) == 231
    # Pure red lands in the color cube's red corner.
    assert CursesBackend._xterm256_index((255, 0, 0)) == 16 + 36 * 5
    indexes = [
        CursesBackend._xterm256_index(rgb)
        for rgb in [(36, 114, 200), (13, 188, 121), (229, 229, 16)]
    ]
    assert all(16 <= i <= 231 for i in indexes)
    assert len(set(indexes)) == 3
