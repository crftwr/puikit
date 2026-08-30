"""The VT output engine's cell model and frame diff.

The pitch tests are the reason this backend is being written at all: under
curses on Windows a full-width glyph occupies one buffer cell while the terminal
advances two columns (three, once PDCurses' trailing space is counted), so every
glyph after it drifts and truncation runs against the wrong budget
(puikit#89, xefm#283). Here a glyph owns the columns it displays, so the
assertions below are about columns, not characters.
"""

import re

import pytest

from puikit.backends._vt import _TRAIL, VTGrid


def cup_positions(vt: str) -> list[tuple[int, int]]:
    """Every absolute cursor position in an emitted frame, as (row, col), 1-based
    exactly as the escape carries them."""
    return [(int(r), int(c)) for r, c in re.findall(r"\x1b\[(\d+);(\d+)H", vt)]


def visible(vt: str) -> str:
    """The frame's payload with all escapes stripped."""
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", vt)


# --- column pitch ---------------------------------------------------------


def test_wide_glyph_owns_two_columns():
    g = VTGrid(20, 3)
    g.draw_text(0, 0, "日本語")
    # Three glyphs, six columns: each lead carries the glyph, each trail is owned.
    assert g.cell_at(0, 0)[0] == "日"
    assert g.cell_at(1, 0) is _TRAIL
    assert g.cell_at(2, 0)[0] == "本"
    assert g.cell_at(3, 0) is _TRAIL
    assert g.cell_at(4, 0)[0] == "語"
    assert g.cell_at(5, 0) is _TRAIL
    # Column 6 is free — nothing was pushed sideways.
    assert g.cell_at(6, 0)[0] == " "


def test_text_after_cjk_lands_on_its_declared_column():
    # xefm#283 exactly: a name column of CJK followed by a size column. Under
    # curses on Windows the size drifted one column right per glyph.
    g = VTGrid(30, 3)
    g.draw_text(0, 0, "日本語")
    g.draw_text(6, 0, "1.2K")
    assert g.cell_at(6, 0)[0] == "1"
    assert "".join(c[0] for c in (g.cell_at(x, 0) for x in (6, 7, 8, 9))) == "1.2K"


def test_mixed_width_run_advances_by_display_width():
    g = VTGrid(20, 2)
    g.draw_text(0, 0, "a日b")
    assert g.cell_at(0, 0)[0] == "a"
    assert g.cell_at(1, 0)[0] == "日"
    assert g.cell_at(2, 0) is _TRAIL
    assert g.cell_at(3, 0)[0] == "b"


def test_emitted_frame_carries_no_padding_after_a_wide_glyph():
    # PDCurses sends a space after a full-width glyph, which is the third column
    # of drift. The engine must emit the glyph and nothing else.
    g = VTGrid(10, 1)
    g.draw_text(0, 0, "日")
    out = visible(g.render())
    assert out.startswith("日")
    # A 10-column row holding one wide glyph emits 9 characters: the glyph, then
    # the 8 blanks of columns 2..9. The trail column contributes nothing — the
    # terminal's own advance covers it. A tenth character would mean the trail
    # was padded, which is precisely the extra column that shifts the row.
    assert len(out) == 9


# --- orphan halves --------------------------------------------------------


def test_overwriting_a_trail_blanks_its_lead():
    # A later draw covering the right half of a wide glyph must not leave the
    # left half on screen as a broken half-glyph.
    g = VTGrid(10, 1)
    g.draw_text(0, 0, "日本")
    g.draw_text(1, 0, "x")  # lands on 日's trail
    assert g.cell_at(0, 0)[0] == " "  # lead cleaned up
    assert g.cell_at(1, 0)[0] == "x"


def test_overwriting_a_lead_blanks_its_trail():
    g = VTGrid(10, 1)
    g.draw_text(2, 0, "日")
    g.draw_text(2, 0, "x")  # lands on the lead
    assert g.cell_at(2, 0)[0] == "x"
    assert g.cell_at(3, 0)[0] == " "  # orphaned trail cleaned up
    assert g.cell_at(3, 0) is not _TRAIL


def test_wide_glyph_replacing_a_wide_glyph_leaves_no_orphan():
    g = VTGrid(10, 1)
    g.draw_text(0, 0, "日本")
    g.draw_text(1, 0, "語")  # straddles both halves
    assert g.cell_at(0, 0)[0] == " "
    assert g.cell_at(1, 0)[0] == "語"
    assert g.cell_at(2, 0) is _TRAIL
    assert g.cell_at(3, 0)[0] == " "


