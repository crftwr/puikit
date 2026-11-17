"""The window is cleared to the theme's page, not a fixed dark color: a
crossfade shows what is under both screens, and on a light theme a dark clear
read as the screen dimming (xefm#900)."""

from puikit import PROFILE_GUI_DESKTOP, Panel
from puikit.backends.memory_backend import MemoryBackend
from puikit.theme import derive_theme
from puikit.widgets import Label


def _light():
    return derive_theme(background=(250, 250, 248), foreground=(30, 30, 30),
                        muted=(120, 120, 120), accent=(0, 102, 204),
                        surface=(236, 236, 232), selection=(200, 220, 255))


def test_each_render_clears_to_the_themes_page():
    backend = MemoryBackend(width=20, height=5, capabilities=PROFILE_GUI_DESKTOP)
    panel = Panel(backend, theme=_light())
    panel.add(Label("x"), x=0, y=0, w=2, h=1)
    panel.render()
    assert backend.clear_color == panel.theme.surface_bg("content") == (250, 250, 248)


def test_a_theme_switch_moves_the_clear_with_it():
    backend = MemoryBackend(width=20, height=5, capabilities=PROFILE_GUI_DESKTOP)
    panel = Panel(backend)
    panel.render()
    dark = backend.clear_color
    panel.theme = _light()
    panel.render()
    assert backend.clear_color == (250, 250, 248) != dark
