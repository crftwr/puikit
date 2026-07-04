"""MarkdownView tests run identically against the TUI and GUI capability profiles."""

import struct

import pytest

from puikit import CapabilityProfile, Event, EventType, Panel, PROFILE_GUI_DESKTOP, PROFILE_TUI, Style, TextAttribute
from puikit.backends.memory_backend import MemoryBackend
from puikit.widgets import MarkdownView
from puikit.widgets.markdown_view import (
    DEFAULT_CODE_FONT,
    DEFAULT_HEADING_SCALES,
    DEFAULT_TEXT_FONT,
    _block_style,
    _parse_inline,
    parse_markdown,
)
from puikit.theme import DEFAULT_THEME


class SizedBackend(MemoryBackend):
    """A grid backend that, unlike the plain MemoryBackend, reports a font's
    point size through its metrics (base = 14pt = one base unit), so tests can
    exercise the sized-heading / variable-row-height path off-screen."""

    def measure_line_height(self, style=Style()):
        font = style.font
        if font is None or font.size is None:
            return 1.0
        return font.size / 14.0

    def measure_text(self, text, style=Style()):
        font = style.font
        scale = font.size / 14.0 if (font and font.size) else 1.0
        return len(text) * scale


def _png_bytes(w: int, h: int) -> bytes:
    """A minimal PNG header (signature + IHDR width/height) — enough for
    puikit.image.image_size to read the dimensions, no pixel data needed."""
    return (
        b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR"
        + struct.pack(">II", w, h) + b"\x00\x00\x00\x00"
    )


@pytest.fixture(params=[PROFILE_TUI, PROFILE_GUI_DESKTOP], ids=["tui", "gui"])
def backend(request):
    return MemoryBackend(width=24, height=8, capabilities=request.param)


# --- parsing (backend-independent) -------------------------------------------


def test_parse_inline_emphasis_and_code():
    runs = _parse_inline("a **b** and `c` and *d*")
    flat = {text: roles for text, roles, _ in runs}
    assert "b" in flat and "bold" in flat["b"]
    assert "c" in flat and "code" in flat["c"]
    assert "d" in flat and "italic" in flat["d"]


def test_parse_inline_nested_emphasis():
    runs = _parse_inline("**bold _and italic_**")
    bold_italic = [text for text, roles, _ in runs if {"bold", "italic"} <= roles]
    assert "and italic" in bold_italic


def test_parse_inline_link_keeps_text_and_href():
    runs = _parse_inline("see [the docs](http://x) now")
    link = [(text, href) for text, roles, href in runs if "link" in roles]
    assert link == [("the docs", "http://x")]
    # The URL is the href, not part of the displayed text.
    assert all("http" not in text for text, _, _ in runs)


def test_parse_inline_escape():
    runs = _parse_inline(r"not \*bold\* here")
    assert "".join(text for text, _, _ in runs) == "not *bold* here"
    assert all("bold" not in roles for _, roles, _ in runs)


def test_parse_blocks():
    sems = parse_markdown(
        "# Title\n\npara line\n\n- item one\n- item two\n\n> quoted\n\n---\n"
    )
    kinds = [s.block for s in sems]
    assert "heading" in kinds
    assert kinds.count("list") == 2
    # A block quote is now a regular block carrying a quote depth (not its own
    # block kind), so nested quotes / lists-in-quotes reflow through one parser.
    assert any(s.quote_depth > 0 for s in sems)
    assert "rule" in kinds


def test_parse_fenced_code_is_literal():
    sems = parse_markdown("```\n**not bold**\n```\n")
    code = [s for s in sems if s.block == "code"]
    assert len(code) == 1
    text, roles, _ = code[0].runs[0]
    assert text == "**not bold**"
    assert "bold" not in roles  # fenced code is not inline-parsed


# --- rendering ----------------------------------------------------------------


def test_renders_heading_text(backend):
    panel = Panel(backend)
    panel.add(MarkdownView("# Hello\n\nworld"), x=0, y=0, w=24, h=8)
    panel.render()
    lines = backend.snapshot()
    assert lines[0].startswith("Hello")
    assert "world" in "".join(lines)


