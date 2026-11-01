"""The ``seed`` uniforms of a shader background, and when they change.

A scene that composes from ``seed`` (see ``SHADER_PRELUDE``) should open on a new
composition when the background is set, and move to a new one when the user
comes back after it parked — once per return, and not with a cut. This is the
bookkeeping for that, shared by the macOS and Windows backends:

* **When.** A new background reseeds at once, with no blend: nothing of it was
  on screen before. A background that parked owes a reseed, paid on the
  first tick at which the user is actually using the app again — focused, with
  recent input, and not under reduced motion. Paying it on *wake* instead
  (whenever the tick re-arms) was wrong: a mouse crossing an unfocused window
  re-arms the tick on every move, the tick finds nobody using the app and parks
  again, and each of those wakes cut to another scene.
* **How.** ``seed_prev`` keeps the old seed, and ``seed_mix`` eases from 0 to 1
  over :data:`BLEND_SECONDS` of wall time, so a scene (and a
  :class:`~puikit.background.LensCycle`) can blend the old composition into the
  new one. Wall time rather than the scene clock: on resume the clock itself is
  still ramping up from a stop, and the change would otherwise take as long as
  that ramp.
"""

from __future__ import annotations

import random

#: How long the move from the old composition to the new one takes on resume.
BLEND_SECONDS = 6.0


def _smoothstep(x: float) -> float:
    x = 0.0 if x < 0.0 else 1.0 if x > 1.0 else x
    return x * x * (3.0 - 2.0 * x)


class SceneSeed:
    """``seed``, ``seed_prev`` and the eased ``seed_mix`` for one backend."""

    def __init__(self) -> None:
        self.seed = 0.0
        self.prev = 0.0
        self._mix = 1.0
        self._owed = False

    @property
    def mix(self) -> float:
        """How far from ``prev`` to ``seed``, eased; 1 once the move is done."""
        return _smoothstep(self._mix)

    def background_set(self) -> None:
        """A new background: a fresh composition, at once and without a blend."""
        self.seed = self.prev = float(random.randrange(65536))
        self._mix = 1.0
        self._owed = False

    def parked(self) -> None:
        """The background stopped: the next real resume brings a new composition."""
        self._owed = True

    def tick(self, dt: float, in_use: bool) -> None:
        """Advance the blend; pay an owed reseed if the app is ``in_use``."""
        if self._owed and in_use:
            self.prev, self._mix = self.seed, 0.0
            self.seed = float(random.randrange(65536))
            self._owed = False
        if self._mix < 1.0:
            self._mix = min(1.0, self._mix + dt / BLEND_SECONDS)
