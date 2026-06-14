"""East Asian width helpers used by measurement, rendering, and text widgets."""

from puikit.text import char_width, display_width, truncate_to_width


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
