"""MarkdownView tests run identically against the TUI and GUI capability profiles."""

import struct

import pytest

from puikit import Event, EventType, Panel, PROFILE_GUI_DESKTOP, PROFILE_TUI, Style, TextAttribute
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
    assert "quote" in kinds
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