def test_heading_is_bold(backend):
    panel = Panel(backend)
    panel.add(MarkdownView("# Hi"), x=0, y=0, w=24, h=8)
    panel.render()
    assert backend.style_at(0, 0).attr & TextAttribute.BOLD


def test_bullet_marker_rendered(backend):
    panel = Panel(backend)
    panel.add(MarkdownView("- one\n- two"), x=0, y=0, w=24, h=8)
    panel.render()
    assert "•" in backend.snapshot()[0]


def test_long_paragraph_wraps(backend):
    panel = Panel(backend)
    view = MarkdownView("word " * 20)
    panel.add(view, x=0, y=0, w=24, h=8)
    panel.render()
    # A 100-char paragraph folds well past the 24-col pane.
    assert len(view._rows) > 1
    assert backend.snapshot()[1].strip().startswith("word")


def test_scroll_moves_viewport(backend):
    panel = Panel(backend)
    view = MarkdownView("\n".join(f"line{i}" for i in range(40)))
    panel.add(view, x=0, y=0, w=24, h=8)
    panel.render()
    assert backend.snapshot()[0].startswith("line0")
    panel.dispatch_event(Event(type=EventType.KEY, key="end"))
    panel.render()
    assert "line39" in "".join(backend.snapshot())
    assert not backend.snapshot()[0].startswith("line0")


def test_scrollbar_when_overflowing(backend):
    panel = Panel(backend)
    view = MarkdownView("\n".join(f"line{i}" for i in range(40)))
    panel.add(view, x=0, y=0, w=24, h=8)
    panel.render()
    # The last column carries the bar; content reserves the gutter, so nothing
    # is drawn past it.
    assert view._wrap_width == 23


def test_empty_source_is_safe(backend):
    panel = Panel(backend)
    panel.add(MarkdownView(""), x=0, y=0, w=24, h=8)
    panel.render()  # must not raise


def test_prose_is_proportional_and_code_is_mono():
    # On a fonts-capable backend the styles reach the backend with their fonts
    # intact, so prose carries the proportional face and code the monospace one.
    backend = MemoryBackend(width=40, height=10, capabilities=PROFILE_GUI_DESKTOP)
    panel = Panel(backend)
    panel.add(MarkdownView("text and `code` here"), x=0, y=0, w=40, h=10)
    panel.render()
    snap = backend.snapshot()[0]
    body = backend.style_at(0, 0).font
    code = backend.style_at(snap.index("code"), 0).font
    assert body is not None and not body.monospace
    assert code is not None and code.monospace


def test_fenced_code_block_is_mono():
    backend = MemoryBackend(width=40, height=10, capabilities=PROFILE_GUI_DESKTOP)
    panel = Panel(backend)
    panel.add(MarkdownView("```\nmono\n```"), x=0, y=0, w=40, h=10)
    panel.render()
    for y, row in enumerate(backend.snapshot()):
        if row.startswith("mono"):
            assert backend.style_at(0, y).font.monospace
            return
    raise AssertionError("code block not rendered")


def test_heading_levels_carry_descending_sizes():
    # Sizes are body_size x the per-level scale; pass the body size (14) the
    # backends resolve so the absolute points come out relative to it.
    sizes = [
        _block_style(
            "heading", lvl, Style(), DEFAULT_THEME,
            DEFAULT_TEXT_FONT, DEFAULT_CODE_FONT, DEFAULT_HEADING_SCALES, 14.0,
        ).font.size
        for lvl in range(1, 7)
    ]
    assert sizes == sorted(sizes, reverse=True)  # # bigger than ## bigger than …
    assert sizes[0] > 14.0  # h1 is larger than the body


def test_headings_scale_with_a_larger_body_face():
    # A document with a larger body face scales its headings with it: an h1 stays
    # the level-1 multiple of whatever body size is passed.
    head = _block_style(
        "heading", 1, Style(), DEFAULT_THEME,
        DEFAULT_TEXT_FONT, DEFAULT_CODE_FONT, DEFAULT_HEADING_SCALES, 20.0,
    )
    assert head.font.size == 20.0 * DEFAULT_HEADING_SCALES[1]