# --- clipping -------------------------------------------------------------


def test_wide_glyph_straddling_the_clip_edge_is_dropped():
    # Half a glyph past the edge would paint into the neighbouring pane.
    g = VTGrid(20, 2)
    g.push_clip(0, 0, 5, 1)
    g.draw_text(0, 0, "あいう")  # 3 wide glyphs = 6 columns, clip allows 5
    g.pop_clip()
    assert g.cell_at(4, 0)[0] == " "  # the third glyph did not half-land
    assert g.cell_at(5, 0)[0] == " "  # nothing past the clip


def test_clip_rejects_rows_outside():
    g = VTGrid(10, 5)
    g.push_clip(0, 1, 10, 2)
    g.draw_text(0, 0, "no")
    g.draw_text(0, 1, "yes")
    g.pop_clip()
    assert g.cell_at(0, 0)[0] == " "
    assert g.cell_at(0, 1)[0] == "y"


# --- the frame diff -------------------------------------------------------


def test_unchanged_frame_emits_nothing():
    g = VTGrid(20, 3)
    g.draw_text(0, 0, "hello")
    g.render()
    g.flip()
    g.clear()
    g.draw_text(0, 0, "hello")
    assert g.render() == ""


def test_only_changed_spans_are_addressed():
    g = VTGrid(20, 3)
    g.draw_text(0, 0, "hello")
    g.draw_text(0, 2, "world")
    g.render()
    g.flip()
    g.clear()
    g.draw_text(0, 0, "hello")
    g.draw_text(0, 2, "WORLD")
    out = g.render()
    # Row 3 (1-based) only.
    assert cup_positions(out) == [(3, 1)]
    assert "WORLD" in visible(out)


def test_a_changed_span_never_starts_mid_glyph():
    # If only the trail half of a wide glyph differs, the cursor must still be
    # placed on the lead — addressing the trail would write into the glyph.
    g = VTGrid(10, 1)
    g.draw_text(0, 0, "ab日")
    g.render()
    g.flip()
    g.clear()
    g.draw_text(0, 0, "ab本")
    out = g.render()
    row, col = cup_positions(out)[0]
    assert (row, col) == (1, 3)  # 1-based: the lead at index 2
    assert "本" in visible(out)


def test_resize_forces_a_full_repaint():
    g = VTGrid(10, 2)
    g.draw_text(0, 0, "hi")
    g.render()
    g.flip()
    assert g.resize(12, 3) is True
    g.draw_text(0, 0, "hi")
    out = g.render()
    assert cup_positions(out)  # everything is dirty again
    assert g.size == (12, 3)


# --- pen ------------------------------------------------------------------


def test_truecolor_is_emitted_as_authored():
    # The curses path snaps to the nearest of ~220 curated palette entries; here
    # the authored RGB goes out verbatim.
    g = VTGrid(10, 1)
    g.draw_text(0, 0, "x", fg=(17, 133, 200), bg=(9, 9, 9))
    out = g.render()
    assert "38;2;17;133;200" in out
    assert "48;2;9;9;9" in out


def test_pen_is_not_re_emitted_for_an_unchanged_style():
    g = VTGrid(10, 1)
    g.draw_text(0, 0, "abc", fg=(1, 2, 3))
    out = g.render()
    assert out.count("38;2;1;2;3") == 1


def test_attributes_map_to_sgr_codes():
    g = VTGrid(10, 1)
    g.draw_text(0, 0, "x", attr=1 | 2 | 4)  # BOLD | UNDERLINE | REVERSE
    out = g.render()
    params = re.search(r"\x1b\[([0-9;]*)m", out).group(1).split(";")
    assert {"1", "4", "7"} <= set(params)


def test_frame_ends_reset_so_nothing_leaks_into_the_shell():
    g = VTGrid(10, 1)
    g.draw_text(0, 0, "x", fg=(1, 2, 3))
    assert g.render().endswith("\x1b[0m")


@pytest.mark.parametrize("text", ["", " ", "​"])
def test_degenerate_text_is_harmless(text):
    g = VTGrid(5, 1)
    g.draw_text(0, 0, text)
    g.render()


# --- glyphs the terminal may measure differently ---------------------------


