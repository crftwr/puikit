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
  spans) a **monospace** ``Font``; headings carry a larger size per level
  (``#`` biggest), set as a **multiple of the body size** so a larger or smaller
  body face scales the headings with it. On ``fonts``-capable backends these render as real
  proportional / fixed-advance faces at their sizes, and on a terminal they all
  fold back to the one grid font (bold / italic preserved as attributes, size
  dropped) — one implementation, every backend, no per-row branch.

Because prose is proportional and headings are sized, wrapping measures runs
through ``DrawContext.measure_text`` (not a column count) and **each display row
keeps its own height** from ``DrawContext.line_height`` — a big heading reserves
its taller row without spacing the body to match. The view is **virtualized**
over those heights: rows carry a cumulative-top index so a draw binary-searches
to the first visible row and stops at the pane edge, so a long document scrolls
cheaply. Navigation is pure scrolling (a document has no "current item"):
arrows / page keys / home / end move the viewport and the mouse wheel scrolls.

The Markdown subset is deliberately small but covers the common cases: ATX
headings (``#``..``######``), paragraphs, ``-``/``*``/``+`` and ordered list
items (nested by indentation), ``>`` block quotes, ``` ``` ``` / ``~~~`` fenced
code, ``---`` horizontal rules, and the inline runs ``**bold**``, ``*italic*`` /
``_italic_``, ```` `code` ````, ``[text](url)`` links, and backslash escapes.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field, replace

from ..backend import DEFAULT_STYLE, Style, TextAttribute
from ..event import Event, EventType
from ..font import Font
from ..image import aspect_extent
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

# Heading sizes per level, as MULTIPLES of the body font size (## smaller than
# #, down to 1.0 = body size at level 6). Kept relative, not absolute points, so
# a document with a larger or smaller body face scales its headings to match;
# the absolute point size is body_size x scale, resolved per backend at layout.
# Visual only — a font size never reshapes the layout, it just draws bigger and
# reserves a taller row (docs/font_system.md §7). Dropped on a terminal, where
# every heading is simply bold. Override via MarkdownView(heading_scales=...).
DEFAULT_HEADING_SCALES = {1: 2.0, 2: 1.6, 3: 1.3, 4: 1.15, 5: 1.07, 6: 1.0}

# A drawn fragment: text, the style it draws in, and an optional hyperlink
# target (a clickable link span carries its URL; everything else is None).
Span = tuple[str, Style, "str | None"]
# An inline run as parsed: text, the set of semantic roles on it, and the link
# URL if the run is (inside) a ``[text](url)`` link.
InlineRun = tuple[str, frozenset, "str | None"]

# Inline code reads as a distinct face independent of the surface; the body fg
# is theme-driven but code keeps a stable warm tint over a panel-ish fill, the
# same convention every editor uses for a code span.
_CODE_FG = (206, 145, 120)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_HRULE_RE = re.compile(r"^(?:-{3,}|\*{3,}|_{3,})$")
_LIST_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
_FENCE_RE = re.compile(r"^\s*(```+|~~~+)")
_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]*)(?:\s+\"[^\"]*\")?\)")
# A standalone image line: ![alt](path). Rendered as its own block, sized to
# the image's aspect ratio (the inline form mid-paragraph is intentionally not
# supported — an image is a block here).
_IMAGE_RE = re.compile(r"^!\[([^\]]*)\]\(([^)\s]*)(?:\s+\"[^\"]*\")?\)$")


@dataclass
class _SemLine:
    """One semantic line of the document: its block kind, an optional rendered
    prefix (a list marker, a quote bar), and its inline runs (role-tagged, not
    yet colored). ``wrap`` is False for atoms that must stay on one row (a fenced
    code line, a rule, a blank spacer). ``data`` carries the image path for an
    ``image`` block."""

    block: str  # heading | para | list | quote | code | rule | blank | image
    level: int  # heading level, or list nesting depth
    prefix: str  # rendered indent / marker, drawn before the inline runs
    runs: list[InlineRun] = field(default_factory=list)
    wrap: bool = True
    data: str | None = None


@dataclass
class _Row:
    """One laid-out display row. ``x0`` is the base-unit x of its first span (a
    hanging indent under a list marker / quote bar); ``height`` is the row's own
    height (a sized heading or an image is taller than body text). A text row
    carries ``spans``; an ``image`` row carries ``(path, alt, w, h)`` instead and
    is drawn with ``draw_image``."""

    x0: float
    spans: list[Span]
    height: float
    image: tuple[str, str | None, float, float] | None = None


# --- inline parsing -----------------------------------------------------------