def test_sized_heading_makes_a_taller_row():
    # On a backend that honors point sizes, an h1 row is taller than a body row,
    # and the cumulative tops reflect the varied heights (not a flat multiply).
    backend = SizedBackend(width=40, height=30, capabilities=PROFILE_GUI_DESKTOP)
    panel = Panel(backend)
    view = MarkdownView("# Big\n\nbody")
    panel.add(view, x=0, y=0, w=40, h=30)
    panel.render()
    heading_h = view._rows[0].height
    body_h = view._rows[-1].height
    assert heading_h > body_h == 1.0
    assert heading_h == DEFAULT_HEADING_SCALES[1]  # body 14pt x scale, over 14pt unit
    # tops are the running sum of heights.
    assert view._row_tops[1] == heading_h


def test_rows_stay_uniform_height_on_terminal(backend):
    # Without font metrics every row is one base unit tall, so the document
    # paginates exactly as it did before sizes existed.
    if backend.capabilities.supports("fonts"):
        pytest.skip("covered by the sized GUI test")
    panel = Panel(backend)
    view = MarkdownView("# H1\n\n## H2\n\nbody")
    panel.add(view, x=0, y=0, w=24, h=8)
    panel.render()
    assert all(r.height == 1.0 for r in view._rows)


def test_fonts_fold_to_attrs_on_terminal(backend):
    # Under the TUI profile the Panel folds fonts away; the document still reads
    # (bold heading survives as an attribute) and stays column-aligned.
    if backend.capabilities.supports("fonts"):
        pytest.skip("GUI profile keeps fonts")
    panel = Panel(backend)
    panel.add(MarkdownView("# Hi\n\nbody"), x=0, y=0, w=24, h=8)
    panel.render()
    assert backend.style_at(0, 0).font is None
    assert backend.style_at(0, 0).attr & TextAttribute.BOLD


# --- headings: no underline, body color --------------------------------------


def test_heading_has_no_underline_and_body_color():
    body = _block_style(
        "para", 0, Style(), DEFAULT_THEME,
        DEFAULT_TEXT_FONT, DEFAULT_CODE_FONT, DEFAULT_HEADING_SCALES, 14.0,
    )
    head = _block_style(
        "heading", 1, Style(), DEFAULT_THEME,
        DEFAULT_TEXT_FONT, DEFAULT_CODE_FONT, DEFAULT_HEADING_SCALES, 14.0,
    )
    assert not head.attr & TextAttribute.UNDERLINE
    assert head.fg == body.fg  # same color as regular text
    assert head.attr & TextAttribute.BOLD


# --- images ------------------------------------------------------------------


def test_image_block_parsed():
    sems = parse_markdown("![a cat](cat.png)")
    assert len(sems) == 1
    assert sems[0].block == "image"
    assert sems[0].data == "cat.png"
    assert sems[0].runs[0][0] == "a cat"  # alt text


def test_image_reserves_aspect_height(tmp_path):
    # A 2:1 PNG sized to a 20-unit-wide pane (square base unit) reserves ~10 rows.
    png = tmp_path / "wide.png"
    png.write_bytes(_png_bytes(40, 20))
    backend = MemoryBackend(width=20, height=40, capabilities=PROFILE_GUI_DESKTOP)
    panel = Panel(backend)
    view = MarkdownView(f"![w]({png})")
    panel.add(view, x=0, y=0, w=20, h=40)
    panel.render()
    assert len(view._rows) == 1
    row = view._rows[0]
    assert row.image is not None
    assert abs(row.height - 10.0) < 0.01


def test_image_alt_glyph_on_terminal(tmp_path):
    png = tmp_path / "img.png"
    png.write_bytes(_png_bytes(10, 10))
    backend = MemoryBackend(width=20, height=20, capabilities=PROFILE_TUI)
    panel = Panel(backend)
    panel.add(MarkdownView(f"![icon]({png})"), x=0, y=0, w=20, h=20)
    panel.render()  # TUI has no images: the Panel draws the alt glyph, no raise


# --- hyperlinks --------------------------------------------------------------


def test_click_link_opens_url():
    opened = []

    class OpeningBackend(MemoryBackend):
        def open_url(self, url):
            opened.append(url)
            return True

    backend = OpeningBackend(width=40, height=8, capabilities=PROFILE_GUI_DESKTOP)
    panel = Panel(backend)
    view = MarkdownView("see [docs](http://x) here")
    panel.add(view, x=0, y=0, w=40, h=8)
    panel.render()
    # Click on the word "docs" (starts at column 4).
    panel.dispatch_event(Event(type=EventType.MOUSE_CLICK, x=5, y=0))
    assert opened == ["http://x"]


