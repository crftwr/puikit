"""Panel / Layout / Layer management.

The Panel is the only API widgets talk to. It places widgets in base unit
coordinates, resolves backend capabilities, and contains all fallback
chains so widget code never branches on TUI/GUI.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field, replace
from typing import Any

from .backend import Backend, Color, DEFAULT_STYLE, Style, TextAttribute, TRANSPARENT, is_transparent
from .capability import CapabilityProfile
from .color import LC_BODY, LC_LARGE, LC_MIN_NONTEXT, legible_ink
from .event import Event, EventType
from .focus import FocusContainer, focus_on_click, move_focus
from .font import Font, FontMetrics, FontSlant, FontWeight
from .theme import Theme, theme_for

# Caret blink half-period (seconds); only matters on animation-capable backends.
_CARET_BLINK = 0.53

# App-wide default for text that names no font, on backends that can render
# variable-advance text: the proportional system UI font, so GUI widgets read
# native instead of fixed-width by default (docs/font_system.md §5). It carries
# no size/family, so it inherits the backend base size and is value-equal across
# uses (one cache key). Widgets that need column alignment (log streams, code,
# the font showcase) pin Font(monospace=True) to opt back into a fixed advance.
# The base unit is unaffected — it is grounded in the backend's monospaced
# base_font, which never reads a Style (docs/font_system.md §3).
_DEFAULT_UI_FONT = Font()

# Checkbox / radio mark box side as a fraction of min(line_height, 2*advance).
# >1.0 makes the mark a touch larger than a single base-unit cell, so widgets
# that stack marks (RadioGroup) must reserve more than one row of pitch — see
# mark_box_units and RadioGroup's row pitch.
_MARK_FACTOR = 1.12


def mark_box_units(bw: int, bh: int) -> tuple[float, float, float]:
    """Geometry of a checkbox/radio mark box for a ``(bw, bh)`` base size:
    ``(side_px, width_units, height_units)``. The box is a pixel-square (square
    in device pixels even though a base unit cell is taller than it is wide), so
    its height in base units can exceed 1.0 — the single source of truth for how
    much vertical room a mark needs."""
    side = min(bh, bw * 2) * _MARK_FACTOR
    return side, (side / bw if bw else 1.0), (side / bh if bh else 1.0)

# How far inside the far edge a clamped margin click lands, so it stays within
# the content rect (whose right/bottom bound is exclusive) without losing
# sub-unit precision. A whole-cell widget floors this to the last cell.
_EDGE_EPS = 1e-3

# Text fallbacks used when a backend cannot draw real icons.
ICON_TEXT_FALLBACKS = {
    "folder": "📁",
    "file": "📄",
    "warning": "⚠",
    "error": "✖",
    "info": "ℹ",
    "check": "✔",
}


@dataclass(frozen=True)
class Rect:
    # Base-unit coordinates. Fractional values are produced by the layout system
    # on pixel_layout-capable backends; whole-unit backends only see integers.
    x: float
    y: float
    w: float
    h: float

    def contains(self, x: float, y: float) -> bool:
        return self.x <= x < self.x + self.w and self.y <= y < self.y + self.h


def _composite(color, base):
    """Flatten an RGBA color over an opaque base to an opaque RGB, for backends
    that cannot composite per pixel (TUI). A 3-tuple (already opaque) or None
    passes through; with no known base the alpha is simply dropped."""
    if color is None or len(color) < 4:
        return color
    r, g, b, a = color
    if base is None:
        return (r, g, b)
    f = a / 255.0
    return (
        round(r * f + base[0] * (1 - f)),
        round(g * f + base[1] * (1 - f)),
        round(b * f + base[2] * (1 - f)),
    )


def _intersect(a: Rect, b: Rect) -> Rect | None:
    x0, y0 = max(a.x, b.x), max(a.y, b.y)
    x1 = min(a.x + a.w, b.x + b.w)
    y1 = min(a.y + a.h, b.y + b.h)
    if x1 <= x0 or y1 <= y0:
        return None
    return Rect(x0, y0, x1 - x0, y1 - y0)


def _auto_ink_target(attr: TextAttribute) -> float:
    """The legibility floor auto-ink holds a text run to, read from its weight:
    dimmed text is deliberately de-emphasized (a low floor keeps it faint but not
    invisible), bold/large text needs less contrast to read than body, and
    everything else is body text."""
    if attr & TextAttribute.DIM:
        return LC_MIN_NONTEXT
    if attr & TextAttribute.BOLD:
        return LC_LARGE
    return LC_BODY


class DrawContext:
    """Drawing surface handed to a widget, translated to the widget's origin
    and clipped to its rectangle. Capability fallbacks live here."""

    def __init__(
        self,
        backend: Backend,
        rect: Rect,
        capabilities: CapabilityProfile,
        clip: Rect | None = None,
        panel: "Panel | None" = None,
        background: tuple[int, int, int] | None = None,
        focused: bool = False,
        hit_rect: Rect | None = None,
        widget: Any = None,
    ):
        self._backend = backend
        self._rect = rect
        self._caps = capabilities
        self._clip = clip if clip is not None else rect
        self._panel = panel
        self._background = background
        # The widget this context draws, so it can look up its own animated
        # state (a running color tween) without the app threading an identity.
        self._widget = widget
        # The region pointer operations (hover, press) test against, so they
        # match exactly what the Panel routes clicks and focus to. By default it
        # is the visible rect clipped to the parent — but a top-level pane passes
        # its bled fill (which reaches into the window margin), so hovering and
        # pressing the margin react the same as clicking it does.
        self._hit_rect = hit_rect if hit_rect is not None else _intersect(self._rect, self._clip)
        # Whether this widget currently holds the focus, resolved down the
        # parent chain (a widget is focused only if every container above it is
        # focused too). Interactive widgets read it to draw a focus cue; the
        # Panel layer owns the resolution so widgets never touch focus state.
        self._focused = focused
        # Backend clips this context pushed itself (e.g. draw_border's
        # interior clip); the Panel pops them when the widget's draw returns.
        self._pushed_clips = 0

    def _text_style(self, style: Style, ink: bool = True) -> Style:
        """Resolve a Style for drawing/measuring *text*: the shared `_resolve`
        seam, plus a theme-driven default foreground. A text run that names no
        color inherits the theme's text color, the same way it inherits the
        pane background — so a light theme (dark text on a light surface) reads
        instead of falling through to the backend's hardcoded near-white. Only
        the text path defaults the foreground: primitives whose `fg` carries a
        different meaning (a scrollbar thumb / box-line color, where `None`
        selects the backend's own default) keep using `_resolve` directly."""
        if style.fg is None and self._panel is not None and self._panel.theme is not None:
            style = Style(self._panel.theme.text, style.bg, style.attr, style.font)
        resolved = self._resolve(style)
        # Opt-in auto-ink: with the final foreground and its opaque background both
        # known, lift the foreground to a weight-aware legibility floor. Floor-only
        # (a color that already reads is returned unchanged) and skipped when there
        # is no concrete background to contrast against — including a transparent
        # fill, where the glyphs land on whatever a widget painted underneath and
        # that widget owns the contrast (e.g. a cursor row that strokes an outline
        # over its own fill).
        if (ink and self._panel is not None and getattr(self._panel, "auto_ink", False)
                and resolved.fg is not None and resolved.bg is not None
                and not is_transparent(resolved.fg) and not is_transparent(resolved.bg)):
            inked = legible_ink(resolved.fg, resolved.bg, _auto_ink_target(resolved.attr))
            if inked != resolved.fg:
                resolved = Style(inked, resolved.bg, resolved.attr, resolved.font)
        # Don't repaint a background the pane already filled: on a compositing
        # backend, draw the glyphs over it transparently. Repainting it would
        # (1) double-blend under a fading layer — the pane's fill and this one
        # compound to 1-(1-a)^2 instead of a, so text backgrounds bloom to ~0.75
        # while the pane is at 0.5 — and (2), being one base-unit tall, clip a
        # taller font's descender under the next stacked element. Auto-ink above
        # already read the concrete bg for contrast, so nothing is lost. A grid
        # backend can't composite (each cell has one bg), so it keeps the fill.
        # REVERSE is excluded: it *swaps* fg/bg in the backend, so a transparent
        # bg would become a transparent foreground — invisible glyphs on the
        # reversed fill (the "Reverse label" showing as a blank rectangle).
        if (resolved.bg is not None and resolved.bg == self._background
                and not (resolved.attr & TextAttribute.REVERSE)
                and self._caps.supports("transparency")):
            resolved = replace(resolved, bg=TRANSPARENT)
        return resolved

    def _resolve(self, style: Style) -> Style:
        """The single seam every Style crosses before the backend sees it:
        styles without an explicit background inherit the pane's, and a font
        is folded down for backends that cannot render it (docs/font_system.md
        §6) — weight/slant become bold/italic attributes, the rest is dropped."""
        bg = self._background if (self._background is not None and style.bg is None) else style.bg
        fg = style.fg
        attr = style.attr
        font = style.font
        if font is None and self._caps.supports("proportional_text"):
            # GUI default: text that names no font flows in the proportional UI
            # font rather than the monospaced base grid font, so widgets read
            # native by default. Widgets that need a fixed advance pin a
            # monospace Font and never reach here (docs/font_system.md §5).
            font = _DEFAULT_UI_FONT
        elif font is not None and not self._caps.supports("fonts"):
            if font.weight >= FontWeight.SEMI_BOLD:
                attr |= TextAttribute.BOLD
            if font.slant is FontSlant.ITALIC:
                attr |= TextAttribute.ITALIC
            font = None
        if not self._caps.supports("transparency"):
            # No per-pixel compositing: flatten any RGBA color over the pane
            # background (bg over the pane, fg over the resolved bg) to the
            # opaque approximation the terminal can actually render.
            base = self._background
            bg = _composite(bg, base)
            fg = _composite(fg, bg or base)
        if (fg, bg, attr, font) == (style.fg, style.bg, style.attr, style.font):
            return style
        return Style(fg, bg, attr, font)

    @property
    def width(self) -> int:
        return int(self._rect.w)

    @property
    def height(self) -> int:
        return int(self._rect.h)

    @property
    def size_units(self) -> tuple[float, float]:
        """Exact extent in base units; fractional on pixel-aware backends."""
        return (self._rect.w, self._rect.h)

    @property
    def base_size(self) -> tuple[int, int]:
        """Pixel size of one base unit, as declared by the backend."""
        return self._backend.base_size

    @property
    def pixel_layout(self) -> bool:
        """True when the backend resolves layout at pixel granularity, so a
        widget that subdivides its own pane (a Splitter, a nested layout) may
        keep fractional base unit boundaries. False on a whole-unit backend
        (a character grid), where every boundary must snap to a whole base unit
        — a fractional pane origin/extent would round adjacent rows onto the
        same cell. Mirrors the Panel's own ``snap = not supports("pixel_layout")``
        rule so widgets resolve sub-layouts exactly as the top level does."""
        return self._caps.supports("pixel_layout")

    @property
    def vector_shapes(self) -> bool:
        """True when the backend renders true device pixels (rounded rects,
        hairlines, sub-unit insets). False on a character grid — including a grid
        handed a GUI profile for a test — where sub-unit padding and frame lines
        must collapse so content stays aligned to whole cells. Widgets read this
        only to drop pixel-only ornamentation; the visible-vs-grid drawing choice
        itself still lives in the Panel layer (round_rect, draw_*_mark)."""
        return self._caps.supports("vector_shapes")

    @property
    def animated(self) -> bool:
        """True when the backend drives per-frame animation ticks. A widget that
        drives its own motion (a busy spinner, a blinking caret) reads it to
        decide whether to register ticks via ``panel.request_animation_ticks``;
        on a still backend it just renders a single frame whenever the panel
        re-renders. This is broader than the ``animation`` capability (rich
        transitions): a TUI cannot composite a transition but its event loop can
        still wake on a timer, so ``animation_ticks`` alone is enough. The
        capability is resolved here, not by the widget."""
        return self._caps.supports("animation") or self._caps.supports("animation_ticks")

    def animated_color(self, default: Any = None, key: Any = None) -> Any:
        """Current value of a color transition started on this widget via
        ``panel.animate(widget, hints={"transition": "color", ...})``, or
        ``default`` if none is running. The Panel interpolates between the
        ``from``/``to`` colors each frame and the widget draws with the result,
        so a row can settle from a highlight to its resting color the same way
        on TUI (palette-snapped per frame) and GUI — no branch on the backend.
        Pass ``key`` to read one of several colors animating on the widget."""
        if self._panel is None or self._widget is None:
            return default
        return self._panel.animated_color(self._widget, key=key, default=default)

    @property
    def native_menus(self) -> bool:
        """True when the backend owns an OS menu bar / context menus. A MenuBar
        widget reads it to know it should register the native bar and claim no
        in-window space, instead of rendering an in-window strip. The
        capability is resolved here, not by the widget."""
        return self._caps.supports("native_menus")

    @property
    def focused(self) -> bool:
        """True when this widget holds the keyboard focus. Interactive widgets
        use it to draw a focus cue (a reversed marker, a cursor); the value is
        resolved by the Panel/container chain, not by the widget."""
        return self._focused

    @property
    def hovered(self) -> bool:
        """True when the mouse pointer is over this widget's visible rect.
        Drives modern hover styling; resolved by pure geometry against the
        Panel's last pointer position, so widgets never track the mouse."""
        if self._panel is None or self._panel.pointer is None:
            return False
        px, py = self._panel.pointer
        return self._hit_rect is not None and self._hit_rect.contains(px, py)

    def hovered_in(self, w: float, h: float | None = None) -> bool:
        """True when the pointer is over the local sub-rect ``(0, 0, w, h)`` of
        this widget. A control that draws narrower than its slot (e.g. a checkbox
        or a field in a full-width ScrollView row) restricts its hover to the
        part it actually occupies, so the cue does not light up the empty space
        to its right. ``h`` defaults to the full widget height."""
        if self._panel is None or self._panel.pointer is None:
            return False
        px, py = self._panel.pointer
        hh = self._rect.h if h is None else h
        within = (
            self._rect.x <= px < self._rect.x + w
            and self._rect.y <= py < self._rect.y + hh
        )
        return within and self._clip.contains(px, py)

    def set_cursor(self, shape: str | None) -> None:
        """Ask for the mouse pointer shape over this widget, named with a CSS/X
        cursor name (``"text"``, ``"pointer"``, ``"not-allowed"``, ...). Call it
        during draw, usually gated on ``hovered`` (a text field requests
        ``"text"`` only while the pointer is over its editable area). One intent,
        resolved by the Panel: a capable backend sets a real OS cursor, others
        no-op. See ``Panel.request_pointer_shape``."""
        if self._panel is not None:
            self._panel.request_pointer_shape(shape)

    @property
    def pressed(self) -> bool:
        """True while the active press both *began* inside this widget and the
        pointer is *still* over it — the held pressed state of an action control
        (a button, a tab title; docs/interaction_states.md §2). Read only by
        controls whose click is otherwise invisible; value/navigation controls
        ignore it. Resolved by geometry against the Panel's press anchor and
        current pointer, so the cue lights on press, clears if the pointer drags
        off (a cancel), and lights again if it returns — widgets never track the
        mouse button. Always False on a backend with no press/release events."""
        if self._panel is None:
            return False
        down = self._panel.press_down
        cur = self._panel.pointer
        if down is None or cur is None or self._hit_rect is None:
            return False
        return (
            self._hit_rect.contains(down[0], down[1])
            and self._hit_rect.contains(cur[0], cur[1])
        )

    @property
    def caret_visible(self) -> bool:
        """Current on/off phase of the text caret blink. A field reads it when
        drawing its caret (via ``draw_caret``); the Panel owns the blink clock,
        so every blinking caret shares one phase. Always True on a still backend
        (a solid, non-blinking caret)."""
        return self._panel.caret_visible if self._panel is not None else True

    @property
    def panel(self) -> "Panel | None":
        """The owning Panel — the one API a widget talks to for layers
        (push_layer for a popup) and text input (request_text_input for IME).
        Captured by widgets during draw and used later in event handling."""
        return self._panel

    @property
    def screen_rect(self) -> tuple[float, float, float, float]:
        """This widget's absolute rect in base units (x, y, w, h). A widget
        uses it to position a popup layer under itself or to tell the backend
        where its text cursor is for the IME candidate window."""
        return (self._rect.x, self._rect.y, self._rect.w, self._rect.h)

    @property
    def theme(self) -> "Theme | None":
        return self._panel.theme if self._panel is not None else None

    @property
    def background(self) -> tuple[int, int, int] | None:
        """The pane background this context inherits (the resolved "bg" hint or
        surface-role color of the slot, else the parent's). A widget that paints
        its own colored fill reads it to pick a foreground that contrasts with
        whatever surface it landed on — so text stays legible under any theme,
        light or dark. ``None`` means no pane background was set (the backend
        default shows through)."""
        return self._background

    def ink(self, color: Color, *, on: Color | None = None, target: float = LC_BODY) -> Color:
        """Return ``color`` adjusted to stay legible on the surface it will paint
        on — ``on`` if given, else this pane's :attr:`background`. Floor-only: a
        color that already meets the APCA ``target`` (Lc) is returned unchanged,
        so a theme's designed hues are preserved wherever they already read and
        only lifted — hue kept, chroma spent minimally — where they would not.

        A widget that paints its own local fill (a selection tint, a highlight)
        passes that fill as ``on`` so its text contrasts against what is actually
        behind it, not the pane default. Pass ``target=LC_LARGE`` for large/bold
        chrome, ``LC_BODY`` for body text. See :func:`puikit.color.legible_ink`.
        """
        bg = on if on is not None else self._background
        if bg is None:
            return color  # no known background to contrast against; leave as-is
        return legible_ink(color, bg, target)

    def layout_context(self) -> "Any":
        """Build a LayoutContext matching this backend's capabilities, so a
        widget can resolve a nested puikit.layout Split against its own rect
        with the same base unit-vs-pixel rules the Panel uses for the top level.
        Capability resolution stays here, not in the widget."""
        from .layout import LayoutContext

        cw, ch = self._backend.base_size
        return LayoutContext(
            cw, ch,
            snap=not self._caps.supports("pixel_layout"),
            hairline=self._caps.supports("hairline"),
            native_menus=self._caps.supports("native_menus"),
            measure=self._backend.measure_text,
            line_height=self._backend.measure_line_height,
            scrollbar_units=self._backend.scrollbar_units,
            image_size=self._backend.image_size,
        )

    def line_height(self, style: Style = DEFAULT_STYLE) -> float:
        """Row pitch of ``style``'s font in this pane's unit: one base unit for
        the grid font, more for a taller per-Style font. A stacked-text widget
        uses it to space its rows so a proportional/sized font does not overlap.
        The font is folded first, matching what draw_text will draw."""
        return self._backend.measure_line_height(self._resolve(style))

    def font_size(self, style: Style = DEFAULT_STYLE) -> float:
        """Resolved point size of ``style``'s font, in points. A widget derives a
        size relative to it (a heading scaled off the body) and keeps only the
        ratio — the absolute size stays the backend's. The font is folded first,
        matching what draw_text will draw, so a whole-unit backend reports the
        nominal base size and the same relative math runs everywhere."""
        return self._backend.measure_font_size(self._resolve(style))

    def font_metrics(self, style: Style = DEFAULT_STYLE) -> FontMetrics:
        """Ascent/descent of ``style``'s font in this pane's unit (base units).
        A widget that lays several fonts on one row sizes the row to
        ``max(ascent)+max(descent)`` and draws each run with ``draw_text_baseline``
        at ``row_top + max(ascent)`` so their baselines line up. The font is
        folded first, matching what draw_text will draw."""
        return self._backend.font_metrics(self._resolve(style))

    def measure_text(self, text: str, style: Style = DEFAULT_STYLE) -> float:
        """Displayed width of ``text`` in this pane's unit (base units;
        fractional on GUI), so a widget can center, right-align, or wrap
        proportional text against its pane size. Whole-unit backends count
        columns; the font is folded first, matching what draw_text will draw."""
        return self._backend.measure_text(text, self._resolve(style))

    def draw_text(self, x: float, y: float, text: str, style: Style = DEFAULT_STYLE,
                  *, ink: bool = True) -> None:
        # ``ink=False`` opts this run out of auto-ink (see Panel.auto_ink): the
        # color is drawn exactly as given, for text whose palette a widget owns
        # deliberately — syntax highlighting, a color legend — and does not want
        # normalized to a contrast floor.
        # Gate on the exact (possibly fractional) extent and let the
        # backend's clip rect cut the overflow: a pane squeezed to 0.97
        # base units by pixel rounding must still render its row 0, clipped at
        # the pane edge, not drop it. y may be fractional and is allowed to
        # start within one unit above the pane (a row mid-scroll off the top,
        # e.g. ListView's smooth scroll): its visible part is clipped in. x may
        # be fractional too (a few pixels of sub-unit padding on a pixel backend);
        # the slice math is taken in whole cells so truncation stays grid-safe.
        if not -1 < y < self._rect.h:
            return
        resolved = self._text_style(style, ink=ink)
        if resolved.font is not None:
            # Proportional / sized flow text: a character is not one base unit
            # wide, so slicing by ceil(width) columns would chop trailing glyphs
            # that still fit. Hand the whole run to the backend and let the pane
            # clip rect trim it at the exact pixel edge instead.
            if text:
                self._backend.draw_text(self._rect.x + x, self._rect.y + y, text, resolved)
            return
        if x < 0:
            text = text[int(math.ceil(-x)):]
            x = 0
        text = text[: max(0, math.ceil(self._rect.w - x))]
        if not text:
            return
        self._backend.draw_text(self._rect.x + x, self._rect.y + y, text, resolved)

    def draw_text_baseline(self, x: float, baseline_y: float, text: str,
                           style: Style = DEFAULT_STYLE, *, ink: bool = True) -> None:
        """Draw ``text`` with its baseline at ``baseline_y`` (this pane's unit)
        instead of positioning by the top of the line box. A widget that mixes
        fonts on one row reads each font's ``font_metrics``, sizes the row, and
        draws every run at the same baseline so they align. Whole-run, unsliced
        (the pane clip trims overflow) — the proportional/mixed-font path, not
        the grid path. ``ink`` matches ``draw_text``."""
        if not text:
            return
        resolved = self._text_style(style, ink=ink)
        self._backend.draw_text_baseline(
            self._rect.x + x, self._rect.y + baseline_y, text, resolved
        )

    def draw_box(
        self,
        x: int,
        y: int,
        w: int,
        h: int,
        style: Style = DEFAULT_STYLE,
        hints: dict[str, Any] | None = None,
    ) -> None:
        self._backend.draw_box(
            self._rect.x + x, self._rect.y + y, w, h, self._resolve(style), hints
        )

    def draw_border(
        self, style: Style = DEFAULT_STYLE, hints: dict[str, Any] | None = None
    ) -> None:
        """Box around the widget's exact extent, framing it. Unlike draw_box
        with width/height (whole base units), this covers fractional edges on
        pixel-layout backends, so adjacent widgets meet without gaps.

        The frame owns its outline, so everything drawn afterwards on this
        context (text, children) is clipped to the interior: the box's stroke
        is one device pixel on pixel-layout backends and one base unit on whole-unit
        backends, and the content clip is inset by exactly that, at pixel
        granularity. Content can fill right up to the inner edge of the frame
        but never paints over the frame line itself."""
        self._backend.draw_box(
            self._rect.x, self._rect.y, self._rect.w, self._rect.h, self._resolve(style), hints
        )
        # Inset the content region by the stroke width: one device pixel on
        # pixel-layout backends, one whole base unit on whole-unit backends.
        if self._caps.supports("pixel_layout"):
            cw, ch = self._backend.base_size
            ix = 1.0 / cw if cw else 0.0
            iy = 1.0 / ch if ch else 0.0
        else:
            ix = iy = 1.0
        interior = Rect(
            self._rect.x + ix, self._rect.y + iy,
            max(0.0, self._rect.w - 2 * ix), max(0.0, self._rect.h - 2 * iy),
        )
        clip = _intersect(self._clip, interior)
        if clip is None:
            clip = Rect(interior.x, interior.y, 0.0, 0.0)
        self._backend.push_clip(clip.x, clip.y, clip.w, clip.h)
        self._clip = clip
        self._pushed_clips += 1

    def _close(self) -> None:
        """Pop any backend clips this context pushed (see draw_border).
        Called by the Panel once the widget's draw returns."""
        for _ in range(self._pushed_clips):
            self._backend.pop_clip()
        self._pushed_clips = 0

    def draw_scrollbar(
        self, x: int, y: int, h: int, pos: float, ratio: float,
        style: Style = DEFAULT_STYLE, orientation: str = "vertical",
        surface: tuple[int, int, int] | None = None,
    ) -> None:
        # Scrollbar colors are theme tokens, not the pane background: fill the
        # thumb (fg) and track (bg) from the theme when the caller leaves them
        # unset, and hand the backend the explicit colors. Routing through
        # _resolve instead would inject the pane background as the track — which,
        # now that the backend paints the track from style.bg, would make the
        # groove vanish into the surface. A caller may still override either.
        # ``orientation`` selects a vertical (default) or horizontal bar.
        theme = self._panel.theme if self._panel is not None else None
        fg = style.fg
        bg = style.bg
        if theme is not None:
            if fg is None:
                fg = getattr(theme, "scrollbar_thumb", None)
            if bg is None:
                bg = getattr(theme, "scrollbar_track", None)
        self._backend.draw_scrollbar(
            self._rect.x + x, self._rect.y + y, h, pos, ratio,
            Style(fg=fg, bg=bg, attr=style.attr), orientation, surface,
        )

    def draw_icon(
        self,
        x: int,
        y: int,
        icon_name: str,
        style: Style = DEFAULT_STYLE,
        hints: dict[str, Any] | None = None,
    ) -> None:
        if self._caps.supports("icons"):
            self._backend.draw_icon(
                self._rect.x + x, self._rect.y + y, icon_name, self._resolve(style)
            )
            return
        hints = hints or {}
        fallback = hints.get("fallback_text") or ICON_TEXT_FALLBACKS.get(icon_name, "?")
        self.draw_text(x, y, fallback, style)

    def draw_image(
        self, x: int, y: int, path: str, hints: dict[str, Any] | None = None
    ) -> None:
        if self._caps.supports("images"):
            self._backend.draw_image(self._rect.x + x, self._rect.y + y, path, hints)
            return
        # Fallback for backends without images (TUI): the picture is replaced
        # by a single glyph — the "alt" emoji — centered in the footprint, so
        # the image still reads as a mark on the grid. Without an explicit alt,
        # a neutral "●" stands in. The widget never branches on capability.
        from .text import display_width

        hints = hints or {}
        # The glyph lives on the whole-unit grid, so snap the (possibly
        # fractional) origin and extent to cells. A caller may pass a float
        # x/y (e.g. a centered icon).
        x, y = int(x), int(y)
        w = int(hints.get("w", self.width - x))
        h = int(hints.get("h", self.height - y))
        if w <= 0 or h <= 0:
            return
        glyph = hints.get("alt") or "●"
        gx = x + max(0, (w - display_width(glyph)) // 2)
        gy = y + h // 2
        self.draw_text(gx, gy, glyph)

    def fill_rect(
        self, x: float, y: float, w: float, h: float, style: Style = DEFAULT_STYLE
    ) -> None:
        self._backend.fill_rect(
            self._rect.x + x, self._rect.y + y, w, h, self._resolve(style)
        )

    # --- modern control faces (vector on capable backends, grid otherwise) ----

    def round_rect(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        style: Style = DEFAULT_STYLE,
        radius: float | None = 4.0,
        hints: dict[str, Any] | None = None,
    ) -> None:
        """A rounded rectangle control face (button, field, mark box). On
        ``vector_shapes`` backends it draws real rounded corners; otherwise the
        rounding is dropped and the rect renders as a plain fill (hints
        ``fill``) and/or a box-drawing outline (``style.fg``), so a control
        reads correctly on a character grid too. ``radius`` is in device
        pixels; ``None`` means fully rounded (a circle/pill)."""
        style = self._resolve(style)
        hints = hints or {}
        if self._caps.supports("vector_shapes"):
            self._backend.draw_round_rect(
                self._rect.x + x, self._rect.y + y, w, h, radius, style, hints
            )
            return
        # Grid fallback: rounding is meaningless on whole cells.
        if hints.get("fill") and style.bg is not None:
            self._backend.fill_rect(self._rect.x + x, self._rect.y + y, w, h, Style(bg=style.bg))
        if style.fg is not None:
            iw, ih = round(w), round(h)
            if iw >= 2 and ih >= 2:
                self._backend.draw_box(
                    self._rect.x + x, self._rect.y + y, iw, ih, style, hints
                )

    def draw_hairline(
        self,
        x: float,
        y: float,
        length: float,
        *,
        vertical: bool = False,
        style: Style = DEFAULT_STYLE,
    ) -> None:
        """A thin separating line — the intent primitive for a rule, a column
        divider, or a menu / blockquote bar. This is where the visible-vs-grid
        choice is made, so a widget never branches on ``vector_shapes`` to pick
        it (mirroring :meth:`round_rect` and :meth:`draw_divider`):

        - ``vector_shapes`` backends stroke a **device-pixel-thin** ``fill_rect``;
        - grid backends draw the box-drawing run (``─`` / ``│``).

        The *across-axis* coordinate is the line's **centerline**; the *along-axis*
        coordinate is its start. So for a horizontal line ``x`` is the left end and
        ``y`` is the vertical center; for a vertical line ``y`` is the top and ``x``
        is the horizontal center. On a grid the glyph lands in the cell that
        centerline falls in (``int``), so passing ``cell + 0.5`` targets that cell.
        ``length`` is the extent in base units along the axis.

        The line color is ``style.fg`` (falling back to the theme divider color);
        pass a full ``Style`` to also set a cell background on the grid glyph.
        Callers wanting seamless multi-row vertical connection on a terminal pass
        an fg-only style so the glyphs sit on the default terminal background. For
        a *layout* Divider use :meth:`draw_divider`; this is the free-form
        companion for lines a widget positions itself."""
        color = style.fg
        if color is None:
            theme = self.theme
            color = theme.divider_color if theme is not None else (110, 110, 124)
        if self._caps.supports("vector_shapes"):
            bw, bh = self.base_size
            if vertical:
                lw = 1.0 / max(1, bw)
                self.fill_rect(x - lw / 2.0, y, lw, length, Style(bg=color))
            else:
                lh = 1.0 / max(1, bh)
                self.fill_rect(x, y - lh / 2.0, length, lh, Style(bg=color))
            return
        # Grid: the box-drawing run, in the cell the centerline falls in. Drawn
        # with ink=False — a divider is a structural line, not text; its color is
        # a deliberate subtle line color and must render exactly (and match the box
        # frame, which resolves without auto-ink), not be lifted to a text floor.
        n = max(1, int(round(length)))
        if vertical:
            col = int(x)
            for row in range(n):
                self.draw_text(col, int(y) + row, "│", style, ink=False)
        else:
            self.draw_text(x, int(y), "─" * n, style, ink=False)

    def draw_frame_divider(self, y: float, style: Style = DEFAULT_STYLE) -> None:
        """A horizontal rule spanning this context edge to edge that **connects
        into the surrounding box frame** — the separator under a dialog's title
        bar. Unlike :meth:`draw_hairline` (a free-floating rule a widget positions
        anywhere), this reaches both side borders and joins them, so it is the
        intent for splitting a framed surface into a header and a body. Like the
        other face primitives it owns the visible-vs-grid choice, so a widget never
        branches on ``vector_shapes`` to draw it:

        - a ``vector_shapes`` backend strokes a **device-pixel-thin** ``fill_rect``
          across the full width, meeting the frame's vertical strokes at both ends;
        - a grid backend draws the box-drawing run with left/right tee glyphs
          (``├`` … ``┤``) so the single-line frame stays continuous through the row.

        ``y`` is the row the rule sits in (grid) / its centerline (vector). The line
        color is ``style.fg`` (falling back to the theme popup-border / divider
        color); ``style.bg`` sets the surface behind the grid glyphs, which callers
        should pin to the dialog fill so the run doesn't sit on the layer's default
        (darker) background."""
        color = style.fg
        if color is None:
            theme = self.theme
            if theme is not None:
                color = getattr(theme, "popup_border", None) or theme.divider_color
            else:
                color = (110, 110, 124)
        if self.vector_shapes:
            _, bh = self.base_size
            lh = 1.0 / max(1, bh)
            wu = self.size_units[0]
            self.fill_rect(0.0, y - lh / 2.0, wu, lh, Style(bg=color))
            return
        # Grid: box-drawing run with tee ends so it fuses with the single-line
        # frame. Carry style.bg so the glyphs sit on the dialog surface, not the
        # layer's default fill (an fg-only style would paint a dark band on TUI).
        # ink=False: the rule is part of the frame — a structural line whose color
        # must match the box border exactly, not be lifted to a text contrast floor
        # (the box border resolves without auto-ink, so the rule must too).
        w = self.width
        if w < 2:
            return
        self.draw_text(0, int(y), "├" + "─" * (w - 2) + "┤", Style(fg=color, bg=style.bg), ink=False)

    def draw_focus_brackets(
        self,
        w: float,
        h: float,
        theme: "Theme",
        *,
        bg: tuple[int, int, int] | None = None,
        fg: tuple[int, int, int] | None = None,
    ) -> None:
        """Grid-only Outline-focus cue for a short whole-widget control
        (DropDown, ComboBox, TextEdit, a short text Button). The control reserves
        a padding column on each side, so a **bold** ``[`` and ``]`` are stamped
        there to frame the active field — the character-grid resolution of the
        accent focus ring vector backends draw (docs/interaction_states.md §6).

        A no-op on ``vector_shapes`` backends (they draw the real ring). The
        brackets sit on the control's vertically centered row; callers only
        invoke this where ``round_rect`` does *not* draw a box frame (a one-row
        field, or a text button under three rows), so the two cues never both
        fire. ``w`` is the *field* width in base units (not the whole slot), and
        ``bg`` is the field background so the brackets sit on the field, not a
        bare cell. ``fg`` is the bracket color and must **contrast that bg** — it
        defaults to the accent, but a control whose fill is *already* the accent
        (a primary button) passes a light color so the brackets never vanish
        accent-on-accent."""
        if self._caps.supports("vector_shapes"):
            return
        iw, ih = round(w), round(h)
        if iw < 2 or ih < 1:
            return
        ty = (ih - 1) // 2
        style = Style(fg=fg or theme.accent, bg=bg, attr=TextAttribute.BOLD)
        self.draw_text(0, ty, "[", style)
        self.draw_text(iw - 1, ty, "]", style)

    def draw_check_mark(
        self, x: float, y: float, *, checked: bool, focused: bool, theme: "Theme",
        row_bg: tuple[int, int, int] | None = None, row_h: float = 1.0,
    ) -> None:
        """Draw a checkbox mark whose row band starts at (x, y), vertically
        centered in the ``row_h``-unit band. Vector backends get a rounded box —
        a neutral fill with a check when on; focus recolors the box border to the
        accent. Grid backends fall back to the ``[x]`` / ``[ ]`` text mark
        (reverse-video when focused). The caller reserves the same column slot
        either way, so the label aligns identically on every backend.

        Focus owns the border *color* (accent), "checked" owns the check glyph
        plus a neutral border emphasis when unfocused — one box, not a box plus a
        separate focus halo."""
        if not self._caps.supports("vector_shapes"):
            mark = "[x]" if checked else "[ ]"
            if focused:
                style = Style(fg=theme.button_text, bg=theme.accent)
            else:
                style = Style(fg=theme.text, bg=row_bg)
            self.draw_text(int(x), y, mark, style)
            return
        bx, by, w_u, h_u, side = self._mark_box(x, y, row_h)
        # Focus recolors the box border to the accent (no separate halo ring);
        # otherwise the border carries "checked" as a neutral emphasis.
        if focused:
            border = theme.accent
        else:
            border = theme.text if checked else theme.control_border
        self.round_rect(
            bx, by, w_u, h_u, Style(bg=theme.control_bg, fg=border),
            radius=max(2.0, side * 0.28), hints={"fill": True},
        )
        if checked:
            self._draw_check(bx, by, w_u, h_u, Style(fg=theme.text))

    def draw_radio_mark(
        self, x: float, y: float, *, selected: bool, focused: bool, theme: "Theme",
        row_bg: tuple[int, int, int] | None = None, row_h: float = 1.0,
    ) -> None:
        """Draw a radio mark whose row band starts at (x, y). Vector backends get
        a circle — with a filled neutral dot when selected — vertically centered
        in the ``row_h``-unit band; grid backends fall back to the ``(•)`` /
        ``( )`` text mark.

        Focus is shown by recoloring the *selected* circle to the accent (a radio
        is a group-level choice, and the selected circle is always present), not
        by a box around the group. On a grid the focused group reverses its
        selected mark."""
        if not self._caps.supports("vector_shapes"):
            mark = "(•)" if selected else "( )"
            if focused and selected:
                style = Style(fg=theme.button_text, bg=theme.accent)
            else:
                style = Style(fg=theme.text, bg=row_bg)
            self.draw_text(int(x), y, mark, style)
            return
        bx, by, w_u, h_u, side = self._mark_box(x, y, row_h)
        # Focus recolors the selected circle to the accent; otherwise neutral.
        if focused and selected:
            border = theme.accent
        else:
            border = theme.text if selected else theme.control_border
        self.round_rect(
            bx, by, w_u, h_u, Style(bg=theme.control_bg, fg=border),
            radius=None, hints={"fill": True},
        )
        if selected:
            dw, dh = w_u * 0.46, h_u * 0.46
            self.round_rect(
                bx + (w_u - dw) / 2.0, by + (h_u - dh) / 2.0, dw, dh,
                Style(bg=theme.text), radius=None, hints={"fill": True},
            )

    def draw_caret(
        self, x: float, y: float, *, height: float, theme: "Theme",
        glyph: str = " ", visible: bool = True,
    ) -> None:
        """Draw a text caret whose top-left sits at (x, y), ``height`` base units
        tall. Vector backends get a thin vertical I-beam in the foreground color
        between glyphs (the ``theme.text`` color, not the accent — focus is
        carried by the field border, so the caret only marks the insertion point,
        docs/interaction_states.md §3). ``visible`` is the blink phase: when False
        the caret is not drawn this frame.

        Grid backends draw **nothing**: the backend already places the terminal's
        own hardware cursor at the focused field's caret (``request_text_input``,
        applied in ``present()``), so the native cursor — in the user's configured
        shape and blink — *is* the caret. Painting a reverse-video block over it
        would only fight the real cursor and ignore the user's terminal settings."""
        if not visible:
            return
        if self._caps.supports("vector_shapes"):
            bw = self.base_size[0]
            w = 1.0 / bw if bw else 0.1  # ~one device pixel wide
            self.fill_rect(x, y, w, height, Style(bg=theme.text))

    def _mark_box(
        self, x: float, y: float, row_h: float = 1.0
    ) -> tuple[float, float, float, float, float]:
        """Geometry for a checkbox/radio mark box: a pixel-square, vertically
        centered in a ``row_h``-unit row band starting at ``y``, returned as
        (x, y, w, h) in base units plus the side length in device pixels. Square
        in pixels even though a base unit cell is taller than it is wide, so the
        box may be taller than one unit — callers that stack marks pass a
        ``row_h`` large enough to contain it (see RadioGroup). As a safety net the
        box is also capped to ``row_h`` so its rounded top/bottom are never
        clipped, even if a layout hands the widget a tighter row than it asked
        for."""
        bw, bh = self.base_size
        side, w_u, h_u = mark_box_units(bw, bh)
        max_side = row_h * bh * 0.96  # leave a hair of vertical margin
        if max_side > 0 and side > max_side:
            side = max_side
            w_u = side / bw if bw else 1.0
            h_u = side / bh if bh else 1.0
        return (x + 0.2, y + (row_h - h_u) / 2.0, w_u, h_u, side)

    def _draw_check(
        self, x: float, y: float, w: float, h: float, style: Style = DEFAULT_STYLE
    ) -> None:
        if self._caps.supports("vector_shapes"):
            self._backend.draw_check(
                self._rect.x + x, self._rect.y + y, w, h, self._resolve(style)
            )

    def draw_chevron(
        self, x: float, y: float, w: float, h: float, *,
        expanded: bool, style: Style = DEFAULT_STYLE,
    ) -> None:
        """Draw a disclosure chevron inscribed in the base-unit rect — a ``>``
        that rotates to ``⌄`` when ``expanded`` — stroked with ``style.fg``. The
        intent primitive for a tree / list expander mark:

        - a ``vector_shapes`` backend strokes crisp diagonals so the mark reads as
          UI chrome rather than a font character;
        - a grid backend draws **nothing** here — its caller keeps the ``▸``/``▾``
          glyph inline in the row's text so the mark truncates with the row's
          box-drawing connectors and lands on the cell grid.

        Vector-only, mirroring :meth:`draw_caret`: the caller reserves the same
        marker slot either way, so the label origin and the expander hit region
        are unchanged whichever path draws the mark."""
        if self._caps.supports("vector_shapes"):
            self._backend.draw_chevron(
                self._rect.x + x, self._rect.y + y, w, h, expanded, self._resolve(style)
            )

    def draw_divider(self, divider: "Any") -> None:
        """Render a layout Divider in this context's coordinates, mirroring
        the Panel's top-level divider drawing: a hairline on hairline-capable
        backends, box-drawing characters otherwise. The hairline/base unit choice
        is made here, so the hosting widget never branches on capability."""
        theme = self.theme
        color = theme.divider_color if theme is not None else (110, 110, 124)
        rect = divider.rect
        if self._caps.supports("hairline"):
            self.fill_rect(rect.x, rect.y, rect.w, rect.h, Style(bg=color))
            return
        # Box-drawing glyphs (│ / ─) must be drawn on the DEFAULT terminal
        # background (bg=None), so we go straight to the backend and skip the
        # pane-background inheritance that DrawContext.draw_text/_resolve would
        # apply. Terminals such as macOS Terminal.app render box-drawing
        # characters as seamless connected lines ONLY on the default background;
        # a custom cell background color forces the per-cell font glyph instead,
        # which leaves inter-line gaps where the terminal's line spacing shows
        # through. (Same character and attributes either way — only the cell
        # background decides whether the line connects.) This also matches the
        # Panel's top-level _draw_divider, which already draws on the default
        # background, so layout dividers and widget dividers render identically.
        style = Style(fg=color)
        ox, oy = self._rect.x, self._rect.y
        if divider.vertical:
            for row in range(int(rect.h)):
                self._backend.draw_text(int(ox + rect.x), int(oy + rect.y) + row, "│", style)
        else:
            self._backend.draw_text(int(ox + rect.x), int(oy + rect.y), "─" * int(rect.w), style)

    def draw_child(
        self,
        widget: Any,
        x: float,
        y: float,
        w: float,
        h: float,
        hints: dict[str, Any] | None = None,
    ) -> None:
        """Draw a child widget at (x, y, w, h) in this context's coordinates.

        The child is clipped to the intersection of its rect with all
        enclosing clips, and gets its own animation group, so a parent's
        transition cascades to children while children can also animate
        individually. A "bg" hint (or the theme color of a "surface" role)
        fills the child's pane; otherwise the parent's background is
        inherited."""
        hints = hints or {}
        rect = Rect(self._rect.x + x, self._rect.y + y, w, h)
        if self._panel is not None:
            rect = self._panel._interpolate_rect(widget, rect)
        clip = _intersect(self._clip, rect)
        if clip is None:
            return
        self._backend.begin_group(widget, rect)
        # The clip is set inside the group so GUI transforms carry it along.
        self._backend.push_clip(clip.x, clip.y, clip.w, clip.h)
        pane_bg = hints.get("bg")
        if pane_bg is None and "surface" in hints and self._panel is not None:
            pane_bg = self._panel.theme.surface_bg(hints["surface"])
        background = pane_bg if pane_bg is not None else self._background
        if pane_bg is not None:
            self._backend.fill_rect(rect.x, rect.y, rect.w, rect.h, Style(bg=pane_bg))
        # A child is focused only if this context is focused and the parent
        # marked this child as its focused one (hints["focused"]).
        child_focused = self._focused and bool(hints.get("focused", False))
        child_ctx = DrawContext(
            self._backend, rect, self._caps,
            clip=clip, panel=self._panel, background=background,
            focused=child_focused, widget=widget,
        )
        widget.draw(child_ctx)
        child_ctx._close()
        self._backend.pop_clip()
        if self._panel is not None:
            self._panel._apply_group_effect(widget, rect)
        self._backend.end_group(widget)


@dataclass
class _Slot:
    widget: Any
    rect: Rect
    hints: dict[str, Any] = field(default_factory=dict)
    z: int = 0
    # Background fill extent. Differs from rect only for layout panes on the
    # window edge: their fill bleeds across the window margin so the frame
    # never shows the backend's default background.
    fill: Rect | None = None
    # Optional geometry recompute for a size-anchored layer. A layer whose rect
    # is derived from the window size (an edge Drawer fills the cross-axis and
    # hugs an edge) passes a callable (sw, sh) -> Rect so its rect is refreshed
    # from the current backend size on every render, tracking window resizes;
    # plain centered/positioned layers leave it None and keep their fixed rect.
    reflow: Any | None = None


def _bleed_to_window(
    rect: Rect, mx: float, my: float, sw: float, sh: float, snap: bool
) -> Rect:
    """Extend the sides of ``rect`` that lie on the margin bounds out to the
    window edges. Interior boundaries are untouched."""
    eps = 1e-6
    x0 = 0.0 if rect.x <= mx + eps else rect.x
    y0 = 0.0 if rect.y <= my + eps else rect.y
    x1 = sw if rect.x + rect.w >= sw - mx - eps else rect.x + rect.w
    y1 = sh if rect.y + rect.h >= sh - my - eps else rect.y + rect.h
    if snap:
        # Whole-unit backends must keep true integer coordinates.
        x0, y0, x1, y1 = (round(v) for v in (x0, y0, x1, y1))
    return Rect(x0, y0, x1 - x0, y1 - y0)


# A non-compositing backend (a terminal) cannot draw a smooth transition, so the
# Panel plays every animation there as exactly TWO frames — one intermediate
# state, then the target — instead of a multi-frame crawl that, once snapped to
# the character grid, only ever reads as flicker (the "2-frame policy"). The
# frame budget below is what ``stepped`` animations consume: ``step`` 1 lands on
# 0.5 (intermediate), ``step`` 2 on 1.0 (target), after which the animation is
# done. Compositing backends ignore this and interpolate continuously by time.
_TUI_ANIM_STEPS = 2


def _anim_progress(anim: Any, now: float) -> float:
    """Progress 0..1 of an animation. A ``stepped`` (terminal) animation snaps
    to the 2-frame schedule; otherwise progress is continuous in wall-clock
    time. The animation channels share this so every kind steps in lockstep."""
    if anim.stepped:
        return min(1.0, anim.step / _TUI_ANIM_STEPS)
    if anim.duration <= 0:
        return 1.0
    return min(1.0, max(0.0, (now - anim.start) / anim.duration))


@dataclass
class _GeometryAnimation:
    """Layout-level (geometry) transition the Panel drives by interpolating the
    widget's rect each frame, so it animates on *any* backend whose event loop
    can wake on a timer. Three shapes, composable:

    - size grow: the rect grows from (from_w, from_h) to its assigned size and
      the widget re-draws at each intermediate size (a true size change).
    - slide: the rect starts offset by (from_dx, from_dy) base units and moves
      to its anchored position (a drawer sliding in from an edge).
    - scale: the rect shrinks toward its center by ``from_scale`` and grows back
      to full — the terminal's stand-in for the composited zoom, expressed as a
      real (cell-snapped) rect inset since a grid cannot sub-scale glyphs.

    The clip travels with the rect, so an off-edge / inset part is clipped
    exactly as a composited backend's transform would clip it. Motion is linear;
    a character grid snaps each frame to whole base units (see
    ``_interpolate_rect``), so the region steps by an integer number of cells —
    and on a terminal the whole thing is just two such steps (see the 2-frame
    policy above)."""

    start: float
    duration: float
    from_w: float | None
    from_h: float | None
    from_dx: float = 0.0
    from_dy: float = 0.0
    from_scale: float | None = None
    # A slide normally moves an off-edge offset *to* the rest position (slide in);
    # ``out`` reverses it — the rect starts at rest and travels *to* the offset
    # (a drawer sliding back off its edge to close). on_complete fires once, after
    # the target frame, so a close can pop its layer when the slide finishes.
    out: bool = False
    on_complete: Any | None = None
    stepped: bool = False
    step: int = 0

    def progress(self, now: float) -> float:
        return _anim_progress(self, now)


@dataclass
class _ColorAnimation:
    """A color tween the Panel drives by interpolating between two RGB(A) colors;
    the animating widget reads the current value through
    ``DrawContext.animated_color`` and draws with it (a row settling from a
    highlight back to its resting color, a value pulsing on change).

    Needs no compositing — a terminal renders a different (palette-snapped) color
    per frame exactly like a GUI renders a different RGB — so it runs on any
    backend with ``animation_ticks``, as a 2-frame step on a terminal and a
    continuous tween on a compositing backend."""

    start: float
    duration: float
    from_color: tuple
    to_color: tuple
    stepped: bool = False
    step: int = 0

    def progress(self, now: float) -> float:
        return _anim_progress(self, now)

    def value_at(self, now: float) -> tuple:
        p = self.progress(now)
        return tuple(round(a + (b - a) * p) for a, b in zip(self.from_color, self.to_color))


@dataclass
class _EffectAnimation:
    """A group-level optical effect — ``fade`` or ``highlight`` — played on a
    non-compositing backend as part of the 2-frame policy. A terminal cannot
    composite alpha, so instead of interpolating opacity it shows ONE
    intermediate frame with a whole-cell stand-in over the widget's group (a
    fade reads as a dim pass, a highlight as a one-frame color flash) and then
    the clean target frame. Compositing backends never use this — there
    ``fade``/``highlight`` go to the backend's real alpha overlay.

    ``active`` is the binary the Panel needs: the effect is shown while the
    animation is on its intermediate frame (progress < 1) and gone on the target
    frame (progress == 1), after which the Panel drops it."""

    kind: str  # "fade" | "highlight"
    start: float
    duration: float
    hints: dict
    stepped: bool = False
    step: int = 0

    def progress(self, now: float) -> float:
        return _anim_progress(self, now)

    def active(self, now: float) -> bool:
        return self.progress(now) < 1.0


class Panel:
    """Owns widget layout, layers, focus, and event routing for one screen."""

    def __init__(self, backend: Backend, theme: Theme | None = None):
        self.backend = backend
        # The theme encodes the backend's region-separation strategy: GUI
        # themes separate surfaces with hairlines, TUI themes with
        # background contrast (a line would cost a whole base unit row/column).
        self.theme = theme if theme is not None else theme_for(backend.capabilities)
        # Opt-in legibility guarantee (off by default so existing apps render
        # byte-for-byte as before). When set, every text run is passed through
        # legible_ink against its own resolved background at draw time, with a
        # weight-aware target — so a widget states the color it *wants* and the
        # draw layer keeps it readable on any theme. See DrawContext._text_style
        # and docs/color_system.md.
        self.auto_ink: bool = False
        self._children: list[_Slot] = []
        self._layers: list[_Slot] = []
        self._dividers: list[Any] = []
        self._focused: Any | None = None
        # Tracks whether the backend's text-input system is currently engaged, so
        # focus changes toggle it only on a real transition (see _sync_text_input).
        self._text_input_on = False
        self._layout: Any | None = None
        self._margin_px = 0.0
        self._margin_units = 0.0
        self._size_anims: dict[Any, _GeometryAnimation] = {}
        # Running color tweens keyed by (widget, key); the widget reads the
        # current value via DrawContext.animated_color (see _ColorAnimation).
        self._color_anims: dict[tuple[Any, Any], _ColorAnimation] = {}
        # Running group optical effects (fade/highlight) keyed by widget, played
        # as a one-frame stand-in on non-compositing backends (see
        # _EffectAnimation and _apply_group_effect).
        self._effect_anims: dict[Any, _EffectAnimation] = {}
        # The app menu bar model (puikit.menu.Menu), if one was installed.
        self._menu_bar: Any | None = None
        # Last known pointer position in screen base units, fed by every mouse
        # event. DrawContext.hovered reads it to resolve hover styling.
        self._pointer: tuple[float, float] | None = None
        # Pointer shape a widget requested during this frame's draw (the topmost
        # hovered widget wins, since it draws last). Collected each render and
        # pushed to the backend once at the end of the frame; None resets to the
        # default arrow. Only does anything on a "pointer_shape" backend.
        self._pointer_shape: str | None = None
        # Active left-button press, captured between MOUSE_DOWN and MOUSE_UP so
        # action controls can show a held pressed cue (DrawContext.pressed) and
        # so a release only fires a click over the widget the press began on.
        # _press_down is the screen base-unit point of the press; _press_slot is
        # the slot (child or layer) it landed in.
        self._press_down: tuple[float, float] | None = None
        self._press_slot: "_Slot | None" = None
        self._press_is_layer = False
        # Reference time the caret blink square wave is measured from; resetting
        # it (on a caret move/edit) restarts the cycle with the caret on, so it
        # never blinks out at the moment the user is looking for it.
        self._caret_phase0 = time.monotonic()

    # --- layout management ---------------------------------------------------

    def add(
        self, widget: Any, x: int, y: int, w: int, h: int, hints: dict[str, Any] | None = None
    ) -> None:
        self._children.append(_Slot(widget, Rect(x, y, w, h), hints or {}))
        if self._focused is None and getattr(widget, "focusable", False):
            self._focused = widget

    def remove(self, widget: Any) -> None:
        self._children = [s for s in self._children if s.widget is not widget]
        self._layers = [s for s in self._layers if s.widget is not widget]
        if self._focused is widget:
            self._focused = None

    def clear(self) -> None:
        self._children.clear()
        self._layers.clear()
        self._dividers.clear()
        self._focused = None
        self._layout = None

    def set_layout(
        self, layout: Any, margin_px: float = 0.0, margin_units: float = 0.0
    ) -> None:
        """Use a declarative layout (see puikit.layout) instead of manual
        add() calls. Rects are recomputed from the backend size on every
        render, so the layout follows window resizes.

        margin_px / margin_units inset the layout from the window frame.
        They follow the min_px/min hint rules: the pixel margin
        applies only on pixel-layout backends (it would cost whole base units on
        a base unit grid), margin_units applies everywhere."""
        self._layout = layout
        self._margin_px = float(margin_px)
        self._margin_units = float(margin_units)
        self._apply_layout()

    def _resolve_margin(
        self, base_w: int, base_h: int, snap: bool
    ) -> tuple[float, float]:
        """Window margin in base units per axis, snapped to whole device pixels
        on pixel-layout backends."""
        if snap:
            margin = round(self._margin_units)
            return (margin, margin)
        mx, my = self._margin_units, self._margin_units
        if base_w > 0:
            mx = round(max(mx, self._margin_px / base_w) * base_w) / base_w
        if base_h > 0:
            my = round(max(my, self._margin_px / base_h) * base_h) / base_h
        return (mx, my)

    def _apply_layout(self) -> None:
        from .layout import LayoutContext

        # size_units is exact (fractional on pixel-layout backends), so the
        # layout tracks window resizes pixel by pixel, not base unit by base unit.
        sw, sh = self.backend.size_units
        cw, ch = self.backend.base_size
        snap = not self.backend.capabilities.supports("pixel_layout")
        mx, my = self._resolve_margin(cw, ch, snap)
        ctx = LayoutContext(
            cw, ch, snap,
            hairline=self.backend.capabilities.supports("hairline"),
            native_menus=self.backend.capabilities.supports("native_menus"),
            measure=self.backend.measure_text,
            line_height=self.backend.measure_line_height,
            metrics=self.backend.font_metrics,
            scrollbar_units=self.backend.scrollbar_units,
            image_size=self.backend.image_size,
        )
        placements = self._layout.resolve(
            mx, my, max(0.0, sw - 2 * mx), max(0.0, sh - 2 * my), ctx
        )
        # Edge panes' backgrounds and the dividers bleed across the window
        # margin: the margin reads as pane padding, never as a bare frame.
        from .layout import Divider

        self._dividers = [
            Divider(_bleed_to_window(d.rect, mx, my, sw, sh, snap), d.vertical, d.level)
            for d in ctx.dividers
        ]
        focused = self._focused
        self._children = [
            _Slot(w, rect, hints, fill=_bleed_to_window(rect, mx, my, sw, sh, snap))
            for w, rect, hints in placements
        ]
        widgets = [slot.widget for slot in self._children]
        if focused not in widgets:
            focused = next(
                (w for w in widgets if getattr(w, "focusable", False)), None
            )
        self._focused = focused

    # --- layer management ------------------------------------------------------

    def push_layer(
        self,
        widget: Any,
        z: int = 0,
        hints: dict[str, Any] | None = None,
        reflow: Any | None = None,
    ) -> None:
        hints = hints or {}
        rect = self._layer_rect(hints)
        self._layers.append(_Slot(widget, rect, hints, z, reflow=reflow))
        self._layers.sort(key=lambda s: s.z)

    def pop_layer(self) -> Any | None:
        if not self._layers:
            return None
        return self._layers.pop().widget

    @property
    def has_layers(self) -> bool:
        """True while any overlay layer (dialog, message box, menu popup) is
        open. The topmost layer is modal — it owns events — so an app whose own
        key handling is outside ``dispatch_event`` can consult this to defer to
        the active layer instead of acting on the key itself."""
        return bool(self._layers)

    def _layer_rect(self, hints: dict[str, Any]) -> Rect:
        # Center within the backend's exact (fractional) base unit extent, dividing
        # by 2 — not integer // on whole base units: a layer must center at device-
        # pixel precision on GUI, not snap to a whole base unit. Whole-unit backends
        # round at draw time. (Layout-unit base units everywhere; never a pixel.)
        sw, sh = self.backend.size_units
        w = hints.get("w", sw)
        h = hints.get("h", sh)
        x = hints.get("x", (sw - w) / 2)
        y = hints.get("y", (sh - h) / 2)
        if not self.backend.capabilities.supports("pixel_layout"):
            # Whole-unit backends snap the layer to the base unit grid. A
            # fractional origin (e.g. centering an odd-height box) would otherwise
            # land the layer on a half row, and a clipped child one base unit tall
            # would see its clip round to a degenerate range and vanish. Pixel
            # backends keep device-precise centering.
            x, y, w, h = (round(v) for v in (x, y, w, h))
        return Rect(x, y, w, h)

    # --- focus ----------------------------------------------------------------

    def focus(self, widget: Any) -> None:
        self._focused = widget

    @property
    def focused(self) -> Any | None:
        return self._focused

    # The Panel is the focus-traversal root. It exposes the same duck-typed
    # interface every container does (puikit.focus), so one walk crosses the
    # whole tree; only the root wraps, so focus cycles instead of escaping the
    # window.

    def focus_children(self) -> list[Any]:
        return [s.widget for s in self._children if getattr(s.widget, "focusable", False)]

    def get_focused(self) -> Any | None:
        return self._focused

    def set_focused(self, widget: Any) -> None:
        self._focused = widget

    def _focus_moved(self) -> None:
        """Top-level panes do not scroll, so the root has nothing to do when
        focus moves."""

    def focus_tab(self, direction: int) -> bool:
        """Advance focus to the next (direction > 0) or previous (< 0) focusable
        in the whole tree, wrapping at the ends."""
        return move_focus(self, direction, wrap=True)

    @property
    def pointer(self) -> tuple[float, float] | None:
        """Last pointer position in screen base units, or None."""
        return self._pointer

    @property
    def press_down(self) -> tuple[float, float] | None:
        """Screen base-unit point where the active left press began, or None.
        DrawContext.pressed pairs it with the current pointer."""
        return self._press_down

    @property
    def caret_visible(self) -> bool:
        """Current blink phase shared by every text caret. Solid (always True)
        on a still backend; a ~``_CARET_BLINK`` second square wave on a backend
        that drives animation ticks (a terminal's timer-woken event loop counts,
        the same one the caret registers its blink tick against), measured from
        the last blink reset so the caret shows immediately after a move."""
        caps = self.backend.capabilities
        if not (caps.supports("animation") or caps.supports("animation_ticks")):
            return True
        return int((time.monotonic() - self._caret_phase0) / _CARET_BLINK) % 2 == 0

    def reset_caret_blink(self) -> None:
        """Restart the caret blink cycle with the caret on. A field calls this
        whenever the caret moves or the text changes, so the caret is always
        visible at the spot the user just acted on."""
        self._caret_phase0 = time.monotonic()

    # --- rendering --------------------------------------------------------------

    def focused_leaf(self) -> Any | None:
        """The deepest focused widget — descending through focused containers to
        the leaf that actually holds focus (e.g. a ``TextEdit`` nested in a
        ScrollView), or None. The Panel's ``_focused`` is only the focused
        top-level slot; text-input gating needs the leaf.

        When an overlay layer is open it is modal — it owns events
        (``dispatch_event`` routes to the top layer) — so it owns the focus leaf
        too: descend from the top layer's widget, not the (page) ``_focused``.
        This is what lets a ``TextEdit`` inside a modal dialog / drawer engage
        the IME, and it survives ``_apply_layout`` (which only manages page
        focus) because it reads the layer, not ``_focused``."""
        w = self._layers[-1].widget if self._layers else self._focused
        while isinstance(w, FocusContainer):
            nxt = w.get_focused()
            if nxt is None:
                break
            w = nxt
        return w

    def _sync_text_input(self) -> None:
        """Engage or release the backend's text-input system to match focus:
        active iff the focused leaf widget declares ``wants_text_input``. Called
        every render; idempotent, so it touches the backend only on a real
        transition (a text field gaining or losing focus)."""
        want = bool(getattr(self.focused_leaf(), "wants_text_input", False))
        if want == self._text_input_on:
            return
        self._text_input_on = want
        if want:
            self.backend.begin_text_input()
        else:
            self.backend.end_text_input()

    def render(self) -> None:
        self._sync_text_input()
        if self._layout is not None:
            self._apply_layout()
        self.backend.clear()
        # Reset the per-frame cursor request; widgets re-declare it via
        # request_pointer_shape while drawing, topmost (last) wins.
        self._pointer_shape = None
        # A widget's draw() can have the side effect of resizing the window —
        # notably MenuBar installing a native OS menu bar the first time it
        # draws (WindowsBackend.set_menu_bar -> SetMenu), which synchronously
        # shrinks the client area to make room for it. That first draw pass
        # already laid out every widget against the taller, menu-less size, so
        # once the menu lands, the bottom-most fixed item (e.g. the status
        # bar) overflows past the new client edge and is clipped — until the
        # next render() recomputes against the corrected size. Detect the
        # change here and redo layout + drawing once against the corrected
        # size, so the first frame is already right (MenuBar only installs
        # once, so this can only recurse one level deep).
        size_before = self.backend.size_units
        for slot in self._children:
            self._draw_slot(slot)
        if self._layout is not None and self.backend.size_units != size_before:
            self._apply_layout()
            self.backend.clear()
            self._pointer_shape = None
            for slot in self._children:
                self._draw_slot(slot)
        for divider in self._dividers:
            self._draw_divider(divider)
        for i, slot in enumerate(self._layers):
            # The topmost layer is modal — it owns events exclusively (see
            # dispatch_event), so it must own the pointer shape too. Discard any
            # cursor a widget *beneath* it requested right before it draws, so a
            # resize/text cursor can't leak out from under the modal (including
            # the area a non-fullscreen dialog does not cover). Only the modal's
            # own request, gated on its hover, then survives.
            if i == len(self._layers) - 1:
                self._pointer_shape = None
            self._render_layer(slot)
        # Push the resolved shape to the backend once per frame. Gated on the
        # capability so the intent is a silent no-op everywhere else.
        if self.backend.capabilities.supports("pointer_shape"):
            self.backend.set_pointer_shape(self._pointer_shape)
        self.backend.present()

    def _pane_background(self, hints: dict[str, Any]) -> tuple[int, int, int] | None:
        """Pane background: explicit "bg" hint, else the theme color of the
        "surface" role. Roles let each backend pick its own separation
        strategy (TUI: contrasting backgrounds; GUI: shared background plus
        hairlines)."""
        background = hints.get("bg")
        if background is None and "surface" in hints:
            background = self.theme.surface_bg(hints["surface"])
        return background

    def _draw_divider(self, divider: Any) -> None:
        rect = divider.rect
        if self.backend.capabilities.supports("hairline"):
            self.backend.fill_rect(
                rect.x, rect.y, rect.w, rect.h, Style(bg=self.theme.divider_color)
            )
            return
        # Whole-unit backends only ever get "strong" dividers (one whole
        # base unit); render them with box-drawing characters.
        style = Style(fg=self.theme.divider_color)
        if divider.vertical:
            for row in range(int(rect.h)):
                self.backend.draw_text(rect.x, rect.y + row, "│", style)
        else:
            self.backend.draw_text(rect.x, rect.y, "─" * int(rect.w), style)

    def _draw_slot(self, slot: _Slot) -> None:
        # Group markers let the backend apply per-widget effects (e.g. the
        # alpha or transform of a running transition) to exactly these
        # commands; the rect gives transforms their pivot.
        rect = self._interpolate_rect(slot.widget, slot.rect)
        self.backend.begin_group(slot.widget, rect)
        background = self._pane_background(slot.hints)
        if background is not None:
            # The fill may bleed past the widget's rect (window margin), so
            # it is drawn before the content clip is applied.
            fill = slot.fill if slot.fill is not None else rect
            self.backend.fill_rect(fill.x, fill.y, fill.w, fill.h, Style(bg=background))
        self.backend.push_clip(rect.x, rect.y, rect.w, rect.h)
        slot_ctx = DrawContext(
            self.backend, rect, self.backend.capabilities,
            panel=self, background=background,
            focused=slot.widget is self._focused,
            # Hover/press use the same region clicks and focus route to: the
            # bled fill, so the window margin belongs to the pane consistently.
            hit_rect=slot.fill if slot.fill is not None else rect,
            widget=slot.widget,
        )
        slot.widget.draw(slot_ctx)
        slot_ctx._close()
        self.backend.pop_clip()
        # A stepped fade/highlight paints its one-frame stand-in over the whole
        # group, after its content (and children) are down.
        self._apply_group_effect(slot.widget, rect)
        self.backend.end_group(slot.widget)

    def _render_layer(self, slot: _Slot) -> None:
        if slot.reflow is not None:
            # A size-anchored layer (an edge Drawer) recomputes its rect from the
            # current backend size, so it follows window resizes instead of
            # staying frozen at the geometry it was pushed with.
            sw, sh = self.backend.size_units
            slot.rect = slot.reflow(sw, sh)
        if slot.hints.get("dim_below"):
            # Every backend implements dim_rect; TUI approximates with dim
            # attributes, GUI draws a translucent overlay. Dim the exact
            # (fractional) extent so the last partial base unit is covered too.
            sw, sh = self.backend.size_units
            # The whole-cell scrim must follow the theme polarity: a light theme
            # dims to a gray veil with dark text, not a fixed near-black bar with
            # gray text. ``per_cell`` asks a TUI backend to composite that veil
            # over each cell's own color so the page shows through faintly (the
            # stand-in for the GUI translucent overlay); GUI backends ignore both
            # hints and composite a real overlay.
            self.backend.dim_rect(0, 0, sw, sh, scrim=self.theme.dim_scrim(), per_cell=True)
        rect = self._interpolate_rect(slot.widget, slot.rect)
        self.backend.begin_group(slot.widget, rect)
        # The shadow blurs beyond the rect, so it is drawn before clipping. A
        # layer that paints a rounded face (e.g. a Drawer) passes "radius" /
        # "corners" hints so the shadow silhouette matches the rounding.
        if slot.hints.get("shadow"):
            if self.backend.capabilities.supports("shadow"):
                # The caster silhouette must be filled with the layer's own
                # surface color so any sub-unit sliver of it left uncovered by
                # the layer's whole-unit content fill blends in, rather than
                # showing the backend's window-dark default as a hard fringe. A
                # bare shadowed modal carries no surface hint, so fall back to
                # the popup surface (what such modals paint).
                caster_bg = self._pane_background(slot.hints) or self.theme.popup_bg
                self.backend.draw_shadow(
                    rect.x, rect.y, rect.w, rect.h,
                    slot.hints.get("radius"), slot.hints.get("corners"),
                    caster_bg,
                )
            else:
                # No real compositing: the backend darkens a one-cell halo around
                # the layer instead (TUI stand-in). Pass the page background so the
                # halo stays continuous over any unpainted cell.
                self.backend.shadow_rect(
                    rect.x, rect.y, rect.w, rect.h,
                    self.theme.surface_bg("content"),
                )
        self.backend.push_clip(rect.x, rect.y, rect.w, rect.h)
        background = self._pane_background(slot.hints)
        # A "self_paint" layer (e.g. a Drawer with a rounded face) paints its
        # own background, so the Panel skips the square fill but still hands the
        # resolved color down for content inheritance — otherwise the square
        # fill would show under/around the rounded corners.
        if background is not None and not slot.hints.get("self_paint"):
            self.backend.fill_rect(rect.x, rect.y, rect.w, rect.h, Style(bg=background))
        layer_ctx = DrawContext(
            self.backend, rect, self.backend.capabilities,
            panel=self, background=background,
            # The top-most layer is the active modal, so it holds the focus.
            focused=bool(self._layers) and slot is self._layers[-1],
            widget=slot.widget,
        )
        slot.widget.draw(layer_ctx)
        layer_ctx._close()
        self.backend.pop_clip()
        self._apply_group_effect(slot.widget, rect)
        self.backend.end_group(slot.widget)

    # --- animation ----------------------------------------------------------------

    def animate(self, widget: Any, hints: dict[str, Any] | None = None) -> bool:
        # The transition's *kind* and the backend's capability together decide
        # who realizes it — resolution lives here in the Panel, never in the app.
        #
        # A COMPOSITING backend ("animation": GUI) renders every transition the
        # smooth way: it owns the optical ones (fade/scale/highlight, real alpha
        # and sub-unit transforms) and the slide transform; the Panel owns size
        # (a true re-measure) and the color tween.
        #
        # A STEPPED backend ("animation_ticks" but not "animation": a terminal)
        # cannot draw a smooth transition, so the Panel plays EVERY kind itself
        # as exactly two frames — an intermediate state then the target (the
        # 2-frame policy) — using whole-cell stand-ins:
        #   - slide / size    -> rect interpolation (a 2-step move / grow)
        #   - scale           -> rect inset (shrink-to-center, then full)
        #   - color           -> color interpolation (read via animated_color)
        #   - fade / highlight -> a one-frame group effect (a dim / color flash)
        # so no animation type is left out and none crawls.
        #
        # A STILL backend (neither capability) applies the change immediately.
        #
        # Returns whether a transition was scheduled: False on a still backend, so
        # a caller that must act *after* a transition (a drawer popping its layer
        # once it has slid out, via an ``on_complete`` hint) can fall back to doing
        # it at once when nothing will animate.
        hints = hints or {}
        caps = self.backend.capabilities
        transition = hints.get("transition")
        smooth = caps.supports("animation")
        stepped = caps.supports("animation_ticks") and not smooth
        if not (smooth or stepped):
            return False  # still backend: the target state shows on next render

        if transition == "color":
            self._start_color_animation(widget, hints, stepped)
        elif transition == "size":
            # Always a Panel-owned re-measure (the widget redraws at each size).
            self._start_geometry_animation(widget, hints, stepped)
        elif transition == "slide":
            if smooth:
                self.backend.animate(widget, hints)  # GPU sub-unit transform
            else:
                self._start_geometry_animation(widget, hints, stepped)
        elif transition == "scale":
            if smooth:
                self.backend.animate(widget, hints)  # composited zoom
            else:
                self._start_geometry_animation(widget, hints, stepped)  # rect inset
        elif transition in ("fade", "highlight"):
            if smooth:
                self.backend.animate(widget, hints)  # real alpha overlay
            else:
                self._start_effect_animation(widget, hints, stepped)
        elif smooth:
            self.backend.animate(widget, hints)
        return True

    def request_animation_ticks(self, callback: Any) -> bool:
        """Register a per-frame tick ``callback`` for a widget that animates
        itself (a busy spinner). Gated on the ``animation`` capability: on a
        still backend nothing is registered — the widget simply renders one
        frame on each ordinary render — so the widget never branches on the
        backend. The callback returns False to unregister. Returns whether
        ticking started."""
        caps = self.backend.capabilities
        if not (caps.supports("animation") or caps.supports("animation_ticks")):
            return False
        self.backend.request_animation_ticks(callback)
        return True

    @property
    def dispatches_to_main_thread(self) -> bool:
        """True when ``call_on_main_thread`` is available — i.e. the backend runs
        a native loop on a UI thread that worker threads can hand work back to.
        An app reads this to choose an event-driven design (wake the UI thread on
        each producer) over a polling animation tick that drains queues on a
        timer. False on single-threaded poll-loop backends, which drain their own
        producers each iteration."""
        return self.backend.capabilities.supports("main_thread_dispatch")

    def call_on_main_thread(self, callback: Any) -> bool:
        """Schedule ``callback`` on the UI thread from any thread, waking the
        loop. Returns False (a no-op) when the backend can't dispatch, so callers
        that only take this path under ``dispatches_to_main_thread`` stay simple."""
        if not self.backend.capabilities.supports("main_thread_dispatch"):
            return False
        self.backend.call_on_main_thread(callback)
        return True

    def _start_geometry_animation(
        self, widget: Any, hints: dict[str, Any], stepped: bool
    ) -> None:
        # Works for any widget in the tree: the target rect is whatever the
        # widget is given at draw time (Panel slot, layer, or Container child).
        # A given call carries one shape — slide offset, size, or scale — and
        # the unused fields stay no-ops, so one mechanism covers all three.
        self._size_anims[widget] = _GeometryAnimation(
            start=time.monotonic(),
            duration=hints.get("duration_ms", 200) / 1000.0,
            from_w=float(hints["from_w"]) if "from_w" in hints else None,
            from_h=float(hints["from_h"]) if "from_h" in hints else None,
            from_dx=float(hints.get("from_dx", 0.0)),
            from_dy=float(hints.get("from_dy", 0.0)),
            from_scale=float(hints["from_scale"]) if "from_scale" in hints else None,
            out=bool(hints.get("out", False)),
            on_complete=hints.get("on_complete"),
            stepped=stepped,
        )
        self.backend.request_animation_ticks(self._animation_tick)

    def _start_color_animation(
        self, widget: Any, hints: dict[str, Any], stepped: bool
    ) -> None:
        # Keyed by (widget, key) so one widget can tween several colors at once
        # (e.g. its foreground and background independently).
        key = (widget, hints.get("key"))
        self._color_anims[key] = _ColorAnimation(
            start=time.monotonic(),
            duration=hints.get("duration_ms", 200) / 1000.0,
            from_color=tuple(hints["from"]),
            to_color=tuple(hints["to"]),
            stepped=stepped,
        )
        self.backend.request_animation_ticks(self._animation_tick)

    def _start_effect_animation(
        self, widget: Any, hints: dict[str, Any], stepped: bool
    ) -> None:
        # A group optical effect (fade/highlight) the Panel paints as a one-frame
        # stand-in over the widget's whole group; only reached on a stepped
        # backend (a compositing backend renders these itself).
        self._effect_anims[widget] = _EffectAnimation(
            kind=hints["transition"],
            start=time.monotonic(),
            duration=hints.get("duration_ms", 200) / 1000.0,
            hints=hints,
            stepped=stepped,
        )
        self.backend.request_animation_ticks(self._animation_tick)

    def animated_color(
        self, widget: Any, key: Any = None, default: Any = None
    ) -> Any:
        """Current value of a color transition started on ``widget`` (see
        ``DrawContext.animated_color``), or ``default`` when none is running."""
        anim = self._color_anims.get((widget, key))
        if anim is None:
            return default
        return anim.value_at(time.monotonic())

    def _apply_group_effect(self, widget: Any, rect: Rect) -> None:
        """Paint a running fade/highlight effect over a freshly-drawn group as a
        single intermediate frame (the 2-frame policy's stand-in for a composited
        overlay). On the target frame the effect is inactive, so nothing is drawn
        and the group reads clean; then the tick drops it."""
        eff = self._effect_anims.get(widget)
        if eff is None or not eff.active(time.monotonic()):
            return
        if eff.kind == "fade":
            # No alpha on a terminal: a fade reads as one dim pass over the
            # group. Unlike the modal dim_below veil (a fixed dark scrim), a fade
            # is opacity — each cell sinks toward its OWN background (fade=True),
            # so the intermediate frame follows the actual grid cells (a popup
            # surface stays popup-colored) instead of collapsing every surface to
            # one pair. The theme scrim is still passed as the polarity-correct
            # fallback for any untouched cell.
            self.backend.dim_rect(
                rect.x, rect.y, rect.w, rect.h, scrim=self.theme.fade_scrim(), fade=True
            )
        else:  # highlight
            color = eff.hints.get("color") or self.theme.accent
            self.backend.flash_rect(rect.x, rect.y, rect.w, rect.h, color)

    def _animation_tick(self) -> bool:
        now = time.monotonic()
        # Advance the stepped (terminal) animations one frame; compositing
        # animations are time-driven and ignore the step counter.
        for anim in (
            *self._size_anims.values(),
            *self._color_anims.values(),
            *self._effect_anims.values(),
        ):
            if anim.stepped:
                anim.step += 1
        self.render()  # renders the new frame (intermediate, then target)
        # Drop the finished ones AFTER they have rendered their target frame, so
        # the steady state (assigned rect, resting color, no effect) persists.
        finished = [
            (w, a) for w, a in self._size_anims.items() if a.progress(now) >= 1.0
        ]
        for w, _a in finished:
            del self._size_anims[w]
        # Fire each geometry animation's completion hook last — a slide-out close
        # pops its layer here, having already rendered its final (off-edge) frame,
        # then re-renders the page without it.
        for _w, a in finished:
            if a.on_complete is not None:
                a.on_complete()
        for k in [k for k, a in self._color_anims.items() if a.progress(now) >= 1.0]:
            del self._color_anims[k]
        for w in [w for w, a in self._effect_anims.items() if a.progress(now) >= 1.0]:
            del self._effect_anims[w]
        return bool(self._size_anims or self._color_anims or self._effect_anims)

    def _interpolate_rect(self, widget: Any, rect: Rect) -> Rect:
        """The widget's rect with any running geometry transition applied: a
        slide offset that decays to zero, a size that grows to its final extent,
        and/or a scale inset that opens to full. The interpolated rect drives
        both the content draw and its clip, so an off-edge / inset part is
        clipped to the screen exactly as a transform would clip it.

        Motion is linear. On a character grid (no ``pixel_layout``) the result
        is snapped to whole base units, so each frame advances by an integer
        number of cells — and on a terminal there are only two such frames."""
        anim = self._size_anims.get(widget)
        if anim is None:
            return rect
        p = anim.progress(time.monotonic())
        x, y = rect.x, rect.y
        from_w = anim.from_w if anim.from_w is not None else rect.w
        from_h = anim.from_h if anim.from_h is not None else rect.h
        w = from_w + (rect.w - from_w) * p
        h = from_h + (rect.h - from_h) * p
        if anim.from_scale is not None:
            # Scale toward the rect center: shrink to from_scale, grow to full.
            s = anim.from_scale + (1.0 - anim.from_scale) * p
            w, h = rect.w * s, rect.h * s
            x += (rect.w - w) / 2.0
            y += (rect.h - h) / 2.0
        # Slide in decays the offset to zero (1 - p); slide out grows it (p).
        slide_p = p if anim.out else (1.0 - p)
        x += anim.from_dx * slide_p
        y += anim.from_dy * slide_p
        result = Rect(x, y, w, h)
        if not self.backend.capabilities.supports("pixel_layout"):
            result = Rect(
                round(result.x), round(result.y), round(result.w), round(result.h)
            )
        return result

    # --- text input -----------------------------------------------------------------

    def request_text_input(self, x: float, y: float, hints: dict[str, Any] | None = None) -> None:
        request = getattr(self.backend, "request_text_input", None)
        if request is not None:
            request(x, y, hints or {})

    # --- pointer shape --------------------------------------------------------

    def request_pointer_shape(self, shape: str | None) -> None:
        """A widget asks for the mouse pointer shape over it, named with a CSS/X
        cursor name (``"text"``, ``"pointer"``, ``"not-allowed"``, ...). Called
        during draw, typically gated on ``ctx.hovered``; the last (topmost) call
        in a frame wins and is applied to the backend at the end of render().

        One intent, every backend: a ``pointer_shape`` backend sets a real OS
        cursor (or asks the terminal via OSC 22); others no-op. The widget never
        branches on the capability."""
        self._pointer_shape = shape

    # --- clipboard ------------------------------------------------------------

    def get_clipboard(self) -> str:
        """Plain-text clipboard contents, delegated to the backend (a real OS
        clipboard where available, a process-local buffer otherwise). Widgets
        call this for paste without knowing which backend they run on."""
        return self.backend.get_clipboard()

    def set_clipboard(self, text: str) -> None:
        """Replace the plain-text clipboard contents. See ``get_clipboard``."""
        self.backend.set_clipboard(text)

    # --- open a URL -----------------------------------------------------------

    def open_url(self, url: str) -> bool:
        """Open ``url`` (a clicked hyperlink) in the OS handler. One intent,
        resolved per backend: ``os_open`` backends launch the default handler
        (a browser, the file's app); others — notably TUI — fall back to copying
        the URL to the clipboard. Returns True if a handler was launched, False
        if the clipboard fallback was used. The caller never branches."""
        return self.backend.open_url(url)

    # --- drag source ----------------------------------------------------------

    def begin_file_drag(
        self,
        paths: Any,
        event: Event | None = None,
        operations: tuple[str, ...] = ("copy",),
        on_complete: Any | None = None,
    ) -> bool:
        """Export ``paths`` (file paths) as an OS drag, so the user can drop
        them onto another app. One intent, resolved per backend: ``os_drag_drop``
        backends start a native drag session (macOS NSDraggingSource); others —
        notably TUI, where the terminal owns the window and no app can be a drag
        source — fall back to copying the paths to the clipboard so the user can
        paste them into the target. The caller (a file list's drag handler)
        never branches.

        ``operations`` is the offered set (``"copy"`` / ``"move"`` / ``"link"``);
        the destination chooses one. PuiKit never deletes files — for a ``move``
        the app does it, prompted by ``on_complete(op)`` once the session ends
        (``op`` is the chosen operation, or ``"none"`` if cancelled). The
        clipboard fallback is copy semantics, so it reports ``"copy"``.

        Returns True if a real drag session began, False if the clipboard
        fallback was used. ``event`` is the originating MOUSE_DRAG event; a
        native session must start from it."""
        paths = [str(p) for p in paths]
        if self.backend.capabilities.supports("os_drag_drop"):
            return self.backend.begin_file_drag(paths, event, operations, on_complete)
        self.backend.set_clipboard("\n".join(paths))
        if on_complete is not None:
            on_complete("copy")
        return False

    # --- menus ----------------------------------------------------------------

    def set_menu_bar(self, menu: Any | None) -> None:
        """Install an app menu bar from a puikit.menu.Menu (its items each
        carry a submenu). On ``native_menus`` backends this becomes the real
        OS menu bar at the top of the screen; on others it is a no-op here —
        the app places a MenuBar widget that renders the bar in-window. The
        MenuBar widget calls this itself once it knows the backend, so the app
        never branches on the capability."""
        self._menu_bar = menu
        if self.backend.capabilities.supports("native_menus"):
            self.backend.set_menu_bar(menu)

    def popup_menu(
        self, menu: Any, x: float, y: float, on_done: Any | None = None
    ) -> None:
        """Open ``menu`` as a context menu with its top-left near base-unit
        (x, y). Native backends hand it to the OS; others push a widget-rendered
        popup layer. One intent, resolved per backend — the caller (a widget's
        right-click handler) never branches."""
        if self.backend.capabilities.supports("native_menus"):
            self.backend.popup_menu(menu, x, y, on_done)
            return
        from .widgets.menu import MenuPopup, popup_geometry

        vector = self.backend.capabilities.supports("vector_shapes")
        w, h, row_h = popup_geometry(menu, self.backend.measure_text, vector)
        popup = MenuPopup(menu, row_h=row_h)

        def close() -> None:
            # Pop only if our popup is still the top layer (a submenu it opened
            # pops itself first).
            if self._layers and self._layers[-1].widget is popup:
                self.pop_layer()
            if on_done is not None:
                on_done()

        popup.on_close = close
        # Keep the popup on-screen: nudge it left/up if it would overflow.
        sw, sh = self.backend.size_units
        x = max(0.0, min(x, sw - w))
        y = max(0.0, min(y, sh - h))
        self.push_layer(
            popup, z=60, hints={"shadow": True, "x": x, "y": y, "w": w, "h": h}
        )

    # --- event routing ----------------------------------------------------------------

    def dispatch_event(self, event: Event) -> bool:
        """Route an event to widgets. Returns True if it was consumed."""
        # Track the pointer for hover/press styling on any positioned mouse event.
        if event.x is not None and event.type in (
            EventType.MOUSE_CLICK, EventType.MOUSE_DOWN, EventType.MOUSE_UP,
            EventType.MOUSE_DRAG, EventType.MOUSE_MOVE, EventType.MOUSE_SCROLL,
            EventType.FILE_DROP,
        ):
            self._pointer = (event.x, event.y)
        # Plain hover movement updates the pointer but is not delivered to a
        # widget; the caller re-renders to reflect the new hover state.
        if event.type is EventType.MOUSE_MOVE:
            return False

        # The topmost layer gets events exclusively (modal behavior); it owns
        # its own focus traversal (e.g. a dialog cycles its buttons), so Tab is
        # routed in, not intercepted here.
        if self._layers:
            slot = self._layers[-1]
            return self._route_mouse(slot, event, clamp=False, is_layer=True)

        # Tab / Shift+Tab walk the whole focus tree from the root, crossing
        # container boundaries and wrapping at the ends — one mechanism instead
        # of each container cycling on its own.
        if event.type is EventType.KEY and event.key == "tab":
            return self.focus_tab(-1 if "shift" in event.modifiers else 1)

        if event.type in (
            EventType.MOUSE_CLICK, EventType.MOUSE_DOWN, EventType.MOUSE_UP,
            EventType.MOUSE_DRAG, EventType.MOUSE_SCROLL, EventType.FILE_DROP,
        ):
            # A drag/release belongs to the slot the press began on, so a gesture
            # that wanders off its origin (a button drag-off, a selection drag
            # past the pane edge) still reaches the right widget.
            if event.type in (EventType.MOUSE_UP, EventType.MOUSE_DRAG) and self._press_slot is not None:
                return self._route_mouse(self._press_slot, event, clamp=True, is_layer=self._press_is_layer)
            for slot in reversed(self._children):
                # Hit-test against the fill extent: a click in the bled
                # window margin belongs to the pane that visually owns it.
                hit = slot.fill if slot.fill is not None else slot.rect
                if event.x is not None and hit.contains(event.x, event.y):
                    return self._route_mouse(slot, event, clamp=True, is_layer=False)
            return False

        if self._focused is not None:
            for slot in self._children:
                if slot.widget is self._focused:
                    return self._deliver(slot, event)
        return False

    def _route_mouse(
        self, slot: "_Slot", event: Event, *, clamp: bool, is_layer: bool
    ) -> bool:
        """Route a positioned mouse event to ``slot``, owning the press→click
        gesture: a left MOUSE_DOWN is captured (and focuses the slot), and the
        matching MOUSE_UP synthesizes a MOUSE_CLICK only when the release lands
        back over the press slot — a release elsewhere cancels. An atomic
        MOUSE_CLICK (backends without down/up, or the right button) passes
        straight through."""
        if event.type is EventType.MOUSE_DOWN:
            self._press_down = (event.x, event.y) if event.x is not None else None
            self._press_slot = slot
            self._press_is_layer = is_layer
            if not is_layer:
                focus_on_click(self, slot.widget)
            self._deliver(slot, event, clamp=clamp)
            # A press always reports handled: it may have moved focus or armed a
            # pressed cue, so the host must re-render even if the widget itself
            # ignored the down (most widgets act on the synthesized click).
            return True
        if event.type is EventType.MOUSE_DRAG:
            handled = self._deliver(slot, event, clamp=clamp)
            # While a press is captured, the held pressed cue tracks the pointer
            # (and clears on drag-off), so the frame needs a redraw regardless.
            return handled or self._press_down is not None
        if event.type is EventType.MOUSE_UP:
            self._press_down = None
            self._press_slot = None
            self._press_is_layer = False
            self._deliver(slot, event, clamp=clamp)
            # Fire the click only if the pointer is still over the press slot (a
            # modal layer always owns it); otherwise the gesture was cancelled.
            over = is_layer or self._over_slot(slot, event)
            if over:
                click = replace(event, type=EventType.MOUSE_CLICK)
                self._deliver(slot, click, clamp=clamp)
            # A release ends a captured press; re-render to settle the cue / show
            # the click result.
            return True
        if event.type is EventType.MOUSE_CLICK and not is_layer:
            focus_on_click(self, slot.widget)
        return self._deliver(slot, event, clamp=clamp)

    def _over_slot(self, slot: "_Slot", event: Event) -> bool:
        if event.x is None:
            return False
        hit = slot.fill if slot.fill is not None else slot.rect
        return hit.contains(event.x, event.y)

    def _deliver(self, slot: _Slot, event: Event, clamp: bool = False) -> bool:
        local = event.translated(-slot.rect.x, -slot.rect.y)
        if clamp and local.x is not None:
            # Margin clicks land outside the content rect; pull them just inside
            # the nearest edge so the widget acts on a coordinate it actually
            # drew. In-bounds clicks are left untouched (no quantizing to whole
            # cells), so routing keeps the same sub-unit precision the hover /
            # press cue reads from the raw pointer.
            x = min(max(local.x, 0.0), max(0.0, slot.rect.w - _EDGE_EPS))
            y = min(max(local.y, 0.0), max(0.0, slot.rect.h - _EDGE_EPS))
            if (x, y) != (local.x, local.y):
                local = replace(local, x=x, y=y)
        return bool(slot.widget.handle_event(local))
