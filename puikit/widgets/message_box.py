"""A modal message box (alert / confirm), shown as a Panel layer.

``show_message_box`` pushes a ``MessageBox`` as a layer with the same
shadow + dim_below intent the demo dialogs use, so GUI raises it with a real
drop shadow over a dimmed page and TUI falls back to draw order — one modal,
every backend. The box shows an icon, a title, a (multi-line) message, and a
row of buttons; left/right or tab move between buttons, enter/space activates
the focused one, escape picks the cancel button, and a click activates a
button directly. It pops itself and reports the chosen label through
``on_result``.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from typing import Any

from ..backend import DEFAULT_STYLE, Style, TextAttribute
from ..event import Event, EventType
from ..font import Font
from ..layout import LayoutContext
from ..panel import DrawContext
from .button import Button
from .markdown_view import MarkdownView

# Horizontal gap between adjacent buttons in the row, in base units.
_BUTTON_GAP = 1.0


class MessageBox:
    """Modal layer content for an alert/confirm dialog. Construct via
    ``show_message_box`` rather than directly; it sizes and pushes the layer."""

    def __init__(
        self,
        message: str,
        title: str = "",
        buttons: Sequence[str] = ("OK",),
        icon: str = "info",
        default: int = 0,
        cancel: int | None = None,
        on_result: Callable[[str], None] | None = None,
        markdown: bool = False,
    ):
        self.message = message
        self.title = title
        self.buttons = list(buttons)
        self.icon = icon
        # When set, the message is rendered as Markdown (via a MarkdownView) so a
        # caller can style parts of it — a `code` span for a filename or path,
        # **bold**, *italic*, a link — instead of one flat run. Built lazily in
        # draw() once the theme is known, so its spans sit on the popup surface.
        self.markdown = markdown
        self._md: MarkdownView | None = None
        # The default button (initially focused, drawn as the prominent
        # "primary" action) and the one escape chooses (defaults to the last
        # button, the conventional Cancel slot).
        self.default = max(0, min(default, len(self.buttons) - 1))
        self.focused = self.default
        self.cancel = cancel if cancel is not None else len(self.buttons) - 1
        self.on_result = on_result
        self._panel: Any = None
        # The row of buttons rendered as real Button widgets, so the message box
        # shares the regular button's flat fill, focus ring, and hover/pressed
        # cues on every backend instead of hand-painting its own. The default
        # button wears the accent "primary" fill; the rest are neutral
        # "secondary" actions, exactly as a screen with two buttons reads.
        self._widgets = [
            Button(
                label,
                variant="primary" if i == self.default else "secondary",
                on_click=(lambda i=i: self._close(i)),
            )
            for i, label in enumerate(self.buttons)
        ]
        # Absolute-to-local button rects (x0, x1, y0, y1) captured during draw,
        # so a click can be routed to the button it landed on.
        self._button_x: list[tuple[float, float, float, float]] = []

    # --- drawing -------------------------------------------------------------

    def _lines(self) -> list[str]:
        return self.message.split("\n")

    def _row_metrics(self, lc: LayoutContext) -> tuple[list[float], float]:
        """Per-button widths and the shared row height, each button measuring
        itself exactly as the layout would size a content button — so the box
        reserves the same widths the buttons draw at."""
        if not self._widgets:
            return [], 0.0
        height = self._widgets[0].measure(lc, "y", 0.0).preferred
        widths = [w.measure(lc, "x", height).preferred for w in self._widgets]
        return widths, height

    def draw(self, ctx: DrawContext) -> None:
        self._panel = ctx.panel
        # The dialog surface follows the theme's popup role, not the backend's
        # hardcoded default fill — otherwise a light theme would draw a dark box
        # (and, with text defaulting to the theme's dark foreground, invisible
        # text). Frame it in the popup border color the same way.
        theme = ctx.theme
        box_style = (
            Style(bg=theme.popup_bg, fg=theme.popup_border) if theme is not None else DEFAULT_STYLE
        )
        ctx.draw_box(0, 0, ctx.width, ctx.height, box_style, hints={"fill": True})
        # The icon, title, and message must sit on the dialog *surface*, so pin
        # their background to the box fill (popup_bg). A bg-less run instead
        # inherits the layer's default background — the backend's dark fill —
        # which paints a dark band behind the text and a dark square behind the
        # icon, invisible on a light theme's white dialog. The foreground falls
        # through to the theme text color (resolved by the Panel's text seam).
        surface_bg = theme.popup_bg if theme is not None else None
        title_style = Style(bg=surface_bg, attr=TextAttribute.BOLD)
        msg_style = Style(bg=surface_bg)
        if self.icon:
            ctx.draw_icon(2, 1, self.icon, msg_style)
        if self.title:
            ctx.draw_text(5, 1, self.title, title_style)

        # Button row along the bottom, centered as a group and drawn as real
        # Button widgets via draw_child — so each carries the flat fill, focus
        # ring, and hover/pressed cues of the regular button. The focused button
        # is marked through the "focused" hint (the layer holds the focus, so the
        # ring lights on exactly one button), and moving focus only moves the
        # ring; the accent "primary" fill stays on the default button. Laid out
        # first so the message region knows where the buttons begin.
        lc = ctx.layout_context()
        widths, bh = self._row_metrics(lc)
        total = sum(widths) + _BUTTON_GAP * (len(widths) - 1)
        wu, hu = ctx.size_units
        bx = max(2.0, (wu - total) / 2.0)
        by = max(0.0, hu - bh - 1.0)
        if lc.snap:
            # Whole-unit backends round each draw coordinate independently, so a
            # half-unit row origin (an odd-width box centering an even-width row)
            # would desync the button's bracket focus cue from its centered label
            # — the closing "]" lands one column short. Snap the origin to the
            # base unit grid so brackets and label round in step.
            bx, by = round(bx), round(by)

        if self.markdown:
            # Rich message: render through a MarkdownView so inline `code`,
            # **bold**, *italic*, and links style parts of the text. Its spans
            # must sit on the popup surface, so pin the view's base background to
            # popup_bg (re-parse only when it actually changes — e.g. a theme
            # toggle — so a static dialog re-lays out once). The region spans from
            # the message row (y=3) down to the button row; the view top-aligns
            # and virtualizes, so extra space is just blank surface.
            md_style = Style(bg=surface_bg)
            if self._md is None:
                self._md = MarkdownView(self.message, style=md_style)
            elif self._md.style != md_style:
                self._md.style = md_style
                self._md.set_source(self.message)
            ctx.draw_child(self._md, 2, 3, max(1.0, wu - 4.0), max(1.0, by - 3.0))
        else:
            for i, line in enumerate(self._lines()):
                # Draw the whole line and let draw_text clip it: it truncates
                # per-font (columns for monospace, pixel clip rect for the
                # proportional GUI font). Slicing by ``ctx.width`` characters here
                # would instead chop proportional text — the box is sized in base
                # units, but a proportional glyph is narrower than a base unit, so
                # a line has more characters than the box has units and the tail
                # gets cut even though it fits.
                ctx.draw_text(2, 3 + i, line, msg_style)

        self._button_x = []
        for i, (widget, w) in enumerate(zip(self._widgets, widths)):
            ctx.draw_child(widget, bx, by, w, bh, hints={"focused": i == self.focused})
            self._button_x.append((bx, bx + w, by, by + bh))
            bx += w + _BUTTON_GAP

    # --- events --------------------------------------------------------------

    def _close(self, index: int) -> None:
        if self._panel is not None:
            self._panel.pop_layer()
        if self.on_result is not None and 0 <= index < len(self.buttons):
            self.on_result(self.buttons[index])

    def handle_event(self, event: Event) -> bool:
        if event.type is EventType.KEY:
            n = len(self._widgets)
            if event.key in ("left", "up"):
                self.focused = (self.focused - 1) % n
            elif event.key in ("right", "down", "tab"):
                self.focused = (self.focused + 1) % n
            elif event.key == "escape":
                self._close(self.cancel)
            else:
                # enter / space activate the focused button — delegated so the
                # same on_click path a mouse takes fires (one activation seam).
                self._widgets[self.focused].handle_event(event)
            return True
        if event.type is EventType.MOUSE_CLICK and event.x is not None:
            ey = event.y or 0
            for i, (x0, x1, y0, y1) in enumerate(self._button_x):
                if x0 <= event.x < x1 and y0 <= ey < y1:
                    self._widgets[i].handle_event(event)
                    return True
        return True  # modal: swallow everything else


def show_message_box(
    panel: Any,
    message: str,
    title: str = "",
    buttons: Sequence[str] = ("OK",),
    icon: str = "info",
    default: int = 0,
    cancel: int | None = None,
    on_result: Callable[[str], None] | None = None,
    z: int = 70,
    dim_below: bool = False,
    markdown: bool = False,
) -> MessageBox:
    """Push a modal MessageBox layer over ``panel`` and return it.

    The box is sized to its title, message, and buttons; GUI renders a drop
    shadow over the page, TUI falls back to draw order. By default the page is
    not dimmed (drop shadow only); pass ``dim_below=True`` to dim it. The chosen
    button label is reported through ``on_result``.

    Pass ``markdown=True`` to render the message as Markdown (via a MarkdownView)
    so a caller can style parts of it — an inline ``code`` span for a filename or
    path, ``**bold**``, ``*italic*``, ``[links](url)``. Separate visual lines with
    a blank line (a paragraph break), since consecutive non-blank lines fold into
    one wrapped paragraph."""
    box = MessageBox(
        message, title=title, buttons=buttons, icon=icon,
        default=default, cancel=cancel, on_result=on_result, markdown=markdown,
    )
    # Size the box to the text as it will actually render: measure through the
    # backend with the proportional UI font (the GUI default) — the title bold,
    # since it draws bold — so a proportional message is not boxed at the wider
    # monospace column count. A whole-unit backend returns column counts, so the
    # terminal box is unchanged.
    mt = panel.backend.measure_text
    prop = Style(font=Font())
    prop_bold = Style(font=Font(), attr=TextAttribute.BOLD)
    if markdown:
        # Parse once; measure each semantic line's *unwrapped* width — a `code`
        # run in the monospace face it draws in, so a code chip is not under-boxed
        # against the narrower proportional font. Spans are reused below to count
        # wrapped rows at the final width.
        from .markdown_view import DEFAULT_CODE_FONT, DEFAULT_TEXT_FONT, _wrap_spans, parse_markdown

        sems = parse_markdown(message)

        def _sem_spans(sem: Any) -> list:
            return [
                (text, Style(font=DEFAULT_CODE_FONT if "code" in roles else DEFAULT_TEXT_FONT), None)
                for text, roles, _ in sem.runs
            ]

        def _sem_prefix_w(sem: Any) -> float:
            return mt(sem.prefix, prop) if sem.prefix else 0.0

        msg_w = max(
            (_sem_prefix_w(s) + sum(mt(t, st) for t, st, _ in _sem_spans(s)) for s in sems),
            default=0.0,
        )
    else:
        lines = message.split("\n")
        msg_w = max((mt(line, prop) for line in lines), default=0.0)
        n_rows = len(lines)
    # Reserve the buttons' own measured widths (the same intrinsic sizing the
    # layout uses for a content button), so the box is exactly wide enough for
    # the row it draws.
    caps = panel.backend.capabilities
    lc = LayoutContext(
        *panel.backend.base_size,
        snap=not caps.supports("pixel_layout"),
        measure=panel.backend.measure_text,
    )
    btn_widths, _ = box._row_metrics(lc)
    label_w = sum(btn_widths) + _BUTTON_GAP * (len(btn_widths) - 1)
    content_w = max(
        mt(title, prop_bold) + 5,
        msg_w + 4,
        label_w + 4,
    )
    w = max(28, min(math.ceil(content_w) + 4, panel.backend.size_units[0]))
    if markdown:
        # Count the *wrapped* display rows the MarkdownView will actually draw at
        # the final content width (w - 4, matching draw()'s region), using the
        # same wrap the widget uses — so a long line that wraps to several rows
        # (a path wider than the box) grows the box instead of being clipped or
        # scrolled inside a one-row region.
        avail = max(1.0, float(w) - 4.0)
        n_rows = max(
            1,
            sum(
                len(_wrap_spans(_sem_spans(s), max(1.0, avail - _sem_prefix_w(s)), mt, word=s.wrap))
                for s in sems
            ),
        )
    # title row at y=1, message from y=3, then the button row (bottom-anchored,
    # its own height, one unit above the border) and the bottom border at h-1.
    # The message is top-anchored and the buttons bottom-anchored, so the extra
    # rows added here fall between them as breathing room above the buttons.
    h = n_rows + 7
    box._panel = panel
    panel.push_layer(
        box, z=z, hints={"shadow": True, "dim_below": dim_below, "w": float(w), "h": float(h)}
    )
    panel.animate(box, hints={"transition": "fade", "duration_ms": 150})
    return box
