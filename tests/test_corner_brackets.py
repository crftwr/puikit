"""``DrawContext.draw_corner_brackets`` — the tactical-HUD region frame.

Corners only, edges left open, resolved by capability: box-drawing glyphs on a
character grid, hairline strokes on a vector backend. The assertions that matter
are the containment ones — a frame that bleeds outside the rect it was given
would overdraw a neighbouring widget.
"""

import pytest

from puikit import PROFILE_GUI_DESKTOP, PROFILE_TUI, Panel, Style
from puikit.capability import CapabilityProfile
from puikit.backends.memory_backend import MemoryBackend
from puikit.widgets.base import Widget

W, H = 20, 8
INK = (130, 205, 255)


class _Framed(Widget):
    def __init__(self, arm=2.0, thickness=1.0, w=None, h=None):
        self.arm, self.thickness = arm, thickness
        self.w, self.h = w, h

    def draw(self, ctx):
        ctx.draw_corner_brackets(
            self.w if self.w is not None else ctx.size_units[0],
            self.h if self.h is not None else ctx.size_units[1],
            Style(fg=INK), arm=self.arm, thickness=self.thickness,
        )


class _VectorBackend(MemoryBackend):
    """MemoryBackend masks vector_shapes off (it is a character grid); a test
    that needs the vector path re-enables it, as tests/test_menu.py does for
    native_menus."""

    @property
    def capabilities(self):
        return CapabilityProfile({**PROFILE_GUI_DESKTOP, "native_menus": False})

    @property
    def base_size(self):
        return (8, 16)


def _grid(widget, w=W, h=H):
    be = MemoryBackend(width=w, height=h, capabilities=PROFILE_TUI)
    panel = Panel(be)
    panel.add(widget, x=0, y=0, w=w, h=h)
    panel.render()
    return be.snapshot()


def _strokes(widget, w=W, h=H):
    be = _VectorBackend(width=w, height=h)
    calls = []
    orig = be.fill_rect
    be.fill_rect = lambda *a, **k: (calls.append(a), orig(*a, **k))[1]
    panel = Panel(be)
    panel.add(widget, x=0, y=0, w=w, h=h)
    panel.render()
    return calls


# --- grid ----------------------------------------------------------------------

class TestGrid:

    def test_marks_all_four_corners(self):
        lines = _grid(_Framed(arm=1))
        assert lines[0][0] == "┏"
        assert lines[0][W - 1] == "┓"
        assert lines[H - 1][0] == "┗"
        assert lines[H - 1][W - 1] == "┛"

    def test_leaves_the_edges_open(self):
        # The whole point of corners over a border: the midpoints stay blank.
        lines = _grid(_Framed(arm=2))
        assert lines[0][W // 2] == " "
        assert lines[H - 1][W // 2] == " "
        assert lines[H // 2][0] == " "

    def test_arm_extends_the_legs(self):
        short, long_ = _grid(_Framed(arm=1)), _grid(_Framed(arm=3))
        assert short[0].count("━") == 0
        assert long_[0].count("━") == 4  # two cells on each of the two top corners
        assert sum(l[0] == "┃" for l in long_) == 4

    def test_arms_never_meet_and_close_the_frame(self):
        # An arm longer than the region must clamp, or opposite legs join and the
        # frame becomes the solid box it exists to avoid.
        lines = _grid(_Framed(arm=99))
        assert " " in lines[0], "top edge closed into a solid border"
        assert any(l[0] == " " for l in lines), "left edge closed into a solid border"

    def test_stays_inside_the_region(self):
        lines = _grid(_Framed(arm=99), w=W, h=H)
        assert len(lines) == H and all(len(l) == W for l in lines)

    @pytest.mark.parametrize("w,h", [(1, 8), (8, 1), (1, 1), (0, 0)])
    def test_too_small_to_frame_draws_nothing(self, w, h):
        # Below 2x2 a "frame" is indistinguishable from noise, so it is skipped.
        lines = _grid(_Framed(w=w, h=h), w=max(w, 4), h=max(h, 4))
        assert all(c == " " for line in lines for c in line)


# --- vector --------------------------------------------------------------------

class TestVector:

    def test_draws_eight_strokes(self):
        # Four corners, two legs each.
        assert len(_strokes(_Framed(arm=3))) == 8

    def test_uses_strokes_not_glyphs(self):
        be = _VectorBackend(width=W, height=H)
        panel = Panel(be)
        panel.add(_Framed(arm=3), x=0, y=0, w=W, h=H)
        panel.render()
        assert all(c == " " for line in be.snapshot() for c in line)

    def test_every_stroke_stays_inside_the_region(self):
        for x, y, w, h, _style in _strokes(_Framed(arm=3)):
            assert x >= -1e-9 and y >= -1e-9
            assert x + w <= W + 1e-9, f"stroke spills past the right edge: {x}+{w}"
            assert y + h <= H + 1e-9, f"stroke spills past the bottom edge: {y}+{h}"

    def test_strokes_are_sub_unit_hairlines(self):
        # A frame that cost a whole base unit would disturb the layout it frames.
        for _x, _y, w, h, _style in _strokes(_Framed(arm=3, thickness=1.0)):
            assert min(w, h) < 1.0

    def test_thickness_scales_the_stroke(self):
        thin = min(min(w, h) for *_xy, w, h, _s in
                   [(x, y, w, h, s) for x, y, w, h, s in _strokes(_Framed(thickness=1.0))])
        thick = min(min(w, h) for *_xy, w, h, _s in
                    [(x, y, w, h, s) for x, y, w, h, s in _strokes(_Framed(thickness=4.0))])
        assert thick > thin

    def test_arms_never_meet_and_close_the_frame(self):
        # Same guarantee the grid path makes: a leg reaching the midpoint would
        # meet the one coming back and close the frame into a solid border, so at
        # least one base unit of gap must survive even at an absurd arm length.
        for _x, _y, w, h, _style in _strokes(_Framed(arm=99)):
            assert w <= (W - 1) / 2 + 1e-9, "horizontal arms meet"
            assert h <= (H - 1) / 2 + 1e-9, "vertical arms meet"
