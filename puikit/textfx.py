"""Text animations — how a string *arrives* on screen.

A ``TextEffect`` is a backend-agnostic **description** of that arrival, the same
intent model as :mod:`puikit.posteffect` and :mod:`puikit.background`. A theme
names one, the Panel plays it, and every widget that draws text takes part
without knowing it exists.

Three properties are deliberate, because they are what keeps this from becoming a
tax on the rest of the framework:

**A widget pays nothing.** The effect is applied inside ``DrawContext.draw_text``,
so a widget's ``draw`` is unchanged — there is no per-string bookkeeping, no key
to invent, no helper to write per widget. A widget that should *not* take part
sets one class attribute (``animates_text = False``); everything else is opted in
by default.

**A new animation costs one function.** A kind is a pure
``(text, progress, frame, params) -> str`` registered in :data:`TEXT_EFFECTS`.
No dataclass, no Panel dispatch branch, no backend change.

**A theme turns it on.** Nothing in an app says "animate this text" — the theme
carries a ``text_effect`` and the Panel reads it. An app never branches on which
theme is active.

Every kind must hold the string's **rendered width constant** for the whole
animation, so a mixed-width (CJK) string cannot reflow mid-flight. The helpers in
:mod:`puikit.text` (``scramble_char`` and its width-matched pools) are how the
scrambling kinds do that; a new kind that substitutes glyphs must use them too.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .text import char_width, display_width, scramble_char

#: A text animation kind: ``(text, progress, frame, params) -> str``. ``progress``
#: is 0..1 and already eased; ``frame`` is a churn counter a kind may use to vary
#: its noise over time; ``params`` carries kind-specific knobs from the effect.
TextKind = Callable[[str, float, int, dict], str]


def decode(text: str, progress: float, frame: int, params: dict) -> str:
    """Characters resolve left to right out of churning junk glyphs — the
    "decoding" look. The un-revealed tail is visible as noise from the first
    frame, so the string holds its full width throughout."""
    if progress >= 1.0 or not text:
        return text
    n = int(max(0.0, progress) * len(text) + 1e-9)
    head = text[:n]
    tail = "".join(
        scramble_char(i, frame, char_width(ch) > 1)
        for i, ch in enumerate(text[n:], start=n)
    )
    return head + tail


def typewriter(text: str, progress: float, frame: int, params: dict) -> str:
    """Characters appear left to right, as if typed; the tail is blank.

    Unlike :func:`decode` the *drawn* string grows, but the widget already
    reserved room for the whole thing (it measured the untouched text), so the
    layout still does not move — only the glyphs are absent.
    """
    if progress >= 1.0 or not text:
        return text
    return text[:int(max(0.0, progress) * len(text) + 1e-9)]


def wipe(text: str, progress: float, frame: int, params: dict) -> str:
    """Like :func:`typewriter`, but the un-revealed tail is held open by a single
    repeated glyph (``params['fill']``, default ``░``) instead of being blank —
    a "loading bar" reading, and a good fit for a monospaced column."""
    if progress >= 1.0 or not text:
        return text
    n = int(max(0.0, progress) * len(text) + 1e-9)
    fill = params.get("fill", "░")
    # Pad by rendered WIDTH, not character count, so a wide-glyph tail keeps its
    # columns when stood in for by a narrow fill character.
    tail_w = display_width(text[n:])
    return text[:n] + fill * max(0, tail_w // max(1, display_width(fill)))


def flicker(text: str, progress: float, frame: int, params: dict) -> str:
    """The whole string is present from the start, but random characters flip to
    junk and settle — a bad-signal / interference reading rather than a reveal.

    Density falls with progress, so it calms down instead of stopping abruptly.
    Suits a status line that should feel *received* rather than typed.
    """
    if progress >= 1.0 or not text:
        return text
    density = (1.0 - max(0.0, progress)) * float(params.get("density", 0.35))
    out = []
    for i, ch in enumerate(text):
        # Reuse the deterministic hash as a per-character coin flip, so the same
        # frame redraws identically (see scramble_char).
        h = (i * 2246822519 + frame * 374761393) & 0xFFFFFFFF
        h ^= h >> 15
        if (h % 1000) / 1000.0 < density and not ch.isspace():
            out.append(scramble_char(i, frame, char_width(ch) > 1))
        else:
            out.append(ch)
    return "".join(out)


#: Name → kind. An app may register its own by assigning into this dict; that is
#: the whole cost of adding a text animation.
TEXT_EFFECTS: dict[str, TextKind] = {
    "decode": decode,
    "typewriter": typewriter,
    "wipe": wipe,
    "flicker": flicker,
}


@dataclass(frozen=True)
class TextEffect:
    """How text arrives, as theme-carried data.

    Fields:
      kind          A key of :data:`TEXT_EFFECTS`. An unknown name disables the
                    effect rather than raising — this arrives from theme and user
                    config, where a typo should cost the animation, not the app.
      duration_ms   How long one string takes to arrive.
      stagger_ms    Delay added per string *within one widget*, so a pane's rows
                    cascade instead of all resolving at once. ``0`` fires them
                    together.
      max_strings   Cap on how many strings a single widget animates in one pass;
                    the rest appear complete. Bounds the cascade on a widget with
                    hundreds of visible strings (a full file pane, a log view),
                    where a long stagger would otherwise take seconds to settle.
                    ``0`` means no cap.
      scramble_fps  Churn rate of the noise glyphs. Deliberately well under the
                    frame rate: re-rolled every frame they are visual noise, and
                    fast luminance churn is what reduced motion exists to prevent.
      easing        Curve name (see :mod:`puikit.easing`). ``None`` is linear,
                    which is right for most kinds — typing is a constant-rate act
                    and an eased reveal reads as the machine hesitating.
      params        Kind-specific knobs (``fill`` for :func:`wipe`, ``density``
                    for :func:`flicker`).
    """

    kind: str = "decode"
    duration_ms: int = 420
    stagger_ms: int = 0
    max_strings: int = 0
    scramble_fps: float = 12.0
    easing: str | None = None
    params: dict = field(default_factory=dict)

    @property
    def is_noop(self) -> bool:
        """True when this would change nothing — no duration, or a kind that is
        not registered."""
        return self.duration_ms <= 0 or self.kind not in TEXT_EFFECTS

    @property
    def fn(self) -> TextKind | None:
        """The registered kind, or ``None`` when the name is unknown."""
        return TEXT_EFFECTS.get(self.kind)


def coerce(spec: Any) -> TextEffect | None:
    """A :class:`TextEffect` from whatever a theme carried: an effect (returned
    as-is), a kind name, a parameter dict, or ``None``/``True``.

    Themes are data — often hand-written in a user's ``config.py`` — so this
    accepts the shorthand forms and never raises on a bad one; an unusable spec
    yields ``None`` and the UI simply draws its text plainly.
    """
    if spec is None or spec is False:
        return None
    if isinstance(spec, TextEffect):
        return None if spec.is_noop else spec
    if spec is True:
        return TextEffect()
    if isinstance(spec, str):
        effect = TextEffect(kind=spec)
        return None if effect.is_noop else effect
    if isinstance(spec, dict):
        known = {f for f in TextEffect.__dataclass_fields__ if f != "params"}
        kw = {k: v for k, v in spec.items() if k in known}
        params = dict(spec.get("params") or {})
        # Unknown keys are treated as kind params, so a kind's own knobs can be
        # written inline (``{"kind": "wipe", "fill": "▒"}``) without a nested dict.
        params.update({k: v for k, v in spec.items()
                       if k not in known and k != "params"})
        try:
            effect = TextEffect(**kw, params=params)
        except TypeError:
            return None
        return None if effect.is_noop else effect
    return None
