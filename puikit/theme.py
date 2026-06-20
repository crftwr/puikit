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
    control_bg: Color = (60, 60, 60)       # text field / dropdown face  #3C3C3C
    control_border: Color = (69, 69, 69)   # control outline             #454545
    button_bg: Color = (14, 99, 156)       # primary button face         #0E639C
    button_hover_bg: Color = (17, 119, 187)  # button hover              #1177BB
    button_text: Color = (255, 255, 255)
    # Secondary (no-accent) button face: a neutral fill for a non-primary
    # action, so a screen with two buttons reads one as the prominent choice.
    button_secondary_bg: Color = (58, 61, 65)         # secondary face   #3A3D41
    button_secondary_hover_bg: Color = (69, 73, 78)   # secondary hover  #45494E
    selection_bg: Color = (9, 71, 113)     # active selection            #094771
    # Text-field selection, split by focus: a clearly legible blue while the
    # field is focused, a muted neutral when focus is elsewhere (the editor
    # selection / inactive-selection pair from VS Code).
    text_selection_bg: Color = (38, 79, 120)          # focused  #264F78
    text_selection_inactive_bg: Color = (58, 61, 65)  # blurred  #3A3D41
    hover_bg: Color = (42, 45, 46)         # row hover                   #2A2D2E
    popup_bg: Color = (37, 37, 38)         # menu / popup surface        #252526
    popup_border: Color = (84, 84, 92)     # menu / popup frame line     #54545C

    def surface_bg(self, role: str) -> Color | None:
        return self.surfaces.get(role)


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
)


# Fallback palette for widgets drawn without a Panel/theme in reach (the
# control colors are identical across themes, so the defaults suffice).
DEFAULT_THEME = Theme()


def theme_for(capabilities: CapabilityProfile) -> Theme:
    """Default theme for a backend's capabilities."""
    return THEME_GUI if capabilities.supports("hairline") else THEME_TUI
