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

For a host that wants find-in-document, the ``search_*`` methods drive an
incremental search over the rendered text — every occurrence of the pattern is
highlighted in place (each styled run keeps its own color under the tint), the
matching display rows form the navigable set, and scrolling brings the current
match into view. The host owns the search-input UI (a text field / bar) and
calls in; match finding, scrolling and highlighting stay here because they need
the widget's own wrapped, proportional layout.

The Markdown subset covers the common GitHub-flavored cases: ATX headings
(``#``..``######``) and setext (``===`` / ``---`` underline) headings,
paragraphs with hard line breaks (two trailing spaces or a backslash),
``-``/``*``/``+`` and ordered list items (nested by indentation) including
``[ ]`` / ``[x]`` task items, ``>`` block quotes (nested and multi-line,
reflowed), ``` ``` ``` /
``~~~`` fenced code, ``---`` horizontal rules, GFM pipe ``| tables |``, block
images, and the inline runs ``**bold**``, ``*italic*`` / ``_italic_``,
``~~strikethrough~~``, ```` `code` ````, ``[text](url)`` / ``[text][ref]``
reference links, ``<autolinks>`` and bare URLs, and backslash escapes. A
``[jump](#heading)`` link scrolls the view to that heading.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field, replace

from ..backend import DEFAULT_STYLE, Color, Style, TextAttribute
from ..event import Event, EventType
from ..font import Font
from ..image import aspect_extent
from ..panel import DrawContext
from ..text import glyph_runs
from ..theme import DEFAULT_THEME, Theme, lift
from .base import Widget

# Syntax highlighting is optional: if Pygments is installed a fenced code block
# with a language tag (``` ```python ```) is colored; without it (or for an
# unknown language) the block still renders, just in the one flat code color.
try:  # pragma: no cover - import guard
    from pygments.lexers import get_lexer_by_name

    _PYGMENTS = True
except Exception:  # pragma: no cover
    _PYGMENTS = False

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

# Left indent reserved per block-quote nesting level, in base units: one column
# for the vertical bar plus a space of gutter before the quoted content.
_QUOTE_INDENT = 2.0
# Horizontal padding inside a table cell, and the width reserved for each
# vertical border column, both in base units (a border is one grid column so the
# ``│`` has a cell of its own and never lands on top of text).
_TABLE_PAD = 1.0
_TABLE_BORDER = 1.0
# Inner left/right padding of a fenced code block, in base units, so the text
# does not sit flush against the edge of its filled background.
_CODE_PAD = 1.0

# Syntax palette (VS Code Dark+ hues, tuned to read on the code block's fill) and
# the neutral default when a token has no category. Categories mirror the seven
# TFM uses in its Pygments text viewer (``get_syntax_color`` in ``tfm_colors``):
# keyword / string / comment / number / operator / builtin / name.
_SYNTAX_DEFAULT_FG: Color = (212, 212, 212)
_SYNTAX: dict[str, Color] = {
    "keyword": (86, 156, 214),    # blue
    "string": (206, 145, 120),    # orange-brown
    "comment": (106, 153, 85),    # green
    "number": (181, 206, 168),    # light green
    "operator": (212, 212, 212),  # default gray (VS Code leaves these neutral)
    "builtin": (78, 201, 176),    # teal
    "name": (156, 220, 254),      # light blue (identifiers)
}


def _syntax_color(token_type) -> Color | None:
    """Map a Pygments token type to a palette color, by the same substring test
    TFM's viewer uses so the two stay visually in step. None → the block default."""
    s = str(token_type)
    if "Keyword" in s:
        return _SYNTAX["keyword"]
    if "String" in s:
        return _SYNTAX["string"]
    if "Comment" in s:
        return _SYNTAX["comment"]
    if "Number" in s:
        return _SYNTAX["number"]
    if "Operator" in s or "Punctuation" in s:
        return _SYNTAX["operator"]
    if "Builtin" in s:
        return _SYNTAX["builtin"]
    if "Name" in s:
        return _SYNTAX["name"]
    return None


def _highlight(lines: list[str], lang: str) -> list[list[tuple[str, "Color | None"]]] | None:
    """Tokenize ``lines`` as ``lang`` with Pygments, returning per-source-line
    ``(text, color)`` segments (color None = block default). Returns None when
    Pygments is absent or the language is unknown, so the caller falls back to
    plain code. One row per input line is preserved so wrapping stays line-based."""
    if not _PYGMENTS or not lang:
        return None
    try:
        lexer = get_lexer_by_name(lang)
    except Exception:
        return None
    out: list[list[tuple[str, Color | None]]] = [[]]
    try:
        tokens = lexer.get_tokens("\n".join(lines))
    except Exception:
        return None
    for token_type, text in tokens:
        color = _syntax_color(token_type)
        parts = text.split("\n")
        for k, part in enumerate(parts):
            if k > 0:
                out.append([])
            if part:
                out[-1].append((part, color))
    # Pygments appends a trailing newline; keep exactly one row per source line.
    if len(out) > len(lines):
        out = out[: len(lines)]
    while len(out) < len(lines):
        out.append([])
    return out

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_HRULE_RE = re.compile(r"^(?:-{3,}|\*{3,}|_{3,})$")
_LIST_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
_FENCE_RE = re.compile(r"^\s*(```+|~~~+)")
_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]*)(?:\s+\"[^\"]*\")?\)")
# A standalone image line: ![alt](path). Rendered as its own block, sized to
# the image's aspect ratio (the inline form mid-paragraph is intentionally not
# supported — an image is a block here).
_IMAGE_RE = re.compile(r"^!\[([^\]]*)\]\(([^)\s]*)(?:\s+\"[^\"]*\")?\)$")
# A setext underline turns the paragraph line(s) above it into a heading: a run
# of ``=`` is level 1, a run of ``-`` is level 2. Only reached with paragraph
# text above (a bare ``---`` at a block start is a horizontal rule instead).
_SETEXT_RE = re.compile(r"^\s{0,3}(=+|-+)\s*$")
# A GitHub task-list marker at the head of a list item's text: ``[ ]`` / ``[x]``.
_TASK_RE = re.compile(r"^\[([ xX])\]\s+(.*)$")
# A link reference definition line: ``[label]: url "optional title"``. Collected
# in a pre-pass so a ``[text][label]`` / ``[label]`` reference resolves even when
# the definition appears later in the document.
_REF_DEF_RE = re.compile(r'^ {0,3}\[([^\]]+)\]:\s*<?([^>\s]+)>?(?:\s+["\'(].*)?$')
# An angle-bracket autolink: an absolute ``<scheme:...>`` URI or a bare
# ``<user@host>`` email (the latter opens as ``mailto:``).
_AUTOLINK_RE = re.compile(
    r"<((?:[a-zA-Z][a-zA-Z0-9+.\-]*:[^<>\s]+)|(?:[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+))>"
)
# A bare URL written inline (GFM autolink extension), linkified where it starts
# at a word boundary. Trailing sentence punctuation is trimmed by ``_trim_url``.
_BARE_URL_RE = re.compile(r"https?://[^\s<>]+")
_SCHEME_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9+.\-]*:")
# GFM pipe-table delimiter row (``| --- | :--: |``): every cell is dashes with
# optional leading/trailing ``:`` for alignment. Presence of this row on the
# line below a header row is what marks a block as a table.
_TABLE_DELIM_RE = re.compile(r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$")


@dataclass
class _Table:
    """A parsed GFM pipe table: each header/body cell is a list of inline runs,
    and ``aligns`` is one of ``left`` / ``center`` / ``right`` per column."""

    header: list[list[InlineRun]]
    aligns: list[str]
    rows: list[list[list[InlineRun]]]


@dataclass
class _SemLine:
    """One semantic line of the document: its block kind, an optional rendered
    prefix (a list marker), and its inline runs (role-tagged, not yet colored).
    ``wrap`` is False for atoms that must stay on one row (a fenced code line, a
    rule, a blank spacer). ``data`` carries the image path for an ``image``
    block, ``checked`` the state of a ``[ ]`` / ``[x]`` task item (else None),
    ``quote_depth`` the block-quote nesting level (0 = not quoted), and ``table``
    the parsed grid for a ``table`` block."""

    block: str  # heading | para | list | code | rule | blank | image | table
    level: int  # heading level, or list nesting depth
    prefix: str  # rendered indent / marker, drawn before the inline runs
    runs: list[InlineRun] = field(default_factory=list)
    wrap: bool = True
    data: str | None = None
    checked: bool | None = None
    quote_depth: int = 0
    table: _Table | None = None


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
    # Structural lines a vector backend strokes as real hairlines instead of box
    # glyphs: ``rule`` = a full-width horizontal rule; ``quote`` = the count of
    # left-margin vertical bars (block-quote nesting depth, 0 = none). Both stay
    # absent on a grid backend only for width — the glyphs still draw there.
    rule: bool = False
    quote: int = 0
    # A ``table`` block lays out as one ``_Row`` per grid row carrying this
    # payload instead of ``spans``; ``_draw_table_row`` renders its cells + rules.
    table: "_TableRow | None" = None
    # A fenced-code row: the fill color painted across the whole block width
    # behind the text (a continuous background, not just per-glyph), else None.
    code_bg: Color | None = None


@dataclass
class _TableRow:
    """A laid-out table row. A ``hline`` row is a pure horizontal border whose
    ``role`` (``top`` / ``mid`` / ``bottom``) picks the box-drawing junctions where
    the column bars cross it; a text row carries per-cell ``(x, width, align,
    wrapped_lines)`` and a vertical bar at each of the shared border ``edges``.
    Keeping horizontals and verticals on separate grid rows means the ``│`` from a
    text row sits directly above/below the ``┼`` of a border row and connects."""

    cells: list[tuple[float, float, str, list[list[Span]]]]
    edges: list[float]
    hline: bool = False
    role: str = ""
    pad_v: float = 0.0  # inner top/bottom margin so text clears the rules (vector)
    bg: Color | None = None  # row band fill (header / zebra stripe), else surface


# Box-drawing glyph for a junction by which of its four arms carry a line. Used
# to stroke a connected table grid on a character backend (a vector backend just
# overlaps real strokes, so it needs none of these).
_BOX = {
    (0, 1, 0, 1): "┌", (0, 1, 1, 0): "┐", (1, 0, 0, 1): "└", (1, 0, 1, 0): "┘",
    (1, 1, 0, 1): "├", (1, 1, 1, 0): "┤", (0, 1, 1, 1): "┬", (1, 0, 1, 1): "┴",
    (1, 1, 1, 1): "┼", (0, 0, 1, 1): "─", (1, 1, 0, 0): "│",
}


def _box_glyph(up: bool, down: bool, left: bool, right: bool) -> str:
    return _BOX.get((int(up), int(down), int(left), int(right)), "─")


# --- inline parsing -----------------------------------------------------------


def _trim_url(url: str) -> str:
    """Trim trailing sentence punctuation a bare URL scooped up (``see http://x.``
    → ``http://x``), keeping a ``)`` only while parens stay balanced (so a URL
    with a real trailing paren survives)."""
    while url and url[-1] in ".,;:!?)":
        if url[-1] == ")" and url.count("(") >= url.count(")"):
            break
        url = url[:-1]
    return url


def _match_bracket(text: str, i: int) -> int:
    """Index just past the ``]`` matching the ``[`` at ``i`` (respecting nesting
    and backslash escapes), or ``-1`` if unbalanced."""
    depth, j, n = 0, i, len(text)
    while j < n:
        if text[j] == "\\":
            j += 2
            continue
        if text[j] == "[":
            depth += 1
        elif text[j] == "]":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return -1


def _resolve_reflink(text: str, i: int, refs: dict[str, str]) -> tuple[str, str, int] | None:
    """Resolve a reference link at ``[`` position ``i`` — full ``[text][label]``,
    collapsed ``[text][]``, or shortcut ``[label]`` — to ``(label_text, url,
    end)`` using ``refs``, or None when it is not a (defined) reference."""
    close = _match_bracket(text, i)
    if close < 0:
        return None
    label_text = text[i + 1 : close - 1]
    n = len(text)
    if close < n and text[close] == "[":  # full or collapsed
        close2 = _match_bracket(text, close)
        if close2 < 0:
            return None
        ref = text[close + 1 : close2 - 1].strip() or label_text
        end = close2
    else:  # shortcut
        ref, end = label_text, close
    url = refs.get(ref.strip().lower())
    if url is None:
        return None
    return label_text, url, end


def _scan_inline(
    text: str, roles: frozenset, href: str | None = None, refs: dict[str, str] | None = None
) -> list[InlineRun]:
    """Split ``text`` into role-tagged runs, recursing so emphasis nests
    (``**bold _and italic_**``). ``roles`` is the set already in force from the
    enclosing scan; ``href`` is the link URL when scanning inside a link; ``refs``
    resolves ``[text][label]`` reference links. A literal ``\\n`` is a hard line
    break, passed through for the wrapper to split a row on."""
    refs = refs or {}
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
        if c == "~" and i + 1 < n and text[i + 1] == "~" and "code" not in roles:
            close = text.find("~~", i + 2)
            if close != -1 and close > i + 2:
                flush()
                out.extend(_scan_inline(text[i + 2 : close], roles | {"strike"}, href, refs))
                i = close + 2
                continue
            buf.append(c)
            i += 1
            continue
        if c == "<":
            m = _AUTOLINK_RE.match(text, i)
            if m:
                flush()
                target = m.group(1)
                url = target if _SCHEME_RE.match(target) else "mailto:" + target
                out.append((target, roles | {"link"}, url))
                i = m.end()
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
                out.extend(_scan_inline(m.group(1), roles | {"link"}, m.group(2), refs))
                i = m.end()
                continue
            ref = _resolve_reflink(text, i, refs)
            if ref is not None:
                label_text, url, end = ref
                flush()
                out.extend(_scan_inline(label_text, roles | {"link"}, url, refs))
                i = end
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
                out.extend(_scan_inline(text[k:close], roles | added, href, refs))
                i = close + len(marker)
                continue
            buf.append(text[i:k])
            i = k
            continue
        # A bare URL at a word boundary (GFM): linkify it, leaving any trailing
        # sentence punctuation behind for normal text.
        if (
            c in "hH"
            and "link" not in roles
            and (i == 0 or not text[i - 1].isalnum())
        ):
            bm = _BARE_URL_RE.match(text, i)
            if bm:
                url = _trim_url(bm.group(0))
                if url:
                    flush()
                    out.append((url, roles | {"link"}, url))
                    i += len(url)
                    continue
        buf.append(c)
        i += 1
    flush()
    return out


def _parse_inline(text: str, refs: dict[str, str] | None = None) -> list[InlineRun]:
    return _scan_inline(text, frozenset(), refs=refs)


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


def _hard_break(raw: str) -> bool:
    """A source line ends with a hard line break when it has two+ trailing spaces
    or a trailing backslash (GFM) — the next line then starts its own row."""
    return raw.endswith("  ") or raw.rstrip(" ").endswith("\\")


def _join_para(raw_lines: list[str]) -> str:
    """Join a paragraph's source lines into one flow: a hard break at a line end
    becomes a literal ``\\n`` (a forced row break the wrapper honors), every other
    join a space. A ``\\`` that signalled the break is dropped."""
    segs: list[str] = []
    n = len(raw_lines)
    for k, raw in enumerate(raw_lines):
        s = raw.strip()
        hard = k < n - 1 and _hard_break(raw)
        if hard and not raw.endswith("  ") and s.endswith("\\"):
            s = s[:-1]
        segs.append(s)
        if k < n - 1:
            segs.append("\n" if hard else " ")
    return "".join(segs)


def _split_table_row(line: str) -> list[str]:
    """Split a pipe-table row into trimmed cell texts, honoring ``\\|`` escapes
    and the optional leading/trailing outer pipes."""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    cells: list[str] = []
    buf: list[str] = []
    k = 0
    while k < len(s):
        ch = s[k]
        if ch == "\\" and k + 1 < len(s):
            buf.append(s[k + 1])
            k += 2
            continue
        if ch == "|":
            cells.append("".join(buf))
            buf = []
            k += 1
            continue
        buf.append(ch)
        k += 1
    cells.append("".join(buf))
    return [c.strip() for c in cells]


def _slug(text: str) -> str:
    """A GitHub-style heading anchor slug: lowercased, punctuation dropped, spaces
    to hyphens. ``## My Section!`` -> ``my-section`` so ``[x](#my-section)`` finds it."""
    text = re.sub(r"[^\w\- ]", "", text).strip().lower()
    return re.sub(r"\s+", "-", text)


def _delim_align(cell: str) -> str:
    """Column alignment from a delimiter cell: ``:--`` left, ``--:`` right,
    ``:-:`` center, plain dashes default to left (GitHub's default)."""
    cell = cell.strip()
    left, right = cell.startswith(":"), cell.endswith(":")
    if left and right:
        return "center"
    if right:
        return "right"
    return "left"


def _collect_refs(lines: list[str]) -> tuple[dict[str, str], list[str]]:
    """First pass: pull ``[label]: url`` reference definitions out of the source
    (skipping fenced code) into a dict, blanking their lines so they never render
    — a later ``[text][label]`` resolves even when the def appears below it."""
    refs: dict[str, str] = {}
    kept: list[str] = []
    in_fence, fence_marker = False, ""
    for ln in lines:
        fm = _FENCE_RE.match(ln)
        if fm:
            marker = fm.group(1)[0]
            if in_fence:
                if marker == fence_marker:
                    in_fence = False
            else:
                in_fence, fence_marker = True, marker
            kept.append(ln)
            continue
        if not in_fence:
            m = _REF_DEF_RE.match(ln)
            if m:
                refs.setdefault(m.group(1).strip().lower(), m.group(2))
                kept.append("")
                continue
        kept.append(ln)
    return refs, kept


def parse_markdown(src: str) -> list[_SemLine]:
    """Parse Markdown source into the semantic line list the view lays out."""
    lines = src.split("\n")
    refs, lines = _collect_refs(lines)
    return _parse_lines(lines, refs)


def _parse_lines(lines: list[str], refs: dict[str, str]) -> list[_SemLine]:
    """The block loop, over a (ref-stripped) line list. Split from
    ``parse_markdown`` so a block quote can re-parse its inner lines recursively
    (nested quotes, lists in quotes, reflowed multi-line quotes) with the same
    reference definitions in scope."""
    out: list[_SemLine] = []
    i, n = 0, len(lines)
    while i < n:
        raw = lines[i]
        stripped = raw.strip()

        fence = _FENCE_RE.match(raw)
        if fence:
            marker = fence.group(1)[0]
            # The info string after the opening fence names the language; its
            # first word drives syntax highlighting (``data`` carries it).
            info = raw[fence.end():].strip()
            lang = info.split()[0] if info else ""
            i += 1
            code_lines: list[str] = []
            while i < n and not lines[i].lstrip().startswith(marker * 3):
                code_lines.append(lines[i])
                i += 1
            i += 1  # consume the closing fence (or run off the end)
            # One sem for the whole block (so it tokenizes as a unit); each source
            # line is a plain run, literal — fenced code is never inline-parsed.
            runs = [(ln, frozenset(), None) for ln in code_lines]
            out.append(_SemLine("code", 0, "", runs, wrap=False, data=(lang or None)))
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
            out.append(_SemLine("heading", len(m.group(1)), "", _parse_inline(m.group(2), refs)))
            i += 1
            continue

        if _HRULE_RE.match(stripped):
            out.append(_SemLine("rule", 0, "", [], wrap=False))
            i += 1
            continue

        if stripped.startswith(">"):
            # Gather the whole block quote (consecutive '>' lines and their lazy
            # paragraph continuations), strip one '>' level, and parse the inner
            # text recursively so a nested quote, a list inside a quote, and a
            # multi-line quoted paragraph all render (the last reflows as a flow).
            inner: list[str] = []
            while i < n and lines[i].strip():
                ln = lines[i]
                if ln.lstrip().startswith(">"):
                    inner.append(re.sub(r"^\s*>\s?", "", ln))
                elif _is_block_break(ln):
                    break
                else:
                    inner.append(ln.strip())
                i += 1
            for sem in _parse_lines(inner, refs):
                sem.quote_depth += 1
                out.append(sem)
            continue

        if "|" in raw and i + 1 < n and _TABLE_DELIM_RE.match(lines[i + 1]):
            header = _split_table_row(raw)
            ncol = len(header)
            aligns = [_delim_align(d) for d in _split_table_row(lines[i + 1])]
            aligns = (aligns + ["left"] * ncol)[:ncol]
            i += 2
            body: list[list[list[InlineRun]]] = []
            while i < n and lines[i].strip() and "|" in lines[i] and not _is_block_break(lines[i]):
                cells = (_split_table_row(lines[i]) + [""] * ncol)[:ncol]
                body.append([_parse_inline(c, refs) for c in cells])
                i += 1
            tbl = _Table(
                header=[_parse_inline(c, refs) for c in header], aligns=aligns, rows=body
            )
            out.append(_SemLine("table", 0, "", [], wrap=False, table=tbl))
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
            # A GitHub task item: a '[ ]'/'[x]' at the head of a bullet item
            # renders as an (un)checked box in place of the bullet.
            checked: bool | None = None
            tm = _TASK_RE.match(text)
            if tm and marker in ("-", "*", "+"):
                checked = tm.group(1).lower() == "x"
                text = tm.group(2)
                bullet = ("☑ " if checked else "☐ ")
            out.append(
                _SemLine(
                    "list", depth, "  " * depth + bullet,
                    _parse_inline(text, refs), checked=checked,
                )
            )
            continue

        # Paragraph, or a setext heading when the gathered line(s) are followed
        # by a '===' / '---' underline.
        para_raw = [raw]
        i += 1
        while i < n and not _is_block_break(lines[i]) and not _SETEXT_RE.match(lines[i]):
            para_raw.append(lines[i])
            i += 1
        if i < n and _SETEXT_RE.match(lines[i]) and any(p.strip() for p in para_raw):
            level = 1 if lines[i].lstrip()[0] == "=" else 2
            out.append(_SemLine("heading", level, "", _parse_inline(_join_para(para_raw), refs)))
            i += 1
            continue
        out.append(_SemLine("para", 0, "", _parse_inline(_join_para(para_raw), refs)))
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
    if "strike" in run_roles:
        attr |= TextAttribute.STRIKETHROUGH
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
    style (and link href) across the break. A literal ``\\n`` cell is a hard line
    break: the flow is split there and each segment starts a fresh row."""
    cells: list[Span] = [
        (g, st, href) for text, st, href in spans for g in glyph_runs(text)
    ]
    if not any(c[0] == "\n" for c in cells):
        return _wrap_cells(cells, width, measure, word=word)
    rows: list[list[Span]] = []
    seg: list[Span] = []
    for c in cells:
        if c[0] == "\n":
            rows.extend(_wrap_cells(seg, width, measure, word=word))
            seg = []
        else:
            seg.append(c)
    rows.extend(_wrap_cells(seg, width, measure, word=word))
    return rows or [[]]


# --- incremental-search highlight ---------------------------------------------

# Search-match background = the surface blended toward amber, firmer for the
# current match — derived from the surface (not a fixed dark constant) so the
# highlight tracks the theme (a dark wash on a dark surface, a pale one on a
# light one), matching TFM's text-viewer isearch.
_SEARCH_HUE: Color = (200, 175, 55)
_SEARCH_TINT = 0.24
_SEARCH_CURRENT_TINT = 0.46


def _mix(a: Color, b: Color, t: float) -> Color:
    """Linear RGB blend a->b by ``t`` (0..1)."""
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))  # type: ignore[return-value]


def _search_bg(surface: Color | None, current: bool) -> Color:
    """Search-highlight background for ``surface``, firmer for the current match."""
    return _mix(surface or (30, 30, 38), _SEARCH_HUE,
                _SEARCH_CURRENT_TINT if current else _SEARCH_TINT)


def _wrap_cells(cells: list[Span], width: float, measure, *, word: bool) -> list[list[Span]]:
    """Wrap a flat cell list to ``width`` base units. ``measure(text, style)``
    reports a fragment's width in base units (a column count on the grid, native
    metrics for a proportional font), so the same wrap follows both. ``word=False``
    breaks between glyphs regardless (the fenced-code path), so no content is lost."""
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
        # A layout depends only on (width, view height, source) — never on the
        # scroll offset — so it is memoized per wrap width for the current view
        # height. draw() probes TWO widths each frame (the full width, then one
        # base unit narrower to reserve the scrollbar), so an overflowing document
        # alternates between them every frame; a single slot would miss on both
        # and re-lay-out the whole document twice per scroll step. Keeping both
        # widths warm makes a pure scroll re-lay-out nothing. Cleared when the
        # view height (image cap) or the source changes; bounded so a live resize
        # (a stream of new widths) can't grow it without limit.
        self._layout_cache: dict[
            float, tuple[list[_Row], list[float], dict[str, float], float]
        ] = {}
        self._cache_view_h: float = -1.0

        # Click targets from the last draw: (x0, y0, x1, y1, url) in widget-local
        # base units, one per visible link span. The Panel is kept to open a URL.
        self._link_hits: list[tuple[float, float, float, float, str]] = []
        self._panel = None
        # Heading slug -> its top offset (base units), for ``[jump](#slug)`` links.
        # Rebuilt each layout alongside the row index.
        self._anchors: dict[str, float] = {}

        # Incremental search (driven by a host viewer's search bar; see the
        # ``search_*`` methods). The pattern highlights every occurrence in the
        # visible rows; the navigable match set is the display rows that contain
        # it (the row is the unit, mirroring TFM's text-viewer isearch, which
        # walks matching lines). ``_search_token`` records the (pattern, wrap
        # width) the set was built at so a resize that re-wraps the document
        # rebuilds it; ``_search_origin`` is the pre-search scroll, restored on
        # cancel.
        self._search_pattern: str = ""
        self._search_rows: list[int] = []  # indices into _rows with a hit
        self._search_pos: int = -1         # index into _search_rows (current match)
        self._search_token: tuple[str, float] = ("", -1.0)
        self._search_origin: float = 0.0

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
        self._layout_cache.clear()
        self._cache_view_h = -1.0
        self.clear_search()

    # --- layout ---------------------------------------------------------------

    def _layout(self, width: float, ctx: DrawContext) -> list[_Row]:
        """Wrap every semantic line to ``width`` base units, returning the flat
        list of ``_Row`` and rebuilding the cumulative ``_row_tops`` index.
        Wrapping and every row height go through ``ctx`` so a proportional / sized
        face measures and spaces correctly, and an image is sized to its aspect
        ratio. Rebuilt only when the width, view height (image cap), or source
        changes."""
        # A change in view height re-contains images, so it invalidates every
        # cached width; drop them all and rebuild for the new height.
        if self._cache_view_h != self._view_h:
            self._layout_cache.clear()
            self._cache_view_h = self._view_h
        cached = self._layout_cache.get(width)
        if cached is not None:
            self._rows, self._row_tops, self._anchors, self._line_pitch = cached
            self._wrap_width = width
            self._wrap_view_h = self._view_h
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
        anchors: list[tuple[str, int]] = []  # (slug, first row index) per heading
        for sem in self._sems:
            qd = sem.quote_depth
            qind = qd * _QUOTE_INDENT
            if sem.block == "blank":
                rows.append(_Row(0.0, [], self._line_pitch, quote=qd))
                continue
            if sem.block == "rule":
                # A hairline on GUI, a ─ run on grid — resolved by draw_hairline
                # in draw(); the layout just marks the row.
                rows.append(_Row(0.0, [], self._line_pitch, rule=True, quote=qd))
                continue
            if sem.block == "code":
                rows.extend(self._layout_code(sem, width, measure, line_height, theme, qd))
                continue
            if sem.block == "table" and sem.table is not None:
                # A vector backend overlaps real strokes, so its border rows need
                # no grid cell of their own (near-zero height keeps the vertical
                # bars of adjacent text rows touching the horizontals); a grid
                # backend gives each border its own one-cell row for the glyphs.
                bh = max(1, ctx.base_size[1])
                border_h = (1.0 / bh) if ctx.vector_shapes else self._line_pitch
                # A small inner top/bottom margin so cell text does not sit flush
                # against the rules on GUI. The grid keeps borders in their own
                # rows (natural gap) and must stay integer-aligned, so 0 there.
                cell_pad_v = max(2.0 / bh, 0.1 * self._line_pitch) if ctx.vector_shapes else 0.0
                rows.extend(
                    self._layout_table(
                        sem.table, width - qind, measure, theme, qd, border_h,
                        cell_pad_v, ctx.vector_shapes,
                    )
                )
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
                rows.append(_Row(x0, [], h, image=(sem.data, alt, w, h), quote=qd))
                continue
            base = _block_style(
                sem.block, sem.level, self.style, theme,
                self.text_font, self.code_font, self.heading_scales, body_size,
            )
            # Quoted text reads muted (a GitHub blockquote), so recolor the base
            # for every run that doesn't set its own color (a link keeps accent).
            if qd:
                base = replace(base, fg=theme.muted_text)
            if sem.block == "heading":
                plain = "".join(t for t, _, _ in sem.runs)
                anchors.append((_slug(plain), len(rows)))
            spans = [
                (text, _run_style(roles, base, theme, self.code_font), href)
                for text, roles, href in sem.runs
            ]
            prefix_w = measure(sem.prefix, base) if sem.prefix else 0.0
            avail = max(1.0, width - qind - prefix_w)
            wrapped = _wrap_spans(spans, avail, measure, word=sem.wrap)
            # A list marker / task box stays a text cell on the first row; a
            # block quote's ``│`` bars are strokes drawn by draw() from the row's
            # quote depth, so the content simply hangs at qind (+ prefix width).
            for k, row in enumerate(wrapped):
                if k == 0 and sem.prefix:
                    cells = [(sem.prefix, base, None)] + row
                    x0 = qind
                else:
                    # Continuation rows hang under the text, indented by the
                    # measured prefix width (exact on every backend).
                    cells = row
                    x0 = qind + prefix_w
                height = max((line_height(st) for _, st, _ in cells), default=line_height(base))
                rows.append(_Row(x0, cells, height, quote=qd))
        self._rows = rows
        tops = [0.0]
        for row in rows:
            tops.append(tops[-1] + row.height)
        self._row_tops = tops
        self._anchors = {slug: tops[idx] for slug, idx in anchors if slug}
        self._wrap_width = width
        self._wrap_view_h = self._view_h
        # Memoize under the current (already view-height-consistent) cache. Steady
        # state holds just the two probe widths; extra entries accrue only mid
        # resize, so evict the oldest to keep the dict bounded.
        if len(self._layout_cache) >= 4:
            self._layout_cache.pop(next(iter(self._layout_cache)))
        self._layout_cache[width] = (rows, tops, self._anchors, self._line_pitch)
        return rows

    def _layout_code(
        self, sem: _SemLine, width: float, measure, line_height, theme: Theme, qd: int
    ) -> list[_Row]:
        """Lay out a fenced code block: one row per (char-wrapped) source line,
        each carrying a ``code_bg`` so ``draw`` paints a continuous block-wide
        background. When Pygments is present and the fence named a language, runs
        are colored per token; otherwise the whole block is the flat code color."""
        qind = qd * _QUOTE_INDENT
        left = qind + _CODE_PAD
        avail = max(1.0, width - qind - 2 * _CODE_PAD)
        code_lines = [t for t, _, _ in sem.runs]
        highlighted = _highlight(code_lines, sem.data) if sem.data else None
        # When highlighting, the uncolored default is a neutral gray; without it,
        # the block keeps the flat warm code color (unchanged look).
        default_fg = _SYNTAX_DEFAULT_FG if highlighted is not None else _CODE_FG
        bg = theme.control_bg

        def style_for(color: Color | None) -> Style:
            return Style(fg=color or default_fg, bg=bg, font=self.code_font)

        base = style_for(None)
        h = line_height(base)
        rows: list[_Row] = []
        segments = highlighted if highlighted is not None else [[(ln, None)] for ln in code_lines]
        for segs in segments:
            spans = [(text, style_for(color), None) for text, color in segs if text]
            wrapped = _wrap_spans(spans or [("", base, None)], avail, measure, word=False)
            for row in wrapped:
                rows.append(_Row(left, row, h, quote=qd, code_bg=bg))
        return rows

    def _layout_table(
        self, tbl: _Table, width: float, measure, theme: Theme, qd: int,
        border_h: float, cell_pad_v: float, vector: bool,
    ) -> list[_Row]:
        """Lay a GFM table out as one ``_Row`` per grid row. Columns take their
        natural content width, scaled down proportionally (with a floor) when the
        table would overflow ``width``; each cell then wraps to its column and the
        row is as tall as its tallest cell. Border edges are shared by every row;
        ``border_h`` is the height a horizontal-rule row reserves (thin on a
        vector backend, a full grid cell on a character one), and ``cell_pad_v``
        the inner top/bottom margin added to each text row.

        Rows are told apart differently per backend: a vector backend draws a
        cheap hairline between every row, while a character grid — where a rule
        would cost a whole text row — zebra-stripes the body instead."""
        ncol = len(tbl.aligns)
        if ncol == 0:
            return []
        qind = qd * _QUOTE_INDENT
        base = Style(fg=self.style.fg, bg=self.style.bg, font=self.text_font)
        if qd:
            base = replace(base, fg=theme.muted_text)
        hdr = replace(base, attr=base.attr | TextAttribute.BOLD)

        def to_spans(runs: list[InlineRun], b: Style) -> list[Span]:
            return [(t, _run_style(r, b, theme, self.code_font), h) for t, r, h in runs]

        def cell_w(spans: list[Span]) -> float:
            return sum(measure(t, st) for t, st, _ in spans if t)

        header_spans = [to_spans(c, hdr) for c in tbl.header]
        body_spans = [[to_spans(c, base) for c in r] for r in tbl.rows]
        content_w = [
            max(
                [cell_w(header_spans[j])]
                + [cell_w(r[j]) for r in body_spans if j < len(r)]
            )
            for j in range(ncol)
        ]
        fixed = ncol * 2 * _TABLE_PAD + (ncol + 1) * _TABLE_BORDER
        avail = width - fixed
        total = sum(content_w)
        if total > avail and total > 0:
            scale = max(0.0, avail) / total
            content_w = [max(3.0, w * scale) for w in content_w]

        # Border-column left coords (ncol + 1) and per-column text origins.
        edges: list[float] = []
        cols: list[tuple[float, float, str]] = []
        x = qind
        for j in range(ncol):
            edges.append(x)
            x += _TABLE_BORDER
            text_x = x + _TABLE_PAD
            cols.append((text_x, content_w[j], tbl.aligns[j]))
            x = text_x + content_w[j] + _TABLE_PAD
        edges.append(x)

        def hline(role: str) -> _Row:
            return _Row(qind, [], border_h, quote=qd, table=_TableRow([], edges, hline=True, role=role))

        def band_row(cell_runs: list[list[InlineRun]], base_style: Style, band_bg: Color | None) -> _Row:
            # Cell text carries the band bg per glyph (a grid draw_text replaces the
            # whole cell style, so the fill alone would be erased under the text);
            # draw() also fills the band across the row so the gaps carry it too.
            cells: list[tuple[float, float, str, list[list[Span]]]] = []
            n_lines = 1
            for j, (text_x, w, align) in enumerate(cols):
                runs = cell_runs[j] if j < len(cell_runs) else []
                lines = _wrap_spans(to_spans(runs, base_style), w, measure, word=True)
                n_lines = max(n_lines, len(lines))
                cells.append((text_x, w, align, lines))
            height = n_lines * self._line_pitch + 2 * cell_pad_v
            return _Row(
                qind, [], height, quote=qd,
                table=_TableRow(cells, edges, pad_v=cell_pad_v, bg=band_bg),
            )

        # The header always takes a distinct fill. Body rows: a vector backend
        # rules a cheap hairline between them; a grid backend zebra-stripes them
        # (a rule would cost a whole extra text row there).
        #
        # Both bands lift off the surface the table actually sits on (the view's
        # own bg — e.g. a popup's gray), not a fixed theme role. control_bg /
        # hover_bg are calibrated against the dark content pane, so over a lighter
        # dialog the header read as a disconnected bar and the zebra washed out;
        # lifting from self.style.bg keeps a consistent step on any surface and
        # picks the right polarity for a light theme. The stripe is a hair
        # stronger than a bare hover so the body-row boundary still reads on TUI,
        # where zebra alternation is the only separator between body rows.
        surface = self.style.bg or theme.surface_bg("content") or (30, 30, 38)
        header_bg = lift(surface, 0.12)
        stripe_bg = lift(surface, 0.08)
        rows: list[_Row] = [
            hline("top"),
            band_row(tbl.header, replace(hdr, bg=header_bg), header_bg),
            hline("mid"),
        ]
        for i, r in enumerate(tbl.rows):
            if i > 0 and vector:
                rows.append(hline("mid"))
            band = None if vector else (stripe_bg if i % 2 == 1 else None)
            rows.append(band_row(r, replace(base, bg=band), band))
        rows.append(hline("bottom"))
        return rows

    # --- drawing --------------------------------------------------------------

    def draw(self, ctx: DrawContext) -> None:
        self._panel = ctx.panel
        self._link_hits = []
        theme = ctx.theme or DEFAULT_THEME
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

        # Reconcile the search match set with this (possibly re-wrapped) layout,
        # then note which row is the current match so it highlights firmer.
        self._sync_search()
        current_match = (self._search_rows[self._search_pos]
                         if 0 <= self._search_pos < len(self._search_rows) else -1)

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
            if row.rule:
                # A full-width horizontal rule centered in the row.
                ctx.draw_hairline(0.0, y + row.height / 2.0, self._wrap_width,
                                  style=Style(fg=theme.muted_text))
                continue
            for d in range(row.quote):
                # One vertical bar per block-quote nesting level, each in its own
                # reserved left column (centerline at that column's center).
                ctx.draw_hairline(d * _QUOTE_INDENT + 0.5, y, row.height, vertical=True,
                                  style=Style(fg=theme.muted_text))
            if row.table is not None:
                self._draw_table_row(ctx, y, row.height, row.table, theme)
                continue
            if row.code_bg is not None:
                # A continuous fill across the whole block width behind the code,
                # so consecutive rows read as one panel (not a ragged per-glyph bg).
                fx = row.x0 - _CODE_PAD
                ctx.fill_rect(fx, y, max(0.0, self._wrap_width - fx), row.height,
                              Style(bg=row.code_bg))
            x = row.x0
            for text, style, href in row.spans:
                if not text:
                    continue
                ctx.draw_text(x, y, text, style)
                w = ctx.measure_text(text, style)
                if href:
                    self._link_hits.append((x, y, x + w, y + row.height, href))
                x += w
            if self._search_pattern and row.spans:
                self._draw_search_row(ctx, y, row, index == current_match, theme)

        # A pointing hand over a link, so it reads as clickable. The pointer is
        # taken in widget-local coords (panel pointer minus this widget's screen
        # origin, as the splitter does) and tested against the visible link
        # spans gathered above. One intent; resolved per backend.
        if ctx.panel is not None and ctx.panel.pointer is not None:
            sx, sy, _sw, _sh = ctx.screen_rect
            lx, ly = ctx.panel.pointer[0] - sx, ctx.panel.pointer[1] - sy
            if any(x0 <= lx < x1 and y0 <= ly < y1 for x0, y0, x1, y1, _ in self._link_hits):
                ctx.set_cursor("pointer")

        if show_bar:
            ratio = view_h / content_h
            denom = content_h - view_h
            pos = self.offset / denom if denom > 0 else 0.0
            # Fractional width keeps the bar flush to the right edge at pixel
            # granularity; ctx.width is truncated to whole base units.
            ctx.draw_scrollbar(
                ctx.size_units[0] - 1, 0, view_h, max(0.0, min(1.0, pos)), ratio, self.style
            )

    def _draw_table_row(
        self, ctx: DrawContext, y: float, height: float, tr: _TableRow, theme: Theme
    ) -> None:
        """Draw one table row. A horizontal-border row is a single rule across the
        table width; a text row draws each cell's wrapped lines aligned within its
        column, then a vertical bar at every column edge."""
        stroke = Style(fg=theme.muted_text)
        if tr.hline:
            cy = y + height / 2.0
            if ctx.vector_shapes:
                # One stroke spanning the column-bar centerlines; the (also-stroked)
                # bars of the neighboring text rows cross it, so the frame connects
                # with no junction glyphs of its own.
                x0 = tr.edges[0] + _TABLE_BORDER / 2.0
                x1 = tr.edges[-1] + _TABLE_BORDER / 2.0
                ctx.draw_hairline(x0, cy, x1 - x0, style=stroke)
                return
            # Character grid: a box-drawing junction where each bar crosses this
            # rule (top → corners/tees, mid → tees/cross, bottom → corners/tees)
            # and a ── run filling the cells between them, so the ``│`` of the
            # text rows above and below join into a real frame.
            up = tr.role != "top"
            down = tr.role != "bottom"
            row = int(cy)
            cols = [int(e + _TABLE_BORDER / 2.0) for e in tr.edges]
            for j, col in enumerate(cols):
                glyph = _box_glyph(up, down, j > 0, j < len(cols) - 1)
                ctx.draw_text(col, row, glyph, stroke)
                if j < len(cols) - 1:
                    gap = cols[j + 1] - col - 1
                    if gap > 0:
                        ctx.draw_text(col + 1, row, "─" * gap, stroke)
            return
        # The row band (header fill or zebra stripe) is painted per cell up to the
        # column lines but never over them, so the lines keep the surface color and
        # the bars stay a plain stroke. A vector line is a thin stroke centered on
        # the edge, so the fill runs to that centerline (no gap); a grid line is a
        # whole column, so the fill stops one column short of it.
        if tr.bg is not None:
            inset = _TABLE_BORDER / 2.0 if ctx.vector_shapes else _TABLE_BORDER
            end = _TABLE_BORDER / 2.0 if ctx.vector_shapes else 0.0
            for j in range(len(tr.edges) - 1):
                x0 = tr.edges[j] + inset
                ctx.fill_rect(x0, y, tr.edges[j + 1] + end - x0, height, Style(bg=tr.bg))
        for text_x, w, align, lines in tr.cells:
            for li, line in enumerate(lines):
                lw = sum(ctx.measure_text(t, st) for t, st, _ in line if t)
                if align == "right":
                    ox = w - lw
                elif align == "center":
                    ox = max(0.0, (w - lw) / 2.0)
                else:
                    ox = 0.0
                x = text_x + ox
                ly = y + tr.pad_v + li * self._line_pitch
                for text, style, href in line:
                    if not text:
                        continue
                    ctx.draw_text(x, ly, text, style)
                    tw = ctx.measure_text(text, style)
                    if href:
                        self._link_hits.append((x, ly, x + tw, ly + self._line_pitch, href))
                    x += tw
        for e in tr.edges:
            ctx.draw_hairline(e + _TABLE_BORDER / 2.0, y, height, vertical=True, style=stroke)

    def _draw_search_row(
        self, ctx: DrawContext, y: float, row: _Row, current: bool, theme: Theme
    ) -> None:
        """Overlay incremental-search highlights on one visible text row: every
        occurrence of the pattern in the row's concatenated text is redrawn with a
        highlight background (firmer for the current match), preserving each
        glyph's own color/font/attrs so a styled run — inline code, a link —
        stays recognisable under the tint. A match spanning a style boundary is
        drawn in its constituent runs.

        The row was already drawn by :meth:`draw`; this second pass over the same
        x layout (spans measured left to right from ``row.x0``) repaints only the
        matched substrings. The tint is a ``fill_rect`` spanning the row's own
        ``height`` first, then the glyphs are redrawn over it: ``draw_text``'s own
        background fills exactly one body cell, which clips a sized heading row
        (taller than a cell) to the top of its glyphs — the fill covers the whole
        row height instead. The glyphs keep their own color/font/attrs (only the
        background is the tint) so a styled run — inline code, a link — stays
        recognisable under it."""
        plain = self._row_plain(row)
        low = plain.lower()
        pat = self._search_pattern.lower()
        idx = low.find(pat)
        if idx < 0:
            return
        surface = self.style.bg or theme.surface_bg("content")
        hl = _search_bg(surface, current)
        # Per-span offsets: (char_start, text, style, x_start), so a match at any
        # character offset maps to an x position even across span boundaries.
        segs: list[tuple[int, str, Style, float]] = []
        x = row.x0
        pos = 0
        for text, style, _ in row.spans:
            segs.append((pos, text, style, x))
            x += ctx.measure_text(text, style)
            pos += len(text)
        while idx >= 0:
            end = idx + len(pat)
            for c0, text, style, sx in segs:
                s = max(idx, c0)
                e = min(end, c0 + len(text))
                if e <= s:
                    continue
                seg = text[s - c0 : e - c0]
                hx = sx + ctx.measure_text(text[: s - c0], style)
                # Paint the tint across the row's full height, then redraw the
                # glyphs over it (a grid backend, which can't composite a
                # transparent glyph over the fill, keeps the tint via bg=hl too).
                ctx.fill_rect(hx, y, ctx.measure_text(seg, style), row.height, Style(bg=hl))
                ctx.draw_text(hx, y, seg, replace(style, bg=hl))
            idx = low.find(pat, end)

    # --- scrolling ------------------------------------------------------------

    def _content_h(self) -> float:
        return self._row_tops[-1]

    def _clamp(self, content_h: float) -> None:
        self.offset = max(0.0, min(self.offset, max(0.0, content_h - self._view_h)))

    def scroll_by(self, amount: float) -> None:
        self.offset += amount
        self._clamp(self._content_h())

    # --- incremental search ---------------------------------------------------
    #
    # A host viewer (TFM's modal file viewer) drives these from its shared search
    # bar so the rendered Markdown view gets the same incremental-search UX as the
    # raw text view. Match finding, scroll-to-match and highlighting all need the
    # widget's own (proportional, wrapped) layout, so they live here rather than
    # in the host.

    @staticmethod
    def _row_plain(row: _Row) -> str:
        """A text row's concatenated plain text (empty for image / rule / table /
        blank rows, which carry no spans, so those never match)."""
        return "".join(t for t, _, _ in row.spans)

    def _recompute_search_rows(self) -> None:
        """Rebuild the navigable match set (display rows whose plain text contains
        the pattern, case-insensitive) from the current layout."""
        rows = self._rows or []
        pat = self._search_pattern.lower()
        self._search_rows = (
            [i for i, r in enumerate(rows) if pat in self._row_plain(r).lower()]
            if pat else []
        )
        self._search_token = (self._search_pattern, self._wrap_width)

    def _sync_search(self) -> None:
        """Keep the match set consistent with the layout that draw is about to
        render: after a resize re-wraps the document (a new wrap width), the row
        indices shift, so rebuild the set and re-clamp the current match."""
        if not self._search_pattern:
            return
        if self._search_token != (self._search_pattern, self._wrap_width):
            self._recompute_search_rows()
            if self._search_rows:
                self._search_pos = max(0, min(self._search_pos, len(self._search_rows) - 1))
            else:
                self._search_pos = -1

    def _scroll_to_search_row(self, row_index: int) -> None:
        """Scroll so display row ``row_index`` sits at the top of the viewport."""
        if 0 <= row_index < len(self._row_tops) - 1:
            self.offset = self._row_tops[row_index]
            self._clamp(self._content_h())

    def search_begin(self) -> None:
        """Remember the current scroll as the pre-search origin (restored by
        :meth:`search_cancel`) and drop any stale highlights. Call when opening the
        search bar."""
        self._search_origin = self.offset
        self.clear_search()

    def search_set(self, pattern: str) -> int:
        """Set the case-insensitive search ``pattern`` (live, per keystroke):
        highlight every occurrence, scroll to the nearest match at/after the
        current viewport top, and return the number of matching rows. With no
        match, restore the pre-search scroll (mirrors the text viewer)."""
        self._search_pattern = pattern
        self._recompute_search_rows()
        if self._search_rows:
            self._search_pos = next(
                (k for k, ri in enumerate(self._search_rows)
                 if self._row_tops[ri] >= self.offset), 0)
            self._scroll_to_search_row(self._search_rows[self._search_pos])
        else:
            self._search_pos = -1
            self.offset = self._search_origin
            self._clamp(self._content_h())
        return len(self._search_rows)

    def search_navigate(self, delta: int) -> None:
        """Walk to the previous (``delta < 0``) / next (``delta > 0``) matching
        row, wrapping at the ends. A no-op with no matches."""
        if not self._search_rows:
            return
        self._search_pos = (self._search_pos + delta) % len(self._search_rows)
        self._scroll_to_search_row(self._search_rows[self._search_pos])

    def search_status(self) -> tuple[int, int]:
        """``(position, total)`` for the bar's counter: the 1-based index of the
        current match (0 when off any match) and the matching-row count."""
        n = len(self._search_rows)
        return (self._search_pos + 1 if (n and self._search_pos >= 0) else 0, n)

    def search_accept(self) -> None:
        """Enter: keep the current scroll; drop the pattern and highlights."""
        self.clear_search()

    def search_cancel(self) -> None:
        """Esc / outside click: restore the pre-search scroll and clear."""
        self.offset = self._search_origin
        self._clamp(self._content_h())
        self.clear_search()

    def clear_search(self) -> None:
        """Drop the search pattern, highlights, and match set."""
        self._search_pattern = ""
        self._search_rows = []
        self._search_pos = -1
        self._search_token = ("", -1.0)

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
        """Open the link under the click, if any. A ``#slug`` link scrolls to that
        heading in-document; otherwise the Panel resolves the open (``os_open``
        backends launch the OS handler, others copy the URL) — no widget branch."""
        if event.x is None or event.y is None:
            return False
        for x0, y0, x1, y1, url in self._link_hits:
            if x0 <= event.x < x1 and y0 <= event.y < y1:
                if url.startswith("#"):
                    top = self._anchors.get(_slug(url[1:]))
                    if top is not None:
                        self.offset = top
                        self._clamp(self._content_h())
                elif self._panel is not None:
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
