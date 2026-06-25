"""Label padding: pixels on a vector backend (sub-unit), whole cells on a grid.

The header / status bars use this to gain breathing room around their text; the
measured size grows so a content-sized Item reserves the padding (and the bar
gets taller on GUI).
"""

from puikit import PROFILE_TUI, Panel
from puikit.backends.memory_backend import MemoryBackend
from puikit.layout import LayoutContext
from puikit.widgets import Label


def test_padding_px_grows_size_on_pixel_backend_only():
    label = Label("hello", padding_px=4)  # 5 columns of text
    grid = LayoutContext(base_w=8, base_h=16, snap=True)
    pixel = LayoutContext(base_w=8, base_h=16, snap=False)

    # Grid: a device-pixel inset would cost whole cells, so it collapses.
    assert grid.measure_text("hello") == 5.0
    assert label.measure(grid, "x", 0).preferred == 5.0
    assert label.measure(grid, "y", 0).preferred == 1.0

    # Pixel: padding is a sub-unit fraction, added on both sides of both axes.
    assert label.measure(pixel, "x", 0).preferred == 5.0 + 2 * (4 / 8)
    assert label.measure(pixel, "y", 0).preferred == 1.0 + 2 * (4 / 16)


def test_padding_units_grows_size_everywhere():
    label = Label("hi", padding_units=2)
    grid = LayoutContext(base_w=1, base_h=1, snap=True)
    assert label.measure(grid, "x", 0).preferred == 2 + 2 * 2
    assert label.measure(grid, "y", 0).preferred == 1.0 + 2 * 2


def test_no_padding_keeps_exact_text_size():
    label = Label("hello")
    grid = LayoutContext(base_w=8, base_h=16, snap=False)
    assert label.measure(grid, "x", 0).preferred == 5.0
    assert label.measure(grid, "y", 0).preferred == 1.0


def test_padding_units_offsets_drawn_text():
    backend = MemoryBackend(width=10, height=4, capabilities=PROFILE_TUI)
    panel = Panel(backend)
    panel.add(Label("hi", padding_units=1), x=0, y=0, w=10, h=4)
    panel.render()
    rows = backend.snapshot()
    assert rows[0].strip() == ""          # one row of top padding
    assert rows[1][0] == " "              # one column of left padding
    assert rows[1][1:3] == "hi"
