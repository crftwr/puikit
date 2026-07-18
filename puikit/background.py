"""Animated 3D background — a description + the pure geometry that drives it.

Like ``PostEffect``, a ``Background3D`` is a *backend-agnostic description*: it
names a scene and carries a few normalized parameters. A backend that owns real
pixels renders it *behind* the display list (macOS strokes the projected edges
with CoreGraphics); a grid backend (curses) has no sub-cell pixels and ignores
it. An app never branches on the backend — it builds one ``Background3D`` and
hands it to ``backend.set_background(...)``; backends without the
``background_3d`` capability inherit the base no-op, so the call is always safe.

The scene is drawn *first* (right after the frame is cleared, before any widget
paints), so it reads as a wallpaper the UI sits on top of. It only shows through
where the UI does not paint an opaque fill — so it is most visible under a sparse
layout, or under semi-transparent panels.

The 3D itself lives here, not in the backend, and is **pure math**: given a view
size and a time, ``wireframe_segments`` returns the 2D line segments to stroke.
Keeping projection out of the backend means it is testable with no window and no
native frameworks — the backend only strokes lines.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

from .backend import Color

#: One projected 2D line segment ``(x0, y0, x1, y1)`` in pixels (top-left origin).
Segment = tuple[float, float, float, float]
#: An animation frame generator: ``(width, height, t, *, speed) -> [Segment, ...]``.
Segments = Callable[..., "list[Segment]"]


@dataclass(frozen=True)
class Background3D:
    """The **animation** background kind and its parameters — an animated scene
    drawn behind the UI (see also :class:`Wallpaper` for the static-image kind and
    ``None`` for the plain solid-color kind). Handed to :meth:`Backend.set_background`.

    Fields:
      kind     Animation type, resolved through :data:`ANIMATIONS`. ``"cube"`` (a
               rotating wireframe cube, also spelled ``"wireframe"``) is the only
               one defined today; the field keeps the door open for more (``"snow"``,
               ``"particle"``, ...) without changing the call site.
      color    Edge/line color. ``None`` lets the backend derive one from the
               active theme's foreground, so the background stays on-palette.
      speed    Rotation-speed multiplier (1.0 = the tuned default).
      opacity  Line alpha, 0..1. Low values keep the animation a subtle backdrop
               that does not fight the UI for attention.
      backdrop The color the frame is cleared to *under* the scene — what the
               reveal-dissolved surfaces (and the bare gaps) fall back to. ``None``
               uses the backend's neutral dark clear, which suits a dark UI but
               muddies a light one (the dissolved surfaces darken toward it) and
               hides a dark scene line (drawn onto near-black). Pass the app's theme
               background so a light theme stays light where dissolved and a dark
               ``color`` reads against it.

    How translucent the UI becomes so the scene shows *through* it is **not** a
    property of the scene: it is the backend-wide "surface reveal" set separately
    with :meth:`Backend.set_surface_opacity`. Keeping it off the scene lets the same
    reveal apply to any wallpaper (a future static image, not just this cube) and be
    owned by the app's theme rather than baked into one background kind.
    """

    kind: str = "wireframe"
    color: Color | None = None
    speed: float = 1.0
    opacity: float = 0.6
    backdrop: Color | None = None

    def __post_init__(self) -> None:
        # Clamp the 0..1 opacity on the frozen dataclass so config can't push a
        # backend out of range; speed is left free (a fast spin is a valid choice).
        v = self.opacity
        object.__setattr__(self, "opacity", 0.0 if v < 0.0 else 1.0 if v > 1.0 else float(v))

    @property
    def is_noop(self) -> bool:
        """True when the background would draw nothing (fully transparent)."""
        return self.opacity <= 0.0


@dataclass(frozen=True)
class Wallpaper:
    """The **wallpaper** background kind: a single static image drawn behind the UI
    (see :class:`Background3D` for the animation kind, ``None`` for solid). Handed to
    :meth:`Backend.set_background`; a backend that owns pixels draws it under the
    display list, a character-grid terminal ignores it.

    Fields:
      image    Filesystem path to the image (``~`` is expanded by the backend). A
               path that fails to load draws nothing (the ``backdrop`` shows), so a
               bad path degrades gracefully rather than raising.
      fit      How the image is scaled into the window: ``"fill"`` (cover, cropping
               overflow — the default), ``"fit"`` (contain, letterboxed to the
               ``backdrop``), ``"stretch"`` (ignore aspect), or ``"center"`` (native
               size, centered).
      opacity  Image alpha, 0..1, composited over the ``backdrop``. ``1`` (default)
               is fully opaque; lower blends the image toward the backdrop.
      backdrop The color the frame is cleared to *under* the image — shown through a
               translucent image, in the letterbox bars of ``"fit"``, or around a
               ``"center"``. ``None`` uses the backend's neutral dark clear; pass the
               theme background so it stays on-palette (mirrors ``Background3D``).
    """

    image: str
    fit: str = "fill"
    opacity: float = 1.0
    backdrop: Color | None = None

    def __post_init__(self) -> None:
        v = self.opacity
        object.__setattr__(self, "opacity", 0.0 if v < 0.0 else 1.0 if v > 1.0 else float(v))

    @property
    def is_noop(self) -> bool:
        """True when the wallpaper would draw nothing (no path, or fully transparent)."""
        return not self.image or self.opacity <= 0.0


# A unit cube centered on the origin, vertices at (±1, ±1, ±1): four on the back
# face (z=-1), four on the front (z=+1).
_CUBE_VERTS: tuple[tuple[float, float, float], ...] = (
    (-1, -1, -1), (1, -1, -1), (1, 1, -1), (-1, 1, -1),
    (-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1),
)

# The 12 edges as index pairs into _CUBE_VERTS: back square, front square, then
# the four connectors between them.
_CUBE_EDGES: tuple[tuple[int, int], ...] = (
    (0, 1), (1, 2), (2, 3), (3, 0),
    (4, 5), (5, 6), (6, 7), (7, 4),
    (0, 4), (1, 5), (2, 6), (3, 7),
)

#: Camera distance along +z and the focal length used for the perspective
#: divide. ``_CAMERA_DIST`` must stay > the cube's half-diagonal (√3) so no
#: vertex reaches or crosses the camera plane (z→0) and blows up the divide.
_CAMERA_DIST = 4.0
_FOCAL = 3.0
#: Cube radius as a fraction of the view's shorter side, so it fits any window.
_FIT = 0.30


def _rotate(v: tuple[float, float, float], ax: float, ay: float
            ) -> tuple[float, float, float]:
    """Rotate a point around the X axis by ``ax`` then the Y axis by ``ay``."""
    x, y, z = v
    # Around X.
    cx, sx = math.cos(ax), math.sin(ax)
    y, z = y * cx - z * sx, y * sx + z * cx
    # Around Y.
    cy, sy = math.cos(ay), math.sin(ay)
    x, z = x * cy + z * sy, -x * sy + z * cy
    return x, y, z


def wireframe_segments(
    width: float, height: float, t: float, *, speed: float = 1.0
) -> list[tuple[float, float, float, float]]:
    """The rotating-cube wireframe as 2D line segments for a ``width`` × ``height``
    view at time ``t`` (seconds).

    Pure function: no backend, no globals, no framework. Each segment is
    ``(x0, y0, x1, y1)`` in **pixels, top-left origin** (matching the flipped
    view), ready to stroke. The cube spins on two axes at rates scaled by
    ``speed`` and is centered and scaled to fit the shorter side, so it never
    clips regardless of the window's aspect ratio. Returns ``[]`` for a
    degenerate (zero-area) view.
    """
    if width <= 0 or height <= 0:
        return []
    ax = t * speed * 0.5
    ay = t * speed * 0.8
    cx, cy = width * 0.5, height * 0.5
    scale = _FIT * min(width, height)

    projected: list[tuple[float, float]] = []
    for v in _CUBE_VERTS:
        rx, ry, rz = _rotate(v, ax, ay)
        f = _FOCAL / (rz + _CAMERA_DIST)  # perspective divide (denominator > 0)
        projected.append((cx + rx * f * scale, cy + ry * f * scale))

    return [
        (projected[a][0], projected[a][1], projected[b][0], projected[b][1])
        for a, b in _CUBE_EDGES
    ]


#: A ready-made default (subtle spinning cube). Apps can address it by name via a
#: preset table, mirroring ``posteffect.PRESETS``.
WIREFRAME = Background3D(kind="wireframe", speed=1.0, opacity=0.6)

PRESETS: dict[str, Background3D] = {
    "wireframe": WIREFRAME,
    "cube": WIREFRAME,
}

#: Animation type → the pure function that projects one frame to 2D line segments
#: (see ``wireframe_segments``). ``Background3D.kind`` is looked up here by a
#: backend that owns pixels, so a new animation is added by registering a segment
#: function — no backend change. Only the wireframe cube exists today; a
#: non-segment animation (falling ``"snow"``, a ``"particle"`` field) would extend
#: this to a richer renderer interface when it lands.
ANIMATIONS: dict[str, "Segments"] = {
    "wireframe": wireframe_segments,
    "cube": wireframe_segments,
}
