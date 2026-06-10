"""Backend interface definition.

A Backend turns drawing intents into actual output and native input into
Event objects. Core primitives must be implemented by every backend;
extended primitives are optional and the Panel layer provides fallbacks
based on the backend's CapabilityProfile.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from enum import IntFlag
from typing import Any

from .capability import CapabilityProfile
from .event import Event


class TextAttribute(IntFlag):
    NORMAL = 0
    BOLD = 1
    UNDERLINE = 2
    REVERSE = 4
    DIM = 8
    BLINK = 16
    ITALIC = 32


Color = tuple[int, int, int]  # RGB, 0-255 per channel


@dataclass(frozen=True)
class Style:
    fg: Color | None = None
    bg: Color | None = None
    attr: TextAttribute = TextAttribute.NORMAL


DEFAULT_STYLE = Style()

EventHandler = Callable[[Event], None]


class CapabilityNotSupported(Exception):
    """Raised when an extended primitive is called on a backend without it."""


class Backend(ABC):
    """Abstract base class for all PuiKit backends."""

    PROFILE: CapabilityProfile = CapabilityProfile()

    @property
    def capabilities(self) -> CapabilityProfile:
        return self.PROFILE

    # --- lifecycle ---------------------------------------------------------

    @abstractmethod
    def open(self) -> None:
        """Initialize the backend (window, terminal mode, ...)."""

    @abstractmethod
    def close(self) -> None:
        """Tear down the backend and restore the environment."""

    def __enter__(self) -> "Backend":
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- geometry ----------------------------------------------------------

    @property
    @abstractmethod
    def size(self) -> tuple[int, int]:
        """Drawable area in cell coordinates (width, height)."""

    @property
    def cell_size(self) -> tuple[int, int]:
        """Size of one cell in pixels. (1, 1) for TUI backends."""
        return (1, 1)

    # --- core drawing primitives (all backends implement) -------------------

    @abstractmethod
    def clear(self) -> None: ...

    @abstractmethod
    def draw_text(self, x: int, y: int, text: str, style: Style = DEFAULT_STYLE) -> None: ...

    @abstractmethod
    def draw_box(self, x: int, y: int, w: int, h: int, style: Style = DEFAULT_STYLE) -> None: ...

    @abstractmethod
    def draw_scrollbar(
        self, x: int, y: int, h: int, pos: float, ratio: float, style: Style = DEFAULT_STYLE
    ) -> None:
        """Vertical scrollbar. ``pos`` (0..1) is the thumb position,
        ``ratio`` (0..1) is the visible fraction of the content."""

    @abstractmethod
    def present(self) -> None:
        """Flush pending drawing to the screen."""

    # --- extended drawing primitives (GUI only; Panel handles fallback) -----

    def draw_icon(self, x: int, y: int, icon_name: str, style: Style = DEFAULT_STYLE) -> None:
        raise CapabilityNotSupported("icons")

    def draw_image(self, x: int, y: int, path: str, hints: dict[str, Any] | None = None) -> None:
        raise CapabilityNotSupported("images")

    # --- event loop ---------------------------------------------------------

    @abstractmethod
    def run_event_loop(self, handler: EventHandler) -> None:
        """Run until quit() is called, delivering events to ``handler``."""

    @abstractmethod
    def run_event_loop_iteration(self, handler: EventHandler, timeout_ms: int = 0) -> bool:
        """Process at most one pending event. Returns False once quit() was
        requested, True otherwise."""

    @abstractmethod
    def quit(self) -> None:
        """Request the event loop to stop after the current iteration."""
