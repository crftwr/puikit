"""East Asian width helpers used by measurement, rendering, and text widgets."""

from puikit.text import (
    char_width,
    display_width,
    glyph_runs,
    truncate_to_width,
    wrap_text,
)


def _cols(text: str) -> int:
    return display_width(text)


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


def test_wrap_text_fits_in_one_line():
    assert wrap_text("hello world", 20, _cols) == ["hello world"]
    assert wrap_text("", 10, _cols) == [""]  # a blank line stays one segment


def test_wrap_text_word_boundaries():
    # Greedy fill: "the quick" is 9 cols, adding " brown" (6) would hit 15 > 10.
    assert wrap_text("the quick brown fox", 10, _cols) == [
        "the quick",
        "brown fox",
    ]


def test_wrap_text_drops_space_at_wrap_boundary():
    # The space between "aaaa" and "bbbb" lands at the break and is dropped, so
    # neither continuation line carries a leading space.
    assert wrap_text("aaaa bbbb", 4, _cols) == ["aaaa", "bbbb"]


def test_wrap_text_breaks_overlong_word():
    # A single word longer than the width is broken between glyphs.
    assert wrap_text("abcdefgh", 3, _cols) == ["abc", "def", "gh"]
    # Word mode still breaks a too-long word, after placing what fits before it.
    assert wrap_text("hi abcdefgh", 3, _cols) == ["hi", "abc", "def", "gh"]


def test_wrap_text_char_mode_ignores_words():
    assert wrap_text("ab cd", 3, _cols, word=False) == ["ab ", "cd"]


def test_wrap_text_keeps_leading_indent_on_first_line():
    # Leading whitespace is content on the first line; only spaces shoved to a
    # wrap boundary are dropped.
    assert wrap_text("  hi there", 6, _cols) == ["  hi", "there"]


def test_wrap_text_wide_cjk_counts_two_columns():
    # Each CJK glyph is two columns, so only two fit in width 4; no spaces means
    # word wrap falls back to glyph breaks.
    assert wrap_text("あいうえ", 4, _cols) == ["あい", "うえ"]
