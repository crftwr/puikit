"""A read-only determinate progress bar.

The bar shows a ratio (0..1) as a filled portion of a track. It is the
non-interactive cousin of a slider: it represents a value along a length but
never takes input. Like ``ScrollBar`` it is painted with background fills (a
rounded pill on vector backends, plain cell fills on a character grid), so it
reads correctly on every backend without the widget branching on capability.

A determinate bar draws a fraction; an *indeterminate* activity ("we are busy
but cannot say how far along") is a different intent — see ``BusyIndicator`` —
because it needs the ``animation`` capability and a clean still-backend
fallback, which a value-bearing bar should not carry.
"""

from __future__ import annotations

from ..backend import DEFAULT_STYLE, Style
from ..layout import LayoutContext, SizeRequest
from ..panel import DrawContext
from ..theme import DEFAULT_THEME
from .base import Widget

# Pill corner radius in device pixels (dropped on a character grid, where the
# bar renders as plain cell fills).
_RADIUS: float | None = None  # None -> fully rounded (a pill)

# Visible bar thickness in device pixels on vector backends. The widget reserves
# a full base unit on the cross axis (so it aligns with sibling rows), but the
# painted pill is a thin band centered in that slot — a full-unit-tall bar reads
# as a heavy block. On a character grid the thickness is carried by the glyph
# instead (see below): a row is the smallest paintable height there.
_THICKNESS_PX: float = 6.0

# Grid (TUI) glyphs. A full-cell background block reads as a heavy bar; a
# box-drawing horizontal line sits at the cell's vertical center and reads as a
# thin rule instead. The filled portion uses the heavy line so progress stays
# legible against the light track even where color is unavailable.
_TRACK_GLYPH = "─"  # ─ light horizontal
_FILL_GLYPH = "━"   # ━ heavy horizontal


class ProgressBar(Widget):
    def __init__(self, value: float = 0.0, style: Style = DEFAULT_STYLE):
        # The fraction filled, 0..1. The fill color comes from ``style.bg`` when
        # set, else the theme accent; the track from ``track_color`` or the
        # theme's control background.
        self.value = value
        self.style = style
        self.track_color: tuple[int, int, int] | None = None

    def draw(self, ctx: DrawContext) -> None:
        theme = ctx.theme or DEFAULT_THEME
        wu, hu = ctx.size_units
        if wu <= 0 or hu <= 0:
            return
        v = max(0.0, min(1.0, self.value))
        track = self.track_color or theme.control_bg
        fill = self.style.bg or theme.accent
        if not ctx.vector_shapes:
            # Grid: a centered box-drawing rule, thinner than a full-cell block.
            self._draw_grid(ctx, theme, wu, v, fill)
            return
        # Vector: a thin band centered in the reserved slot. A full-unit-tall
        # pill reads as a heavy block, so cap the painted height.
        th = min(hu, _THICKNESS_PX / ctx.base_size[1])
        y = (hu - th) / 2.0
        # The track first, then the filled pill clipped to the fraction width.
        ctx.round_rect(0, y, wu, th, Style(bg=track), radius=_RADIUS, hints={"fill": True})
        fw = wu * v
        if fw > 0:
            ctx.round_rect(0, y, fw, th, Style(bg=fill), radius=_RADIUS, hints={"fill": True})

    def _draw_grid(self, ctx: DrawContext, theme, wu: float, v: float, fill) -> None:
        cols = int(round(wu))
        if cols <= 0:
            return
        # Light rule for the whole track, then overpaint the filled run with the
        # heavy rule in the fill color. Foreground glyphs keep the surrounding
        # pane background, so the bar reads as a thin line, not a filled band.
        ctx.draw_text(0, 0, _TRACK_GLYPH * cols, Style(fg=theme.control_border))
        filled = int(round(cols * v))
        if filled > 0:
            ctx.draw_text(0, 0, _FILL_GLYPH * filled, Style(fg=fill))

    def measure(self, ctx: LayoutContext, axis: str, available: float) -> SizeRequest:
        # A one-line bar on the cross axis; the main axis fills its slot (place
        # it with a weight or a fixed size). The bar is value-only — a caption
        # is a sibling Label, the way ScrollBar leaves its readout to a Label.
        if axis == "y":
            return SizeRequest(min=1.0, preferred=1.0, max=1.0)
        return SizeRequest()
