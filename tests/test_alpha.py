"""RGBA color and image-alpha tests.

The same RGBA intent renders two ways: a transparency-capable backend (GUI)
receives the 4-tuple and composites it per pixel; a backend without the
capability (TUI) has the Panel layer flatten it over the pane background to an
opaque RGB before it ever reaches the backend."""

import pytest

from puikit import Panel, PROFILE_GUI_DESKTOP, PROFILE_TUI, Style
from puikit.backend import TRANSPARENT, is_transparent
from puikit.backends.memory_backend import MemoryBackend
from puikit.panel import _composite
from puikit.widgets import ImageView, Widget


# --- the composite helper ---------------------------------------------------


def test_composite_passes_opaque_and_none_through():
    assert _composite(None, (0, 0, 0)) is None
    assert _composite((10, 20, 30), (0, 0, 0)) == (10, 20, 30)


def test_composite_blends_rgba_over_base():
    # 50%-ish alpha (128/255) of white over black -> mid gray.
    assert _composite((255, 255, 255, 128), (0, 0, 0)) == (128, 128, 128)
    # Fully transparent -> the base shows; fully opaque -> the color shows.
    assert _composite((255, 0, 0, 0), (10, 20, 30)) == (10, 20, 30)
    assert _composite((255, 0, 0, 255), (10, 20, 30)) == (255, 0, 0)


def test_composite_without_base_drops_alpha():
    assert _composite((255, 0, 0, 128), None) == (255, 0, 0)


# --- RGBA fills resolved per backend ----------------------------------------


class _Fill(Widget):
    def __init__(self, color):
        self.color = color

    def draw(self, ctx):
        wu, hu = ctx.size_units
        ctx.fill_rect(0, 0, wu, hu, Style(bg=self.color))


@pytest.mark.parametrize(
    "profile,expected",
    [
        (PROFILE_TUI, (128, 128, 128)),          # flattened over the pane bg
        (PROFILE_GUI_DESKTOP, (255, 255, 255, 128)),  # passed through unchanged
    ],
    ids=["tui-flattens", "gui-passes-through"],
)
def test_rgba_fill_resolves_per_capability(profile, expected):
    backend = MemoryBackend(width=6, height=3, capabilities=profile)
    panel = Panel(backend)
    panel.add(_Fill((255, 255, 255, 128)), x=0, y=0, w=6, h=3, hints={"bg": (0, 0, 0)})
    panel.render()
    assert backend.style_at(0, 0).bg == expected


def test_rgba_fill_flattens_over_pane_background_color():
    # A translucent red over a blue pane flattens toward purple on TUI.
    backend = MemoryBackend(width=4, height=2, capabilities=PROFILE_TUI)
    panel = Panel(backend)
    panel.add(_Fill((255, 0, 0, 128)), x=0, y=0, w=4, h=2, hints={"bg": (0, 0, 200)})
    panel.render()
    r, g, b = backend.style_at(0, 0).bg
    assert r > 120 and b > 90 and g == 0  # blended, not pure red or pure blue


# --- transparent (no-fill) background ---------------------------------------


def test_is_transparent_only_true_for_alpha_zero_rgba():
    assert is_transparent(TRANSPARENT)
    assert is_transparent((10, 20, 30, 0))
    assert not is_transparent(None)          # inherit the pane bg, not transparent
    assert not is_transparent((10, 20, 30))  # opaque RGB
    assert not is_transparent((10, 20, 30, 255))
    assert not is_transparent((10, 20, 30, 1))


class _Text(Widget):
    def __init__(self, bg, fill=None):
        self.bg = bg
        self.fill = fill

    def draw(self, ctx):
        if self.fill is not None:
            ctx.fill_rect(0, 0, 4, 1, Style(bg=self.fill))
        ctx.draw_text(0, 0, "x", Style(fg=(255, 255, 255), bg=self.bg))


@pytest.mark.parametrize(
    "profile,expected",
    [
        # TUI cannot composite: a transparent text bg flattens to the *pane*
        # background (fully-transparent over base == base), ignoring the red
        # fill just painted into the cell — so the cell reads the pane colour.
        (PROFILE_TUI, (0, 0, 40)),
        # A transparency-capable backend keeps the alpha-0 bg, so the glyph
        # paints no background and the red fill beneath it shows through — the
        # cell reads the fill, not the pane colour. (This is what lets a list row
        # fill its selection once and draw glyphs transparently on top.)
        (PROFILE_GUI_DESKTOP, (200, 0, 0)),
    ],
    ids=["tui-flattens-to-pane-bg", "gui-keeps-transparent"],
)
def test_transparent_text_bg_resolves_per_capability(profile, expected):
    backend = MemoryBackend(width=4, height=2, capabilities=profile)
    panel = Panel(backend)
    panel.add(_Text(TRANSPARENT, fill=(200, 0, 0)), x=0, y=0, w=4, h=2, hints={"bg": (0, 0, 40)})
    panel.render()
    assert backend.style_at(0, 0).bg == expected


def test_none_text_bg_reads_as_pane_background():
    # A None bg (the default) still reads as the pane background: on a grid it
    # inherits it opaquely; on a compositing backend the glyphs draw
    # transparently over the pane's already-painted fill, which is the same
    # colour — so the cell shows the pane colour either way.
    backend = MemoryBackend(width=4, height=2, capabilities=PROFILE_GUI_DESKTOP)
    panel = Panel(backend)
    panel.add(_Text(None), x=0, y=0, w=4, h=2, hints={"bg": (0, 0, 40)})
    panel.render()
    assert backend.style_at(0, 0).bg == (0, 0, 40)


@pytest.mark.parametrize(
    "profile,expected",
    [
        # Compositing backend: a default (None) text bg equals the pane's
        # already-painted background, so the glyphs draw transparently over it
        # instead of re-filling — a red fill drawn just beneath shows through,
        # proving there is no second fill (which would double-blend under a fade
        # to ~0.75 and clip a taller font's descender).
        (PROFILE_GUI_DESKTOP, (200, 0, 0)),
        # Grid backend cannot composite, so the inherited pane colour is filled
        # into the cell, covering the red.
        (PROFILE_TUI, (0, 0, 40)),
    ],
    ids=["gui-composites-over-fill", "grid-refills"],
)
def test_default_bg_text_does_not_refill_pane_background(profile, expected):
    backend = MemoryBackend(width=4, height=2, capabilities=profile)
    panel = Panel(backend)
    panel.add(_Text(None, fill=(200, 0, 0)), x=0, y=0, w=4, h=2, hints={"bg": (0, 0, 40)})
    panel.render()
    assert backend.style_at(0, 0).bg == expected


# --- image global opacity ---------------------------------------------------


def test_imageview_alpha_hint_flows_to_backend():
    backend = MemoryBackend(width=8, height=4, capabilities=PROFILE_GUI_DESKTOP)
    panel = Panel(backend)
    panel.add(ImageView("p.png", alpha=0.4), x=0, y=0, w=8, h=4)
    panel.render()
    assert backend.image_calls[0][3]["alpha"] == pytest.approx(0.4)


def test_imageview_alpha_defaults_to_opaque():
    backend = MemoryBackend(width=8, height=4, capabilities=PROFILE_GUI_DESKTOP)
    panel = Panel(backend)
    panel.add(ImageView("p.png"), x=0, y=0, w=8, h=4)
    panel.render()
    assert backend.image_calls[0][3]["alpha"] == 1.0
