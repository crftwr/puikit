"""An image view with selectable object-fit.

The widget declares the intent — "draw this image into my rect with this
fit" — and the backend decides how. A backend with the ``images`` capability
(GUI) renders the real picture; backends without it (TUI) fall back in the
Panel layer to the ``alt`` emoji (a neutral ``●`` when none is given),
centered in the footprint. The widget never branches on the backend.

``fit`` controls how the image relates to the rect the layout assigns:

- ``"fill"``    — stretch to the rect, ignoring aspect ratio (default).
- ``"contain"`` — largest aspect-preserving box inside the rect; background
                  bands may show around it.
- ``"cover"``   — cover the rect with aspect preserved; the image is cropped.
- ``"width"``   — the layout gives the width; the height follows the aspect
                  ratio. Place it as an intrinsic item in a vertical stack:
                  ``Item(ImageView(p, fit="width"), size="content")``.
- ``"height"``  — the layout gives the height; the width follows the aspect
                  ratio. Place it as an intrinsic item in a horizontal split:
                  ``Item(ImageView(p, fit="height"), size="content")``.

The ``"width"`` / ``"height"`` modes size the widget itself (resolved in
``measure`` from the image's aspect ratio); the others fill the slot they are
given. All five resolve identically on TUI and GUI — only the draw fidelity
differs.
"""

from __future__ import annotations

from ..image import ASPECT_FITS, FILL, FITS, WIDTH, aspect_extent
from ..layout import LayoutContext, SizeRequest
from ..panel import DrawContext
from .base import Widget


class ImageView(Widget):
    def __init__(
        self, path: str, fit: str = FILL, alt: str | None = None, alpha: float = 1.0,
        src: tuple[float, float, float, float] | None = None,
    ):
        if fit not in FITS:
            raise ValueError(f"unknown image fit {fit!r}; expected one of {sorted(FITS)}")
        self.path = path
        self.fit = fit
        # Emoji/glyph shown in place of the picture on backends without images
        # (TUI). None -> a neutral "●".
        self.alt = alt
        # Global opacity (0..1) the image is drawn at, on top of its own
        # per-pixel alpha. 1.0 = fully opaque. Composited by transparency-
        # capable backends; ignored on TUI (the alt glyph stands in).
        self.alpha = alpha
        # Source crop as normalized (x, y, w, h) fractions of the image (0..1,
        # top-left origin), or None for the whole image. This is the pan/zoom
        # window: the fit still shapes the destination, while src picks how much
        # of the source feeds it. Normalized so it is independent of the units a
        # backend measures the image in (points vs pixels); each backend scales
        # by its own image size. puikit.image.zoom_window computes one from a
        # zoom + pan center.
        self.src = src

    def draw(self, ctx: DrawContext) -> None:
        # The aspect modes have already shaped the rect (via measure), so they
        # draw as a plain fill; only fill/contain/cover carry a draw-time fit.
        draw_fit = FILL if self.fit in ASPECT_FITS else self.fit
        wu, hu = ctx.size_units
        ctx.draw_image(
            0, 0, self.path,
            hints={"w": wu, "h": hu, "fit": draw_fit, "alt": self.alt,
                   "alpha": self.alpha, "src": self.src},
        )

    def measure(self, ctx: LayoutContext, axis: str, available: float) -> SizeRequest:
        # Only the aspect modes have an intrinsic size, and only on their
        # dependent axis: "width" derives height from the given width
        # (available), "height" derives width from the given height. The other
        # axis (and the other fits) fill the slot.
        if self.fit not in ASPECT_FITS:
            return SizeRequest()
        dependent = "y" if self.fit == WIDTH else "x"
        if axis != dependent:
            return SizeRequest()
        size = ctx.measure_image(self.path)
        if size is None:
            return SizeRequest()
        extent = aspect_extent(
            available, self.fit == WIDTH, size[0], size[1], ctx.base_w, ctx.base_h
        )
        return SizeRequest(min=extent, preferred=extent, max=extent)
