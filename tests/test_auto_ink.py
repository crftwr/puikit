"""Panel.auto_ink: the opt-in draw-time legibility guarantee.

Renders text through a real Panel and reads back the fg that reached the cells,
so these exercise the DrawContext._text_style seam end to end (off by default,
weight-aware target, floor-only, transparent-fill skip)."""

from puikit import Item, Panel, VSplit
from puikit.backend import Style, TextAttribute, TRANSPARENT
from puikit.backends.memory_backend import MemoryBackend
from puikit.capability import PROFILE_GUI_DESKTOP
from puikit.color import LC_BODY, LC_MIN_NONTEXT, apca_lc, legible_ink
from puikit.theme import THEME_TUI
from puikit.widgets.base import Widget

CONTENT = THEME_TUI.surfaces["content"]   # (30, 30, 38)
DIM_FG = (66, 72, 86)                      # deliberately low-contrast on CONTENT


class _Ink(Widget):
    def __init__(self, fg, attr=TextAttribute.NORMAL, bg=None, ink=True):
        self.fg, self.attr, self.bg, self.ink = fg, attr, bg, ink

    def draw(self, ctx):
        ctx.draw_text(0, 0, "hello", Style(fg=self.fg, attr=self.attr, bg=self.bg), ink=self.ink)


def _render(fg, attr=TextAttribute.NORMAL, auto=True, bg=None, caps=None, ink=True):
    backend = MemoryBackend(width=12, height=3, **({"capabilities": caps} if caps else {}))
    panel = Panel(backend, theme=THEME_TUI)
    panel.auto_ink = auto
    panel.set_layout(VSplit(Item(_Ink(fg, attr, bg, ink), hints={"surface": "content"})))
    panel.render()
    return backend.style_at(0, 0).fg


def test_off_by_default_leaves_color_exact():
    assert DIM_FG == _render(DIM_FG, auto=False)


def test_normal_text_lifted_to_body_floor():
    got = _render(DIM_FG)
    assert got == legible_ink(DIM_FG, CONTENT, LC_BODY)
    assert abs(apca_lc(got, CONTENT)) >= LC_BODY - 0.5
    assert got != DIM_FG


def test_dim_text_kept_dimmer_than_body():
    body = _render(DIM_FG, TextAttribute.NORMAL)
    dim = _render(DIM_FG, TextAttribute.DIM)
    assert abs(apca_lc(dim, CONTENT)) >= LC_MIN_NONTEXT - 0.5      # not invisible
    assert abs(apca_lc(dim, CONTENT)) < abs(apca_lc(body, CONTENT))  # still recedes


def test_already_legible_color_untouched():
    bright = (230, 230, 240)
    assert abs(apca_lc(bright, CONTENT)) >= LC_BODY
    assert bright == _render(bright)


def test_transparent_fill_skipped_on_compositing_backend():
    # Over a transparent fill (compositing backend), the glyphs land on whatever
    # the widget painted underneath, which owns the contrast — auto-ink stays out.
    got = _render(DIM_FG, bg=TRANSPARENT, caps=PROFILE_GUI_DESKTOP)
    assert got == DIM_FG


def test_per_draw_ink_false_opts_out():
    # A run drawn with ink=False keeps its exact color even while auto_ink is on
    # (syntax highlighting, color legends); ink=True on the same color is lifted.
    assert _render(DIM_FG, ink=False) == DIM_FG
    assert _render(DIM_FG, ink=True) != DIM_FG
