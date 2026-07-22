"""Embedded Japanese (Noto CJK JP) fonts across the non-terminal backends.

Three groups:

* ``text.is_cjk`` / ``text.cjk_segments`` — the pure-Python classification the
  GUI backends route on (cross-platform).
* The **web** backend measurement chain ``[primary, cjk]`` — exactness against a
  direct ``_ttf`` sum, Latin invariance, graceful absence, bold invariance.
* The **macOS** cascade (darwin only) — Japanese resolves to the bundled Noto CJK
  face, Latin stays on the primary, and measurement reflects the CJK advances.

The Windows backend routes CJK through a per-segment CJK text format; its logic
that is testable off-Windows is the shared ``text.cjk_segments`` split covered
below (DirectWrite rendering itself needs a Windows run).
"""

import os
import sys

import pytest

from puikit import Font, FontWeight, Style, TextAttribute
from puikit.font import grid_aligned
from puikit.backend import DEFAULT_STYLE
from puikit.backends import _ttf
from puikit.backends.web_backend import WebBackend
from puikit.text import cjk_segments, display_width, is_cjk

_FONT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "puikit", "fonts")
_CJK_SANS = os.path.join(_FONT_DIR, "NotoSansCJKjp-Regular.otf")
_CJK_MONO = os.path.join(_FONT_DIR, "NotoSansMonoCJKjp-Regular.otf")
_HAS_CJK = os.path.exists(_CJK_SANS) and os.path.exists(_CJK_MONO)
requires_cjk = pytest.mark.skipif(not _HAS_CJK, reason="Noto CJK JP fonts not downloaded")

# Han, hiragana, katakana, halfwidth katakana, CJK punctuation, fullwidth Latin.
_JP = "漢字あいうアイウｱｲｳ、。「」ＡＢ"


# --- text.is_cjk / cjk_segments ---------------------------------------------


@pytest.mark.parametrize("ch", ["漢", "あ", "ア", "ｱ", "、", "。", "「", "」", "Ａ"])
def test_is_cjk_true_for_japanese(ch):
    assert is_cjk(ch)


@pytest.mark.parametrize("ch", ["A", "z", "1", " ", "-", "é", "✓", "@"])
def test_is_cjk_false_for_latin_and_symbols(ch):
    assert not is_cjk(ch)


def test_is_cjk_covers_halfwidth_katakana_which_is_width_one():
    # Halfwidth katakana is display-width 1, so char_width alone can't gate it.
    assert display_width("ｱ") == 1 and is_cjk("ｱ")


def test_cjk_segments_splits_and_roundtrips():
    text = "file_漢字.txt ｱｲｳ"
    segs = cjk_segments(text)
    assert segs == [("file_", False), ("漢字", True), (".txt ", False), ("ｱｲｳ", True)]
    assert "".join(seg for seg, _ in segs) == text


def test_cjk_segments_empty_and_pure():
    assert cjk_segments("") == []
    assert cjk_segments("plain") == [("plain", False)]
    assert cjk_segments("漢字") == [("漢字", True)]


# --- web backend measurement chain ------------------------------------------


@requires_cjk
def test_web_measures_japanese_from_cjk_advances_exactly():
    """A *proportional* Japanese run (a sized mono or the UI face — both flow,
    not grid) measures the exact sum of the CJK table's advances, no em-estimate
    term. (Grid-aligned text measures in columns instead — see the grid test.)"""
    b = WebBackend(open_browser=False)
    for style, kind in ((Style(font=Font(size=14.0, monospace=True)), "mono"),
                        (Style(font=Font(size=14.0, monospace=False)), "sans")):
        face = b._face(style)
        primary, cjk = face.tables[0], b._cjk_tables[kind]
        assert cjk is not None
        em = face.px / b._base_w
        expected = 0.0
        for ch in _JP:
            cp = ord(ch)
            table = primary if primary.has_glyph(cp) else cjk
            assert table.has_glyph(cp)  # every _JP char is covered by the chain
            expected += table.advance(cp) * em
        assert b.measure_text(_JP, style) == pytest.approx(expected, abs=1e-9)


