"""A push button whose face is an image.

Same interaction as ``Button`` — click or activate (space/enter) fires
``on_click``, it lightens on hover and shows an accent focus ring — but the
face is an image instead of a text label. The image is the intent; the
backend decides the fidelity. GUI renders the real picture inset over the
button surface; TUI falls back in the Panel layer to a framed alt text. The
focus ring and hover tint are drawn the same way on every backend, so the
button reads as interactive even where the image cannot render.

Like ``ImageView`` it fills its slot, so size it through the layout.
"""

from __future__ import annotations

from collections.abc import Callable

from ..backend import Color, Style
from ..event import Event, EventType
from ..image import CONTAIN, COVER, FILL
from ..panel import DrawContext
from ..theme import DEFAULT_THEME
from ._input import is_activate
from .base import Widget

_FACE_FITS = frozenset({FILL, CONTAIN, COVER})


def _lighten(color: Color, amount: float = 0.12) -> Color:
    """Nudge a color toward white, for the hover state."""
    return tuple(round(c + (255 - c) * amount) for c in color)  # type: ignore[return-value]


class ImageButton(Widget):
    focusable = True

    def __init__(
        self,
        path: str,
        on_click: Callable[[], None] | None = None,
        alt: str | None = None,
        pad: int = 1,
        fit: str = CONTAIN,
    ):
        if fit not in _FACE_FITS:
            raise ValueError(
                f"unknown image fit {fit!r}; a button face expects one of {sorted(_FACE_FITS)}"
            )
        self.path = path
        self.on_click = on_click
        # Text shown in place of the picture on backends without images (TUI).
        self.alt = alt
        # Gap between the button edge and the image, in base units; it leaves
        # room for the focus ring and reads the image as a raised face.
        self.pad = pad
        # How the face image fills the padded area. "contain" (default) keeps
        # the whole glyph visible; "cover"/"fill" suit edge-to-edge artwork.
        self.fit = fit

    def draw(self, ctx: DrawContext) -> None:
        theme = ctx.theme or DEFAULT_THEME
        bg = _lighten(theme.control_bg) if ctx.hovered else theme.control_bg
        wu, hu = ctx.size_units
        ctx.fill_rect(0, 0, wu, hu, Style(bg=bg))

        # Image inset by the pad, scaled to the remaining area. The fill behind
        # it shows as a border and through any transparency in the picture.
        pad = self.pad
        iw = max(0.0, wu - 2 * pad)
        ih = max(0.0, hu - 2 * pad)
        if iw > 0 and ih > 0:
            ctx.draw_image(
                pad, pad, self.path,
                hints={"w": iw, "h": ih, "fit": self.fit, "alt": self.alt},
            )

        # Accent focus ring around the whole face, matching Button's focus cue.
        if ctx.focused and ctx.width >= 1 and ctx.height >= 1:
            ctx.draw_box(0, 0, ctx.width, ctx.height, Style(fg=theme.accent, bg=bg))

    def handle_event(self, event: Event) -> bool:
        if event.type is EventType.MOUSE_CLICK or is_activate(event):
            if self.on_click is not None:
                self.on_click()
            return True
        return False
