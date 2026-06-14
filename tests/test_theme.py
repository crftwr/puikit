"""Region separation: surface roles and dividers resolved per backend.

The same layout must separate regions with background contrast on TUI
(no base units to spare for lines) and with hairlines on GUI (1 device pixel,
same backgrounds allowed) — without the app branching.
"""

import pytest

from puikit import DEFAULT_STYLE, HSplit, Item, Panel, VSplit
from puikit.backends.memory_backend import MemoryBackend
from puikit.capability import PROFILE_GUI_DESKTOP, PROFILE_TUI
from puikit.theme import THEME_GUI, THEME_TUI, theme_for
from puikit.widgets import Label


def test_theme_for_matches_separation_strategy():
    assert theme_for(PROFILE_TUI) is THEME_TUI
    assert theme_for(PROFILE_GUI_DESKTOP) is THEME_GUI


def test_gui_theme_shares_backgrounds_tui_theme_contrasts():
    # GUI separates with hairlines, so surfaces may share a background;
    # TUI has no hairlines, so the theme must provide the contrast.
    assert THEME_GUI.surface_bg("status") == THEME_GUI.surface_bg("content")
    assert THEME_TUI.surface_bg("status") != THEME_TUI.surface_bg("content")


def test_surface_roles_get_contrasting_backgrounds_on_tui():
    backend = MemoryBackend(width=20, height=10)
    panel = Panel(backend)
    panel.set_layout(
        VSplit(
            Item(Label("main"), hints={"surface": "content"}),
            Item(Label("ready"), size=1, hints={"surface": "status"}),
            divider="subtle",
        )
    )
    panel.render()
    content_bg = backend.style_at(10, 4).bg
    status_bg = backend.style_at(10, 9).bg
    assert content_bg == THEME_TUI.surface_bg("content")
    assert status_bg == THEME_TUI.surface_bg("status")
    assert content_bg != status_bg
    # The subtle divider reserved no row: main extends to the status bar.
    assert panel._children[0].rect.h == 9


def test_explicit_bg_hint_overrides_theme():
    backend = MemoryBackend(width=10, height=4)
    panel = Panel(backend)
    panel.set_layout(
        VSplit(Item(Label("x"), hints={"surface": "status", "bg": (1, 2, 3)}))
    )
    panel.render()
    assert backend.style_at(5, 2).bg == (1, 2, 3)


def test_strong_divider_draws_line_glyphs_on_tui():
    backend = MemoryBackend(width=21, height=6)
    panel = Panel(backend)
    panel.set_layout(HSplit(Label("L"), Label("R"), divider="strong"))
    panel.render()
    lines = backend.snapshot()
    assert all(line[10] == "│" for line in lines)
    assert backend.style_at(10, 0).fg == panel.theme.divider_color


class RecordingGuiBackend(MemoryBackend):
    """GUI-profile memory backend that records fill_rect calls, since a
    1-device-pixel hairline rounds away on the character grid."""

    def __init__(self, **kwargs):
        super().__init__(capabilities=PROFILE_GUI_DESKTOP, **kwargs)
        self.fill_calls = []

    @property
    def base_size(self):
        return (10, 20)

    def fill_rect(self, x, y, w, h, style=DEFAULT_STYLE):
        self.fill_calls.append((x, y, w, h, style))
        super().fill_rect(x, y, w, h, style)


def test_hairline_divider_drawn_as_one_pixel_fill_on_gui():
    backend = RecordingGuiBackend(width=10, height=6)
    panel = Panel(backend)
    panel.set_layout(HSplit(Label("L"), Label("R"), divider="subtle"))
    panel.render()
    hairlines = [
        call for call in backend.fill_calls if call[4].bg == THEME_GUI.divider_color
    ]
    assert len(hairlines) == 1
    x, y, w, h, _ = hairlines[0]
    assert w * 10 == pytest.approx(1)  # one device pixel wide
    assert h == pytest.approx(6.0)  # full height of the split