def _scan_inline(text: str, roles: frozenset, href: str | None = None) -> list[InlineRun]:
    """Split ``text`` into role-tagged runs, recursing so emphasis nests
    (``**bold _and italic_**``). ``roles`` is the set already in force from the
    enclosing scan; ``href`` is the link URL when scanning inside a link."""
    out: list[InlineRun] = []
    buf: list[str] = []
    i, n = 0, len(text)

    def flush() -> None:
        if buf:
            out.append(("".join(buf), roles, href))
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
                out.append((text[k:close], roles | {"code"}, href))
                i = close + len(fence)
                continue
            buf.append(c)
            i += 1
            continue
        if c == "[":
            m = _LINK_RE.match(text, i)
            if m:
                flush()
                # Carry the link's URL into every run of its (still inline-parsed)
                # label, so a click anywhere on the label opens it.
                out.extend(_scan_inline(m.group(1), roles | {"link"}, m.group(2)))
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
                out.extend(_scan_inline(text[k:close], roles | added, href))
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
                out.append(_SemLine("code", 0, "", [(lines[i], frozenset(), None)], wrap=False))
                i += 1
            i += 1  # consume the closing fence (or run off the end)
            continue

        if stripped == "":
            out.append(_SemLine("blank", 0, "", [], wrap=False))
            i += 1
            continue

        img = _IMAGE_RE.match(stripped)
        if img:
            alt, path = img.group(1), img.group(2)
            out.append(
                _SemLine("image", 0, "", [(alt, frozenset(), None)], wrap=False, data=path)
            )
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
    block: str,
    level: int,
    base: Style,
    theme: Theme,
    text_font: Font,
    code_font: Font,
    heading_scales: dict[int, float],
    body_size: float,
) -> Style:
    """Base style for a whole semantic line, before inline roles are layered.
    Prose blocks carry the proportional ``text_font``; a heading carries it at
    its per-level point size (``body_size`` x the level's scale); a fenced code
    block carries the monospace ``code_font``."""
    if block == "heading":
        # A heading reads as a heading by weight and size alone — same color as
        # the body, no underline. On a terminal the size folds away, leaving the
        # bold that still distinguishes it. The size is relative to the body, so
        # a larger/smaller body face scales every heading with it.
        attr = base.attr | TextAttribute.BOLD
        scale = heading_scales.get(level)
        font = replace(text_font, size=body_size * scale) if scale is not None else text_font
        return Style(fg=base.fg, bg=base.bg, attr=attr, font=font)
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


def _merge_cells(cells: list[Span]) -> list[Span]:
    """Collapse a run of (glyph, style, href) cells into spans, joining neighbors
    that share a style *and* href so the row draws in as few ``draw_text`` calls
    as possible (and a link stays one contiguous hit region)."""
    spans: list[Span] = []
    for glyph, style, href in cells:
        if spans and spans[-1][1] == style and spans[-1][2] == href:
            spans[-1] = (spans[-1][0] + glyph, style, href)
        else:
            spans.append((glyph, style, href))
    return spans


def _measure_cells(cells: list[Span], measure) -> float:
    """Width of a run of (glyph, style, href) cells, measuring each maximal
    same-style sub-run as a unit (so proportional kerning within a run is honored)."""
    return sum(measure(text, style) for text, style, _ in _merge_cells(cells))