def test_cursor_is_restated_after_an_emoji():
    # Terminals disagree about emoji width. A base in the emoji planes is two
    # columns everywhere, but a legacy symbol promoted by a variation selector
    # (U+2328 KEYBOARD + U+FE0F) is two to this width model and ONE to several
    # terminals, VS Code's among them. Left alone the disagreement propagates:
    # every glyph after it lands a column off, and a later partial repaint —
    # which does address absolutely — overwrites a neighbour and doubles a
    # character. Re-stating the cursor confines a wrong guess to one cell.
    g = VTGrid(30, 1)
    g.draw_text(0, 0, "⌨️ Keys")
    positions = cup_positions(g.render())
    assert (1, 3) in positions, positions   # re-anchored where the space belongs


def test_cjk_is_not_re_anchored():
    # Every terminal counts East Asian Wide as two columns, so it cannot drift
    # and the extra escape would be pure overhead on the text needing it least.
    g = VTGrid(30, 1)
    g.draw_text(0, 0, "日本語 text")
    assert cup_positions(g.render()) == [(1, 1)]


def test_plain_text_is_not_re_anchored():
    g = VTGrid(30, 1)
    g.draw_text(0, 0, "plain ascii")
    assert cup_positions(g.render()) == [(1, 1)]


def test_re_anchoring_does_not_change_what_is_drawn():
    # The escape moves the cursor; it must not alter the glyphs or their columns.
    g = VTGrid(20, 1)
    g.draw_text(0, 0, "✂️ cut")
    assert g.cell_at(0, 0)[0] == "✂️"
    assert g.cell_at(1, 0) is _TRAIL
    assert g.cell_at(2, 0)[0] == " "
    assert g.cell_at(3, 0)[0] == "c"
    assert "cut" in visible(g.render())


def test_several_emoji_each_re_anchor():
    g = VTGrid(30, 1)
    g.draw_text(0, 0, "⌨️a✂️b")
    # One CUP to open the span, then one after each emoji.
    assert len(cup_positions(g.render())) == 3


# --- colored underlines ---------------------------------------------------

_UL = 2  # TextAttribute.UNDERLINE, as the grid takes it (a plain int)


def test_underline_color_goes_out_as_one_subparameter():
    # SGR 58 in the colon form. A terminal without colored underlines drops the
    # whole parameter and still draws the rule; the semicolon spelling would
    # leave it reading 2 (dim) and three stray color codes instead.
    g = VTGrid(10, 1)
    g.draw_text(0, 0, "ab", fg=(200, 200, 200), attr=_UL, ul=(231, 76, 76))
    vt = g.render()
    assert "58:2::231:76:76" in vt
    assert "58;2;" not in vt


def test_underline_color_rides_with_the_attributes_not_the_colors():
    # Two cells that differ only in the color of their rule: the pen has to be
    # re-established, and from a reset, the same as any other attribute change.
    g = VTGrid(10, 1)
    g.draw_text(0, 0, "a", fg=(10, 10, 10), attr=_UL, ul=(231, 76, 76))
    g.draw_text(1, 0, "b", fg=(10, 10, 10), attr=_UL, ul=(80, 80, 80))
    vt = g.render()
    assert "58:2::231:76:76" in vt and "58:2::80:80:80" in vt


def test_a_plain_cell_after_a_colored_underline_resets():
    # No 59 is emitted: the next pen starts from SGR 0, which restores the
    # default underline color along with everything else.
    g = VTGrid(10, 1)
    g.draw_text(0, 0, "a", fg=(10, 10, 10), attr=_UL, ul=(231, 76, 76))
    g.draw_text(1, 0, "b", fg=(10, 10, 10))
    vt = g.render()
    tail = vt[vt.index("58:2::231:76:76"):]
    assert "\x1b[0" in tail
    assert "59" not in tail.replace("58:2::231:76:76", "")


def test_a_wide_glyph_broken_apart_keeps_the_rule_color():
    # The blank left behind by overwriting half a wide glyph inherits the pen it
    # was overwritten with, the rule color included.
    g = VTGrid(10, 1)
    g.draw_text(0, 0, "日")
    g.draw_text(1, 0, "x", fg=(1, 2, 3), attr=_UL, ul=(9, 9, 9))
    assert g.cell_at(0, 0) == (" ", (1, 2, 3), None, _UL, (9, 9, 9))
