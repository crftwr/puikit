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
    # slot above the 16 ANSI colors, so rendering is exact.
    calls = []
    monkeypatch.setattr(curses, "can_change_color", lambda: True)
    monkeypatch.setattr(curses, "COLORS", 256, raising=False)
    monkeypatch.setattr(curses, "init_color", lambda *a: calls.append(a))

    backend = CursesBackend()
    backend._bind_palette()

    assert backend._palette_term == list(range(16, 16 + len(_TUI_PALETTE)))
    assert len(calls) == len(_TUI_PALETTE)
    r, g, b = _TUI_PALETTE[0]
    assert calls[0] == (16, r * 1000 // 255, g * 1000 // 255, b * 1000 // 255)


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