def test_web_grid_measures_columns_not_advance():
    """A grid-aligned font (font=None, or an unsized/unnamed monospace request)
    measures in COLUMNS — a wide CJK glyph is 2 columns — so a monospace mixed
    CJK/Latin layout stays column-aligned (matches the native backends)."""
    b = WebBackend(open_browser=False)
    for style in (DEFAULT_STYLE, Style(font=Font(monospace=True))):
        assert grid_aligned(style.font)
        assert b.measure_text("あいう", style) == pytest.approx(6.0)   # 3 × 2 cols
        assert b.measure_text("漢字ｱ", style) == pytest.approx(5.0)     # 2+2+1
        assert b.measure_text("abcdef", style) == pytest.approx(6.0)   # unchanged


def test_web_grid_text_serializes_column_aligned_cells():
    """The grid draw path emits a ``gtext`` op whose cells sit on base-unit
    column boundaries: a batched primary-font run per contiguous stretch, and
    each wide/CJK glyph on its own column."""
    b = WebBackend(open_browser=False)
    bw = b._base_w
    ops = b._serialize([("text", 0, 0, "a漢b", DEFAULT_STYLE)])
    gtext = [o for o in ops if o[0] == "gtext"]
    assert len(gtext) == 1
    *_, x_px, total_px, cells = gtext[0]
    # "a" at col 0, "漢" at col 1 (its own cell), "b" at col 3 (after 2-wide 漢).
    assert cells == [["a", 0.0], ["漢", 1 * bw], ["b", 3 * bw]]
    assert total_px == pytest.approx(4 * bw)  # 1 + 2 + 1 columns
    assert x_px == 0.0


@requires_cjk
def test_web_css_chain_names_cjk_family_in_order():
    b = WebBackend(open_browser=False)
    assert '"PuiMono", "PuiMonoCJK", monospace' in b._face(DEFAULT_STYLE).css
    sans = b._face(Style(font=Font(size=14.0, monospace=False))).css
    assert '"PuiSans", "PuiSansCJK", sans-serif' in sans


def test_web_latin_identical_with_and_without_cjk():
    """Latin measures byte-for-byte the same whether or not the CJK tables load —
    the CJK face sits after the primary, so any glyph it already covers is
    untouched. (Proportional style, to exercise the advance chain — a grid font
    would measure by column and never consult the tables.)"""
    latin = "The quick brown fox — Hello, World! 12345 (){}[]"
    prop = Style(font=Font(size=14.0, monospace=False))
    with_cjk = WebBackend(open_browser=False)
    without = WebBackend(open_browser=False)
    without._cjk_tables = {"mono": None, "sans": None}
    without._face_cache.clear()
    without._measure_cache.clear()
    assert with_cjk.measure_text(latin, prop) == without.measure_text(latin, prop)


def test_web_graceful_absence_uses_em_estimate():
    """With the CJK tables forced absent, a *proportional* Japanese run still
    measures (via the em-width estimate) exactly as before the CJK face existed.
    (Grid text measures by column and does not use the estimate.)"""
    prop = Style(font=Font(size=14.0, monospace=False))
    b = WebBackend(open_browser=False)
    b._cjk_tables = {"mono": None, "sans": None}
    b._face_cache.clear()
    b._measure_cache.clear()
    face = b._face(prop)
    em = face.px / b._base_w
    expected = sum((display_width(ch) / 2.0) * em for ch in _JP)
    assert b.measure_text(_JP, prop) == pytest.approx(expected, abs=1e-9)


