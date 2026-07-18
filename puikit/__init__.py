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
from .easing import EASINGS, Easing, resolve as resolve_easing
from .event import Event, EventType
from .font import Font, FontSlant, FontWeight
from .background import Shader, Wallpaper
from .layout import HSplit, Item, VSplit
from .menu import SEPARATOR, Menu, MenuItem, MenuSeparator
from .panel import DrawContext, Panel, Rect
from .posteffect import CRT, PRESETS, PostEffect
from .theme import THEME_GUI, THEME_TUI, Theme, derive_theme, lift, mix, theme_for

__version__ = "0.1.0"

__all__ = [
    "Backend",
    "Shader",
    "Wallpaper",
    "CapabilityNotSupported",
    "CapabilityProfile",
    "DEFAULT_STYLE",
    "DrawContext",
    "EASINGS",
    "Easing",
    "Event",
    "EventType",
    "Font",
    "FontSlant",
    "FontWeight",
    "HSplit",
    "Item",
    "Menu",
    "MenuItem",
    "MenuSeparator",
    "Panel",
    "PostEffect",
    "CRT",
    "PRESETS",
    "PROFILE_GAME",
    "PROFILE_GUI_DESKTOP",
    "PROFILE_GUI_WEB",
    "PROFILE_MOBILE",
    "PROFILE_TUI",
    "Rect",
    "SEPARATOR",
    "Style",
    "TextAttribute",
    "THEME_GUI",
    "THEME_TUI",
    "Theme",
    "derive_theme",
    "lift",
    "mix",
    "resolve_easing",
    "theme_for",
    "VSplit",
]
