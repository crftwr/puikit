"""Semantic surface roles mapped to concrete colors per backend.

Apps tag panes with a surface role (hints={"surface": "status"}) instead of
hardcoding colors, so each backend can apply its own region-separation
strategy:

- hairline-capable backends (GUI) may give adjacent roles the same
  background and rely on 1-device-pixel divider lines (zero base unit cost)
- whole-unit backends (TUI) cannot afford a full row/column for a line, so
  the theme guarantees adjacent roles contrasting backgrounds instead

An explicit "bg" hint always overrides the theme; the app then owns the
separation quality on whole-unit backends.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .backend import Color
from .capability import CapabilityProfile


@dataclass(frozen=True)
class Theme:
    """Maps surface roles to pane background colors, plus the divider color
    used for layout dividers (hairlines on GUI, line base units on TUI).

    The control palette below gives the interactive widgets a coherent,
    VS Code-like look (flat fills, an accent focus color, hover/selection
    tints) in one place. The defaults are shared by every backend; TUI snaps
    each color to the nearest xterm-256 cell, GUI paints it exactly."""

    surfaces: dict[str, Color] = field(default_factory=dict)
    divider_color: Color = (110, 110, 124)

    # --- control palette (VS Code Dark+) -------------------------------------
    accent: Color = (0, 122, 204)          # focus ring / primary accent #007ACC
    text: Color = (212, 212, 212)          # default control foreground  #D4D4D4
    muted_text: Color = (157, 157, 157)    # secondary text              #9D9D9D
    # Text field / dropdown / combo face. Lifted well above the base content
    # surface (~#1E1E26) so an input reads as a distinct inset region, not a
    # bare patch of background. On TUI the gap matters most: the base surface
    # and the old #3C3C3C face quantized to neighbouring gray stops and the
    # field nearly vanished, so the face is raised to a clearly separated stop.
    control_bg: Color = (78, 78, 86)       # text field / dropdown face  #4E4E56
    # Hover face for an input/dropdown: a lift *above* control_bg. The generic
    # row hover_bg below is a subtle tint over the dark content surface and is
    # darker than the lighter field face, so a field must hover toward lighter,
    # not toward the row tint, or it would counterintuitively darken on hover.
    control_hover_bg: Color = (96, 96, 105)  # input hover face           #606069
    control_border: Color = (104, 104, 114)  # control outline (above face) #686872
    button_bg: Color = (14, 99, 156)       # primary button face         #0E639C
    button_hover_bg: Color = (17, 119, 187)  # button hover              #1177BB
    button_text: Color = (255, 255, 255)
    # Secondary (no-accent) button face: a neutral fill for a non-primary
    # action, so a screen with two buttons reads one as the prominent choice.
    button_secondary_bg: Color = (58, 61, 65)         # secondary face   #3A3D41
    button_secondary_hover_bg: Color = (69, 73, 78)   # secondary hover  #45494E
    # Outline for a neutral (secondary / bare-icon) button face. The flat
    # secondary fill is only a small lift off the surface, so on a light theme it
    # nearly matches the dialog/page background and the button loses its frame.
    # A subtle outline (a step further off the surface) keeps the button readable
    # as a button on any theme — the primary/accent face needs no border.
    button_secondary_border: Color = (104, 104, 114)  # secondary outline #686872
    selection_bg: Color = (10, 105, 178)   # active selection            #0A69B2
    # List/row selection split by focus (interaction_states.md §4b/§5): the loud
    # accent fill goes to the *focused* widget, the muted neutral to a list whose
    # focus has moved away — the louder cue always marks focus, never the reverse.
    # The focused fill sits close to the accent (#007ACC) rather than the dim
    # VS Code #094771, so the active row clearly reads as "selected + focused"
    # and never blends into the dark content background — on TUI especially,
    # where the old fill quantized to within a stop of the surrounding panes.
    selection_active_bg: Color = (10, 105, 178)       # focused  #0A69B2
    selection_inactive_bg: Color = (55, 55, 61)       # blurred  #37373D
    # Text-field selection, split by focus: a clearly legible blue while the
    # field is focused, a muted neutral when focus is elsewhere (the editor
    # selection / inactive-selection pair from VS Code).
    text_selection_bg: Color = (38, 79, 120)          # focused  #264F78
    text_selection_inactive_bg: Color = (58, 61, 65)  # blurred  #3A3D41
    hover_bg: Color = (42, 45, 46)         # row hover                   #2A2D2E
    popup_bg: Color = (37, 37, 38)         # menu / popup surface        #252526
    popup_border: Color = (84, 84, 92)     # menu / popup frame line     #54545C
    # Scrollbar split by brightness: the track sits close to the background (a
    # faint groove), the knob lands on the opposite brightness side so it reads
    # clearly against both the track and the pane. The base defaults are neutral
    # grays that already exist as curated TUI-palette stops, so folding them in
    # (theme colors seed that palette) costs no extra slot; a derived theme
    # (derive_theme) tints them along its own background→foreground axis.
    scrollbar_track: Color = (48, 48, 48)   # track (near background)
    scrollbar_thumb: Color = (140, 140, 140)  # knob (opposite brightness)

    def surface_bg(self, role: str) -> Color | None:
        return self.surfaces.get(role)

    def fade_scrim(self) -> tuple[Color, Color]:
        """The (fg, bg) pair a whole-cell backend paints over a group for the
        2-frame ``fade`` stand-in. A fade is opacity: the group's content blends
        *into its own background*, so the scrim background stays at the page
        color and the foreground sinks halfway toward it — the text reads faint,
        not gone. This is polarity-correct by construction (a light theme washes
        toward near-white, a dark one toward near-black), so a fade never flashes
        a fixed dark scrim over a light surface. Distinct from the modal
        ``dim_below`` scrim, which deliberately darkens to make the page recede.
        """
        bg = self.surfaces.get("content", (30, 30, 38))
        return _mix(bg, self.text, 0.4), bg

    def dim_scrim(self) -> tuple[Color, Color]:
        """The (fg, bg) pair a whole-cell backend paints uniformly over the page
        under a modal layer (``dim_below``). Unlike :meth:`fade_scrim` — which
        washes a group toward its *own* background to read as opacity — the modal
        dim deliberately pushes the page toward shadow so it recedes behind the
        layer, but it must stay **polarity-correct**: a dark theme darkens toward
        near-black while a light theme settles on a mid gray, never a near-black
        bar. So the veil background steps the content surface toward black (light
        → gray, dark → darker), and the veil foreground leans back toward the
        theme's own text — muted light-gray text on a dark veil, muted dark text
        on a gray veil — so the dimmed page keeps the theme's contrast direction
        instead of inverting it (gray bg + dark text on a light theme, not black
        bg + gray text)."""
        bg = self.surfaces.get("content", (30, 30, 38))
        veil_bg = _mix(bg, _BLACK, 0.30)
        return _mix(veil_bg, self.text, 0.55), veil_bg


# --- derivation ---------------------------------------------------------------
# A full Theme names ~24 colors. Most are not independent decisions: a hover is a
# lift from its resting face, an inactive selection a muted version of the active
# one, a border a step above the surface it frames. `derive_theme` takes the six
# colors that *are* genuine choices and computes the rest, so a theme reads as a
# short, legible declaration. Any derived field can still be pinned by passing it
# as an override (explicit always wins).

_WHITE: Color = (255, 255, 255)
_BLACK: Color = (0, 0, 0)


def _lum(c: Color) -> float:
    """Rec. 601 luma; only the relative magnitude matters here."""
    return 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]


def _clamp(v: float) -> int:
    return max(0, min(255, round(v)))


def _mix(a: Color, b: Color, t: float) -> Color:
    """Linear blend a→b by t in [0, 1]."""
    return (
        _clamp(a[0] + (b[0] - a[0]) * t),
        _clamp(a[1] + (b[1] - a[1]) * t),
        _clamp(a[2] + (b[2] - a[2]) * t),
    )


def derive_theme(
    *,
    background: Color,
    foreground: Color,
    muted: Color,
    accent: Color,
    surface: Color,
    selection: Color,
    **overrides: Any,
) -> Theme:
    """Build a full :class:`Theme` from six base colors.

    - ``background`` — the content surface; its luminance also picks the lift
      *direction* (a dark theme raises elements lighter, a light theme darker).
    - ``foreground`` — primary text.
    - ``muted`` — secondary text / dividers (its own base because several themes
      use a designed "comment" gray that is not a plain fg↔bg blend).
    - ``accent`` — focus rings, primary button, status bar.
    - ``surface`` — the raised panel shade (sidebar / header / popup / inputs
      derive from it).
    - ``selection`` — the active list/text selection fill.

    Every other color is a lighten/darken/blend of these. Pass any concrete
    :class:`Theme` field name as a keyword to override its derived value; a
    ``surfaces`` override merges per-role rather than replacing the whole dict.
    """
    dark = _lum(background) < 128

    def lift(c: Color, amt: float) -> Color:
        # Raise an element away from the background toward the contrast pole:
        # lighter on a dark theme (which has the headroom), darker — and only
        # half as far, so panels stay subtle — on a light theme.
        return _mix(c, _WHITE, amt) if dark else _mix(c, _BLACK, amt * 0.5)

    derived: dict[str, Any] = dict(
        surfaces={
            "content": background,
            "sidebar": surface,
            "header": lift(surface, 0.12),
            "status": accent,
        },
        divider_color=_mix(surface, muted, 0.5),
        accent=accent,
        text=foreground,
        muted_text=muted,
        control_bg=lift(background, 0.18),
        control_hover_bg=lift(background, 0.26),
        control_border=lift(background, 0.34),
        button_bg=accent,
        # Primary-button hover tracks the theme polarity, not the accent: lighten
        # on a dark theme, darken on a light one (VS Code's #0062A3 light hover).
        button_hover_bg=_mix(accent, _WHITE if dark else _BLACK, 0.12),
        # On-accent label: white over a dark accent, the page color over a light
        # one (a bright accent needs dark text to stay legible).
        button_text=_WHITE if _lum(accent) < 140 else background,
        button_secondary_bg=lift(background, 0.16),
        button_secondary_hover_bg=lift(background, 0.24),
        # A step further off the surface than the fill, so the neutral button
        # keeps a visible frame even where the fill itself nearly matches the
        # surface (a light theme darkens the fill only slightly).
        button_secondary_border=lift(background, 0.34),
        selection_bg=selection,
        selection_active_bg=selection,
        # Inactive (focus-elsewhere) selection: a muted neutral, a blend of the
        # surface toward the text color, never the loud accent fill.
        selection_inactive_bg=_mix(background, foreground, 0.18),
        text_selection_bg=_mix(accent, background, 0.45),
        text_selection_inactive_bg=_mix(background, foreground, 0.16),
        hover_bg=lift(background, 0.07),
        popup_bg=surface,
        popup_border=lift(surface, 0.18),
        # Scrollbar along the background→foreground axis: the track stays near
        # the background (a faint groove), the knob crosses past the midpoint to
        # the foreground (opposite-brightness) side so it reads on both.
        scrollbar_track=_mix(background, foreground, 0.10),
        scrollbar_thumb=_mix(background, foreground, 0.55),
    )
    if "surfaces" in overrides:
        derived["surfaces"] = {**derived["surfaces"], **overrides.pop("surfaces")}
    derived.update(overrides)
    return Theme(**derived)


# GUI: adjacent surfaces may share a background; hairlines do the separating.
THEME_GUI = Theme(
    surfaces={
        "content": (30, 30, 38),
        "sidebar": (30, 30, 38),
        "header": (38, 38, 48),
        "status": (30, 30, 38),
    },
    divider_color=(90, 90, 104),
)

# TUI: background contrast does the separating; no base units spent on lines.
THEME_TUI = Theme(
    surfaces={
        "content": (30, 30, 38),
        "sidebar": (42, 42, 54),
        "header": (56, 56, 72),
        "status": (72, 72, 92),
    },
    divider_color=(128, 128, 144),
    # A menu/popup floats over content with no hairline frame on a character
    # grid — only the background contrast bounds it. The default dark popup
    # (37,37,38) sits too close to the content surface (30,30,38) to read, so
    # raise it to a clear mid gray; the in-popup separators/fences then need a
    # darker line (the default border is light, for a dark GUI popup) to show
    # against it.
    popup_bg=(64, 64, 64),
    popup_border=(44, 44, 44),
)


# Fallback palette for widgets drawn without a Panel/theme in reach (the
# control colors are identical across themes, so the defaults suffice).
DEFAULT_THEME = Theme()


def theme_for(capabilities: CapabilityProfile) -> Theme:
    """Default theme for a backend's capabilities."""
    return THEME_GUI if capabilities.supports("hairline") else THEME_TUI
