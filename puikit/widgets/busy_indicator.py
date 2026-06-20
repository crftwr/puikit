"""An indeterminate busy / activity indicator (a spinner).

Where ``ProgressBar`` shows *how far*, a BusyIndicator shows only *that work is
happening* — the indeterminate case. It is the framework's clean test of the
``animation`` capability and its fallback:

- on ``animation`` backends (GUI) it registers a per-frame tick through the
  Panel and drives its own re-renders, so the spinner turns on its own;
- on a still backend (TUI) no tick is registered — the frame is derived from
  the wall clock, so the spinner advances whenever the app re-renders for any
  other reason (a key, a resize) and is otherwise a static glyph.

Either way the widget draws one frame per ``draw`` and never branches on the
backend; the capability is resolved in the Panel layer (``ctx.animated`` /
``panel.request_animation_ticks``).
"""

from __future__ import annotations

import time

from ..backend import DEFAULT_STYLE, Style
from ..layout import LayoutContext, SizeRequest
from ..panel import DrawContext
from ..text import display_width
from ..theme import DEFAULT_THEME
from .base import Widget

# Braille spinner frames — single-width on every backend, so the glyph stays
# column-aligned on a character grid the same as on a vector backend.
_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
# Gap (in cells) between the spinner glyph and an optional label.
_GAP = 1


class BusyIndicator(Widget):
    def __init__(
        self,
        label: str = "",
        running: bool = True,
        fps: float = 12.0,
        style: Style = DEFAULT_STYLE,
    ):
        self.label = label
        self.running = running
        self.fps = fps
        self.style = style
        self._panel = None
        self._ticking = False
        # Set on every draw, cleared on every tick: a liveness flag. If a tick
        # fires without an intervening draw, this widget has left the layout
        # (e.g. its page was swapped out), so the tick unregisters itself rather
        # than pinning the detached widget alive and re-rendering forever.
        self._drawn = False

    # --- control -------------------------------------------------------------

    def start(self) -> None:
        # On the next draw a capable backend re-registers the tick; on a still
        # backend this just resumes advancing the wall-clock frame.
        self.running = True

    def stop(self) -> None:
        self.running = False

    # --- drawing -------------------------------------------------------------

    def _frame(self) -> str:
        if not self.running:
            return _FRAMES[0]
        return _FRAMES[int(time.monotonic() * self.fps) % len(_FRAMES)]

    def draw(self, ctx: DrawContext) -> None:
        self._panel = ctx.panel
        self._drawn = True
        theme = ctx.theme or DEFAULT_THEME
        fg = self.style.fg or theme.accent
        ctx.draw_text(0, 0, self._frame(), Style(fg=fg, bg=self.style.bg))
        if self.label:
            ctx.draw_text(
                display_width(_FRAMES[0]) + _GAP, 0, self.label,
                Style(fg=theme.text, bg=self.style.bg),
            )
        # Drive our own re-renders on a capable backend, exactly once: the guard
        # keeps each render from stacking another tick. On a still backend the
        # Panel declines to register and the spinner is simply static.
        if (
            self.running
            and not self._ticking
            and ctx.animated
            and ctx.panel is not None
        ):
            self._ticking = ctx.panel.request_animation_ticks(self._tick)

    def _tick(self) -> bool:
        # Unregister if stopped, detached, or no longer being drawn (its page
        # was swapped out). A later draw flips _ticking back on and re-registers.
        if not self.running or self._panel is None or not self._drawn:
            self._ticking = False
            return False
        self._drawn = False
        self._panel.render()
        return True

    def measure(self, ctx: LayoutContext, axis: str, available: float) -> SizeRequest:
        if axis == "y":
            return SizeRequest(min=1.0, preferred=1.0, max=1.0)
        w = float(display_width(_FRAMES[0]))
        if self.label:
            w += _GAP + ctx.measure_text(self.label, self.style)
        return SizeRequest(min=w, preferred=w, max=w)
