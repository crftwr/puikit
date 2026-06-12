"""PuiKit — a capability-based Python UI framework for TUI and GUI."""

from .backend import (
    Backend,
    CapabilityNotSupported,
    DEFAULT_STYLE,
    Style,
    TextAttribute,
)
from .capability import (
    CapabilityProfile,
    PROFILE_GAME,
    PROFILE_GUI_DESKTOP,
    PROFILE_GUI_WEB,
    PROFILE_MOBILE,
    PROFILE_TUI,
)
from .event import Event, EventType
from .layout import HSplit, Item, VSplit
from .panel import DrawContext, Panel, Rect
from .theme import THEME_GUI, THEME_TUI, Theme, theme_for

__version__ = "0.1.0"

__all__ = [
    "Backend",
    "CapabilityNotSupported",
    "CapabilityProfile",
    "DEFAULT_STYLE",
    "DrawContext",
    "Event",
    "EventType",
    "HSplit",
    "Item",
    "Panel",
    "PROFILE_GAME",
    "PROFILE_GUI_DESKTOP",
    "PROFILE_GUI_WEB",
    "PROFILE_MOBILE",
    "PROFILE_TUI",
    "Rect",
    "Style",
    "TextAttribute",
    "THEME_GUI",
    "THEME_TUI",
    "Theme",
    "theme_for",
    "VSplit",
]
