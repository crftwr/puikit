"""MarkdownView tests run identically against the TUI and GUI capability profiles."""

import pytest

from puikit import Event, EventType, Panel, PROFILE_GUI_DESKTOP, PROFILE_TUI, TextAttribute
from puikit.backends.memory_backend import MemoryBackend
from puikit.widgets import MarkdownView
from puikit.widgets.markdown_view import _parse_inline, parse_markdown


@pytest.fixture(params=[PROFILE_TUI, PROFILE_GUI_DESKTOP], ids=["tui", "gui"])
def backend(request):
    return MemoryBackend(width=24, height=8, capabilities=request.param)


# --- parsing (backend-independent) -------------------------------------------


def test_parse_inline_emphasis_and_code():
    runs = _parse_inline("a **b** and `c` and *d*")
    flat = {text: roles for text, roles in runs}
    assert "b" in flat and "bold" in flat["b"]
    assert "c" in flat and "code" in flat["c"]
    assert "d" in flat and "italic" in flat["d"]


def test_parse_inline_nested_emphasis():
    runs = _parse_inline("**bold _and italic_**")
    bold_italic = [text for text, roles in runs if {"bold", "italic"} <= roles]
    assert "and italic" in bold_italic


def test_parse_inline_link_keeps_text_drops_url():
    runs = _parse_inline("see [the docs](http://x) now")
    link = [text for text, roles in runs if "link" in roles]
    assert link == ["the docs"]
    assert all("http" not in text for text, _ in runs)


def test_parse_inline_escape():
    runs = _parse_inline(r"not \*bold\* here")
    assert "".join(text for text, _ in runs) == "not *bold* here"
    assert all("bold" not in roles for _, roles in runs)


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
    text, roles = code[0].runs[0]
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
