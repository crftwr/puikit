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


#: Default glyph for the ``flash`` option — a full block, which on a character
#: grid fills its cell exactly and so reads as a solid rectangle. This is as
#: close to "flash a rectangle" as a kind can get: a kind returns a *string*, and
#: ``draw_text`` applies one style per run, so a per-character colored/inverted
#: rectangle is not expressible without per-character styling. A block glyph
#: needs no such thing and renders identically on every backend.
FLASH_GLYPH = "█"

#: Sentinel standing for "this source character is not drawn yet", emitted only
#: on a **proportional** run. There it replaces the width-matched blank a grid
#: run uses, because on a proportional face no glyph reliably matches a
#: character's advance. The Panel strips it and positions the visible pieces by
#: *measuring* the real text instead — so a gap costs nothing and holds nothing
#: open (see ``DrawContext._draw_measured``). Never reaches a backend.
HIDDEN = "\x00"


def _text_salt(text: str) -> int:
    """A deterministic hash of ``text``, seeding any per-string randomness.

    Deterministic rather than ``hash()``: Python randomizes string hashing per
    process, so a scatter order would differ between runs and could not be
    tested. Derived from the content so two *different* strings scatter
    differently — without it a pane of equal-length rows would reveal in the
    identical order and read as a marching pattern rather than as noise.
    """
    h = 2166136261
    for ch in text:
        h = ((h ^ ord(ch)) * 16777619) & 0xFFFFFFFF
    return h


def _pad(glyph: str, width: int) -> str:
    """``glyph`` repeated to occupy ``width`` columns — how a stand-in keeps the
    column count of the character it replaces, so a wide (CJK) glyph never
    collapses to one column mid-animation."""
    return glyph * max(1, width)


def _render_reveal(text: str, progress: float, frame: int, params: dict,
                   threshold) -> str:
    """Shared renderer for the reveal kinds.

    ``threshold(i, n, salt) -> float`` decides *when* character ``i`` resolves,
    which is the only thing that differs between typing left to right and
    landing in scattered order. Everything else — the flash window, the stand-in
    for a not-yet-revealed character, and holding the column count — is the same
    for all of them and lives here rather than in each kind.

    Three states per character: already resolved (the real glyph), *just*
    resolved (the flash block, while ``flash`` is non-zero), and not yet
    (``hidden``: blanks, or churning junk with ``hidden="scramble"``).

    Trailing blanks are trimmed. A kind whose un-revealed tail is blank then
    returns a bare prefix — the drawn string simply stops, painting nothing over
    cells it does not own — while interior gaps keep their spacing, which is what
    holds a scattered reveal's positions still.

    **Proportional text.** All of the above assumes a stand-in occupies the same
    space as the character it replaces, which is true by column count on a
    grid-aligned run and false on a proportional one: a blank is far narrower
    than ``W``, and a block glyph has its own advance. Substituting *between*
    resolved glyphs there would put them at the wrong x and make them jump as the
    gaps fill — over a base unit of drift on a real UI font.

    So when ``params`` says the run is not grid-aligned (the Panel injects
    ``grid``), the **order** degrades to left-to-right rather than the result
    being trimmed. Every resolved character is then a true prefix and every
    stand-in is trailing, so nothing can be displaced — and the reveal still
    progresses visibly the whole way through.

    On such a run the reveal keeps its **order** — a scattered reveal still
    scatters — and instead emits :data:`HIDDEN` for an un-revealed character and
    exactly one glyph per source character, so the result stays index-aligned
    with the source. The Panel then draws each visible piece at the x its
    character will finally occupy, measured from the real text, and skips the
    hidden ones entirely. Position comes from measurement rather than from
    string layout, so a gap needs no placeholder to hold it open and nothing can
    be displaced.

    Two earlier attempts were worse and are worth remembering. Padding with
    width-matched blanks moved every resolved glyph after a gap (a blank is far
    narrower than ``W``). Trimming to the contiguous resolved prefix fixed the
    positions but destroyed the animation: a scattered reveal's prefix stays
    empty until nearly the end, so the text sat invisible behind the flash and
    then appeared all at once.
    """
    if progress >= 1.0 or not text:
        return text
    p = max(0.0, progress)
    n = len(text)
    salt = _text_salt(text)
    flash = max(0.0, min(1.0, float(params.get("flash", 0.0))))
    flash_glyph = params.get("flash_glyph", FLASH_GLYPH)
    hidden = params.get("hidden", " ")
    scramble = hidden == "scramble"
    grid = bool(params.get("grid", True))
    out: list[str] = []
    last_visible = -1
    for i, ch in enumerate(text):
        width = char_width(ch)
        # Compressed into [0, 1-flash] so even the last character finishes its
        # flash before the animation ends, rather than being cut off at p == 1.
        t = threshold(i, n, salt) * (1.0 - flash)
        if p >= t + flash:
            out.append(ch)
            last_visible = i
        elif p >= t:
            # One glyph per source character on a proportional run, so the result
            # stays index-aligned and the Panel can position it by measurement.
            out.append(_pad(flash_glyph, width) if grid else flash_glyph)
            last_visible = i
        elif scramble:
            out.append(scramble_char(i, frame, width > 1))
            last_visible = i
        elif grid:
            out.append(_pad(hidden, width))
        else:
            out.append(HIDDEN)
    if not grid:
        return "".join(out)  # full length: index alignment is what positions it
    return "".join(out[: last_visible + 1])


