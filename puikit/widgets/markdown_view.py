"""A scrolling, read-only rich-text view of CommonMark-ish Markdown.

Where :class:`~puikit.widgets.text_block.TextBlock` renders one flat ``Style``
across its whole text and :class:`~puikit.widgets.log_view.LogView` renders one
``Style`` per line, a Markdown document is *intra-line* rich: a single sentence
may mix bold, italic, inline ``code`` and a link. ``MarkdownView`` keeps the
PuiKit seam intact while still drawing that:

- it parses the source once into **semantic** blocks (headings, paragraphs,
  list items, block quotes, fenced code, rules) whose inline runs carry only
  *roles* (``bold`` / ``italic`` / ``code`` / ``link``), never colors;
- at draw time the active :class:`~puikit.theme.Theme` turns those roles into
  concrete ``Style``s, so the same document follows the backend's palette —
  accent links and headings on GUI, the nearest xterm-256 cell on TUI;
- prose carries a **proportional** ``Font`` and code (fenced blocks and inline
  spans) a **monospace** ``Font``; on ``fonts``-capable backends they render as
  real proportional / fixed-advance faces, and on a terminal both fold back to
  the one grid font (with bold / italic preserved as attributes) — one
  implementation, every backend, no per-row branch.

Because prose is proportional, wrapping measures runs through
``DrawContext.measure_text`` (not a column count) and the row pitch comes from
``DrawContext.line_height``, so a taller face does not overlap the next row.
Like ``LogView`` it is **virtualized**: the source is wrapped to the pane width
once (re-wrapped only on a resize) into display rows, and only the rows inside
the viewport are ever drawn, so a long document scrolls cheaply. Navigation is
pure scrolling (a document has no "current item"): arrows / page keys / home /
end move the viewport and the mouse wheel scrolls.

The Markdown subset is deliberately small but covers the common cases: ATX
headings (``#``..``######``), paragraphs, ``-``/``*``/``+`` and ordered list
items (nested by indentation), ``>`` block quotes, ``` ``` ``` / ``~~~`` fenced
code, ``---`` horizontal rules, and the inline runs ``**bold**``, ``*italic*`` /
``_italic_``, ```` `code` ````, ``[text](url)`` links, and backslash escapes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..backend import DEFAULT_STYLE, Style, TextAttribute
from ..event import Event, EventType
from ..font import Font
from ..panel import DrawContext
from ..text import glyph_runs
from ..theme import DEFAULT_THEME, Theme
from .base import Widget

# Default body / code faces. ``Font()`` (all defaults) is the backend's
# proportional UI font on GUI; ``Font(monospace=True)`` is its fixed-advance
# face. Both fold to the single terminal font on a non-``fonts`` backend, so a
# document still reads correctly there (see docs/font_system.md §6).
DEFAULT_TEXT_FONT = Font()
DEFAULT_CODE_FONT = Font(monospace=True)

# A drawn fragment: text plus the style it draws in.
Span = tuple[str, Style]
# An inline run as parsed: text plus the set of semantic roles on it.
InlineRun = tuple[str, frozenset]

# Inline code reads as a distinct face independent of the surface; the body fg
# is theme-driven but code keeps a stable warm tint over a panel-ish fill, the
# same convention every editor uses for a code span.
_CODE_FG = (206, 145, 120)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_HRULE_RE = re.compile(r"^(?:-{3,}|\*{3,}|_{3,})$")
_LIST_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
_FENCE_RE = re.compile(r"^\s*(```+|~~~+)")
_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]*)(?:\s+\"[^\"]*\")?\)")


@dataclass
class _SemLine:
    """One semantic line of the document: its block kind, an optional rendered
    prefix (a list marker, a quote bar), and its inline runs (role-tagged, not
    yet colored). ``wrap`` is False for atoms that must stay on one row (a fenced
    code line, a rule, a blank spacer)."""

    block: str  # heading | para | list | quote | code | rule | blank
    level: int  # heading level, or list nesting depth
    prefix: str  # rendered indent / marker, drawn before the inline runs
    runs: list[InlineRun] = field(default_factory=list)
    wrap: bool = True


# --- inline parsing -----------------------------------------------------------


def _scan_inline(text: str, roles: frozenset) -> list[InlineRun]:
    """Split ``text`` into role-tagged runs, recursing so emphasis nests
    (``**bold _and italic_**``). ``roles`` is the set already in force from the
    enclosing scan."""
    out: list[InlineRun] = []
    buf: list[str] = []
    i, n = 0, len(text)

    def flush() -> None:
        if buf:
            out.append(("".join(buf), roles))
            buf.clear()

    while i < n:
        c = text[i]
        if c == "\\" and i + 1 < n:
            buf.append(text[i + 1])
            i += 2
            continue
        if c == "`":
            k = i
            while k < n and text[k] == "`":
                k += 1
            fence = text[i:k]
            close = text.find(fence, k)
            if close != -1:
                flush()
                # An inline code span is literal: its contents are not re-parsed.
                out.append((text[k:close], roles | {"code"}))
                i = close + len(fence)
                continue
            buf.append(c)
            i += 1
            continue
        if c == "[":
            m = _LINK_RE.match(text, i)
            if m:
                flush()
                out.extend(_scan_inline(m.group(1), roles | {"link"}))
                i = m.end()
                continue
            buf.append(c)
            i += 1
            continue
        if c in "*_" and "code" not in roles:
            k = i
            while k < n and text[k] == c:
                k += 1
            run = min(k - i, 3)
            marker = c * run
            # 1 delimiter = italic, 2 = bold, 3 = both.
            added = {1: {"italic"}, 2: {"bold"}, 3: {"bold", "italic"}}[run]
            close = text.find(marker, k)
            if close != -1 and close > k:
                flush()
                out.extend(_scan_inline(text[k:close], roles | added))
                i = close + len(marker)
                continue
            buf.append(text[i:k])
            i = k
            continue
        buf.append(c)
        i += 1
    flush()
    return out


def _parse_inline(text: str) -> list[InlineRun]:
    return _scan_inline(text, frozenset())


# --- block parsing ------------------------------------------------------------


def _is_block_break(line: str) -> bool:
    """True when ``line`` starts a new block, so a paragraph stops gathering."""
    s = line.strip()
    return (
        s == ""
        or bool(_HEADING_RE.match(line))
        or bool(_HRULE_RE.match(s))
        or s.startswith(">")
        or bool(_LIST_RE.match(line))
        or bool(_FENCE_RE.match(line))
    )


def parse_markdown(src: str) -> list[_SemLine]:
    """Parse Markdown source into the semantic line list the view lays out."""
    lines = src.split("\n")
    out: list[_SemLine] = []
    i, n = 0, len(lines)
    while i < n:
        raw = lines[i]
        stripped = raw.strip()

        fence = _FENCE_RE.match(raw)
        if fence:
            marker = fence.group(1)[0]
            i += 1
            while i < n and not lines[i].lstrip().startswith(marker * 3):
                out.append(_SemLine("code", 0, "", [(lines[i], frozenset())], wrap=False))
                i += 1
            i += 1  # consume the closing fence (or run off the end)
            continue

        if stripped == "":
            out.append(_SemLine("blank", 0, "", [], wrap=False))
            i += 1
            continue

        m = _HEADING_RE.match(raw)
        if m:
            out.append(_SemLine("heading", len(m.group(1)), "", _parse_inline(m.group(2))))
            i += 1
            continue

        if _HRULE_RE.match(stripped):
            out.append(_SemLine("rule", 0, "", [], wrap=False))
            i += 1
            continue

        if stripped.startswith(">"):
            content = re.sub(r"^\s*>\s?", "", raw)
            out.append(_SemLine("quote", 0, "│ ", _parse_inline(content)))
            i += 1
            continue

        lm = _LIST_RE.match(raw)
        if lm:
            depth = len(lm.group(1)) // 2
            marker = lm.group(2)
            bullet = "• " if marker in ("-", "*", "+") else marker + " "
            # Gather lazy continuation lines: indented, non-blank lines that are
            # not themselves a new block fold into this item's text.
            content = [lm.group(3)]
            i += 1
            while i < n and lines[i].strip() and lines[i][:1] in (" ", "\t") and not _is_block_break(lines[i]):
                content.append(lines[i].strip())
                i += 1
            text = " ".join(content)
            out.append(_SemLine("list", depth, "  " * depth + bullet, _parse_inline(text)))
            continue

        # Paragraph: gather consecutive plain lines into one wrapped flow.
        para = [stripped]
        i += 1
        while i < n and not _is_block_break(lines[i]):
            para.append(lines[i].strip())
            i += 1
        out.append(_SemLine("para", 0, "", _parse_inline(" ".join(para))))
    return out


# --- styling (roles -> concrete Style, theme-driven) --------------------------


def _block_style(
    block: str, level: int, base: Style, theme: Theme, text_font: Font, code_font: Font
) -> Style:
    """Base style for a whole semantic line, before inline roles are layered.
    Prose blocks carry the proportional ``text_font``; a fenced code block
    carries the monospace ``code_font``."""
    if block == "heading":
        attr = base.attr | TextAttribute.BOLD
        if level <= 2:
            attr |= TextAttribute.UNDERLINE
            return Style(fg=theme.accent, bg=base.bg, attr=attr, font=text_font)
        return Style(fg=base.fg, bg=base.bg, attr=attr, font=text_font)
    if block == "quote":
        return Style(
            fg=theme.muted_text, bg=base.bg, attr=base.attr | TextAttribute.ITALIC, font=text_font
        )
    if block == "code":
        return Style(fg=_CODE_FG, bg=theme.control_bg, attr=base.attr, font=code_font)
    return Style(fg=base.fg, bg=base.bg, attr=base.attr, font=text_font)


def _run_style(run_roles: frozenset, base: Style, theme: Theme, code_font: Font) -> Style:
    """Layer inline roles onto a block's base style. An inline ``code`` run
    swaps to the monospace ``code_font`` over the block's proportional one."""
    fg, bg, attr, font = base.fg, base.bg, base.attr, base.font
    if "bold" in run_roles:
        attr |= TextAttribute.BOLD
    if "italic" in run_roles:
        attr |= TextAttribute.ITALIC
    if "code" in run_roles:
        fg, bg, font = _CODE_FG, theme.control_bg, code_font
    if "link" in run_roles:
        fg, attr = theme.accent, attr | TextAttribute.UNDERLINE
    return Style(fg=fg, bg=bg, attr=attr, font=font)