@requires_cjk
def test_web_bold_cjk_measures_equal_to_regular():
    """Bold and regular CJK runs measure equal widths (advances are
    weight-invariant; bold reuses the Regular CJK table)."""
    b = WebBackend(open_browser=False)
    regular = Style(font=Font(size=14.0, monospace=True))
    bold = Style(font=Font(size=14.0, monospace=True, weight=FontWeight.BOLD))
    assert b.measure_text(_JP, regular) == pytest.approx(b.measure_text(_JP, bold), abs=1e-9)
    # The bold face reuses the *same* (Regular) CJK table object.
    assert b._face(regular).tables[1] is b._face(bold).tables[1]


# --- direct _ttf on the CJK OTF ---------------------------------------------


@requires_cjk
@pytest.mark.parametrize("path", [_CJK_MONO, _CJK_SANS])
def test_ttf_parses_cjk_otf(path):
    face = _ttf.load(path)
    assert face.units_per_em == 1000
    assert face.has_glyph(ord("あ")) and face.has_glyph(ord("漢"))
    assert face.advance(ord("漢")) == pytest.approx(1.0, abs=1e-6)  # full-width ≈ 1 em
    assert face.advance(ord("ｱ")) == pytest.approx(0.5, abs=1e-6)   # halfwidth kana ≈ 0.5 em


# --- macOS cascade (darwin only) --------------------------------------------

_IS_DARWIN = sys.platform == "darwin"


@pytest.mark.skipif(not (_IS_DARWIN and _HAS_CJK), reason="macOS + Noto CJK JP required")
def test_macos_cascade_routes_japanese_to_bundled_cjk():
    CoreText = pytest.importorskip("CoreText")
    Cocoa = pytest.importorskip("Cocoa")
    from Foundation import NSAttributedString
    from puikit.backends import macos_backend as mb

    assert mb._ensure_bundled_fonts()  # primary Noto faces
    if not mb._ensure_cjk_fonts():
        pytest.skip("Core Text could not register the CJK faces")

    size = 14.0
    base = Cocoa.NSFont.fontWithName_size_("Noto Sans Mono", size)
    composed = mb._with_cjk_cascade(base, size, monospace=True)

    def family_for(font, ch):
        n = len(ch.encode("utf-16-le")) // 2
        cf = CoreText.CTFontCreateForString(font, ch, (0, n))
        return str(CoreText.CTFontCopyFamilyName(cf))

    def width(font, s):
        a = NSAttributedString.alloc().initWithString_attributes_(
            s, {Cocoa.NSFontAttributeName: font})
        return a.size().width

    # Latin stays on the primary; Japanese resolves to the bundled CJK face.
    assert family_for(composed, "A") == "Noto Sans Mono"
    assert family_for(composed, "漢") == "Noto Sans Mono CJK JP"
    assert family_for(composed, "ｱ") == "Noto Sans Mono CJK JP"
    # Emoji fallback is preserved (the system cascade sits after the CJK entry).
    assert family_for(composed, "😀") == family_for(base, "😀")

    # Measurement reflects the CJK advances, and Latin is unchanged vs. the base.
    tab = _ttf.load(_CJK_MONO)
    assert width(composed, _JP) == pytest.approx(
        sum(tab.advance(ord(c)) for c in _JP) * size, abs=0.5)
    assert width(composed, "Latin only 123") == width(base, "Latin only 123")


@pytest.mark.skipif(not _IS_DARWIN, reason="macOS only")
def test_macos_cascade_noop_without_cjk_files(monkeypatch):
    from puikit.backends import macos_backend as mb
    Cocoa = pytest.importorskip("Cocoa")
    assert mb._ensure_bundled_fonts()
    base = Cocoa.NSFont.fontWithName_size_("Noto Sans Mono", 14.0)
    # Force the "files absent" path: cascade must return the base font untouched.
    monkeypatch.setattr(mb.os.path, "exists", lambda _p: False)
    monkeypatch.setattr(mb, "_cjk_fonts_registered", None)
    assert mb._ensure_cjk_fonts() is False
    assert mb._with_cjk_cascade(base, 14.0, True) is base