def _wrap_spans(spans: list[Span], width: float, measure, *, word: bool) -> list[list[Span]]:
    """Word-wrap a styled line to ``width`` base units, preserving each glyph's
    style (and link href) across the break. ``measure(text, style)`` reports a
    fragment's width in base units (a column count on the grid, native metrics
    for a proportional font), so the same wrap follows both. ``word=False`` breaks
    between glyphs regardless (the fenced-code path), so no content is lost."""
    cells: list[Span] = [
        (g, st, href) for text, st, href in spans for g in glyph_runs(text)
    ]
    if width <= 0 or not cells:
        return [_merge_cells(cells)] if cells else [[]]
    rows: list[list[Span]] = []
    cur: list[Span] = []
    cur_w = 0.0

    def hard_break(token: list[Span]) -> None:
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
        heading_scales: dict[int, float] = DEFAULT_HEADING_SCALES,
    ):
        self.style = style
        # Prose face (proportional on GUI) and code face (monospace). Both fold
        # to the single grid font on a terminal; bold/italic survive as attrs.
        self.text_font = text_font
        self.code_font = code_font
        # Heading size per level, as a multiple of the body font size (visual
        # only; dropped on a terminal). Resolved to absolute points at layout.
        self.heading_scales = heading_scales
        self._sems: list[_SemLine] = parse_markdown(source)

        # Top of the viewport, in base units.
        self.offset: float = 0.0
        # Row pitch of the body face, the unit a line-scroll moves by (1.0 on a
        # terminal). Set each layout.
        self._line_pitch: float = 1.0

        # Wrap cache: a list of _Row. _row_tops[i] is the cumulative top of row i
        # (in base units); _row_tops[-1] is the total content height. Valid only
        # at _wrap_width; rebuilt on a resize.
        self._rows: list[_Row] | None = None
        self._row_tops: list[float] = [0.0]
        self._wrap_width: float = -1.0
        # Image height is capped to the viewport, so the layout also depends on
        # the view height; cache on both to re-contain images on a vertical resize.
        self._wrap_view_h: float = -1.0
        self._view_h: float = 1.0

        # Click targets from the last draw: (x0, y0, x1, y1, url) in widget-local
        # base units, one per visible link span. The Panel is kept to open a URL.
        self._link_hits: list[tuple[float, float, float, float, str]] = []
        self._panel = None

    # --- construction ---------------------------------------------------------

    @classmethod
    def from_file(
        cls,
        path: str,
        style: Style = DEFAULT_STYLE,
        text_font: Font = DEFAULT_TEXT_FONT,
        code_font: Font = DEFAULT_CODE_FONT,
        heading_scales: dict[int, float] = DEFAULT_HEADING_SCALES,
    ) -> "MarkdownView":
        """Build a view from a ``*.md`` file (read as UTF-8)."""
        with open(path, encoding="utf-8") as f:
            return cls(
                f.read(),
                style=style,
                text_font=text_font,
                code_font=code_font,
                heading_scales=heading_scales,
            )

    def set_source(self, source: str) -> None:
        self._sems = parse_markdown(source)
        self.offset = 0.0
        self._rows = None
        self._wrap_width = -1.0
        self._wrap_view_h = -1.0

    # --- layout ---------------------------------------------------------------

    def _layout(self, width: float, ctx: DrawContext) -> list[_Row]:
        """Wrap every semantic line to ``width`` base units, returning the flat
        list of ``_Row`` and rebuilding the cumulative ``_row_tops`` index.
        Wrapping and every row height go through ``ctx`` so a proportional / sized
        face measures and spaces correctly, and an image is sized to its aspect
        ratio. Rebuilt only when the width, view height (image cap), or source
        changes."""
        if (
            self._rows is not None
            and self._wrap_width == width
            and self._wrap_view_h == self._view_h
        ):
            return self._rows
        theme = ctx.theme or DEFAULT_THEME
        measure = ctx.measure_text
        # Row height is the line height of the row's font(s); cache per font so a
        # long document does not re-resolve the same face per row. The grid font
        # reports 1.0, so a terminal stays one unit per row.
        lh_cache: dict[Font | None, float] = {}
        lc = None  # LayoutContext, built lazily for image sizing

        def line_height(style: Style) -> float:
            font = style.font
            if font not in lh_cache:
                lh_cache[font] = ctx.line_height(style)
            return lh_cache[font]

        self._line_pitch = line_height(Style(font=self.text_font))
        # Body face point size, the size headings scale off of. The backend owns
        # the absolute value (the base size when text_font names none); the
        # widget keeps only the per-level ratio (DEFAULT_HEADING_SCALES).
        body_size = ctx.font_size(Style(font=self.text_font))
        rows: list[_Row] = []
        for sem in self._sems:
            if sem.block == "blank":
                rows.append(_Row(0.0, [], self._line_pitch))
                continue
            if sem.block == "rule":
                # A grid (font=None) rule spans the pane exactly: one ─ per unit.
                style = Style(fg=theme.muted_text)
                rows.append(_Row(0.0, [("─" * max(1, int(width)), style, None)], self._line_pitch))
                continue
            if sem.block == "image":
                if lc is None:
                    lc = ctx.layout_context()
                size = lc.measure_image(sem.data) if sem.data else None
                alt = sem.runs[0][0] if sem.runs else None
                if size and size[0] > 0 and size[1] > 0:
                    w = width
                    h = aspect_extent(w, True, size[0], size[1], lc.base_w, lc.base_h)
                    # An image taller than the viewport is contained to fit it
                    # (aspect kept, width shrunk, centered), so it never reserves
                    # more than one screenful of blank scroll.
                    if self._view_h > 0 and h > self._view_h:
                        w *= self._view_h / h
                        h = self._view_h
                    x0 = max(0.0, (width - w) / 2.0)
                else:
                    # Unknown / unreadable image: reserve one line and let the
                    # backend draw the alt glyph (TUI) or its own missing-image.
                    w, h, x0 = width, self._line_pitch, 0.0
                rows.append(_Row(x0, [], h, image=(sem.data, alt, w, h)))
                continue
            base = _block_style(
                sem.block, sem.level, self.style, theme,
                self.text_font, self.code_font, self.heading_scales, body_size,
            )
            spans = [
                (text, _run_style(roles, base, theme, self.code_font), href)
                for text, roles, href in sem.runs
            ]
            prefix_style = Style(fg=theme.muted_text) if sem.block == "quote" else base
            prefix_w = measure(sem.prefix, prefix_style) if sem.prefix else 0.0
            avail = max(1.0, width - prefix_w)
            wrapped = _wrap_spans(spans, avail, measure, word=sem.wrap)
            for k, row in enumerate(wrapped):
                if k == 0 and sem.prefix:
                    cells = [(sem.prefix, prefix_style, None)] + row
                    x0 = 0.0
                else:
                    # Continuation rows hang under the text, indented by the
                    # measured prefix width (exact on every backend).
                    cells = row
                    x0 = prefix_w
                height = max((line_height(st) for _, st, _ in cells), default=line_height(base))
                rows.append(_Row(x0, cells, height))
        self._rows = rows
        tops = [0.0]
        for row in rows:
            tops.append(tops[-1] + row.height)
        self._row_tops = tops
        self._wrap_width = width
        self._wrap_view_h = self._view_h
        return rows

    # --- drawing --------------------------------------------------------------

    def draw(self, ctx: DrawContext) -> None:
        self._panel = ctx.panel
        self._link_hits = []
        # Use the exact (fractional) width on pixel backends so proportional text
        # wraps to the real pane edge, not a column-snapped one.
        full_w, view_h = ctx.size_units
        self._view_h = view_h

        # Whether the bar shows depends on the content height, which depends on
        # the wrap width, which depends on whether the bar shows. Lay out once at
        # the full width to learn if the content overflows, then (only if it
        # does) re-lay-out one base unit narrower to make room for the bar.
        rows = self._layout(full_w, ctx)
        content_h = self._row_tops[-1]
        show_bar = content_h > view_h
        if show_bar:
            rows = self._layout(full_w - 1.0, ctx)
            content_h = self._row_tops[-1]
        self._clamp(content_h)

        # Binary-search the cumulative tops for the first row whose bottom is
        # below the viewport top, then draw down until a row starts past the
        # bottom edge (the clip trims the partial rows at either end).
        first = max(0, bisect.bisect_right(self._row_tops, self.offset) - 1)
        for index in range(first, len(rows)):
            row = rows[index]
            y = self._row_tops[index] - self.offset
            if y >= view_h:
                break
            if row.image is not None:
                path, alt, w, h = row.image
                ctx.draw_image(row.x0, y, path, hints={"w": w, "h": h, "fit": "contain", "alt": alt})
                continue
            x = row.x0
            for text, style, href in row.spans:
                if not text:
                    continue
                ctx.draw_text(x, y, text, style)
                w = ctx.measure_text(text, style)
                if href:
                    self._link_hits.append((x, y, x + w, y + row.height, href))
                x += w

        if show_bar:
            ratio = view_h / content_h
            denom = content_h - view_h
            pos = self.offset / denom if denom > 0 else 0.0
            # Fractional width keeps the bar flush to the right edge at pixel
            # granularity; ctx.width is truncated to whole base units.
            ctx.draw_scrollbar(
                ctx.size_units[0] - 1, 0, view_h, max(0.0, min(1.0, pos)), ratio, self.style
            )

    # --- scrolling ------------------------------------------------------------

    def _content_h(self) -> float:
        return self._row_tops[-1]

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
        if event.type is EventType.MOUSE_CLICK:
            return self._handle_click(event)
        if event.type is EventType.KEY:
            return self._handle_key(event.key)
        return False

    def _handle_click(self, event: Event) -> bool:
        """Open the link under the click, if any. The Panel resolves the open:
        ``os_open`` backends launch the OS handler, others copy the URL to the
        clipboard — the widget never branches."""
        if event.x is None or event.y is None:
            return False
        for x0, y0, x1, y1, url in self._link_hits:
            if x0 <= event.x < x1 and y0 <= event.y < y1:
                if self._panel is not None:
                    self._panel.open_url(url)
                return True
        return False

    def _handle_key(self, key: str | None) -> bool:
        if key == "up":
            self.scroll_by(-self._line_pitch)
        elif key == "down":
            self.scroll_by(self._line_pitch)
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