# --- span wrapping (style-preserving) -----------------------------------------


def _merge_cells(cells: list[tuple[str, Style]]) -> list[Span]:
    """Collapse a run of (glyph, style) cells into spans, joining neighbors that
    share a style so the row draws in as few ``draw_text`` calls as possible."""
    spans: list[Span] = []
    for glyph, style in cells:
        if spans and spans[-1][1] == style:
            spans[-1] = (spans[-1][0] + glyph, style)
        else:
            spans.append((glyph, style))
    return spans


def _measure_cells(cells: list[tuple[str, Style]], measure) -> float:
    """Width of a run of (glyph, style) cells, measuring each maximal same-style
    sub-run as a unit (so proportional kerning within a run is honored)."""
    return sum(measure(text, style) for text, style in _merge_cells(cells))


def _wrap_spans(spans: list[Span], width: float, measure, *, word: bool) -> list[list[Span]]:
    """Word-wrap a styled line to ``width`` base units, preserving each glyph's
    style across the break. ``measure(text, style)`` reports a fragment's width
    in base units (a column count on the grid, native metrics for a proportional
    font), so the same wrap follows both. ``word=False`` breaks between glyphs
    regardless (the fenced-code path), so no content is lost to a long token."""
    cells: list[tuple[str, Style]] = [
        (g, st) for text, st in spans for g in glyph_runs(text)
    ]
    if width <= 0 or not cells:
        return [_merge_cells(cells)] if cells else [[]]
    rows: list[list[tuple[str, Style]]] = []
    cur: list[tuple[str, Style]] = []
    cur_w = 0.0

    def hard_break(token: list[tuple[str, Style]]) -> None:
        nonlocal cur, cur_w
        for cell in token:
            w = measure(cell[0], cell[1])
            if cur and cur_w + w > width:
                rows.append(cur)
                cur, cur_w = [], 0.0
            cur.append(cell)
            cur_w += w

    if not word:
        hard_break(cells)
        if cur:
            rows.append(cur)
        return [_merge_cells(r) for r in rows]

    i, n = 0, len(cells)
    while i < n:
        is_space = cells[i][0].isspace()
        j = i
        while j < n and cells[j][0].isspace() == is_space:
            j += 1
        token = cells[i:j]
        i = j
        tok_w = _measure_cells(token, measure)
        if cur_w + tok_w <= width:
            cur.extend(token)
            cur_w += tok_w
            continue
        if cur:
            rows.append(cur)
            cur, cur_w = [], 0.0
        if is_space:
            continue  # whitespace at a wrap boundary is dropped
        if tok_w <= width:
            cur, cur_w = list(token), tok_w
        else:
            hard_break(token)
    if cur:
        rows.append(cur)
    return [_merge_cells(r) for r in rows] or [[]]


