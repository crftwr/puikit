"""The shared search-jump scroll rule (puikit.widgets._scroll).

A match already sitting at least ``margin`` inside both viewport edges leaves
the scroll alone; one outside that band is centered vertically, optionally
snapping the centering shift to whole rows so a grid never rests between
cells. MarkdownView, JsonView and TableView all jump through this.
"""

from puikit.widgets._scroll import search_scroll_offset


def test_match_inside_band_does_not_scroll():
    # Viewport 0..20, margin 3: rows 3..16 (top edge 3..16 with row_h 1) hold.
    assert search_scroll_offset(0.0, 20.0, 5.0, 1.0, 3.0) == 0.0
    assert search_scroll_offset(0.0, 20.0, 16.0, 1.0, 3.0) == 0.0


def test_match_below_band_centers():
    # Row 30 in a 20-unit viewport at offset 0: centered, (20 - 1) / 2 above.
    assert search_scroll_offset(0.0, 20.0, 30.0, 1.0, 3.0) == 30.0 - 9.5


def test_match_above_band_centers():
    assert search_scroll_offset(40.0, 20.0, 10.0, 1.0, 3.0) == 10.0 - 9.5


def test_match_just_inside_edge_still_centers():
    # Row 18 is visible at offset 0 but within the 3-unit bottom margin.
    assert search_scroll_offset(0.0, 20.0, 18.0, 1.0, 3.0) == 18.0 - 9.5


def test_snap_quantizes_centering_shift():
    # shift (20 - 1) / 2 = 9.5 floors to 9 whole rows.
    assert search_scroll_offset(0.0, 20.0, 30.0, 1.0, 3.0, snap=1.0) == 21.0


def test_margin_shrinks_for_a_nearly_full_row():
    # A 16-unit row in a 20-unit viewport: margin collapses to 2, so the row
    # fully on screen counts as in-band.
    assert search_scroll_offset(0.0, 20.0, 2.0, 16.0, 3.0) == 0.0


def test_row_taller_than_viewport_aligns_top():
    assert search_scroll_offset(0.0, 20.0, 50.0, 25.0, 3.0) == 50.0