def test_link_hover_requests_pointer_cursor():
    shapes = []

    class CursorBackend(MemoryBackend):
        def set_pointer_shape(self, shape):
            shapes.append(shape)

    caps = CapabilityProfile({**PROFILE_GUI_DESKTOP, "pointer_shape": True})
    backend = CursorBackend(width=40, height=8, capabilities=caps)
    panel = Panel(backend)
    view = MarkdownView("see [docs](http://x) here")
    panel.add(view, x=0, y=0, w=40, h=8)
    panel.render()

    # Over plain text ("see"): no link cursor this frame.
    panel.dispatch_event(Event(type=EventType.MOUSE_MOVE, x=0.0, y=0.0))
    panel.render()
    assert shapes[-1] is None

    # Over the link word "docs" (starts at column 4): a pointing hand.
    panel.dispatch_event(Event(type=EventType.MOUSE_MOVE, x=5.0, y=0.0))
    panel.render()
    assert shapes[-1] == "pointer"


def test_click_outside_link_does_nothing():
    opened = []

    class OpeningBackend(MemoryBackend):
        def open_url(self, url):
            opened.append(url)
            return True

    backend = OpeningBackend(width=40, height=8, capabilities=PROFILE_GUI_DESKTOP)
    panel = Panel(backend)
    view = MarkdownView("see [docs](http://x) here")
    panel.add(view, x=0, y=0, w=40, h=8)
    panel.render()
    panel.dispatch_event(Event(type=EventType.MOUSE_CLICK, x=0, y=0))  # on "see"
    assert opened == []


def test_link_falls_back_to_clipboard_without_os_open(backend):
    if backend.capabilities.supports("os_open"):
        pytest.skip("covered by the GUI open test")
    panel = Panel(backend)
    view = MarkdownView("[docs](http://x)")
    panel.add(view, x=0, y=0, w=24, h=8)
    panel.render()
    panel.dispatch_event(Event(type=EventType.MOUSE_CLICK, x=1, y=0))
    assert panel.get_clipboard() == "http://x"


_LINE_DOC = "> quoted line\n\n---\n"


class _VectorBackend(MemoryBackend):
    """Memory backend that keeps ``vector_shapes`` on (the base MemoryBackend
    forces it off, being a character grid) so a widget's vector-stroke path can
    be exercised off-screen. A distinct base-unit size makes strokes sub-unit."""

    def __init__(self, **kw):
        super().__init__(capabilities=PROFILE_GUI_DESKTOP, **kw)

    @property
    def capabilities(self):
        return self._capabilities  # unmodified: vector_shapes stays True

    @property
    def base_size(self):
        return (8, 16)


def test_rule_and_quote_are_hairlines_not_glyphs_on_gui():
    # The horizontal rule (─) and the blockquote bar (│) must reach a vector
    # backend as real strokes, never box-drawing characters.
    backend = _VectorBackend(width=20, height=8)
    texts: list[str] = []
    fills: list[tuple] = []
    orig_text, orig_fill = backend.draw_text, backend.fill_rect

    def text_spy(x, y, text, style=None):
        texts.append(text)
        return orig_text(x, y, text) if style is None else orig_text(x, y, text, style)

    def fill_spy(x, y, w, h, style=None):
        fills.append((x, y, w, h))
        return orig_fill(x, y, w, h) if style is None else orig_fill(x, y, w, h, style)

    backend.draw_text, backend.fill_rect = text_spy, fill_spy
    panel = Panel(backend)
    panel.add(MarkdownView(_LINE_DOC), x=0, y=0, w=20, h=8)
    panel.render()
    joined = "".join(texts)
    assert "─" not in joined and "│" not in joined  # no box glyphs on GUI
    # Both structural lines are thinner than a base unit (device-pixel strokes).
    assert any(0 < h < 1.0 or 0 < w < 1.0 for _, _, w, h in fills)


