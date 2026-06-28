"""East Asian width helpers used by measurement, rendering, and text widgets."""

from puikit.text import (
    char_width,
    display_width,
    elide,
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


def test_truncate_with_ellipsis():
    # Text that fits is returned unchanged, no ellipsis appended.
    assert truncate_to_width("abc", 5, ellipsis="…") == "abc"
    assert truncate_to_width("abc", 3, ellipsis="…") == "abc"
    # Text that overflows: prefix + ellipsis, together within max_width.
    assert truncate_to_width("abcdef", 4, ellipsis="…") == "abc…"  # 3 + 1
    assert truncate_to_width("abcdef", 4, ellipsis="..") == "ab.."  # 2 + 2
    # A wide ellipsis is measured in columns, not characters.
    assert display_width(truncate_to_width("abcdefgh", 5, ellipsis="…")) <= 5
    # No room for the ellipsis -> bare prefix fallback.
    assert truncate_to_width("abcdef", 1, ellipsis="..") == "a"
    # Empty ellipsis keeps the pure-truncation behaviour.
    assert truncate_to_width("abcdef", 3, ellipsis="") == "abc"


def test_elide_fits_unchanged():
    assert elide("abc", 5) == "abc"
    assert elide("abc", 3) == "abc"


def test_elide_end():
    # 6 cols: 5 of text ("longf") + "…".
    assert elide("longfilename", 6) == "longf…"
    assert display_width(elide("longfilename", 6)) == 6


def test_elide_start():
    assert elide("longfilename", 6, where="start") == "…ename"
    assert display_width(elide("longfilename", 6, where="start")) == 6


def test_elide_middle():
    # budget 5 -> left 2, right 3 around the ellipsis.
    assert elide("longfilename", 6, where="middle") == "lo…ame"
    assert display_width(elide("longfilename.txt", 10, where="middle")) <= 10


def test_elide_custom_ellipsis_measured_in_columns():
    assert elide("abcdefgh", 5, ellipsis="..") == "abc.."  # 3 + 2
    # No room for the ellipsis -> bare prefix fallback.
    assert elide("abcdef", 1, ellipsis="..") == "a"


def test_elide_respects_wide_glyphs():
    # Wide (2-col) CJK: a 5-col end-elide keeps "中中" (4) + "…" (1), not a split.
    assert elide("中中中中", 5) == "中中…"
    assert display_width(elide("中中中中", 5)) == 5
    # The emoji-with-selector glyph is never split from its base.
    assert elide("🏷️abcdef", 3, where="start") == "…ef"


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


# --- proportional fonts ------------------------------------------------------------
#
# A real per-Style font is measured natively and divided by the base unit width
# (see MacOSBackend.measure_text), so the wrap measure returns *fractional* base
# units, not a column count. Modelling that as a callable here is exactly how the
# DrawContext/LayoutContext feed wrap_text — narrow glyphs are < 1 unit, wide
# glyphs > 1, and wrapping must honour the measured width, not character counts.

def _proportional(text: str) -> float:
    widths = {"i": 0.5, "l": 0.5, "w": 2.0, "m": 2.0, " ": 0.5}
    return sum(widths.get(ch, 1.0) for ch in text)


def test_wrap_text_proportional_font_fits_more_narrow_glyphs():
    # Six 'i' glyphs measure 3.0 and fit a width a column count (6 cols) would
    # reject — the wrap follows the measured width, not the glyph count.
    assert wrap_text("iiiiii ii", 3.0, _proportional) == ["iiiiii", "ii"]
    # Contrast: counting columns, only three 'i' fit in width 3.
    assert wrap_text("iiiiii ii", 3.0, _cols) == ["iii", "iii", "ii"]


def test_wrap_text_proportional_font_breaks_on_wide_glyph():
    # Two 'w' glyphs measure 4.0, so they cannot share a 2.5-wide line even
    # though a column count (2 cols) would have let them.
    assert wrap_text("ww", 2.5, _proportional) == ["w", "w"]
    # A narrow + wide word: "iw" (2.5) fits, the second wide word breaks.
    assert wrap_text("iw iww", 4.0, _proportional) == ["iw", "iw", "w"]


# --- Japanese ----------------------------------------------------------------------
#
# With the base grid font, Japanese is measured by display_width (each glyph two
# columns) and carries no ASCII spaces, so word wrap falls back to per-glyph
# breaks. _cols is display_width, matching the base-font branch of the backend.

def test_wrap_text_japanese_breaks_between_glyphs():
    # Each kana/kanji is two columns: three fit in width 6, the rest wrap.
    assert wrap_text("今日は晴れ", 6, _cols) == ["今日は", "晴れ"]
    # Width 5 cannot hold a straddling third wide glyph: two per line.
    assert wrap_text("今日は晴れ", 5, _cols) == ["今日", "は晴", "れ"]


def test_wrap_text_mixed_japanese_and_latin():
    # A Latin word and a Japanese run are distinct wrap units; the Latin word
    # stays whole and the Japanese run breaks between its glyphs to fit.
    assert wrap_text("Run これをテスト", 6, _cols) == ["Run", "これを", "テスト"]


def test_wrap_text_japanese_keeps_combined_glyph_together():
    # An emoji-presentation sequence is one glyph (two columns); the wrap must
    # never split the base from its selector at a break boundary.
    assert wrap_text("あ🏷️い", 4, _cols) == ["あ🏷️", "い"]
