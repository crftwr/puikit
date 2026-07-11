"""Full-screen post-processing effects (a CRT / phosphor "look").

A ``PostEffect`` is a *backend-agnostic description* of a screen effect — the
same intent model as the rest of PuiKit. It names an effect family and carries
normalized (0..1) parameters; a backend that owns real pixels interprets them
(macOS via Core Image, Windows via Direct2D effects), while a grid backend
(curses) has no sub-cell pixels and simply ignores it.

An app never branches on the backend: it builds one ``PostEffect`` and hands it
to ``backend.set_post_effect(...)``. Backends without the ``post_effects``
capability inherit the base no-op, so the call is always safe.

The effect is *composited over the whole rendered frame*, after the display
list is rasterized. It is not a widget and not part of the layout — it is a
property of the backend's output surface, set once (typically when a theme that
recommends it becomes active) and re-applied across resizes by the backend.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .backend import Color


def _clamp01(value: float) -> float:
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else float(value)


@dataclass(frozen=True)
class PostEffect:
    """A composited screen effect and its parameters.

    All strengths are normalized 0..1 (a backend maps them onto its own units —
    e.g. ``bloom`` becomes a Core Image blur radius / Direct2D standard
    deviation). ``0`` for every strength is a no-op pass; ``None`` passed to
    ``set_post_effect`` clears the effect entirely.

    Fields:
      name       Effect family. Only ``"crt"`` is defined today; the field keeps
                 the door open for other families (e.g. a future ``"lcd"``)
                 without changing the call site.
      tint       Monochrome phosphor color. When set, the frame's luminance is
                 remapped onto this hue (black→black, white→tint), giving the
                 single-color "screen" look. ``None`` leaves color untouched —
                 useful when the active *theme* is already monochrome and the
                 effect only needs glow / scanlines.
      bloom      Phosphor glow: bright areas bleed into their neighbours.
      scanline   Horizontal CRT scanline darkening.
      vignette   Corner/edge darkening (the tube's rounded falloff).
      curvature  Barrel distortion — the screen bulges toward the viewer.
      flicker    Subtle per-frame brightness wobble (needs an animating backend;
                 a still backend renders it as a constant slight dim).
      glow       Overall exposure lift, making the phosphor feel emissive.
      roll       A "vertical hold" glitch: a bright noisy scanline band that
                 occasionally sweeps top-to-bottom, like an untuned CRT. Animated
                 (self-driven), so it needs a backend with an animation timer; a
                 still backend ignores it. Sets how bright/frequent the band is.
    """

    name: str = "crt"
    tint: Color | None = None
    bloom: float = 0.0
    scanline: float = 0.0
    vignette: float = 0.0
    curvature: float = 0.0
    flicker: float = 0.0
    glow: float = 0.0
    roll: float = 0.0

    def __post_init__(self) -> None:
        # Clamp on a frozen dataclass via object.__setattr__ so callers (and
        # config files) can't push a backend into an out-of-range parameter.
        for f in ("bloom", "scanline", "vignette", "curvature", "flicker",
                  "glow", "roll"):
            object.__setattr__(self, f, _clamp01(getattr(self, f)))

    @property
    def is_noop(self) -> bool:
        """True when the effect would change nothing (no tint and every strength
        zero) — a backend can skip the whole composite pass."""
        return self.tint is None and not any(
            (self.bloom, self.scanline, self.vignette, self.curvature,
             self.flicker, self.glow, self.roll)
        )

    def with_tint(self, tint: Color | None) -> "PostEffect":
        """A copy tinted to ``tint`` (used to derive the phosphor color from the
        active theme's foreground when a preset leaves it unset)."""
        return replace(self, tint=tint)


#: The default CRT screen effect — a soft phosphor glow: a tight bloom halo on
#: bright text, subtle scanlines, a light vignette, and an occasional rolling
#: band. No ``tint`` — pair it with a monochrome theme (e.g. TFM's phosphor-green
#: "Phosphor") and it reinforces that hue, or call ``.with_tint(color)`` to force
#: one. Values are the tuned defaults; override any of them per-theme in config.
CRT = PostEffect(
    name="crt", bloom=0.30, scanline=0.15, vignette=0.15, glow=0.22, roll=0.10,
)

#: Named presets addressable from config (a theme may recommend ``"crt"`` by name
#: instead of spelling out every parameter).
PRESETS: dict[str, PostEffect] = {
    "crt": CRT,
}
