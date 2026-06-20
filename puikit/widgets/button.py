"""A flat, VS Code-style push button — text, image, or both.

One button class covers three faces:

- **text** (``Button("OK")``) — a bold centered label on a flat fill; it sizes
  its width to the label via ``measure``.
- **image** (``Button(image="play.png")``) — a picture inset over a neutral
  control surface, scaled by ``fit`` (contain/cover/fill); it fills its slot.
- **image + text** (``Button("Play", image="play.png")``) — an icon and a
  label side by side, centered as a group on the fill.

A labeled button comes in two variants: ``variant="primary"`` (default) wears
the **accent** fill — the prominent action; ``variant="secondary"`` wears a
**neutral** fill for a non-primary action, so a screen with two buttons reads
one as the obvious choice. Either way the focus ring picks a color that
contrasts its own fill (a light ring on the accent fill, the accent on a
neutral fill), so focus never collides with the fill (docs/interaction_states.md §5).

All faces share one interaction: a click or activate (space/enter) fires
``on_click``, the face lightens on hover and darkens while pressed, and the
focus ring marks focus (a framed ring on vector backends, a box or underline on
a character grid). The image face is an intent — GUI renders the real picture,
TUI falls back in the Panel layer to the alt emoji — so the button reads as
interactive on every backend, and no draw branches on it.
"""

from __future__ import annotations

from collections.abc import Callable

from ..backend import Style, TextAttribute
from ..event import Event, EventType
from ..image import CONTAIN, COVER, FILL
from ..layout import LayoutContext, SizeRequest
from ..panel import DrawContext
from ..theme import DEFAULT_THEME
from ._input import is_activate
from .base import CONTROL_HEIGHT, Widget

# Fits valid for a button face. The aspect modes ("width"/"height") size a
# widget to an image's ratio; a button is sized by the layout or its label,
# so only the within-a-given-box fits apply.
_FACE_FITS = frozenset({FILL, CONTAIN, COVER})

# Corner radius of the button face, in device pixels. Dropped on character-grid
# backends (the face renders as a plain fill there).
_RADIUS = 5.0

# Focus-ring color on an accent (primary) fill: a bright near-white, so the ring
# never collides with the accent the way an accent-on-accent ring would. A
# neutral fill uses the theme accent for its ring instead (resolved in _colors).
_FOCUS_RING = (240, 240, 245)

_VARIANTS = frozenset({"primary", "secondary"})


def _lighten(color: tuple[int, int, int], amount: float = 0.12) -> tuple[int, int, int]:
    """Nudge a color toward white, for the hover state of a fill."""
    return tuple(round(c + (255 - c) * amount) for c in color)  # type: ignore[return-value]


def _darken(color: tuple[int, int, int], amount: float = 0.18) -> tuple[int, int, int]:
    """Nudge a color toward black, for the pressed state of a fill — the
    opposite direction from hover, so rest/hover/pressed stay distinct."""
    return tuple(round(c * (1.0 - amount)) for c in color)  # type: ignore[return-value]


