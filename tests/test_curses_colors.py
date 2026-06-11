"""Color mapping for the curses backend (no terminal needed)."""

import pytest

curses = pytest.importorskip("curses")

from puikit.backends.curses_backend import CursesBackend  # noqa: E402


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