def test_rule_and_quote_are_glyphs_on_tui():
    backend = MemoryBackend(width=20, height=8, capabilities=PROFILE_TUI)
    panel = Panel(backend)
    panel.add(MarkdownView(_LINE_DOC), x=0, y=0, w=20, h=8)
    panel.render()
    joined = "".join(backend.snapshot())
    assert "─" in joined and "│" in joined  # grid keeps the box glyphs


# --- GitHub-flavored extensions ----------------------------------------------


def _roles(runs, needle):
    for text, roles, href in runs:
        if needle in text:
            return roles, href
    raise AssertionError(f"no run containing {needle!r}")


def test_parse_strikethrough():
    roles, _ = _roles(_parse_inline("keep ~~drop~~ keep"), "drop")
    assert "strike" in roles


def test_strikethrough_renders_attribute(backend):
    panel = Panel(backend)
    panel.add(MarkdownView("~~x~~"), x=0, y=0, w=24, h=8)
    panel.render()
    assert backend.style_at(0, 0).attr & TextAttribute.STRIKETHROUGH


def test_single_tilde_is_literal():
    roles, _ = _roles(_parse_inline("a ~b~ c"), "~b~")
    assert "strike" not in roles


def test_angle_autolink_and_bare_url():
    runs = _parse_inline("see <https://a.test> and http://b.test now")
    assert _roles(runs, "https://a.test") == (frozenset({"link"}), "https://a.test")
    assert _roles(runs, "http://b.test") == (frozenset({"link"}), "http://b.test")


def test_bare_url_trailing_punctuation_trimmed():
    # The sentence period is not part of the link.
    roles, href = _roles(_parse_inline("go to https://a.test. ok"), "https://a.test")
    assert href == "https://a.test" and "link" in roles


def test_email_autolink_gets_mailto():
    _, href = _roles(_parse_inline("mail <me@x.test> please"), "me@x.test")
    assert href == "mailto:me@x.test"


def test_reference_link_resolves_and_def_is_hidden():
    sems = parse_markdown("see [the docs][d] now\n\n[d]: https://ref.test\n")
    para = next(s for s in sems if s.block == "para")
    _, href = _roles(para.runs, "the docs")
    assert href == "https://ref.test"
    # The definition line itself renders nothing.
    assert not any("ref.test" in t for s in sems for t, _, _ in s.runs)


def test_shortcut_reference_link():
    sems = parse_markdown("[Puikit] rocks\n\n[puikit]: https://p.test\n")
    para = next(s for s in sems if s.block == "para")
    _, href = _roles(para.runs, "Puikit")
    assert href == "https://p.test"


def test_setext_headings():
    sems = parse_markdown("Big\n===\n\nSmall\n---\n")
    heads = [(s.block, s.level) for s in sems if s.block == "heading"]
    assert heads == [("heading", 1), ("heading", 2)]


def test_setext_dash_beats_horizontal_rule():
    # A '---' directly under paragraph text is a heading underline, not a rule.
    sems = parse_markdown("Title\n---\n")
    assert sems[0].block == "heading" and sems[0].level == 2
    assert all(s.block != "rule" for s in sems)


def test_bare_dashes_stay_a_rule():
    sems = parse_markdown("para\n\n---\n")
    assert any(s.block == "rule" for s in sems)


def test_task_list_items():
    sems = [s for s in parse_markdown("- [ ] todo\n- [x] done\n") if s.block == "list"]
    assert [s.checked for s in sems] == [False, True]
    assert sems[0].prefix.endswith("☐ ") and sems[1].prefix.endswith("☑ ")
    # The checkbox marker is stripped from the item text.
    assert sems[0].runs[0][0] == "todo"


def test_task_checkbox_renders(backend):
    panel = Panel(backend)
    panel.add(MarkdownView("- [x] done"), x=0, y=0, w=24, h=8)
    panel.render()
    assert "☑" in backend.snapshot()[0]


def test_hard_line_break_splits_a_row(backend):
    # Without the break these fit on one row; the two trailing spaces force two.
    panel = Panel(backend)
    panel.add(MarkdownView("alpha  \nbeta"), x=0, y=0, w=24, h=8)
    panel.render()
    snap = backend.snapshot()
    assert snap[0].startswith("alpha") and snap[1].startswith("beta")


def test_backslash_hard_break():
    sems = parse_markdown("alpha\\\nbeta\n")
    # The break becomes a literal newline in the flowed run; the '\' is dropped.
    assert sems[0].runs[0][0] == "alpha\nbeta"