class Button(Widget):
    focusable = True

    def __init__(
        self,
        label: str | None = None,
        on_click: Callable[[], None] | None = None,
        image: str | None = None,
        style: Style | None = None,
        variant: str = "primary",
        fit: str = CONTAIN,
        alt: str | None = None,
        pad_x: int = 2,
        pad: int = 1,
        gap: int = 1,
    ):
        if not label and image is None:
            raise ValueError("a Button needs a label, an image, or both")
        if image is not None and fit not in _FACE_FITS:
            raise ValueError(
                f"unknown image fit {fit!r}; a button face expects one of {sorted(_FACE_FITS)}"
            )
        if variant not in _VARIANTS:
            raise ValueError(
                f"unknown button variant {variant!r}; expected one of {sorted(_VARIANTS)}"
            )
        self.label = label or ""
        self.on_click = on_click
        self.image = image
        # None -> the theme's button colors; a Style overrides the fill.
        self.style = style
        # "primary" -> accent fill (prominent action); "secondary" -> neutral
        # fill (non-primary action). Ignored for a bare-icon tile, which is
        # always neutral.
        self.variant = variant
        self.fit = fit
        # Emoji/glyph shown in place of the picture on backends without images
        # (TUI). None -> a neutral "●".
        self.alt = alt
        self.pad_x = pad_x  # horizontal padding measured around a text label
        self.pad = pad      # inset of the image from the button edge
        self.gap = gap      # space between image and label when both are shown

    # --- colors --------------------------------------------------------------

    def _colors(self, ctx: DrawContext):
        theme = ctx.theme or DEFAULT_THEME
        # `ring` is the focus-ring color, chosen to contrast the fill: a light
        # ring on the accent fill, the accent on a neutral fill.
        if self.style is not None and self.style.bg is not None:
            bg = self.style.bg
            fg = self.style.fg or theme.button_text
            hover = _lighten(bg)
            ring = _FOCUS_RING
        elif self.variant == "secondary":
            # A non-primary action: a neutral fill, no accent.
            bg = theme.button_secondary_bg
            fg, hover, ring = theme.button_text, theme.button_secondary_hover_bg, theme.accent
        elif self.label:
            # A primary labeled action: the accent button fill.
            bg, fg, hover, ring = theme.button_bg, theme.button_text, theme.button_hover_bg, _FOCUS_RING
        else:
            # A bare icon is a neutral tile, not a primary action.
            bg, fg = theme.control_bg, theme.button_text
            hover, ring = _lighten(theme.control_bg), theme.accent
        # Press wins over hover (the pointer is over the button while pressed),
        # and moves the fill the opposite way — darker — so the three states
        # read distinctly (docs/interaction_states.md §3).
        if ctx.pressed:
            face = _darken(bg)
        elif ctx.hovered:
            face = hover
        else:
            face = bg
        return face, fg, theme, ring

    # --- draw ----------------------------------------------------------------

    def draw(self, ctx: DrawContext) -> None:
        bg, fg, theme, ring = self._colors(ctx)
        wu, hu = ctx.size_units
        # A rounded fill on vector backends, a plain fill on a character grid.
        ctx.round_rect(0, 0, wu, hu, Style(bg=bg), radius=_RADIUS, hints={"fill": True})

        if self.image is not None and self.label:
            self._draw_icon_label(ctx, bg, fg)
        elif self.image is not None:
            self._draw_image(ctx)
        else:
            self._draw_label(ctx, bg, fg)

        # Focus cue.
        # - Vector backends: a full-perimeter ring in a high-contrast *non-blue*
        #   color, inset slightly so it reads as a focus halo rather than the
        #   fill edge — accent-on-accent would vanish against the blue fill
        #   (docs/interaction_states.md §5). Drawn at any size, so even a
        #   one-row text button gets a real ring instead of a faint underline.
        # - Character grid: a box-drawing frame when there is room (an image or
        #   a 2+ row button); a one-row text button has no room for a box, so
        #   _draw_label underlines instead.
        if ctx.focused:
            if ctx.vector_shapes and wu >= 1 and hu >= 1:
                inset = min(0.12, wu / 2, hu / 2)
                ctx.round_rect(
                    inset, inset, wu - 2 * inset, hu - 2 * inset,
                    Style(fg=ring, bg=bg), radius=_RADIUS,
                )
            elif not ctx.vector_shapes and (self.image is not None or ctx.height >= 3):
                # A grid box needs three rows — top border, label, bottom border;
                # at two rows the borders would eat the label, so a short text
                # button underlines instead (handled in _draw_label).
                ctx.round_rect(0, 0, wu, hu, Style(fg=ring, bg=bg), radius=_RADIUS)

    def _draw_label(self, ctx: DrawContext, bg, fg) -> None:
        attr = TextAttribute.BOLD
        # Underline is the grid-only cue for a short text button (under three
        # rows, where a box would overwrite the label); vector backends draw a
        # perimeter ring, taller grids draw a box.
        if ctx.focused and not ctx.vector_shapes and ctx.height < 3:
            attr |= TextAttribute.UNDERLINE
        style = Style(fg=fg, bg=bg, attr=attr)
        # Center against the exact (fractional) pane width and measured label
        # width, so the label tracks the pane pixel by pixel on pixel-layout
        # backends instead of snapping to whole base units.
        wu = ctx.size_units[0]
        tx = max(0.0, (wu - ctx.measure_text(self.label, style)) / 2.0)
        ty = max(0.0, (ctx.size_units[1] - 1.0) / 2.0)  # center the label line vertically
        ctx.draw_text(tx, ty, self.label, style)

    def _draw_image(self, ctx: DrawContext) -> None:
        wu, hu = ctx.size_units
        pad = self.pad
        iw = max(0.0, wu - 2 * pad)
        ih = max(0.0, hu - 2 * pad)
        if iw > 0 and ih > 0:
            ctx.draw_image(
                pad, pad, self.image,
                hints={"w": iw, "h": ih, "fit": self.fit, "alt": self.alt},
            )

    def _draw_icon_label(self, ctx: DrawContext, bg, fg) -> None:
        # An icon (a square sized to the inner height, the image fit inside it)
        # and the label side by side, centered as a group on the button face.
        wu, hu = ctx.size_units
        pad = self.pad
        inner_h = max(1.0, hu - 2 * pad)
        icon_w = inner_h
        style = Style(fg=fg, bg=bg, attr=TextAttribute.BOLD)
        # Measured (fractional) label width and a fractional text origin, so the
        # group stays centered at pixel granularity on pixel-layout backends.
        text_w = ctx.measure_text(self.label, style)
        group_w = icon_w + self.gap + text_w
        gx = max(float(pad), (wu - group_w) / 2.0)
        ctx.draw_image(
            gx, pad, self.image,
            hints={"w": icon_w, "h": inner_h, "fit": self.fit, "alt": self.alt},
        )
        tx = gx + icon_w + self.gap
        ty = max(0.0, (ctx.size_units[1] - 1.0) / 2.0)  # center the label line vertically
        ctx.draw_text(tx, ty, self.label, style)

    # --- measure -------------------------------------------------------------

    def measure(self, ctx: LayoutContext, axis: str, available: float) -> SizeRequest:
        if axis == "y":
            # Text-only is a single line: one cell on a grid, a little taller
            # (centered label + padding) on pixel backends. An image button
            # fills its slot vertically (size it through the layout).
            if self.image is None:
                h = 1.0 if ctx.snap else CONTROL_HEIGHT
                return SizeRequest(min=1.0, preferred=h, max=h)
            return SizeRequest()
        # axis == "x": natural width.
        if self.image is None:
            w = ctx.measure_text(self.label, self.style or Style()) + 2 * self.pad_x
            return SizeRequest(min=w, preferred=w, max=w)
        if not self.label:
            return SizeRequest()  # image-only fills its slot
        # image + text: the icon square (from the resolved height) plus the gap,
        # the label, and the edge pads — a fixed width the layout reserves.
        inner_h = max(1.0, available - 2 * self.pad)
        text_w = ctx.measure_text(self.label, self.style or Style())
        w = 2 * self.pad + inner_h + self.gap + text_w
        return SizeRequest(min=w, preferred=w, max=w)

    # --- events --------------------------------------------------------------

    def handle_event(self, event: Event) -> bool:
        if event.type is EventType.MOUSE_CLICK or is_activate(event):
            if self.on_click is not None:
                self.on_click()
            return True
        return False
