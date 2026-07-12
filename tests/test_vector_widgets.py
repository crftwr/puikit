"""The modern, vector control faces.

On a character grid the controls fall back to box-drawing + ASCII marks (covered
by test_input_widgets, run against the TUI and GUI profiles alike). Here we
exercise the *vector* path: a backend that declares ``vector_shapes`` receives
rounded-rect / check primitives instead of "[x]" / "(•)" text marks, and the
Panel layer is the only place that branch lives.
"""

import pytest

from puikit import CapabilityProfile, Event, EventType, PROFILE_GUI_DESKTOP, Panel
from puikit.backends.memory_backend import MemoryBackend
from puikit.widgets import (
    Button, Checkbox, DropDown, RadioGroup, TextEdit, TreeNode, TreeView,
)


class _VectorBackend(MemoryBackend):
    """A grid backend that *claims* vector_shapes and records the vector
    primitives, so the Panel's vector path can be tested headlessly. (The real
    MemoryBackend masks the capability off because it cannot render vectors.)"""

    @property
    def capabilities(self) -> CapabilityProfile:
        return CapabilityProfile({**self._capabilities, "vector_shapes": True})


@pytest.fixture
def backend():
    return _VectorBackend(width=30, height=12, capabilities=PROFILE_GUI_DESKTOP)


def _snapshot_has_ascii_marks(backend) -> bool:
    text = "\n".join(backend.snapshot())
    return any(m in text for m in ("[x]", "[ ]", "(•)", "( )"))


def test_checkbox_draws_vector_box_not_ascii(backend):
    panel = Panel(backend)
    panel.add(Checkbox("Enable", checked=True), x=0, y=0, w=12, h=1)
    panel.render()
    # The rounded box was drawn, and a check was stroked because it is checked.
    assert backend.round_rect_calls, "checkbox should draw a rounded mark box"
    assert backend.check_calls, "a checked box should stroke a check mark"
    # No ASCII fallback mark leaked onto the grid; the label still renders.
    assert not _snapshot_has_ascii_marks(backend)
    assert backend.snapshot()[0].lstrip().startswith("Enable") or "Enable" in backend.snapshot()[0]


def test_unchecked_checkbox_has_box_but_no_check(backend):
    panel = Panel(backend)
    panel.add(Checkbox("off", checked=False), x=0, y=0, w=12, h=1)
    panel.render()
    assert backend.round_rect_calls
    assert backend.check_calls == []  # nothing to check


def test_checkbox_mark_box_is_pixel_square_and_neutral(backend):
    panel = Panel(backend)
    box = Checkbox("x", checked=True)
    other = Checkbox("y")  # takes focus, so `box` renders unfocused
    panel.add(box, x=0, y=0, w=12, h=2)
    panel.add(other, x=0, y=2, w=12, h=2)
    panel.focus(other)
    panel.render()
    x, y, w, h, radius, style, hints = backend.round_rect_calls[0]
    assert hints.get("fill") is True
    # Accent is reserved for focus: an unfocused checked box is neutral
    # (control_bg fill, a neutral text-colored border emphasis) — not accent.
    assert style.bg == panel.theme.control_bg      # checked -> neutral fill
    assert style.fg == panel.theme.text            # checked + unfocused -> neutral border
    # Square in pixels: w*base_w ~= h*base_h.
    bw, bh = backend.base_size
    assert w * bw == pytest.approx(h * bh, rel=1e-6)


def test_checkbox_focus_recolors_box_without_halo(backend):
    panel = Panel(backend)
    box = Checkbox("x", checked=True)
    panel.add(box, x=0, y=0, w=12, h=2)
    panel.focus(box)
    panel.render()
    # One box only — focus recolors its border to the accent, no second ring.
    assert len(backend.round_rect_calls) == 1
    style = backend.round_rect_calls[0][5]
    assert style.bg == panel.theme.control_bg
    assert style.fg == panel.theme.accent          # focused -> blue border