def test_nested_blockquote_depth():
    sems = parse_markdown("> outer\n> > inner\n")
    depths = [s.quote_depth for s in sems if s.runs]
    assert depths == [1, 2]


def test_blockquote_multiline_reflows():
    sems = parse_markdown("> one\n> two\n")
    quoted = [s for s in sems if s.quote_depth]
    assert len(quoted) == 1  # two source lines flow into one paragraph
    assert quoted[0].runs[0][0] == "one two"


def test_table_parses_alignment():
    sems = parse_markdown("| L | C | R |\n| :- | :-: | -: |\n| a | b | c |\n")
    tbl = next(s.table for s in sems if s.block == "table")
    assert tbl.aligns == ["left", "center", "right"]
    assert len(tbl.rows) == 1 and len(tbl.header) == 3


def test_table_renders_cells_and_borders(backend):
    panel = Panel(backend)
    doc = "| Name | Age |\n| :-- | --: |\n| Bob | 30 |\n"
    panel.add(MarkdownView(doc), x=0, y=0, w=30, h=10)
    panel.render()
    joined = "".join(backend.snapshot())
    assert all(cell in joined for cell in ("Name", "Age", "Bob", "30"))
    assert "─" in joined and "│" in joined  # boxed grid


def test_table_uses_box_drawing_junctions_on_tui():
    backend = MemoryBackend(width=24, height=10, capabilities=PROFILE_TUI)
    panel = Panel(backend)
    panel.add(MarkdownView("| A | B |\n|-|-|\n| 1 | 2 |\n"), x=0, y=0, w=24, h=10)
    panel.render()
    joined = "".join(backend.snapshot())
    # A connected grid: real corners, tees, and a cross where the bars meet.
    assert "┌" in joined and "┐" in joined  # top corners
    assert "└" in joined and "┘" in joined  # bottom corners
    assert "┼" in joined  # the header/body separator crossing a column bar


def test_table_borders_are_strokes_not_glyphs_on_gui():
    # On a vector backend the whole frame is device-thin strokes; no box glyphs.
    backend = _VectorBackend(width=30, height=16)
    texts: list[str] = []
    fills: list[tuple] = []
    orig_text, orig_fill = backend.draw_text, backend.fill_rect

    def text_spy(x, y, text, style=None):
        texts.append(text)
        return orig_text(x, y, text) if style is None else orig_text(x, y, text, style)

    def fill_spy(x, y, w, h, style=None):
        fills.append((x, y, w, h))
        return orig_fill(x, y, w, h) if style is None else orig_fill(x, y, w, h, style)

    backend.draw_text, backend.fill_rect = text_spy, fill_spy
    panel = Panel(backend)
    panel.add(MarkdownView("| A | B |\n|-|-|\n| 1 | 2 |\n"), x=0, y=0, w=30, h=16)
    panel.render()
    joined = "".join(texts)
    assert not any(g in joined for g in "─│┼┌┐└┘┬┴├┤")  # no box glyphs on GUI
    assert all(cell in joined for cell in "AB12")  # cells still drawn
    assert any(0 < w < 1.0 or 0 < h < 1.0 for _, _, w, h in fills)  # thin strokes


def test_table_right_alignment(backend):
    panel = Panel(backend)
    doc = "| N |\n| --: |\n| 7 |\n"
    panel.add(MarkdownView(doc), x=0, y=0, w=12, h=8)
    panel.render()
    # The lone digit hugs the right edge of its (widened) column, not the left.
    body = next(row for row in backend.snapshot() if "7" in row)
    assert body.index("7") > body.index("│")


def test_anchor_link_scrolls_to_heading():
    doc = "\n".join(["[go](#target-section)"] + [""] * 30 + ["## Target Section", "body"])
    view = MarkdownView(doc)
    backend = MemoryBackend(width=30, height=6, capabilities=PROFILE_TUI)
    panel = Panel(backend)
    panel.add(view, x=0, y=0, w=30, h=6)
    panel.render()
    assert view.offset == 0.0
    panel.dispatch_event(Event(type=EventType.MOUSE_CLICK, x=1, y=0))
    assert view.offset > 0.0  # jumped down toward the heading
