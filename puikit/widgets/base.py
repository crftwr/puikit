"""Widget base class.

Widgets draw through the DrawContext given by the Panel and never talk to a
backend directly, so one implementation runs on every backend.
"""

from __future__ import annotations

from typing import Any

from ..backend import Style, TextAttribute
from ..event import Event
from ..layout import LayoutContext, SizeRequest
from ..panel import DrawContext
from ..text import display_width

# Height of a single-line control box (field, button) in base units on
# pixel backends: one text line plus a little vertical padding above and below
# (the text is centered). Whole-unit (TUI) backends use a single cell instead —
# a taller box there would spend extra rows and fall back to a box-drawing frame
# that overdraws the text. Controls report this via ``view_height`` so a host
# like ScrollView reserves the right room per backend.
CONTROL_HEIGHT = 1.5


def selected_row_style(
    base: Style, theme: Any, focused: bool, vector: bool = False
) -> Style:
    """Style for the selected row of a list-like widget (ListView, TreeView).

    A selection reads as *active* only while the widget holds focus, and the
    **louder** cue always marks the focused state — never the reverse
    (interaction_states.md §4b/§6). That ordering is resolved per backend, not
    ported as a fixed attribute:

    - focused, grid: a reverse-video highlight (the loudest cue a terminal has);
    - focused, vector: the accent selection fill, with the row's own (light)
      text kept legible on it — *not* reverse, whose white fill would read
      louder than any unfocused color and invert the emphasis;
    - unfocused, either: the muted inactive fill, so a list whose focus moved
      away dims its selection like every other control dims its cue.

    Without the theme tokens in reach the highlight is kept when focused and
    dropped otherwise (better visible than lost)."""
    active = getattr(theme, "selection_active_bg", None) if theme is not None else None
    inactive = getattr(theme, "selection_inactive_bg", None) if theme is not None else None
    if active is None or inactive is None:
        if focused:
            return Style(base.fg, base.bg, base.attr | TextAttribute.REVERSE)
        return base
    if not focused:
        return Style(base.fg, inactive, base.attr)
    if vector:
        return Style(base.fg, active, base.attr)
    return Style(base.fg, base.bg, base.attr | TextAttribute.REVERSE)


def draw_list_row(
    ctx: DrawContext, y: float, clipped: str, text_w: int, style: Style, x: float = 0.0
) -> None:
    """Draw one full-width row of a list-like widget (ListView, TreeView).

    A row background must span the whole pane width, but a proportional font (the
    GUI default) is narrower than its column count, so the text's own background
    would fall short of the right edge — the gap behind a selection highlight in
    the screenshots. A solid-fill background is therefore painted as a full-width
    rect first; a reverse-video highlight (a focused selection on a grid) instead
    covers the row by padding the text to the column count, since a terminal has
    no separate fill to stretch.

    ``clipped`` is the row text already truncated to the columns it may occupy.
    ``x`` is its origin in base units: a TreeView passes a fixed per-depth indent
    here (on a vector strip) so the indent is a layout distance, not a count of
    proportional spaces whose width drifts with the font. The reverse-video grid
    path keeps ``x`` at zero and carries any indent as leading spaces, so the
    inverse still covers the whole row."""
    if style.bg is not None and not (style.attr & TextAttribute.REVERSE):
        ctx.fill_rect(0, y, text_w, 1.0, Style(bg=style.bg))
        ctx.draw_text(x, y, clipped, style)
    else:
        text = clipped + " " * (text_w - display_width(clipped))
        ctx.draw_text(x, y, text, style)


class Widget:
    #: Whether this widget can take keyboard focus.
    focusable = False

    def draw(self, ctx: DrawContext) -> None:
        """Draw the widget into its assigned rectangle."""

    def handle_event(self, event: Event) -> bool:
        """Handle an event in widget-local coordinates.

        Return True if the event was consumed."""
        return False

    def measure(self, ctx: LayoutContext, axis: str, available: float) -> SizeRequest:
        """Intrinsic size along ``axis`` ("x" = width, "y" = height), used
        when the layout places this widget with ``size="content"`` (or a
        ``min="content"`` floor, or cross-axis ``align``). ``available`` is
        the resolved extent on the other axis, in base units.

        Widgets that measure themselves from a font do so here via
        ``ctx.measure_text``; widgets with a backend-fixed extent read it off
        ``ctx``. The default has no opinion, so the item just fills its slot."""
        return SizeRequest()