class MarkdownView(Widget):
    focusable = True

    def __init__(
        self,
        source: str = "",
        style: Style = DEFAULT_STYLE,
        text_font: Font = DEFAULT_TEXT_FONT,
        code_font: Font = DEFAULT_CODE_FONT,
    ):
        self.style = style
        # Prose face (proportional on GUI) and code face (monospace). Both fold
        # to the single grid font on a terminal; bold/italic survive as attrs.
        self.text_font = text_font
        self.code_font = code_font
        self._sems: list[_SemLine] = parse_markdown(source)

        # Top of the viewport, in base units. A display row is self._pitch base
        # units tall (1.0 for the grid font, more for a taller proportional one),
        # so the virtualization is a uniform multiply like LogView.
        self.offset: float = 0.0
        self._pitch: float = 1.0

        # Wrap cache: each row is (x0, spans) — the x of its first span in base
        # units (a hanging indent under a list marker / quote bar) and the styled
        # fragments. Valid only at self._wrap_width; rebuilt on a resize.
        self._rows: list[tuple[float, list[Span]]] | None = None
        self._wrap_width: float = -1.0
        self._view_h: float = 1.0

    # --- construction ---------------------------------------------------------

    @classmethod
    def from_file(
        cls,
        path: str,
        style: Style = DEFAULT_STYLE,
        text_font: Font = DEFAULT_TEXT_FONT,
        code_font: Font = DEFAULT_CODE_FONT,
    ) -> "MarkdownView":
        """Build a view from a ``*.md`` file (read as UTF-8)."""
        with open(path, encoding="utf-8") as f:
            return cls(f.read(), style=style, text_font=text_font, code_font=code_font)

    def set_source(self, source: str) -> None:
        self._sems = parse_markdown(source)
        self.offset = 0.0
        self._rows = None
        self._wrap_width = -1.0

    # --- layout ---------------------------------------------------------------

    def _layout(self, width: float, ctx: DrawContext) -> list[tuple[float, list[Span]]]:
        """Wrap every semantic line to ``width`` base units, returning the flat
        list of (x0, spans) display rows. Wrapping and the row pitch both go
        through ``ctx`` so a proportional face measures and spaces correctly.
        Rebuilt only when the width (or source) changes."""
        if self._rows is not None and self._wrap_width == width:
            return self._rows
        theme = ctx.theme or DEFAULT_THEME
        measure = ctx.measure_text
        # Uniform row pitch: the taller of the prose and code faces, so neither
        # overlaps the next row. The grid font reports 1.0, so a terminal is
        # unchanged.
        self._pitch = max(
            ctx.line_height(Style(font=self.text_font)),
            ctx.line_height(Style(font=self.code_font)),
        )
        rows: list[tuple[float, list[Span]]] = []
        for sem in self._sems:
            if sem.block == "blank":
                rows.append((0.0, []))
                continue
            if sem.block == "rule":
                # A grid (font=None) rule spans the pane exactly: one ─ per unit.
                rows.append((0.0, [("─" * max(1, int(width)), Style(fg=theme.muted_text))]))
                continue
            base = _block_style(
                sem.block, sem.level, self.style, theme, self.text_font, self.code_font
            )
            spans = [(text, _run_style(roles, base, theme, self.code_font)) for text, roles in sem.runs]
            prefix_style = Style(fg=theme.muted_text) if sem.block == "quote" else base
            prefix_w = measure(sem.prefix, prefix_style) if sem.prefix else 0.0
            avail = max(1.0, width - prefix_w)
            wrapped = _wrap_spans(spans, avail, measure, word=sem.wrap)
            for k, row in enumerate(wrapped):
                if k == 0 and sem.prefix:
                    rows.append((0.0, [(sem.prefix, prefix_style)] + row))
                else:
                    # Continuation rows hang under the text, indented by the
                    # measured prefix width (exact on every backend).
                    rows.append((prefix_w, row))
        self._rows = rows
        self._wrap_width = width
        return rows

    # --- drawing --------------------------------------------------------------

    def draw(self, ctx: DrawContext) -> None:
        # Use the exact (fractional) width on pixel backends so proportional text
        # wraps to the real pane edge, not a column-snapped one.
        full_w, view_h = ctx.size_units
        self._view_h = view_h

        # Whether the bar shows depends on the row count, which depends on the
        # wrap width, which depends on whether the bar shows. Lay out once at the
        # full width to learn if the content overflows, then (only if it does)
        # re-lay-out one base unit narrower to make room for the bar.
        rows = self._layout(full_w, ctx)
        content_h = len(rows) * self._pitch
        show_bar = content_h > view_h
        if show_bar:
            rows = self._layout(full_w - 1.0, ctx)
            content_h = len(rows) * self._pitch
        self._clamp(content_h)

        pitch = self._pitch
        first = int(self.offset / pitch)
        frac = self.offset - first * pitch
        r = 0
        while True:
            index = first + r
            y = r * pitch - frac
            if y >= view_h or index >= len(rows):
                break
            if index >= 0:
                x, spans = rows[index]
                for text, style in spans:
                    if text:
                        ctx.draw_text(x, y, text, style)
                        x += ctx.measure_text(text, style)
            r += 1

        if show_bar:
            ratio = view_h / content_h
            denom = content_h - view_h
            pos = self.offset / denom if denom > 0 else 0.0
            ctx.draw_scrollbar(ctx.width - 1, 0, view_h, max(0.0, min(1.0, pos)), ratio, self.style)

    # --- scrolling ------------------------------------------------------------

    def _content_h(self) -> float:
        return len(self._rows) * self._pitch if self._rows is not None else 0.0

    def _clamp(self, content_h: float) -> None:
        self.offset = max(0.0, min(self.offset, max(0.0, content_h - self._view_h)))

    def scroll_by(self, amount: float) -> None:
        self.offset += amount
        self._clamp(self._content_h())

    # --- events ---------------------------------------------------------------

    def handle_event(self, event: Event) -> bool:
        if event.type is EventType.MOUSE_SCROLL:
            amount = event.hints.get("scroll_units")
            if amount is None:
                amount = float(event.scroll)
            self.scroll_by(-amount)
            return True
        if event.type is EventType.KEY:
            return self._handle_key(event.key)
        return False

    def _handle_key(self, key: str | None) -> bool:
        if key == "up":
            self.scroll_by(-1)
        elif key == "down":
            self.scroll_by(1)
        elif key == "pageup":
            self.scroll_by(-self._view_h)
        elif key == "pagedown":
            self.scroll_by(self._view_h)
        elif key == "home":
            self.offset = 0.0
        elif key == "end":
            self.offset = max(0.0, self._content_h() - self._view_h)
        else:
            return False
        return True