def _linear_threshold(i: int, n: int, salt: int) -> float:
    """Left to right: character ``i`` resolves once ``(i+1)/n`` of the way in.

    ``(i+1)/n`` rather than ``i/n`` so that progress 0 shows *nothing* — with
    ``i/n`` the first character has threshold 0 and is already resolved before
    the animation starts.
    """
    return (i + 1) / n if n else 0.0


def _scatter_threshold(i: int, n: int, salt: int) -> float:
    """Scattered order: a stable pseudo-random instant per character.

    Salted by the string's content, so the order is fixed for a given string
    (a redraw of one frame is identical) but differs between strings. Uniform
    rather than an exact shuffle — some characters land together and small gaps
    open up, which reads as more organic than a perfectly even scatter and
    costs no sort.
    """
    h = (i * 2654435761 + salt) & 0xFFFFFFFF
    h ^= h >> 16
    h = (h * 2246822519) & 0xFFFFFFFF
    h ^= h >> 13
    return (h & 0xFFFFFF) / float(0x1000000)


def decode(text: str, progress: float, frame: int, params: dict) -> str:
    """Characters resolve left to right out of churning junk glyphs — the
    "decoding" look. The un-revealed tail is visible as noise from the first
    frame, so the string holds its full width throughout."""
    params = {"hidden": "scramble", **params}
    return _render_reveal(text, progress, frame, params, _linear_threshold)


def typewriter(text: str, progress: float, frame: int, params: dict) -> str:
    """Characters appear left to right, as if typed; the tail is blank.

    Unlike :func:`decode` the *drawn* string grows, but the widget already
    reserved room for the whole thing (it measured the untouched text), so the
    layout still does not move — only the glyphs are absent.

    Pair with ``flash`` for a typing cursor: the character being written shows as
    a solid block for an instant before settling into its glyph.
    """
    return _render_reveal(text, progress, frame, params, _linear_threshold)


def scatter(text: str, progress: float, frame: int, params: dict) -> str:
    """Characters resolve in **random order**, each landing in its final place.

    Positions never move: a character that has not resolved yet holds its
    columns with blanks (or with churning junk under ``hidden="scramble"``), so
    the ones already resolved stay exactly where they will end up. The result
    reads as a message materializing out of nothing rather than being typed.

    The order is stable for a given string and differs between strings, so a
    pane of similar rows does not reveal in lockstep. On a proportional run the
    order degrades to left-to-right — see :func:`_render_reveal`.
    """
    return _render_reveal(text, progress, frame, params, _scatter_threshold)


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
    "scatter": scatter,
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


def merge(base: "TextEffect", override: Any) -> "TextEffect | None":
    """``base`` with ``override`` applied on top — how a *widget* varies the
    active effect without restating it.

    The split of authority is deliberate. A theme decides **whether** text
    animates at all (and is the only thing that can turn it on); a widget may
    then say **which** flavor suits its content — a text viewer materializing a
    screenful rather than typing it out. A widget preference alone animates
    nothing, so a theme that opts out stays completely plain no matter what its
    widgets would have preferred.

    Merging rather than replacing keeps the theme's *timing* — ``duration_ms``,
    ``stagger_ms``, ``max_strings`` are how a theme paces the whole UI — so a
    widget naming only ``{"kind": "scatter"}`` inherits the rest. Anything the
    override does name wins, params included. An unusable override falls back to
    ``base`` rather than disabling the effect, matching ``coerce``'s rule that
    bad data costs the animation, never the app.
    """
    if override is None:
        return base
    if isinstance(override, TextEffect):
        return override
    fields = {f: getattr(base, f) for f in TextEffect.__dataclass_fields__}
    if isinstance(override, str):
        fields["kind"] = override
    elif isinstance(override, dict):
        known = {f for f in TextEffect.__dataclass_fields__ if f != "params"}
        params = dict(base.params)
        params.update(override.get("params") or {})
        params.update({k: v for k, v in override.items()
                       if k not in known and k != "params"})
        fields.update({k: v for k, v in override.items() if k in known})
        fields["params"] = params
    else:
        return base
    try:
        merged = TextEffect(**fields)
    except TypeError:
        return base
    return base if merged.is_noop else merged
