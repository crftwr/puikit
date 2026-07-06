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


def test_frame_divider_matches_box_border_under_auto_ink():
    # A frame divider (a dialog's title-bar rule) is a structural line, not text:
    # with auto_ink on it must render the exact popup_border color, identical to
    # the box frame it joins — not get lifted to a text floor, which showed the
    # rule in a different color than the surrounding frame.
    from puikit.color import LC_BODY, apca_lc, legible_ink
    from puikit.theme import THEME_TUI

    border, bg = THEME_TUI.popup_border, THEME_TUI.popup_bg
    # Meaningful only if the border would otherwise be lifted (it's a dim line).
    assert abs(apca_lc(border, bg)) < LC_BODY
    assert legible_ink(border, bg, LC_BODY) != border

    class _Frame(Widget):
        def draw(self, ctx):
            ctx.draw_box(0, 0, ctx.width, ctx.height,
                         Style(bg=bg, fg=border), hints={"fill": True})
            ctx.draw_frame_divider(2, Style(fg=border, bg=bg))

    backend = MemoryBackend(width=12, height=5)
    panel = Panel(backend, theme=THEME_TUI)
    panel.auto_ink = True
    panel.set_layout(VSplit(Item(_Frame(), hints={"surface": "content"})))
    panel.render()

    box_line = backend.style_at(2, 0).fg     # a '─' in the box top border
    rule_line = backend.style_at(2, 2).fg    # a '─' in the frame divider
    assert box_line == border                # frame border not lifted
    assert rule_line == border               # divider not lifted (the fix)
    assert rule_line == box_line             # …so the two match
