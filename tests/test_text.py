"""East Asian width helpers used by measurement, rendering, and text widgets."""

from puikit.text import char_width, display_width, glyph_runs, truncate_to_width


def test_char_width_ascii_and_wide():
    assert char_width("a") == 1
    assert char_width("あ") == 2  # hiragana: wide
    assert char_width("中") == 2  # CJK: wide
    assert char_width("Ａ") == 2  # fullwidth latin


def test_display_width_mixed():
    assert display_width("abc") == 3
    assert display_width("あいう") == 6
    assert display_width("abあいうえおcde") == 15


def test_truncate_to_width_drops_straddling_wide_char():
    # 6 columns fits "ab" (2) + "あい" (4); the next wide char would straddle.
    assert truncate_to_width("abあいうえお", 6) == "abあい"
    assert truncate_to_width("abあいうえお", 5) == "abあ"  # 4 cols, next would hit 6
    assert truncate_to_width("abc", 0) == ""


def test_variation_selector_emoji_is_two_columns():
    # An emoji-presentation selector (U+FE0F) renders the base as one two-column
    # emoji glyph; counting the selector as a separate column (the old bug) made
    # widths inconsistent between bare and selector emoji.
    assert display_width("🏷️") == 2  # 1F3F7 (Neutral) + FE0F
    assert display_width("⚠️") == 2  # 26A0 (Wide) + FE0F, not 3
    assert display_width("📋") == 2  # bare wide emoji, same as the above
    assert char_width("️") == 0  # the selector contributes no column of its own


def test_glyph_runs_keep_selectors_with_base():
    assert glyph_runs("🏷️ Label") == ["🏷️", " ", "L", "a", "b", "e", "l"]
    # ZWJ sequence stays a single glyph.
    assert glyph_runs("👩‍🚀x") == ["👩‍🚀", "x"]


def test_truncate_keeps_selector_with_base():
    # The selector must not be split from the base it modifies at the boundary.
    assert truncate_to_width("🏷️X", 2) == "🏷️"
    assert truncate_to_width("🏷️X", 1) == ""  # the 2-column glyph would straddle