def test_radio_selected_draws_circle_and_dot(backend):
    panel = Panel(backend)
    rg = RadioGroup(["a", "b"], selected=1)
    panel.add(rg, x=0, y=0, w=12, h=5)
    panel.focus(rg)
    panel.render()
    # Two rows -> two ring circles; the selected row adds an inner dot circle
    # (all fully rounded, radius None). No box around the group.
    circles = [c for c in backend.round_rect_calls if c[4] is None]
    assert len(circles) == 3
    dot = circles[-1]
    assert dot[5].bg == panel.theme.text            # the dot is neutral, not accent
    # Focus colors the selected circle's border accent; no radius-bearing ring.
    selected_circle = circles[1]
    assert selected_circle[5].fg == panel.theme.accent
    assert [c for c in backend.round_rect_calls if c[4] is not None] == []
    assert not _snapshot_has_ascii_marks(backend)


def test_radio_focus_colors_selected_circle_and_offsets_rows(backend):
    # Focus colors the selected circle blue (no box around the group); the rows
    # stay inset and the taller pitch is backed out again for hit-testing.
    panel = Panel(backend)
    rg = RadioGroup(["a", "b", "c"], selected=1)
    panel.add(rg, x=0, y=0, w=12, h=8)
    panel.focus(rg)
    panel.render()
    assert rg._pad_y > 0                                   # rows inset from the top
    assert [c for c in backend.round_rect_calls if c[4] is not None] == []  # no ring
    circles = [c for c in backend.round_rect_calls if c[4] is None]
    accent = [c for c in circles if c[5].fg == panel.theme.accent]
    assert len(accent) == 1                                # only the selected circle
    # A click lands on the right row despite the inset and the taller row pitch.
    click_y = rg._pad_y + 2 * rg._pitch + 0.1
    panel.dispatch_event(Event(type=EventType.MOUSE_CLICK, x=1, y=click_y, button="left"))
    assert rg.selected == 2


def test_button_face_is_rounded(backend):
    panel = Panel(backend)
    panel.add(Button("OK"), x=0, y=0, w=10, h=1)
    panel.render()
    # The fill is a rounded rect with the button color; focused single-row
    # buttons keep the label underline (no box at height 1).
    assert backend.round_rect_calls
    fill = backend.round_rect_calls[0]
    assert fill[5].bg == panel.theme.button_bg
    assert fill[6].get("fill") is True


def test_textedit_field_is_rounded_with_accent_border_when_focused(backend):
    panel = Panel(backend)
    field = TextEdit("hi", width=12)
    panel.add(field, x=0, y=0, w=12, h=1)
    panel.render()
    # Fill first (background only), border stroked last so the text cannot
    # paint over it (full ring, not a partial line).
    fill = backend.round_rect_calls[0]
    assert fill[5].bg == panel.theme.control_bg
    assert fill[5].fg is None
    assert fill[6].get("fill") is True
    border = backend.round_rect_calls[-1]
    assert border[5].fg == panel.theme.accent  # focused border
    assert border[5].bg is None
    assert "fill" not in border[6]


def test_dropdown_field_is_rounded(backend):
    panel = Panel(backend)
    dd = DropDown(["Red", "Green"], width=12)
    panel.add(dd, x=0, y=0, w=12, h=1)
    panel.render()
    assert backend.round_rect_calls
    assert backend.round_rect_calls[0][6].get("fill") is True


def _mixed_tree():
    return [
        TreeNode("src", children=[TreeNode("main.py")], expanded=True),  # branch, open
        TreeNode("docs", children=[TreeNode("guide.md")]),              # branch, closed
        TreeNode("README"),                                            # leaf
    ]


def test_tree_branches_draw_vector_chevrons_not_glyphs(backend):
    panel = Panel(backend)
    panel.add(TreeView(_mixed_tree()), x=0, y=0, w=30, h=12)
    panel.render()
    # One chevron per visible branch (src, docs); the visible leaves
    # (main.py under expanded src, README) draw none.
    assert len(backend.chevron_calls) == 2
    expanded_flags = sorted(call[4] for call in backend.chevron_calls)
    assert expanded_flags == [False, True]  # docs collapsed, src expanded
    # The ▸/▾ glyphs are gone from the grid — the mark is a vector stroke now.
    text = "\n".join(backend.snapshot())
    assert "▸" not in text and "▾" not in text
    assert "src" in text and "README" in text  # labels still render


def test_tree_leaf_only_draws_no_chevron(backend):
    panel = Panel(backend)
    panel.add(TreeView([TreeNode("a"), TreeNode("b")]), x=0, y=0, w=30, h=12)
    panel.render()
    assert backend.chevron_calls == []
