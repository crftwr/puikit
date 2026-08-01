"""PuiKit — a capability-based Python UI framework for TUI and GUI."""

from .backend import (
    Backend,
    CapabilityNotSupported,
    DEFAULT_STYLE,
    Style,
    TextAttribute,
    WindowHandle,
    WindowStyle,
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

#: Single source of truth for the version string -- the ONLY place the literal
#: appears in this repo. Bumping the version means editing this line and nothing
#: else: pyproject.toml derives it via its dynamic ``version``
#: (``attr = "puikit.__version__"``), so packaging metadata and ``puikit
#: .__version__`` can no longer disagree. (They did once: 1.0.2 was released
#: with this line left at 1.0.1.)
__version__ = "1.0.7"
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
    "WindowHandle",
    "WindowStyle",
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
