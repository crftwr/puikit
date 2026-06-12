"""Semantic surface roles mapped to concrete colors per backend.

Apps tag panes with a surface role (hints={"surface": "status"}) instead of
hardcoding colors, so each backend can apply its own region-separation
strategy:

- hairline-capable backends (GUI) may give adjacent roles the same
  background and rely on 1-device-pixel divider lines (zero cell cost)
- cell-grid backends (TUI) cannot afford a full row/column for a line, so
  the theme guarantees adjacent roles contrasting backgrounds instead

An explicit "bg" hint always overrides the theme; the app then owns the
separation quality on cell-grid backends.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .backend import Color
from .capability import CapabilityProfile


@dataclass(frozen=True)
class Theme:
    """Maps surface roles to pane background colors, plus the divider color
    used for layout dividers (hairlines on GUI, line cells on TUI)."""

    surfaces: dict[str, Color] = field(default_factory=dict)
    divider_color: Color = (110, 110, 124)

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

# TUI: background contrast does the separating; no cells spent on lines.
THEME_TUI = Theme(
    surfaces={
        "content": (30, 30, 38),
        "sidebar": (42, 42, 54),
        "header": (56, 56, 72),
        "status": (72, 72, 92),
    },
    divider_color=(128, 128, 144),
)


def theme_for(capabilities: CapabilityProfile) -> Theme:
    """Default theme for a backend's capabilities."""
    return THEME_GUI if capabilities.supports("hairline") else THEME_TUI
