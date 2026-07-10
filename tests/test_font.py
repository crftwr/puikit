"""Font system tests: the same Style folds to attributes on a backend without
`fonts` and survives intact on one with it (docs/font_system.md §6, §11)."""

from puikit import (
    Font,
    FontSlant,
    FontWeight,
    Panel,
    PROFILE_GUI_DESKTOP,
    PROFILE_TUI,
    Style,
    TextAttribute,
)
from puikit.backends.memory_backend import MemoryBackend
from puikit.widgets import Label


def _render(profile, style):
    backend = MemoryBackend(width=20, height=2, capabilities=profile)
    panel = Panel(backend)
    panel.add(Label("Ag", style), x=0, y=0, w=10, h=1)
    panel.render()
    return backend


def test_capability_flags_present():
    assert not PROFILE_TUI.supports("fonts")
    assert not PROFILE_TUI.supports("proportional_text")
    assert PROFILE_GUI_DESKTOP.supports("fonts")
    assert PROFILE_GUI_DESKTOP.supports("proportional_text")


def test_weight_folds_to_bold_on_tui():
    backend = _render(PROFILE_TUI, Style(font=Font(weight=FontWeight.SEMI_BOLD)))
    style = backend.style_at(0, 0)
    assert style.attr & TextAttribute.BOLD
    assert style.font is None  # face/size dropped where they cannot exist


def test_light_weight_does_not_fold_to_bold_on_tui():
    backend = _render(PROFILE_TUI, Style(font=Font(weight=FontWeight.LIGHT)))
    style = backend.style_at(0, 0)
    assert not style.attr & TextAttribute.BOLD
    assert style.font is None


def test_italic_folds_to_italic_on_tui():
    backend = _render(PROFILE_TUI, Style(font=Font(slant=FontSlant.ITALIC)))
    style = backend.style_at(0, 0)
    assert style.attr & TextAttribute.ITALIC
    assert style.font is None


def test_face_and_size_are_dropped_on_tui():
    backend = _render(PROFILE_TUI, Style(font=Font(family="Georgia", size=28)))
    style = backend.style_at(0, 0)
    assert style.font is None
    assert style.attr == TextAttribute.NORMAL  # nothing to fold


def test_font_survives_on_fonts_capable_backend():
    font = Font(family="Georgia", size=18, weight=FontWeight.SEMI_BOLD)
    backend = _render(PROFILE_GUI_DESKTOP, Style(font=font))
    style = backend.style_at(0, 0)
    # The Panel does not fold on a `fonts` backend: the descriptor reaches it
    # intact, and weight is left for the backend (not turned into an attribute).
    assert style.font == font
    assert not style.attr & TextAttribute.BOLD


def test_measure_text_counts_columns_on_whole_unit_backend():
    backend = MemoryBackend(width=20, height=2, capabilities=PROFILE_TUI)
    assert backend.measure_text("hello") == 5.0
    # A font request does not change the column count on a whole-unit backend.
    assert backend.measure_text("hello", Style(font=Font(size=28))) == 5.0


def test_font_metrics_grid_default_on_whole_unit_backend():
    # The ABC default (whole-unit backends): one base unit of ascent, no
    # descent — so ascent+descent == 1.0 line, matching measure_line_height.
    backend = MemoryBackend(width=20, height=2, capabilities=PROFILE_TUI)
    fm = backend.font_metrics(Style())
    assert (fm.ascent, fm.descent) == (1.0, 0.0)
    assert fm.line_height == 1.0


def test_draw_text_baseline_default_delegates_to_draw_text():
    # The ABC default converts baseline_y to a top-of-box y (baseline - ascent)
    # then draws normally; on a whole-unit backend ascent is 1.0, so a baseline
    # at row 3 lands the text's box top on row 2.
    backend = MemoryBackend(width=20, height=4, capabilities=PROFILE_TUI)
    backend.clear()
    backend.draw_text_baseline(0, 3, "Ag")
    rows = backend.snapshot()
    assert rows[2].startswith("Ag")  # box top at baseline_y(3) - ascent(1.0)
    assert rows[3].strip() == ""
