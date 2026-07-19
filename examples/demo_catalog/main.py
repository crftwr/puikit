"""PuiKit widget catalog with a left navigation pane.

The pages are listed in the navigation on the left; moving the selection
(up/down or mouse) switches the content pane on the right. The whole shell
*and every page* are built with the layout system — no page hand-places a
widget at a coordinate. Each `build_*_page` returns a `Split`, hosted in a
single `LayoutView` whose layout is swapped per page; the host's margin gives
every page symmetric padding (declared, not positioned). Widgets that need
free-form internals (the animation card) stay `Container`s as *leaves*, placed
by the layout. The layout re-resolves on resize, snapped to base units on TUI and
to device pixels on GUI, with surface roles and dividers.

Keys: up/down in the nav switch pages, tab / shift+tab walk focus through the
nav and the page's widgets (one tree, wrapping at the ends), 1..9 jump to a
page, d opens a layered dialog, q quits.

    python examples/demo_catalog/main.py                          # TUI
    python examples/demo_catalog/main.py --backend gui            # macOS GUI
    python examples/demo_catalog/main.py --backend gui --font-size 18
"""

import argparse
import colorsys
import math
import os
from dataclasses import replace

from puikit import (
    SEPARATOR,
    EventType,
    Font,
    FontSlant,
    FontWeight,
    HSplit,
    Item,
    Menu,
    MenuItem,
    Panel,
    Style,
    TextAttribute,
    Theme,
    VSplit,
    derive_theme,
)
from puikit.backends import create_backend
from puikit.layout import SizeRequest
from puikit.text import elide
from puikit.widgets import (
    BusyIndicator,
    Button,
    Checkbox,
    ComboBox,
    Container,
    DropDown,
    ImageView,
    Label,
    LayoutView,
    ListView,
    LogView,
    MarkdownView,
    MenuBar,
    ProgressBar,
    RadioGroup,
    ScrollBar,
    ScrollView,
    Splitter,
    Tabs,
    TextBlock,
    TextEdit,
    TreeNode,
    TreeView,
    Widget,
    show_drawer,
    show_message_box,
)

DIM = Style(attr=TextAttribute.DIM)
BOLD = Style(attr=TextAttribute.BOLD)

# Pane background colors, following the VS Code Dark+ palette: a dark title
# bar, a slightly lighter sidebar, the near-black editor body, and an accent
# blue status bar. GUI renders the exact RGB; TUI approximates via xterm-256.
TITLE_BG = (60, 60, 60)      # title bar      #3C3C3C
NAV_BG = (37, 37, 38)        # sidebar        #252526
CONTENT_BG = (30, 30, 30)    # editor body    #1E1E1E
STATUS_BG = (0, 122, 204)    # status bar      #007ACC (accent)
STATUS_FG = Style(fg=(255, 255, 255), bg=STATUS_BG)
# Button face: a lighter fill so buttons read as raised against a footer bar.
BUTTON_FACE = Style(fg=(232, 234, 240), bg=(74, 88, 124))


# --- theme palettes --------------------------------------------------------------
#
# Theme switching is pure intent: the shell tags its panes with semantic surface
# roles (header / sidebar / content / status) instead of hardcoded colors, and
# every widget reads its accent / selection / control colors from `panel.theme`
# at draw time. So cycling the active `Theme` (the `t` key) recolors the whole
# catalog — chrome and page widgets alike — with no per-widget repaint logic and
# no backend branch; the surface backgrounds re-resolve from the new theme on the
# next render. Each palette keeps light text on dark surfaces so every backend
# (TUI included, where colors snap to xterm-256) stays legible.
#
# The `status` surface is the accent color so the footer bar reads as the
# theme's signature hue; the status/title label foregrounds are refreshed in
# `apply_theme` because a Label carries a fixed Style (its own glyph background).


def _luminance(color: tuple[int, int, int]) -> float:
    r, g, b = color
    return 0.299 * r + 0.587 * g + 0.114 * b


def _on_accent_fg(accent: tuple[int, int, int]) -> tuple[int, int, int]:
    """Black or white, whichever reads on the accent (used for the status bar)."""
    return (28, 28, 28) if _luminance(accent) > 150 else (255, 255, 255)


def _readable_fg(bg: tuple[int, int, int]) -> tuple[int, int, int]:
    """A near-black or near-white foreground that reads on ``bg``. Demo widgets
    that paint a custom fill (the layout Regions over a vivid block or a themed
    surface) use this so their text stays legible whether the theme is light or
    dark — instead of assuming a dark background the way a fixed light color
    would."""
    return (28, 28, 28) if _luminance(bg) > 140 else (235, 235, 235)


def _contrast(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    """Rough luminance-contrast ratio between two colors (>=1). Used to decide
    whether a region's signature hue is legible on its background or should fall
    back to a plain readable foreground."""
    la, lb = _luminance(a), _luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 12.5) / (lo + 12.5)


# Each theme is six base colors; derive_theme computes the ~24-color palette
# (hovers, borders, inactive selections, dividers, secondary buttons) from them
# by lighten/darken/blend rules (see puikit.theme.derive_theme). A theme reads
# as the handful of decisions that actually differ between palettes; the derived
# fields keep the contrast relationships consistent (a distinct sidebar, an
# input face lifted off the page, a selection that leans on the accent). Pass a
# concrete Theme field as a keyword to any call to pin an exception.
#
#   background — content surface (its luminance also picks the lift direction)
#   foreground — primary text
#   muted      — secondary text / dividers
#   accent     — focus rings, primary button, status bar
#   surface    — raised panels (sidebar / header / popup / inputs derive from it)
#   selection  — active list / text selection fill
DEMO_THEMES: list[tuple[str, Theme]] = [
    (
        "Dark+",
        derive_theme(
            background=(30, 30, 30),
            foreground=(212, 212, 212),
            muted=(157, 157, 157),
            accent=(0, 122, 204),
            surface=(48, 48, 52),
            selection=(10, 105, 178),
        ),
    ),
    (
        "Monokai",
        derive_theme(
            background=(39, 40, 34),
            foreground=(248, 248, 242),
            muted=(140, 140, 130),
            accent=(166, 226, 46),
            surface=(56, 57, 48),
            selection=(86, 122, 38),
        ),
    ),
    (
        "Dracula",
        derive_theme(
            background=(40, 42, 54),
            foreground=(248, 248, 242),
            muted=(98, 114, 164),
            accent=(189, 147, 249),
            surface=(56, 59, 76),
            selection=(120, 86, 175),
        ),
    ),
    (
        "Solarized",
        derive_theme(
            background=(0, 43, 54),
            foreground=(147, 161, 161),
            muted=(88, 110, 117),
            accent=(38, 139, 210),
            surface=(10, 62, 78),
            selection=(26, 102, 150),
        ),
    ),
    (
        "Nord",
        derive_theme(
            background=(46, 52, 64),
            foreground=(216, 222, 233),
            muted=(76, 86, 106),
            accent=(136, 192, 208),
            surface=(62, 70, 88),
            selection=(76, 128, 158),
        ),
    ),
    # --- light variants -------------------------------------------------------
    # A light background flips the derivation: panels and inputs sink (darken)
    # instead of lifting, and `text` defaults a bg-less run dark (so the nav and
    # plain labels read on the light surface). Same six bases, opposite polarity.
    (
        "Light+",
        derive_theme(
            background=(255, 255, 255),
            foreground=(30, 30, 30),
            muted=(110, 110, 110),
            accent=(0, 122, 204),
            surface=(235, 235, 238),
            selection=(120, 180, 240),
        ),
    ),
    (
        "Solarized Light",
        derive_theme(
            background=(253, 246, 227),
            foreground=(88, 110, 117),
            muted=(147, 161, 161),
            accent=(38, 139, 210),
            surface=(234, 228, 206),
            selection=(150, 195, 230),
        ),
    ),
]


def _popup_box_style(ctx) -> Style:
    """Fill + frame style for an overlay box, taken from the theme's popup role.
    Without this the box falls back to the backend's hardcoded dark default
    fill, which (with text now defaulting to the theme's foreground) is
    invisible dark-on-dark under a light theme."""
    theme = ctx.theme
    if theme is None:
        return Style()
    return Style(bg=theme.popup_bg, fg=theme.popup_border)


def _popup_text_style(ctx, attr: TextAttribute = TextAttribute.NORMAL) -> Style:
    """Background-pinned text style for content drawn on a popup/dialog surface.
    A bg-less run on a pushed layer inherits the layer's default (dark)
    background — the backend's hardcoded fill — which paints a dark band behind
    the text and icon, invisible on a light theme's light dialog. Pin the
    background to the popup fill so the text sits on the dialog surface; the
    foreground still falls through to the theme's text color."""
    theme = ctx.theme
    bg = theme.popup_bg if theme is not None else None
    return Style(bg=bg, attr=attr)


class DemoDialog(Widget):
    """A modal dialog layer. Pushed with shadow/dim_below hints: GUI backends
    render a drop shadow and a translucent dim overlay, TUI approximates the
    dim with dark attributes and skips the shadow."""

    def __init__(self, on_close):
        self.on_close = on_close

    def draw(self, ctx):
        ctx.draw_box(0, 0, ctx.width, ctx.height, _popup_box_style(ctx), hints={"fill": True})
        ctx.draw_icon(2, 1, "info", _popup_text_style(ctx))
        ctx.draw_text(5, 1, "A layered dialog", _popup_text_style(ctx, TextAttribute.BOLD))
        ctx.draw_text(2, 3, "The content below is dimmed.", _popup_text_style(ctx))
        ctx.draw_text(2, ctx.height - 2, "esc / enter: close", _popup_text_style(ctx, TextAttribute.DIM))

    def handle_event(self, event):
        if event.type is EventType.KEY and event.key in ("escape", "enter"):
            self.on_close()
        return True  # modal: swallow everything


class LayerCard(Widget):
    """A demo overlay layer for the Layering page. Each card names the hints it
    was pushed with and closes itself on esc/enter. It is the *same* push_layer
    intent as DemoDialog: GUI composites a real layer (translucent dim, drop
    shadow), TUI falls back to draw order, approximates dim with attributes, and
    skips the shadow — one intent, two fidelities. The card never branches on
    the backend; the Panel layer resolves the capability."""

    def __init__(self, title, notes, on_close, on_open=None):
        self.title = title
        self.notes = notes
        self.on_close = on_close
        # When set, the card can spawn a *new* layer on top of itself: a layer
        # opened from within a previous layer, stacked by z-order.
        self.on_open = on_open

    def draw(self, ctx):
        ctx.draw_box(0, 0, ctx.width, ctx.height, _popup_box_style(ctx), hints={"fill": True})
        # Content sits at x=2; keep every line inside the right border (at
        # column width-1) with a one-column pad, so text never overwrites the
        # frame. The Panel clips to the full rect — the border included — so
        # the inset is the card's own responsibility.
        inner = max(0, ctx.width - 2 - 2)

        def line(x, y, text, style=None):
            avail = max(0, inner - (x - 2))
            ctx.draw_text(x, y, text[:avail], style) if style else ctx.draw_text(x, y, text[:avail])

        ctx.draw_icon(2, 1, "info", _popup_text_style(ctx))
        line(5, 1, self.title, _popup_text_style(ctx, TextAttribute.BOLD))
        for i, note in enumerate(self.notes):
            line(2, 3 + i, note, _popup_text_style(ctx))
        hint = "esc / enter: close top layer"
        if self.on_open is not None:
            hint = "o: open another, " + hint
        line(2, ctx.height - 2, hint, _popup_text_style(ctx, TextAttribute.DIM))

    def handle_event(self, event):
        if event.type is EventType.KEY:
            if event.key in ("escape", "enter"):
                self.on_close()
            elif event.key == "o" and self.on_open is not None:
                self.on_open()
        return True  # modal: the topmost layer swallows everything


# --- pages ---------------------------------------------------------------------


def build_label_page(panel: Panel) -> VSplit:
    # Each page returns a layout (not hand-placed coordinates): the rows stack
    # with a 1-base unit gap, and the trailing weighted item soaks up the slack.
    return VSplit(
        Item(Label("Plain label"), size=1),
        Item(Label("Bold label", BOLD), size=1),
        Item(Label("Reverse label", Style(attr=TextAttribute.REVERSE)), size=1),
        Item(Label("Colored label", Style(fg=(13, 188, 121))), size=1),
        Item(Label(""), weight=1),
        gap=1,
    )


class KeysView(Widget):
    """Live keyboard probe: shows what each keypress delivers under the PuiKit
    keyboard contract (``key`` / ``char`` / ``modifiers``), newest at the
    bottom. The same `Event` is produced on every backend, so a `Shift-A` reads
    as ``key='a'`` + ``{shift}`` whether typed in a terminal or the native
    window. Escape is passed through (returns False) so the shell can still
    quit; every other key is consumed so you can watch q, digits, Shift-letters,
    Ctrl/Cmd chords, and named keys as they arrive."""

    focusable = True

    _MOD_ORDER = ("cmd", "ctrl", "alt", "shift")

    def __init__(self):
        self.history: list[tuple[str, str, str]] = []

    def handle_event(self, event):
        if event.type is not EventType.KEY:
            return False
        if event.key == "escape":
            return False  # let the catalog shell quit
        mods = "+".join(m for m in self._MOD_ORDER if m in event.modifiers) or "—"
        char = "—" if event.char is None else repr(event.char)
        self.history.append((str(event.key), char, mods))
        del self.history[:-256]
        return True

    def draw(self, ctx) -> None:
        theme = ctx.theme
        text_fg = theme.text if theme else None
        muted_fg = theme.muted_text if theme else None
        accent = theme.accent if theme else None

        if ctx.focused:
            ctx.draw_text(0, 0, "● capturing — press any key",
                          Style(fg=accent, attr=TextAttribute.BOLD))
        else:
            ctx.draw_text(0, 0, "○ Tab here to capture keys",
                          Style(fg=text_fg, attr=TextAttribute.BOLD))
        ctx.draw_text(0, 1, "Esc quits · Tab moves focus", Style(fg=muted_fg, attr=TextAttribute.DIM))

        ctx.draw_text(0, 3, f"{'key':<14}{'char':<10}modifiers",
                      Style(fg=text_fg, attr=TextAttribute.BOLD))

        rows = max(0, ctx.height - 4)
        recent = self.history[-rows:] if rows else []
        for i, (key, char, mods) in enumerate(recent):
            newest = i == len(recent) - 1
            style = Style(fg=accent if newest else text_fg,
                          attr=TextAttribute.BOLD if newest else TextAttribute.NORMAL)
            ctx.draw_text(0, 4 + i, f"{key:<14}{char:<10}{mods}", style)


def build_keys_page(panel: Panel) -> VSplit:
    intro = Label(
        "Keyboard contract probe — one Event per keypress, identical on every backend.",
        DIM,
    )
    return VSplit(
        Item(intro, size="content"),
        Item(KeysView(), weight=1),
        gap=1,
    )


class TruncateView(Widget):
    """Live text-fitting probe for `puikit.text`. A width budget you grow and
    shrink with ←/→ (or the wheel), and several sample strings fitted to it three
    ways with `elide`: an **end** ellipsis (keep the start), a **middle**
    ellipsis (keep both ends — the filename/path idiom), and a **start** ellipsis
    (keep the end). A dotted guide marks the budget edge; the fitted lines stop
    at it while the dim full text overflows past it.

    The point over TTK: the samples render in a **proportional** font on GUI and
    are fitted by their real measured width (`elide(..., measure=ctx.measure_text)`),
    not a column count — so the fit follows the actual glyph advances, kerning and
    all. On TUI the proportional font folds to the grid and `measure_text` returns
    columns, so the same code degrades to monospace fitting. Wide CJK and
    emoji-with-selector glyphs are never split."""

    focusable = True

    SAMPLES = (
        "the_quick_brown_fox.tar.gz",
        "/usr/local/share/app/config.yaml",
        "実装メモ_2026年.md",
        "🏷️_tagged_release_v1.txt",
    )
    MODES = ("end", "middle", "start")
    # Proportional on GUI (system face); folds to the grid font on TUI.
    SAMPLE_FONT = Font()

    def __init__(self, width: int = 16):
        self.width = width

    def _resize(self, delta: int) -> None:
        self.width = max(1, min(48, self.width + delta))

    def handle_event(self, event):
        if event.type is EventType.KEY and event.key in ("left", "right"):
            self._resize(1 if event.key == "right" else -1)
            return True
        if event.type is EventType.MOUSE_SCROLL:
            self._resize(1 if event.scroll > 0 else -1)
            return True
        return False

    def draw(self, ctx) -> None:
        theme = ctx.theme
        text_fg = theme.text if theme else None
        muted = theme.muted_text if theme else None
        accent = theme.accent if theme else None

        w = self.width  # in base units (= columns on TUI, the grid cell on GUI)
        tag_x = 0
        text_x = 8
        guide_x = text_x + w

        # Fit by the sample font's real measured width, not a column count.
        full_style = Style(fg=muted, attr=TextAttribute.DIM, font=self.SAMPLE_FONT)
        samp_style = Style(fg=text_fg, font=self.SAMPLE_FONT)

        def measure(s: str) -> float:
            return ctx.measure_text(s, samp_style)

        hint = ("focused — ←/→ or scroll to resize" if ctx.focused
                else "Tab here, then ←/→ to resize")
        ctx.draw_text(0, 0, hint, Style(fg=accent if ctx.focused else text_fg,
                                        attr=TextAttribute.BOLD))
        ctx.draw_text(0, 1, f"budget = {w} base units (proportional fit)",
                      Style(fg=muted, attr=TextAttribute.DIM))

        y = 3
        for sample in self.SAMPLES:
            if y >= ctx.height:
                break
            ctx.draw_text(tag_x, y, "full", Style(fg=muted, attr=TextAttribute.DIM))
            ctx.draw_text(text_x, y, sample, full_style)
            y += 1
            for mode in self.MODES:
                if y >= ctx.height:
                    break
                fitted = elide(sample, w, where=mode, measure=measure)
                ctx.draw_text(tag_x, y, mode, Style(fg=text_fg))
                ctx.draw_text(text_x, y, fitted, samp_style)
                y += 1
            y += 1  # blank line between samples

        # Dotted guide at the budget edge, drawn last so it cuts across the
        # overflowing full lines while the fitted lines stop short of it.
        for gy in range(3, min(ctx.height, y)):
            ctx.draw_text(guide_x, gy, "┊", Style(fg=accent))


def build_truncate_page(panel: Panel) -> VSplit:
    intro = Label(
        "Text fitting — elide() by measured width (proportional on GUI), end/middle/start.",
        DIM,
    )
    return VSplit(
        Item(intro, size="content"),
        Item(TruncateView(), weight=1),
        gap=1,
    )


def build_widgets_page(panel: Panel) -> VSplit:
    # A form of basic interactive widgets, stacked in a ScrollView so the page
    # scrolls when the controls outgrow the pane. Each control reports a state
    # change into the shared status line. Focus moves with tab / shift+tab
    # (the Panel walks the whole focus tree; the ScrollView scrolls the focused
    # child into view); space / enter activates, arrows move within the control.
    status = Label("tab: next field  ·  space/enter: activate  ·  arrows: adjust", DIM)

    def set_status(msg: str) -> None:
        status.text = msg

    feature = Checkbox(
        "Enable feature", checked=True,
        on_change=lambda v: set_status(f"Checkbox 'Enable feature' -> {v}"),
    )
    hidden = Checkbox(
        "Show hidden files",
        on_change=lambda v: set_status(f"Checkbox 'Show hidden files' -> {v}"),
    )
    size = RadioGroup(
        ["Small", "Medium", "Large"], selected=1,
        on_change=lambda i, t: set_status(f"Radio -> {t}"),
    )
    color = DropDown(
        ["Red", "Green", "Blue", "Magenta", "Cyan"],
        on_change=lambda i, t: set_status(f"DropDown -> {t}"),
    )
    name = TextEdit(
        "edit me",
        on_change=lambda s: set_status(f"TextEdit -> {s!r}"),
        on_submit=lambda s: set_status(f"TextEdit submitted -> {s!r}"),
    )
    action = Button("Apply", on_click=lambda: set_status("Button 'Apply' clicked"))
    cancel = Button(
        "Cancel", variant="secondary",
        on_click=lambda: set_status("Button 'Cancel' clicked"),
    )
    # The two variants side by side: each sized to its label, a trailing spacer
    # pushes the pair to the left.
    buttons = LayoutView(HSplit(
        Item(action, size="content"),
        Item(cancel, size="content"),
        Item(Label(""), weight=1),
        gap=2,
    ))
    # The one Button class, two image faces: an image-only tile, and an
    # icon+label action button sized to its content. GUI draws the picture;
    # TUI shows the alt glyph — the page never branches.
    play = os.path.join(ASSETS, "play.png")
    image_tile = LayoutView(HSplit(
        Item(Button(image=play, alt="▶",
                    on_click=lambda: set_status("Image button clicked")), size=8),
        Item(Label(""), weight=1),
    ))
    image_text = LayoutView(HSplit(
        Item(Button("Play", image=play, alt="▶",
                    on_click=lambda: set_status("Image+text button clicked")),
             size="content"),
        Item(Label(""), weight=1),
    ))

    heading = lambda text: Label(text, BOLD)  # noqa: E731 - tiny local helper
    scroller = ScrollView(
        [
            (heading("Check boxes"), 1),
            (feature, "content"),
            (hidden, "content"),
            (heading("Radio buttons"), 1),
            (size, "content"),
            (heading("Drop-down (opens a floating popup over the page)"), 1),
            (color, "content"),
            (heading("Text edit"), 1),
            (name, "content"),
            (heading("Button (primary / secondary variants)"), 1),
            (buttons, "content"),
            (heading("Button — image face / image+text"), 1),
            (image_tile, 4),
            (image_text, 3),
            (heading("Static text — single line (Label, selectable)"), 1),
            (Label("The quick brown fox jumps over the lazy dog.", selectable=True), 1),
            (heading("Static text — multi line (TextBlock, selectable)"), 1),
            (
                TextBlock(
                    "Drag to select, Cmd/Ctrl+A to select all, Cmd/Ctrl+C to copy:\n"
                    "  · it never reflows on the backend,\n"
                    "  · it just clips at the pane edge,\n"
                    "  · and the ScrollView scrolls past it.",
                    selectable=True,
                ),
                4,
            ),
        ],
        gap=1,
    )
    return VSplit(
        Item(scroller, weight=1, hints={"surface": "content"}),
        Item(status, size=1),
        gap=1,
    )


# Each row is two base units tall: a primary line and a dim details line. The
# A floor for the custom row height (row_height=ROW_H): the row widget measures
# its own taller, font-height-based size (two proportional-font lines), and this
# only guarantees a minimum. Rows scroll in base units, so they page correctly
# on every backend.
ROW_H = 2


class _FileRow(Container):
    """A custom, multi-line list row composed from ordinary widgets: a checkbox
    and an icon+name on the first line, a dim "size · modified" line below it.

    It reports its own height — two lines of the row font — through ``measure``,
    so ListView reserves the right room per row (content-sized, not a fixed grid
    height), and lays its two lines out by the font's *line height* rather than
    one base unit so a descender is not clipped. That "size by font height, not
    grid height" is the application's responsibility for a hand-composed widget;
    this is the demonstration of it. ListView routes clicks into this Container,
    so the checkbox toggles where it is hit; the same row runs on every backend."""

    def __init__(self, item: tuple[str, str, str, str]):
        super().__init__()
        name, icon, size, modified = item
        self._detail = Label(f"{size}  ·  {modified}", DIM)
        self.add(Checkbox(""), x=0, y=0, w=3, h=1)
        self.add(Label(f"{icon} {name}", BOLD), x=4, y=0, w=26, h=1)
        self.add(self._detail, x=4, y=1, w=26, h=1)

    def measure(self, ctx, axis, available):
        if axis == "y":
            two_lines = 2.0 * ctx.measure_line_height()
            return SizeRequest(min=two_lines, preferred=two_lines, max=two_lines)
        return super().measure(ctx, axis, available)

    def draw(self, ctx) -> None:
        # Lay the two lines out by the font's line height (a taller fraction than
        # one base unit for a proportional font), each line one line-height tall
        # so its descenders are not clipped by the child box.
        line_h = ctx.line_height()
        for slot in self._children:
            slot.rect = replace(slot.rect, y=line_h if slot.widget is self._detail else 0.0, h=line_h)
        super().draw(ctx)


def _make_file_row(item: tuple[str, str, str, str]) -> Container:
    return _FileRow(item)


def build_list_page(panel: Panel) -> VSplit:
    items = [f"Item {i:03d}" for i in range(50)]
    status = Label("Use arrows / page keys; enter to select · space toggles a row", DIM)
    listview = ListView(
        items, on_select=lambda i, text: setattr(status, "text", f"Selected: {text}")
    )

    # The same widget, now with a row_factory and a taller row_height: each item
    # becomes a composed, multi-line widget instead of a plain string.
    files = [
        ("report.pdf", "📄", "1.2 MB", "Jun 14"),
        ("photos", "📁", "48 items", "Jun 12"),
        ("notes.md", "📝", "4.1 KB", "Jun 16"),
        ("archive.zip", "🗜️", "92 MB", "May 30"),
        ("music", "📁", "210 items", "Apr 02"),
        ("readme.txt", "📄", "812 B", "Jun 01"),
        ("budget.xlsx", "📊", "56 KB", "Jun 09"),
        ("logo.png", "🖼️", "240 KB", "Mar 21"),
        ("src", "📁", "37 items", "Jun 17"),
        ("LICENSE", "📄", "1.1 KB", "Jan 05"),
        ("config.toml", "⚙️", "640 B", "Jun 15"),
        ("video.mp4", "🎬", "1.4 GB", "Feb 18"),
    ]
    rich = ListView(
        files,
        row_factory=_make_file_row,
        row_height=ROW_H,
        on_select=lambda i, item: setattr(status, "text", f"Selected: {item[0]}"),
    )

    return VSplit(
        Item(
            HSplit(
                Item(
                    VSplit(Item(Label("Text rows", BOLD), size=1), Item(listview, weight=1)),
                    size=24,
                ),
                Item(
                    VSplit(
                        Item(Label("Custom rows (taller, row_factory)", BOLD), size=1),
                        Item(rich, weight=1),
                    ),
                    size=34,
                ),
                Item(Label(""), weight=1),
                gap=2,
            ),
            weight=1,
        ),
        Item(status, size=1),
        gap=1,
    )


def build_log_page(panel: Panel) -> VSplit:
    # A virtualized, append-only stream: per-line color, word wrap, drag-select
    # + copy, and tail-following. The buffer is seeded large to show that only
    # the visible window is ever drawn — scrolling stays cheap regardless of
    # buffer size.
    LEVELS = {
        "DEBUG": Style(attr=TextAttribute.DIM),
        "INFO": Style(fg=(180, 200, 230)),
        "WARN": Style(fg=(229, 192, 16)),
        "ERROR": Style(fg=(229, 80, 80), attr=TextAttribute.BOLD),
    }
    order = list(LEVELS)

    def line(i: int) -> tuple[str, Style]:
        level = order[i % len(order)]
        msg = (
            f"{i:05d} [{level:5}] event {i} — a longer message that wraps to the "
            f"pane width so the word-wrap path and the row virtualization are "
            f"exercised together"
        )
        return (msg, LEVELS[level])

    log = LogView(
        [line(i) for i in range(1000)],
        wrap="word",
        selectable=True,
        auto_scroll=True,
        max_lines=20000,
    )
    counter = {"n": 1000}

    def add() -> None:
        text, style = line(counter["n"])
        counter["n"] += 1
        log.append(text, style)

    def clear() -> None:
        log.clear()

    controls = HSplit(
        Item(Button("Append line", on_click=add), size="content"),
        Item(Button("Clear", on_click=clear), size="content"),
        Item(
            Label("drag to select · ⌘/Ctrl+A all · ⌘/Ctrl+C copy · ↑↓/PgUp/PgDn/End scroll", DIM),
            weight=1,
        ),
        gap=2,
    )
    return VSplit(
        Item(log, weight=1, hints={"surface": "content"}),
        Item(controls, size="content"),
        gap=1,
    )


# {scene} is filled in with a real asset path at build time so the image block
# has something to render (GUI draws the picture; TUI shows the alt glyph).
_MARKDOWN_SAMPLE = """\
# MarkdownView

A read-only **rich-text** viewer for *Markdown*, parsed once into semantic
blocks whose inline roles (`bold`, `code`, links) are colored by the active
`Theme` — so the same document follows each backend's palette. Headings stand
out by **weight and size** alone, in the body color. The subset tracks the
common GitHub-flavored cases.

## Inline runs

Mix **bold**, *italic*, ***both***, ~~struck out~~, inline `code`, and a real
[PuiKit link](https://github.com/crftwr/tfm) in one sentence. Prose uses a
proportional font and `code` a monospace one on GUI; both fold to the one grid
font (bold / italic kept) on a terminal.

## Links every which way

Click [the Anthropic site](https://www.anthropic.com) — on GUI it opens in the
browser; on a terminal the URL is copied to the clipboard instead. An
`<autolink>` like <https://example.com> and a bare URL such as
https://puikit.dev also linkify, and a [reference link][repo] resolves from a
definition further down. A hard break splits this line here
onto the next row (two trailing spaces did that).

Setext heading
--------------

This section title uses the underlined form (`===` / `---`), which parses to the
same heading as `##`.

## Lists & tasks

- first bullet, long enough to wrap to the pane width and show the hanging
  indent that keeps the continuation aligned under the text
- second bullet
  - a nested item
- [x] a finished task
- [ ] a pending task
1. ordered one
2. ordered two

## Table

| Widget       | Backend | Aligns |
| :----------- | :-----: | -----: |
| `MarkdownView` | GUI/TUI |     1 |
| `Table`      |   any   |    12 |

Columns take their natural width and align by the `:` markers in the delimiter
row; the frame connects — crossing hairline strokes on GUI, box-drawing corners
and joints (`┌ ┬ ┼ ┤ ┘`) on a terminal. Rows are told apart by a hairline
between each on GUI, and by a distinct header fill + zebra body stripes on a
terminal (where a rule per row would cost real vertical space).

## Block quotes

> Quotes render muted, with a bar in the gutter, and reflow across
> several source lines into one paragraph.
> > Nested quotes stack another bar.

## Fenced code

A continuous panel background sits behind the block; with Pygments installed a
language tag turns on syntax colors (a flat code color otherwise).

```python
def greet(name):
    print(f"hello {name}")  # not inline-parsed as Markdown
```

---

[Back to top](#markdownview) · scroll with the arrow / page keys, Home / End,
or the mouse wheel.

[repo]: https://github.com/crftwr/tfm
"""


def build_markdown_page(panel: Panel) -> VSplit:
    # One source string parsed to semantic blocks; the Theme colors the roles,
    # so headings/links/code read correctly on TUI and GUI from the same widget.
    scene = os.path.join(os.path.dirname(__file__), "assets", "scene.png")
    view = MarkdownView(_MARKDOWN_SAMPLE.replace("{scene}", scene))
    return VSplit(
        Item(view, weight=1, hints={"surface": "content"}),
        Item(Label("↑↓/PgUp/PgDn/Home/End scroll · click a link to open it", DIM), size="content"),
        gap=1,
    )


def build_scrollbar_page(panel: Panel) -> VSplit:
    columns = [
        Item(
            VSplit(
                Item(Label(f"{pos:.1f} / {ratio:.1f}"), size=1),
                # Scrollbar width is intrinsic (backend-fixed); height 10.
                Item(
                    HSplit(Item(ScrollBar(pos, ratio), size="content"), Item(Label(""), weight=1)),
                    size=10,
                ),
                Item(Label(""), weight=1),
            ),
            size=12,
        )
        for pos, ratio in [(0.0, 0.3), (0.5, 0.3), (1.0, 0.3), (0.0, 0.8)]
    ]
    return VSplit(
        Item(Label("Standalone scroll bars (pos / ratio):"), size=1),
        Item(HSplit(*columns, Item(Label(""), weight=1), gap=2), weight=1),
        gap=1,
    )


class AnimTarget(Container):
    """The animated target is a widget tree: transitions on the container
    cascade to all children, and the overflowing child shows clipping."""

    def __init__(self):
        super().__init__()
        self.last_label = Label("none yet", Style(fg=(13, 188, 121)))
        self.add(Label("Target", BOLD), x=5, y=1, w=12, h=1)
        self.add(Label("Last transition:"), x=2, y=3, w=20, h=1)
        self.add(self.last_label, x=2, y=4, w=22, h=1)
        # Wider than the card on purpose: clipped at the container edge.
        self.add(
            Label("This long child label is clipped at the card edge", DIM),
            x=2, y=6, w=50, h=1,
        )

    def draw(self, ctx) -> None:
        # The card's pane background comes from its slot's surface role, which
        # children inherit (so their default text reads on it under any theme);
        # the border just frames it in the theme's accent.
        theme = ctx.theme
        border = theme.accent if theme is not None else (36, 114, 200)
        # The accent label tracks the card background: a bright green reads on a
        # dark card, a deeper green on a light one.
        bg = ctx.background
        self.last_label.style = Style(
            fg=(21, 128, 61) if bg is not None and _luminance(bg) > 140 else (13, 188, 121)
        )
        # Reserve each hand-placed child's box to the font's line height, so a
        # taller proportional font's descender is not clipped by its box (base_h
        # is the mono grid unit; the UI font's line box is taller). Sizing a
        # widget by font height rather than grid height is the application's job
        # under puikit's model — this is the demonstration of it.
        line_h = ctx.line_height()
        for slot in self._children:
            if slot.rect.h < line_h:
                slot.rect = replace(slot.rect, h=line_h)
        ctx.draw_border(Style(fg=border))
        ctx.draw_icon(2, 1, "check")
        super().draw(ctx)  # children, each clipped to the card


ANIMATIONS = [
    ("Fade (opacity)", {"transition": "fade", "duration_ms": 500}),
    ("Slide (position)", {"transition": "slide", "duration_ms": 500, "from_dx": -8, "from_dy": 0}),
    ("Drop (position)", {"transition": "slide", "duration_ms": 500, "from_dx": 0, "from_dy": -4}),
    ("Scale (visual zoom)", {"transition": "scale", "duration_ms": 500, "from_scale": 0.5}),
    ("Size (layout reflow)", {"transition": "size", "duration_ms": 500, "from_w": 8, "from_h": 3}),
    ("Highlight (color)", {"transition": "highlight", "duration_ms": 700, "color": (229, 229, 16)}),
    ("Flash red (color)", {"transition": "highlight", "duration_ms": 700, "color": (205, 49, 49), "strength": 0.6}),
]


def build_animation_page(panel: Panel) -> VSplit:
    target = AnimTarget()

    def run(index: int, name: str) -> None:
        target.last_label.text = name
        panel.animate(target, hints=dict(ANIMATIONS[index][1]))

    listview = ListView([name for name, _ in ANIMATIONS], on_select=run)
    return VSplit(
        Item(Label("Pick a transition, press enter", DIM), size=1),
        Item(
            HSplit(
                Item(listview, size=24),
                # AnimTarget stays a Container — a legitimate free-form *leaf*
                # (bordered card with hand-placed children); the layout system
                # only positions it. Capped to a 40x12 card via a nested split.
                Item(
                    VSplit(
                        Item(target, size=12, hints={"surface": "sidebar"}),
                        Item(Label(""), weight=1),
                    ),
                    size=40,
                ),
                Item(Label(""), weight=1),
                gap=2,
            ),
            weight=1,
        ),
        gap=1,
    )


class Region(Widget):
    """A layout region that reports its own computed geometry. Regions draw
    no borders: the surface backgrounds and the layout dividers separate
    them. The base unit extent is fractional on pixel-layout backends and whole on
    TUI, so the same layout reads differently per backend."""

    def __init__(self, name: str, color: tuple[int, int, int], note: str = ""):
        self.name = name
        self.color = color
        self.note = note

    def draw(self, ctx) -> None:
        w_units, h_units = ctx.size_units
        cw, ch = ctx.base_size
        units_line = f"{w_units:.2f} x {h_units:.2f} base units"
        px_line = f"= {w_units * cw:.0f} x {h_units * ch:.0f} px"
        # The region's background is whatever its slot resolved (a themed surface
        # role or a fixed vivid block). Pick a body color that reads on it, and
        # keep the signature hue for the title only while it stays legible there
        # — otherwise (a bright hue on a light surface) fall back to plain text.
        bg = ctx.background
        body_fg = _readable_fg(bg) if bg is not None else None
        title_fg = self.color if (bg is None or _contrast(self.color, bg) >= 2.2) else body_fg
        if ctx.height >= 5:
            ctx.draw_text(1, 0, self.name, Style(fg=title_fg, attr=TextAttribute.BOLD))
            ctx.draw_text(1, 2, units_line, Style(fg=body_fg))
            ctx.draw_text(1, 3, px_line, Style(fg=body_fg))
            if self.note:
                ctx.draw_text(1, 4, self.note, Style(fg=body_fg, attr=TextAttribute.DIM))
        else:
            line = f"{self.name}  {units_line} {px_line}" + (
                f"  ({self.note})" if self.note else ""
            )
            ctx.draw_text(1, 0, line, Style(fg=title_fg))


def build_layout_page(panel: Panel) -> VSplit:
    # One layout definition, resolved at the page's own granularity: every
    # boundary snaps to whole base units on TUI, lands on device pixels on GUI.
    # Header/status use divider="subtle" (a GUI hairline, nothing on TUI —
    # the themed surface backgrounds carry the contrast); the body panes use
    # divider="strong" (a hairline on GUI, one whole │ base unit column on TUI).
    return VSplit(
        Item(Label("One layout, two granularities — resize the window", DIM), size=1),
        Item(
            VSplit(
                Item(
                    Region("Header", (229, 229, 16), "fixed: 1 base unit"),
                    size=1,
                    hints={"surface": "header"},
                ),
                Item(
                    HSplit(
                        Item(
                            Region("Sidebar", (13, 188, 121), "weight 1, min 220px"),
                            weight=1,
                            hints={"min_px": 220, "min": 18, "surface": "sidebar"},
                        ),
                        Item(
                            Region("Main", (36, 114, 200), "weight 2"),
                            weight=2,
                            hints={"surface": "content"},
                        ),
                        Item(
                            Region("Inspector", (188, 63, 188), "weight 1"),
                            weight=1,
                            hints={"surface": "sidebar"},
                        ),
                        divider="strong",
                    )
                ),
                Item(
                    Region("Status", (220, 220, 220)),
                    size=1,
                    hints={"surface": "status"},
                ),
                divider="subtle",
            ),
            weight=1,
        ),
        gap=1,
    )


def build_intrinsic_page(panel: Panel) -> VSplit:
    # Three widgets that size *themselves*; the layout reserves what they
    # report and the rest flexes around them. None of these sizes is named by
    # the app — they come from the widget's own measure().
    return VSplit(
        Item(Label("Widgets measure themselves; the layout reserves it", DIM), size=1),
        Item(
            VSplit(
            # 1. Content HEIGHT: the message reserves exactly its line count.
            Item(
                TextBlock(
                    "This message area is sized to its content:\n"
                    "it reserves exactly as many lines as the\n"
                    "text has — three here — no more, no less.",
                ),
                size="content",
                hints={"surface": "content"},
            ),
            # 2. Backend-fixed WIDTH meets a weighted split: the scrollbar
            #    claims its fixed width first, then main:side divide the rest
            #    2:1. No conflict — weight only ever splits the remainder.
            #    These panes are weight=1, so they flex to fill the window —
            #    the open space below the captions is that flex, on purpose.
            Item(
                HSplit(
                    # Explicit, distinct fills so the 2:1 proportion is a
                    # *visible* block: resize the window and watch Main stay
                    # twice Side while the scrollbar keeps its fixed width.
                    Item(
                        Region("Main", (120, 170, 240), "weight 2 of 3"),
                        weight=2,
                        hints={"bg": (34, 48, 78)},
                    ),
                    Item(
                        Region("Side", (130, 220, 170), "weight 1 of 3"),
                        weight=1,
                        hints={"bg": (28, 58, 46)},
                    ),
                    # Fixed width, reserved before the 2:1 split divides the rest.
                    Item(ScrollBar(0.3, 0.4), size="content"),
                    divider="subtle",
                ),
                weight=1,
            ),
            # 3. Content WIDTH + cross-axis align: each button is as wide as
            #    its label and centered in the taller row. An explicit footer
            #    bg makes the action bar read as chrome (matching the title /
            #    status bars), since the GUI theme keeps surface roles close.
            Item(
                HSplit(
                    Item(
                        Button("Cancel", style=BUTTON_FACE),
                        size="content", align="center",
                    ),
                    Item(Label("", DIM), weight=1),  # flexible spacer
                    Item(
                        Button("OK", style=BUTTON_FACE),
                        size="content", align="center",
                    ),
                    gap=1,
                ),
                size=5,
                hints={"bg": TITLE_BG},
            ),
            divider="subtle",
            ),
            weight=1,
        ),
        gap=1,
    )


def build_fonts_page(panel: Panel) -> VSplit:
    # One widget vocabulary, two honest resolutions. On GUI each row renders a
    # real face / size / weight / slant (proportional unless monospace=True);
    # on TUI the Panel folds weight/slant into bold/italic attributes and drops
    # face, size, and proportional flow — the same Style, degraded in one place
    # (docs/font_system.md §6). No row branches on the backend.
    # size="content" lets each Label reserve its *own* line height: the grid
    # font is one base unit, a taller proportional/sized font more, so the rows
    # never overlap regardless of face or point size — the height comes from the
    # widget's own measure, not a number the page guesses.
    def row(label: str, style: Style = Style()) -> Item:
        return Item(Label(label, style), size="content")

    return VSplit(
        Item(
            Label("GUI renders faces, sizes, weights, slants; TUI folds them", DIM),
            size=1,
        ),
        # The app-wide GUI default for text that names no font is the
        # proportional UI font; on TUI it folds to the one terminal font.
        row("Default (font=None) — proportional UI font on GUI"),
        # Pin the monospace face explicitly to still demonstrate the column-
        # aligned base grid font the layout's base unit is derived from.
        row("Base grid font — monospaced, column-aligned", Style(font=Font(monospace=True))),
        # Weights: only >= SEMI_BOLD survives on TUI (as bold).
        row("Light (300) — folds to plain on TUI", Style(font=Font(weight=FontWeight.LIGHT))),
        row("Semibold (600) — folds to bold on TUI", Style(font=Font(weight=FontWeight.SEMI_BOLD))),
        row("Bold (700)", Style(font=Font(weight=FontWeight.BOLD))),
        # Slant: italic survives on TUI as the italic attribute.
        row("Italic — folds to italic on TUI", Style(font=Font(slant=FontSlant.ITALIC))),
        # A named installed family (GUI only; ignored on TUI).
        row("Named family: Georgia, 16pt", Style(font=Font(family="Georgia", size=16))),
        # Decorative size: content sizing reserves the 28pt line height for it,
        # so it sits in its own tall row instead of overlapping its neighbours.
        row("Big Title — sized text", Style(font=Font(size=28, weight=FontWeight.SEMI_BOLD))),
        Item(Label(""), weight=1),
        gap=0,
    )


def build_wrap_page(panel: Panel) -> VSplit:
    # Text wrapping is content-driven on *both* axes: a long logical line is
    # folded to the pane width and the block reserves the rows it needs
    # (size="content"). The fold uses the pane's own text measurement, so it
    # follows the font — column counts under the base grid font, proportional
    # advances under a real Style.font on GUI — and wide CJK glyphs without the
    # widget ever reading a font or branching on the backend. Resize the window
    # (GUI) or the terminal (TUI) and every paragraph reflows to the new width.
    heading = lambda text: Label(text, BOLD)  # noqa: E731 - tiny local helper

    en = (
        "With wrapping on, a long logical line is folded on word boundaries to "
        "fit the pane width, and the block grows as tall as the wrapped text "
        "needs. Resize the window and the same paragraph reflows."
    )
    # Japanese carries no ASCII spaces, so word wrap falls back to per-glyph
    # breaks; each kana/kanji is two columns on the base grid font.
    ja = (
        "日本語のテキストは単語の区切りに空白を使わないため、"
        "行折り返しは文字単位の境界で行われます。"
        "ウィンドウの幅を変えると、同じ段落が新しい幅に合わせて流れ直します。"
    )
    PROP = Style(font=Font())  # proportional on GUI; folds to the grid font on TUI
    # The GUI default is now proportional, so the grid-aligned rows pin the
    # monospace face explicitly to keep the column-aligned vs. flowing contrast.
    MONO = Style(font=Font(monospace=True))

    scroller = ScrollView(
        [
            (heading("Word wrap — base grid font (column-aligned)"), 1),
            (TextBlock(en, style=MONO, wrap=True), "content"),
            (heading("Word wrap — proportional font (GUI flows by advances)"), 1),
            # Same text, same width: GUI wraps at different points than the
            # monospaced block above because the glyph advances differ; TUI
            # folds the font to the grid and wraps identically. One intent.
            (TextBlock(en, style=PROP, wrap=True), "content"),
            (heading("Japanese — wraps between glyphs (no spaces)"), 1),
            (TextBlock(ja, style=MONO, wrap=True), "content"),
            (heading("Japanese — proportional font"), 1),
            (TextBlock(ja, style=PROP, wrap=True), "content"),
            (heading("Character wrap (wrap=\"char\") — breaks anywhere"), 1),
            (TextBlock("x" * 200, wrap="char"), "content"),
            (heading("Unwrapped (wrap=False) — clips at the pane edge"), 1),
            (TextBlock("This single line is not wrapped, so it runs off the "
                       "right edge of the pane and the overflow is clipped."), 1),
        ],
        gap=1,
    )
    status = Label("Resize the window / terminal — every paragraph reflows", DIM)
    return VSplit(
        Item(scroller, weight=1, hints={"surface": "content"}),
        Item(status, size=1),
        gap=1,
    )


class PairMeter(Widget):
    """A live readout of the TUI backend's curses color-pair usage, drawn at the
    right of the status bar. It reads ``backend.color_pair_stats()`` every frame
    (so the count reflects what the *current* screen — page + any dialog — has
    allocated), and shows ``used/capacity`` plus an ``ovf`` (overflow) counter:
    distinct (fg, bg) requests that arrived after COLOR_PAIRS ran out. A non-zero
    ``ovf`` is the live proof the screen is exhausting pairs. Backends without
    the method (GUI) render nothing — color pairs are a curses concept."""

    # A fixed-width format so measure and draw reserve the same extent every
    # frame (no per-frame width jitter as the numbers change).
    _FMT = " pairs {used:>4}/{cap:<4} ovf {ovf:>5} "

    def __init__(self, style: Style | None = None):
        self.style = style if style is not None else Style()

    def _text(self, backend) -> str:
        stats = getattr(backend, "color_pair_stats", None)
        if stats is None:
            return ""
        used, cap, ovf = stats()
        return self._FMT.format(used=used, cap=cap, ovf=ovf)

    def draw(self, ctx) -> None:
        text = self._text(ctx.panel.backend)
        if not text:
            return
        # Flag overflow in red so it pops the moment pairs run out.
        used, cap, ovf = ctx.panel.backend.color_pair_stats()
        style = self.style if ovf == 0 else Style(fg=(255, 120, 120), bg=self.style.bg)
        ctx.draw_text(0, 0, text, style)

    def measure(self, ctx, axis: str, available: float):
        if axis == "x":
            w = ctx.measure_text(self._FMT.format(used=0, cap=0, ovf=0), self.style)
            return SizeRequest(min=w, preferred=w, max=w)
        h = ctx.measure_line_height(self.style)
        return SizeRequest(min=h, preferred=h, max=h)


class Swatch(Widget):
    """A filled color block labeled with its RGB. The app passes one RGB
    intent; GUI paints the exact channel values, TUI approximates them through
    the xterm-256 palette — the same color, two fidelities. No swatch branches
    on the backend; the fill carries the difference."""

    def __init__(self, color: tuple[int, int, int], name: str = ""):
        self.color = color
        self.name = name

    def draw(self, ctx) -> None:
        # Fill the exact (fractional) cell size, not ctx.width/height which
        # truncate to whole base units: on pixel-layout (GUI) the cells have
        # fractional widths, and truncating leaves the remainder unpainted —
        # a visible gap between blocks. ctx.size is the unrounded extent.
        w, h = ctx.size_units
        ctx.fill_rect(0, 0, w, h, Style(bg=self.color))
        # A thin gradient cell has no room for a label: the color is the point.
        if ctx.width < 6:
            return
        r, g, b = self.color
        # Pick a legible foreground from the swatch's luminance, so the label
        # stays readable on both light and dark fills.
        lum = 0.299 * r + 0.587 * g + 0.114 * b
        fg = (20, 20, 20) if lum > 140 else (235, 235, 235)
        rgb_text = f"{r}, {g}, {b}"
        if ctx.height >= 2 and self.name:
            ctx.draw_text(1, 0, self.name, Style(fg=fg, bg=self.color, attr=TextAttribute.BOLD))
            ctx.draw_text(1, 1, rgb_text, Style(fg=fg, bg=self.color))
        else:
            ctx.draw_text(1, 0, self.name or rgb_text, Style(fg=fg, bg=self.color))


PALETTE = [
    ("Red", (205, 49, 49)),
    ("Green", (13, 188, 121)),
    ("Yellow", (229, 229, 16)),
    ("Blue", (36, 114, 200)),
    ("Magenta", (188, 63, 188)),
    ("Cyan", (17, 168, 205)),
]


def _hsv_rgb(h: float, s: float, v: float) -> tuple[int, int, int]:
    r, g, b = colorsys.hsv_to_rgb(h, s, v)
    return (round(r * 255), round(g * 255), round(b * 255))


def _hls_rgb(h: float, l: float, s: float) -> tuple[int, int, int]:
    r, g, b = colorsys.hls_to_rgb(h, l, s)
    return (round(r * 255), round(g * 255), round(b * 255))


def build_color_page(panel: Panel) -> VSplit:
    # One RGB intent per swatch; GUI paints exact channels, TUI snaps each to
    # the nearest curated-palette color. A 2D color table shows the difference
    # plainly: hue sweeps across columns, lightness down rows — from light tints
    # at the top, through vivid mid-tones, to dark shades at the bottom — so the
    # table spans bright and dark colors alike. The grid reads as a smooth field
    # on GUI and as discrete bands on TUI, where the palette quantizes it. The
    # grid is built from the layout system — a VSplit of HSplit rows — so it
    # re-resolves to fill the pane on every resize.
    cols, rows = 32, 13

    def cell(cx: int, cy: int) -> Swatch:
        hue = cx / cols
        # Lightness sweeps the full range at full saturation: pure white at the
        # top row, vivid mid-tones at the center, pure black at the bottom — so
        # the table spans both extremes plus every tint and shade between.
        lightness = 1.0 - (cy / (rows - 1))
        return Swatch(_hls_rgb(hue, lightness, 1.0))

    grid = VSplit(
        *[
            Item(HSplit(*[Item(cell(cx, cy), weight=1) for cx in range(cols)]), weight=1)
            for cy in range(rows)
        ]
    )
    return VSplit(
        Item(Label("GUI paints exact RGB; TUI snaps to the curated palette", DIM), size=1),
        # Named palette: each swatch is wide enough to label with its RGB.
        Item(HSplit(*[Item(Swatch(c, n), weight=1) for n, c in PALETTE], gap=1), size=4),
        Item(Label("Hue x lightness table — tints to shades (smooth on GUI, banded on TUI):", DIM), size=1),
        Item(grid, weight=1),
        gap=1,
    )


# Each scenario is (nav name, card title, note lines, push hints). A None hints
# marks the special "stacked" scenario, which pushes several layers at once.
LAYER_SCENARIOS = [
    (
        "Plain overlay", "Plain overlay",
        ["A bare layer over the page.", "Draw order only — no effects."],
        {"w": 44, "h": 8},
    ),
    (
        "Drop shadow", "Drop shadow",
        ["GUI renders a real drop shadow;", "TUI ignores the shadow hint."],
        {"w": 44, "h": 8, "shadow": True},
    ),
    (
        "Dim below", "Dim below",
        ["GUI dims the page with a translucent", "overlay; TUI uses dim attributes."],
        {"w": 44, "h": 8, "dim_below": True},
    ),
    (
        "Modal dialog", "Modal dialog",
        ["shadow + dim_below together — the", "canonical modal: raised, isolating."],
        {"w": 44, "h": 8, "shadow": True, "dim_below": True},
    ),
    ("Stacked z-order", None, None, None),
    ("Open from layer", None, None, None),
]


def build_layer_page(panel: Panel) -> VSplit:
    # Layers are pushed onto the Panel, not placed by the page: push_layer
    # overlays the whole screen above the content. The page only declares the
    # intent (hints); the Panel resolves the capability per backend.
    status = Label("Pick a layer scenario, press enter", DIM)

    def push(title, notes, hints, z) -> None:
        card = LayerCard(title, notes, panel.pop_layer)
        panel.push_layer(card, z=z, hints=hints)
        # GUI fades the layer in over 150ms; TUI shows it immediately.
        panel.animate(card, hints={"transition": "fade", "duration_ms": 150})

    def open_nested(depth: int) -> None:
        # A layer opened from within the previous layer: the active card's
        # 'o' handler calls this, pushing a new card one z above itself. Each
        # is modal, so events go to the top; esc pops back down the stack.
        card = LayerCard(
            f"Nested layer (depth {depth + 1})",
            ["Opened from the layer below it.",
             "Press 'o' again to go deeper, esc to back out."],
            panel.pop_layer,
            on_open=lambda: open_nested(depth + 1),
        )
        panel.push_layer(
            card,
            z=10 + depth,
            hints={"w": 38, "h": 8, "x": 4 + depth * 4, "y": 2 + depth * 2, "shadow": True},
        )
        panel.animate(card, hints={"transition": "fade", "duration_ms": 150})

    def open_scenario(index: int, name: str) -> None:
        _, title, notes, hints = LAYER_SCENARIOS[index]
        if name == "Stacked z-order":  # push three layers at once
            for i in range(3):
                push(
                    f"Stacked layer (z={10 + i})",
                    [f"Card {i + 1} of 3 — the overlap shows z-order.",
                     "esc pops the top, revealing the one below."],
                    {"w": 34, "h": 7, "x": 6 + i * 5, "y": 3 + i * 2, "shadow": True},
                    z=10 + i,
                )
            status.text = "Three layers stacked by z — esc pops them in turn"
            return
        if name == "Open from layer":  # the layer itself opens the next one
            open_nested(0)
            status.text = "Press 'o' inside the layer to open another on top"
            return
        push(title, notes, hints, z=10)
        status.text = f"Pushed {name} — esc / enter closes it"

    listview = ListView([name for name, *_ in LAYER_SCENARIOS], on_select=open_scenario)
    explainer = TextBlock(
        "Layers are pushed onto the Panel, not placed by the page.\n"
        "One push_layer intent composites differently per backend:\n"
        "GUI does real layer compositing — translucent dim, drop\n"
        "shadows, z-ordered overlap; TUI falls back to draw order,\n"
        "approximates dim by graying cells and the shadow with a\n"
        "thin half-block edge down-right of the layer.\n"
        "The page never branches on the capability.",
    )
    return VSplit(
        Item(Label("Pushed layers overlay the whole panel, above the page", DIM), size=1),
        Item(
            HSplit(
                Item(listview, size=24),
                Item(explainer, weight=1, hints={"surface": "content"}),
                gap=2,
            ),
            weight=1,
        ),
        Item(status, size=1),
        gap=1,
    )


ASSETS = os.path.join(os.path.dirname(__file__), "assets")


def build_images_page(panel: Panel) -> VSplit:
    # One image intent, five object-fits, all resolved by the layout, never by
    # the page. A GUI backend renders the real picture (scaled, letterboxed, or
    # cropped per fit); a terminal that speaks an inline-image protocol (kitty /
    # iTerm2 / WezTerm / sixel, with Pillow installed) draws it too; any other
    # TUI has no `images` capability, so the Panel layer stamps each image's alt
    # emoji in its place. The page never branches on the backend.
    # The asset is a 16:9 scene, so the fits read distinctly.
    status = Label("Resize the window to watch each fit re-resolve", DIM)

    scene = os.path.join(ASSETS, "scene.png")

    def fit_cell(title: str, fit: str) -> Item:
        # Each cell hands the image the same square-ish pane; the fit decides
        # how the 16:9 scene relates to it (stretch / letterbox / crop).
        return Item(
            VSplit(
                Item(Label(title, BOLD), size=1),
                Item(ImageView(scene, fit=fit, alt="🏞️"), weight=1),
                gap=0,
            ),
            weight=1,
        )

    # Top row: the three fits that share a given width and height.
    box_fits = HSplit(
        fit_cell("fill", "fill"),
        fit_cell("contain", "contain"),
        fit_cell("cover", "cover"),
        gap=2,
    )

    # Bottom row: the two aspect-driven fits size the widget themselves. "width"
    # is intrinsic in a vertical stack (height follows the width it is given);
    # "height" is intrinsic in a horizontal split (width follows the height).
    aspect_fits = HSplit(
        Item(
            VSplit(
                Item(Label("fit=width (height follows)", BOLD), size=1),
                Item(ImageView(scene, fit="width", alt="🏞️"), size="content"),
                Item(Label(""), weight=1),
                gap=0,
            ),
            weight=1,
        ),
        Item(
            VSplit(
                Item(Label("fit=height (width follows)", BOLD), size=1),
                Item(
                    HSplit(
                        Item(ImageView(scene, fit="height", alt="🏞️"), size="content"),
                        Item(Label(""), weight=1),
                    ),
                    weight=1,
                ),
                gap=0,
            ),
            weight=1,
        ),
        gap=2,
    )

    return VSplit(
        Item(Label("ImageView object-fits — drawn on GUI and image-capable terminals", DIM), size=1),
        Item(box_fits, weight=1, hints={"surface": "content"}),
        Item(aspect_fits, weight=1, hints={"surface": "sidebar"}),
        Item(status, size=1),
        gap=1,
    )


class CheckerImage(Widget):
    """Per-pixel alpha. A checkerboard is painted first; an RGBA image is drawn
    over it, so the image's *own* alpha channel decides which checks show
    through — opaque pixels hide them, transparent pixels reveal them, and the
    feathered rim blends. GUI composites this pixel by pixel; TUI has no images,
    so the Panel layer stamps the alt emoji over the checkerboard instead. The
    widget never branches on the backend."""

    def __init__(self, image: str, alt: str = "🎫", cell: int = 2):
        self.image = image
        self.alt = alt
        self.cell = cell

    def draw(self, ctx) -> None:
        wu, hu = ctx.size_units
        c = self.cell
        for ry in range(math.ceil(hu / c)):
            for rx in range(math.ceil(wu / c)):
                shade = (74, 74, 86) if (rx + ry) % 2 == 0 else (150, 150, 166)
                ctx.fill_rect(rx * c, ry * c, c, c, Style(bg=shade))
        # contain keeps the round badge's aspect, so the transparent corners
        # sit over the checkerboard and read as genuinely see-through.
        ctx.draw_image(
            0, 0, self.image, hints={"w": wu, "h": hu, "fit": "contain", "alt": self.alt}
        )


def build_alpha_page(panel: Panel) -> VSplit:
    # Pixel-level alpha channel: an RGBA badge (a feathered, hue-swept disk on a
    # transparent field) composited over a checkerboard. The checks showing
    # through the corners and the soft rim are the alpha channel at work.
    badge = os.path.join(ASSETS, "badge.png")
    explainer = TextBlock(
        "badge.png is an RGBA image (color type 6).\n"
        "Its alpha channel is per pixel:\n"
        "  · opaque disk center — hides the checks,\n"
        "  · feathered rim — blends with the checks,\n"
        "  · transparent corners — checks show through.\n"
        "GUI composites it pixel by pixel; TUI shows\n"
        "the alt emoji over the same checkerboard.",
    )
    return VSplit(
        Item(Label("Per-pixel alpha — an RGBA image over a checkerboard", DIM), size=1),
        Item(
            HSplit(
                Item(CheckerImage(badge, alt="🎫"), weight=3),
                Item(explainer, weight=2, hints={"surface": "content"}),
                gap=2,
            ),
            weight=1,
        ),
        gap=1,
    )


class RGBAOverlays(Widget):
    """Three translucent RGBA fills over the pane's base color. Where they
    overlap, the channels composite (SourceOver) on GUI — red+green+blue build
    up toward white; on TUI there is no per-pixel compositing, so the Panel
    flattens each fill over the pane background instead (no overlap build-up).
    Same RGBA intent, two fidelities."""

    def draw(self, ctx) -> None:
        wu, hu = ctx.size_units
        a = 130  # ~51% opacity in the 0-255 alpha channel
        bw, bh = wu * 0.52, hu * 0.6
        ctx.fill_rect(wu * 0.04, hu * 0.08, bw, bh, Style(bg=(222, 64, 64, a)))    # red
        ctx.fill_rect(wu * 0.24, hu * 0.30, bw, bh, Style(bg=(70, 200, 96, a)))    # green
        ctx.fill_rect(wu * 0.44, hu * 0.08, bw, bh, Style(bg=(74, 124, 232, a)))   # blue


class TintedImage(Widget):
    """Image + RGBA blending: an opaque photo with a translucent color wash
    drawn on top. GUI composites the wash over the picture (a colored tint);
    TUI cannot, so the wash flattens to a flat color block (the picture's alt
    emoji is painted under it). The wash is one RGBA fill, resolved per
    backend."""

    def __init__(self, image: str, tint, alt: str = "🏞️"):
        self.image = image
        self.tint = tint
        self.alt = alt

    def draw(self, ctx) -> None:
        wu, hu = ctx.size_units
        ctx.draw_image(0, 0, self.image, hints={"w": wu, "h": hu, "fit": "cover", "alt": self.alt})
        ctx.fill_rect(0, 0, wu, hu, Style(bg=self.tint))


def build_blending_page(panel: Panel) -> VSplit:
    # Image + RGBA blending, two ways: (1) the same photo drawn at falling
    # global opacities over a light backdrop, so it blends toward the
    # background; (2) translucent RGBA color fills compositing over a base and
    # over each other, plus a color wash over a photo.
    scene = os.path.join(ASSETS, "scene.png")
    light = (228, 228, 234)  # a light backdrop a faded image blends toward
    base = (20, 20, 28)      # the dark base the RGBA overlays composite onto

    # The pane-background hint must sit on the *leaf* item the layout places —
    # a hint on an item wrapping a nested split is not carried to its children,
    # and the TUI flatten needs that background to composite an RGBA color over.
    def opacity_cell(caption: str, alpha: float) -> Item:
        return Item(
            VSplit(
                Item(Label(caption, BOLD), size=1, hints={"bg": light}),
                Item(
                    ImageView(scene, fit="cover", alt="🏞️", alpha=alpha),
                    weight=1, hints={"bg": light},
                ),
                gap=0,
            ),
            weight=1,
        )

    return VSplit(
        Item(Label("Image + RGBA blending — GUI composites, TUI approximates", DIM), size=1),
        Item(
            HSplit(
                opacity_cell("image @ 100%", 1.0),
                opacity_cell("image @ 60%", 0.6),
                opacity_cell("image @ 30%", 0.3),
                gap=2,
            ),
            weight=1,
        ),
        Item(
            HSplit(
                Item(
                    VSplit(
                        Item(Label("RGBA fills — overlaps blend on GUI", BOLD), size=1, hints={"bg": base}),
                        Item(RGBAOverlays(), weight=1, hints={"bg": base}),
                        gap=0,
                    ),
                    weight=1,
                ),
                Item(
                    VSplit(
                        Item(Label("Photo + translucent color wash", BOLD), size=1, hints={"surface": "sidebar"}),
                        Item(TintedImage(scene, (0, 96, 208, 120)), weight=1, hints={"surface": "sidebar"}),
                        gap=0,
                    ),
                    weight=1,
                ),
                gap=2,
            ),
            weight=1,
        ),
        gap=1,
    )


def build_tabs_page(panel: Panel) -> VSplit:
    # A Tabs widget swaps a content pane under a strip of titles. Each tab is
    # an ordinary widget — a label, a scrolling list, a text block — placed by
    # the Tabs widget, not the page. Left/right (or a click on a title) switch
    # the active tab; the active content fills the area below the strip and
    # receives forwarded events (the list scrolls while its tab is active).
    overview = TextBlock(
        "Tabs pair a title with a content widget and show one at a time.\n"
        "\n"
        "  · ←/→ or click a title to switch tabs\n"
        "  · the active tab is marked with the theme accent when focused\n"
        "  · other keys and the mouse climb through to the active content\n"
        "\n"
        "The same Tabs widget runs on every backend — only the strip's accent\n"
        "underline is a vector flourish the Panel layer drops on a grid.",
    )
    rows = ListView([f"List row {i:02d}" for i in range(40)])
    notes = TextBlock(
        "This tab holds a multi-line TextBlock.\n"
        "Switch back and forth: each tab keeps its own state\n"
        "(the list remembers its selection and scroll position).",
    )
    tabs = Tabs(
        [("Overview", overview), ("A long list", rows), ("Notes", notes)],
        on_change=lambda i, t: setattr(status, "text", f"Tab → {t}"),
    )
    status = Label("←/→ switch tabs · click a title · content fills below", DIM)
    return VSplit(
        Item(tabs, weight=1, hints={"surface": "content"}),
        Item(status, size=1),
        gap=1,
    )


def build_tree_page(panel: Panel) -> VSplit:
    # A TreeView flattens the currently-visible nodes (respecting each node's
    # expanded flag) and draws them indented by depth, with an expander marker
    # per branch. It scrolls like ListView when the rows overflow.
    status = Label("↑/↓ move · →/← expand/collapse · enter activate · click the marker", DIM)

    def on_select(node: TreeNode) -> None:
        status.text = f"Selected: {node.label}"

    def on_activate(node: TreeNode) -> None:
        kind = "branch" if node.children else "leaf"
        status.text = f"Activated {kind}: {node.label}"

    roots = [
        TreeNode(
            "puikit",
            expanded=True,
            children=[
                TreeNode(
                    "widgets",
                    expanded=True,
                    children=[
                        TreeNode("list.py"),
                        TreeNode("tabs.py"),
                        TreeNode("tree.py"),
                        TreeNode("menu.py"),
                    ],
                ),
                TreeNode(
                    "backends",
                    children=[TreeNode("curses_backend.py"), TreeNode("macos_backend.py")],
                ),
                TreeNode("panel.py"),
                TreeNode("layout.py"),
            ],
        ),
        TreeNode("docs", children=[TreeNode("layout_system.md"), TreeNode("font_system.md")]),
        TreeNode("README.md"),
    ]
    tree = TreeView(roots, on_select=on_select, on_activate=on_activate)
    explainer = TextBlock(
        "A node carries a label, optional children, and an expanded flag.\n"
        "\n"
        "  · →  expands a collapsed branch, or steps into the first child\n"
        "  · ←  collapses an open branch, or jumps out to the parent\n"
        "  · enter toggles a branch / activates a leaf\n"
        "  · a click on the ▸ / ▾ marker toggles that branch\n"
        "\n"
        "The selection highlight, indentation, and scroll bar are the same\n"
        "intents ListView uses — one implementation, every backend.",
    )
    return VSplit(
        Item(
            HSplit(
                Item(tree, size=32, hints={"surface": "sidebar"}),
                Item(explainer, weight=1, hints={"surface": "content"}),
                gap=2,
            ),
            weight=1,
        ),
        Item(status, size=1),
        gap=1,
    )


class MenuPlayground(Widget):
    """The context-menu target on the Menu page. A right-click (or 'm' when
    focused) asks the Panel to pop a context menu at the pointer — native on
    GUI, a widget popup on TUI — so the page never branches on the backend."""

    focusable = True

    def __init__(self, build_menu, act):
        self.build_menu = build_menu
        self.act = act
        self._panel = None
        self._abs = (0.0, 0.0, 0.0, 0.0)

    def draw(self, ctx) -> None:
        self._panel = ctx.panel
        self._abs = ctx.screen_rect
        ctx.draw_text(0, 0, "Right-click anywhere in this area for a context menu", BOLD)
        ctx.draw_text(0, 1, "(or focus it with tab and press 'm')")
        ctx.draw_text(0, 3, "Its 'Paste' enables only while the checkbox above is on —", DIM)
        ctx.draw_text(0, 4, "a custom condition the menu re-evaluates each time it opens.", DIM)

    def _popup(self, x: float, y: float) -> None:
        if self._panel is not None:
            self._panel.popup_menu(self.build_menu(), x, y)

    def handle_event(self, event) -> bool:
        if event.type is EventType.MOUSE_CLICK and event.button == "right":
            rx, ry, *_ = self._abs
            self._popup(rx + (event.x or 0), ry + (event.y or 0))
            return True
        if event.type is EventType.KEY and event.key == "m":
            rx, ry, *_ = self._abs
            self._popup(rx + 2, ry + 2)
            return True
        return False


def build_menu_page(panel: Panel) -> VSplit:
    # One Menu model drives both an OS-native menu bar / context menu on GUI
    # (NSMenu) and an in-window, widget-rendered menu on TUI — the Panel layer
    # resolves which, so this page never branches on the capability. The model
    # shows submenus, separators, keyboard-shortcut hints, a live checkmark,
    # and items whose enabled state is a *custom condition* (a predicate
    # evaluated when the menu opens).
    status = Label("Use the menu bar or right-click the area; watch the status line", DIM)
    state = {"clipboard": False, "wrap": True}

    def act(msg: str) -> None:
        status.text = msg

    clip = Checkbox(
        "Clipboard has content (enables the Paste items)",
        checked=False,
        on_change=lambda v: state.__setitem__("clipboard", v),
    )

    menu_model = Menu(
        MenuItem(
            "File",
            submenu=Menu(
                MenuItem("New", on_select=lambda: act("File ▸ New"), shortcut="Cmd+N"),
                MenuItem("Open…", on_select=lambda: act("File ▸ Open"), shortcut="Cmd+O"),
                SEPARATOR,
                MenuItem("Close", on_select=lambda: act("File ▸ Close"), shortcut="Cmd+W"),
            ),
        ),
        MenuItem(
            "Edit",
            submenu=Menu(
                MenuItem("Undo", on_select=lambda: act("Edit ▸ Undo"), shortcut="Cmd+Z"),
                MenuItem("Redo", on_select=lambda: act("Edit ▸ Redo"),
                         enabled=False, shortcut="Cmd+Y"),
                SEPARATOR,
                MenuItem("Cut", on_select=lambda: act("Edit ▸ Cut"), shortcut="Cmd+X"),
                MenuItem("Copy", on_select=lambda: act("Edit ▸ Copy"), shortcut="Cmd+C"),
                MenuItem("Paste", on_select=lambda: act("Edit ▸ Paste"),
                         enabled=lambda: state["clipboard"], shortcut="Cmd+V"),
            ),
        ),
        MenuItem(
            "View",
            submenu=Menu(
                MenuItem(
                    "Word Wrap",
                    on_select=lambda: (state.__setitem__("wrap", not state["wrap"]),
                                       act("View ▸ Word Wrap"))[-1],
                    checked=lambda: state["wrap"],
                ),
                SEPARATOR,
                MenuItem(
                    "Appearance",
                    submenu=Menu(
                        MenuItem("Light", on_select=lambda: act("Appearance ▸ Light")),
                        MenuItem("Dark", on_select=lambda: act("Appearance ▸ Dark")),
                    ),
                ),
            ),
        ),
    )

    def context_menu() -> Menu:
        return Menu(
            MenuItem("Cut", on_select=lambda: act("Context ▸ Cut")),
            MenuItem("Copy", on_select=lambda: act("Context ▸ Copy")),
            MenuItem("Paste", on_select=lambda: act("Context ▸ Paste"),
                     enabled=lambda: state["clipboard"]),
            SEPARATOR,
            MenuItem("Select All", on_select=lambda: act("Context ▸ Select All")),
        )

    heading = lambda text: Label(text, BOLD)  # noqa: E731
    scroller = ScrollView(
        [
            (heading("Menu bar — native OS bar on GUI, in-window strip on TUI"), 1),
            (MenuBar(menu_model), "content"),
            (heading("Custom enable condition"), 1),
            (clip, 1),
            (heading("Context menu"), 1),
            (MenuPlayground(context_menu, act), 6),
        ],
        gap=1,
    )
    return VSplit(
        Item(scroller, weight=1, hints={"surface": "content"}),
        Item(status, size=1),
        gap=1,
    )


def build_messagebox_page(panel: Panel) -> VSplit:
    # A MessageBox is a modal layer (the same shadow + dim_below intent the
    # dialog page uses), sized to its content and reporting the chosen button.
    # Pick a scenario and press enter; the result lands in the status line.
    status = Label("Pick a dialog, press enter", DIM)

    def result(prefix):
        return lambda b: setattr(status, "text", f"{prefix} → {b}")

    def show(_index: int, label: str) -> None:
        if label.startswith("Alert"):
            show_message_box(
                panel, "Your changes have been saved.", title="Saved",
                buttons=("OK",), icon="info", on_result=result("Alert"),
            )
        elif label.startswith("Confirm"):
            show_message_box(
                panel, "Delete the selected file?\nThis action cannot be undone.",
                title="Confirm delete", buttons=("Delete", "Cancel"),
                icon="warning", default=1, on_result=result("Confirm"),
            )
        elif label.startswith("Three"):
            show_message_box(
                panel, "Save changes before closing?", title="Unsaved changes",
                buttons=("Save", "Don't Save", "Cancel"),
                icon="warning", on_result=result("Choice"),
            )
        else:
            show_message_box(
                panel, "Could not open the file.\nCheck that it still exists.",
                title="Error", buttons=("OK",), icon="error", on_result=result("Error"),
            )

    listview = ListView(
        ["Alert (1 button)", "Confirm (2 buttons)", "Three buttons", "Error"],
        on_select=show,
    )
    explainer = TextBlock(
        "show_message_box pushes a modal layer over the Panel.\n"
        "\n"
        "  · ←/→ or tab move between buttons\n"
        "  · enter / space activate the focused button\n"
        "  · escape picks the cancel (last) button\n"
        "  · a click activates a button directly\n"
        "\n"
        "GUI raises it with a real drop shadow over a dimmed page; TUI falls\n"
        "back to draw order — one modal intent, two fidelities. The chosen\n"
        "button label is reported back through on_result.",
    )
    return VSplit(
        Item(Label("Modal alert / confirm dialogs, reported to the status line", DIM), size=1),
        Item(
            HSplit(
                Item(listview, size=24),
                Item(explainer, weight=1, hints={"surface": "content"}),
                gap=2,
            ),
            weight=1,
        ),
        Item(status, size=1),
        gap=1,
    )


def build_drawer_page(panel: Panel) -> VSplit:
    # A Drawer slides in from a screen edge as a Panel layer, hosting an
    # arbitrary content widget. One intent (show_drawer with a side), resolved
    # per backend: GUI slides it in over a dimmed page with a drop shadow, TUI
    # shows it at once and separates it by the surface background. Escape (or a
    # click on the dimmed scrim) closes it; tab cycles the controls inside.
    status = Label("Pick an edge, press enter — esc / scrim-click closes the drawer", DIM)

    def open_drawer(_index: int, label: str) -> None:
        side = label.split()[-1].lower()  # "Slide in from left" -> "left"

        # The drawer hosts a small form built from ordinary widgets; it is fully
        # interactive (tab moves focus, the button closes the drawer itself).
        controls = ScrollView(
            [
                (Label(f"This drawer is anchored to the {side} edge.", DIM), 1),
                (Label("Filters", BOLD), 1),
                (Checkbox("Show hidden files", checked=True), 1),
                (Checkbox("Follow symlinks"), 1),
                (Label("Sort by", BOLD), 1),
                (RadioGroup(["Name", "Size", "Modified"], selected=0), "content"),
                (Label("Search", BOLD), 1),
                (TextEdit("", on_submit=lambda s: setattr(status, "text", f"Search → {s!r}")), "content"),
                (Button("Close drawer", on_click=lambda: drawer.close()), "content"),
            ],
            gap=1,
        )
        drawer = show_drawer(
            panel,
            controls,
            side=side,
            title=f"{side.capitalize()} drawer",
            on_close=lambda: setattr(status, "text", f"{side.capitalize()} drawer closed"),
        )
        status.text = f"{side.capitalize()} drawer open — tab to move, esc to close"

    listview = ListView(
        [
            "Slide in from left",
            "Slide in from right",
            "Slide in from top",
            "Slide in from bottom",
        ],
        on_select=open_drawer,
    )
    explainer = TextBlock(
        "show_drawer(panel, content, side=...) pushes a layer anchored to a\n"
        "screen edge and slides it in.\n"
        "\n"
        "  · side: left / right / top / bottom\n"
        "  · the drawer fills the whole cross-axis (full height for a side\n"
        "    drawer, full width for a top/bottom drawer)\n"
        "  · it hosts any content widget — here a small filters form\n"
        "  · esc, or a click on the dimmed scrim, closes it\n"
        "  · tab / shift+tab cycle the controls inside (modal focus root)\n"
        "\n"
        "GUI slides it in over a dimmed page with a drop shadow; TUI shows it\n"
        "at once and leans on the surface background for separation — one\n"
        "intent, every backend. The page never branches on the capability.",
    )
    return VSplit(
        Item(Label("Edge drawers slide in over the page — one intent, every backend", DIM), size=1),
        Item(
            HSplit(
                Item(listview, size=24),
                Item(explainer, weight=1, hints={"surface": "content"}),
                gap=2,
            ),
            weight=1,
        ),
        Item(status, size=1),
        gap=1,
    )


def build_progress_page(panel: Panel) -> VSplit:
    # Determinate ProgressBars (a value along a track) next to indeterminate
    # BusyIndicators (motion only). The spinners turn on their own on GUI (the
    # animation capability drives per-frame ticks) and advance on each render on
    # TUI — one widget, resolved in the Panel layer. A "Step" button advances a
    # live bar; its percentage rides in a sibling Label (the bar itself is
    # value-only, like ScrollBar).
    state = {"value": 0.35}
    live = ProgressBar(state["value"])
    readout = Label("35 %", BOLD)

    def step() -> None:
        state["value"] = 0.0 if state["value"] >= 1.0 else min(1.0, state["value"] + 0.1)
        live.value = state["value"]
        readout.text = f"{round(state['value'] * 100):d} %"

    def bar_row(caption: str, value: float) -> Item:
        bar = ProgressBar(value)
        return Item(
            HSplit(
                Item(Label(caption), size=14),
                Item(bar, weight=1),
                Item(Label(f"{round(value * 100):d} %", DIM), size=6),
                gap=1,
            ),
            size=1,
        )

    return VSplit(
        Item(Label("Determinate progress vs. indeterminate activity", DIM), size=1),
        Item(Label("Determinate — ProgressBar fills a known fraction", BOLD), size=1),
        bar_row("Downloading", 0.0),
        bar_row("Indexing", 0.25),
        bar_row("Building", 0.6),
        bar_row("Finishing", 1.0),
        Item(Label(""), size=1),
        Item(Label("Live — press the button to advance", BOLD), size=1),
        Item(
            HSplit(
                Item(Button("Step", style=BUTTON_FACE, on_click=step), size="content", align="center"),
                Item(live, weight=1),
                Item(readout, size=6),
                gap=1,
            ),
            size=1,
        ),
        Item(Label(""), size=1),
        Item(Label("Indeterminate — BusyIndicator shows motion only", BOLD), size=1),
        Item(
            HSplit(
                Item(BusyIndicator("Loading…"), size="content"),
                Item(BusyIndicator("Syncing", fps=6), size="content"),
                Item(BusyIndicator(), size="content"),
                Item(Label("(spinners turn on GUI; tick-advance on TUI)", DIM), weight=1),
                gap=3,
            ),
            size=1,
        ),
        Item(Label(""), weight=1),
        gap=1,
    )


def build_splitter_page(panel: Panel) -> VSplit:
    # A Splitter hosts two panes and a draggable handle: the interactive form of
    # a layout divider. The outer split is horizontal (drag the vertical handle
    # left/right); its right pane is itself a vertical Splitter (drag the
    # horizontal handle up/down). Children keep their own focus and events — Tab
    # descends, clicks route to the pane under the pointer — so nesting is free.
    status = Label("Drag a handle to resize · click a list · tab moves focus", DIM)

    def on_select(which):
        return lambda i, t: setattr(status, "text", f"{which}: {t}")

    left = ListView([f"Files {i:02d}" for i in range(30)], on_select=on_select("Left"))
    top_right = ListView(
        [f"Preview line {i:02d}" for i in range(30)], on_select=on_select("Top-right")
    )
    bottom_right = TextBlock(
        "This pane is the second child of a vertical Splitter.\n"
        "\n"
        "  · the outer Splitter divides left | right\n"
        "  · the right side is a Splitter dividing top / bottom\n"
        "  · drag either handle; the panes re-apportion live\n"
        "  · neither pane shrinks past its minimum\n"
        "\n"
        "A dual-pane file manager is exactly this intent.",
    )
    right = Splitter(
        top_right, bottom_right, orientation="vertical", fraction=0.5,
        min_first=4, min_second=4,
    )
    split = Splitter(left, right, orientation="horizontal", fraction=0.4, min_first=10, min_second=16)
    return VSplit(
        Item(split, weight=1, hints={"surface": "content"}),
        Item(status, size=1),
        gap=1,
    )


def build_combo_page(panel: Panel) -> VSplit:
    # A ComboBox is an editable DropDown: type to filter the floating list, or
    # enter free text. It is composed from an embedded TextEdit (cursor, IME)
    # and the same push_layer popup the DropDown uses; the page never branches
    # on the backend.
    status = Label("Type to filter · ↑/↓ choose · enter commit · esc cancel", DIM)

    fruits = [
        "Apple", "Apricot", "Avocado", "Banana", "Blueberry", "Cherry",
        "Date", "Elderberry", "Fig", "Grape", "Kiwi", "Lemon", "Mango",
        "Nectarine", "Orange", "Papaya", "Peach", "Pear", "Plum", "Raspberry",
    ]
    fixed = DropDown(
        ["Red", "Green", "Blue"],
        on_change=lambda i, t: setattr(status, "text", f"DropDown (read-only) → {t}"),
    )
    editable = ComboBox(
        fruits, text="",
        on_change=lambda s: setattr(status, "text", f"ComboBox → {s!r}"),
        width=24,
    )
    custom = ComboBox(
        ["localhost", "127.0.0.1", "0.0.0.0"], text="",
        on_change=lambda s: setattr(status, "text", f"Host → {s!r}"),
        width=24, allow_custom=True,
    )

    heading = lambda text: Label(text, BOLD)  # noqa: E731
    scroller = ScrollView(
        [
            (heading("Read-only DropDown — pick one of a fixed set"), 1),
            (fixed, "content"),
            (heading("Editable ComboBox — type to filter the list"), 1),
            (editable, "content"),
            (Label("Try typing 'ap' or 'berry' to narrow the list", DIM), 1),
            (heading("Free-text ComboBox — enter accepts custom text"), 1),
            (custom, "content"),
            (Label("Type a host not in the list and press enter", DIM), 1),
        ],
        gap=1,
    )
    return VSplit(
        Item(scroller, weight=1, hints={"surface": "content"}),
        Item(status, size=1),
        gap=1,
    )


def _drag_demo_files() -> list[str]:
    """Create a few small real files once and return their paths. Real files
    make the GUI drag actually drop usable content into the target app."""
    import tempfile

    folder = os.path.join(tempfile.gettempdir(), "puikit_drag_demo")
    os.makedirs(folder, exist_ok=True)
    specs = {
        "puikit-notes.txt": "Dragged out of the PuiKit demo catalog.\n",
        "puikit-data.csv": "name,value\nalpha,1\nbeta,2\n",
        "puikit-readme.md": "# PuiKit\nDrag me onto another app.\n",
    }
    paths = []
    for name, body in specs.items():
        path = os.path.join(folder, name)
        if not os.path.exists(path):
            with open(path, "w") as handle:
                handle.write(body)
        paths.append(path)
    return paths


class _DragWell(Widget):
    """A box you drag *from* to export files. On a drag gesture it issues one
    intent — panel.begin_file_drag(paths, event) — and lets the Panel resolve
    it: a native OS drag on GUI, a clipboard copy of the paths on TUI. The well
    never branches on the backend; it just reports what happened to the status
    line."""

    focusable = True

    def __init__(self, panel, title, paths, status):
        self.panel = panel
        self.title = title
        self.paths = paths
        self.status = status
        self._armed = False  # a press arms; the first drag after it fires once

    def measure(self, ctx, axis, available):
        # Height is content-driven: top border, title, hint, a blank row, one
        # row per file, a blank pad, then the bottom border. The box never
        # clips its own file list regardless of how many paths it carries.
        if axis == "y":
            h = float(len(self.paths) + 6)
            return SizeRequest(min=h, preferred=h, max=h)
        return SizeRequest(min=0.0, preferred=0.0, max=0.0)

    def draw(self, ctx):
        # An open/closed hand signals the well is something to drag from: a grab
        # hand while hovering, a closed (grabbing) hand once armed by a press.
        # Gate BOTH on hover: a cursor request only applies while the pointer is
        # over this widget (the topmost hovered widget wins). Requesting it
        # unconditionally while armed would pin "grabbing" over the whole window,
        # so it would never reset when the pointer moved off the well.
        if ctx.hovered:
            ctx.set_cursor("grabbing" if self._armed else "grab")
        ctx.draw_box(0, 0, ctx.width, ctx.height, hints={"fill": True})
        inner = max(0, ctx.width - 4)
        ctx.draw_text(2, 1, self.title[:inner], BOLD)
        hint = "drag from here ⇢" + ("  (focused)" if ctx.focused else "")
        ctx.draw_text(2, 2, hint[:inner], DIM)
        for i, path in enumerate(self.paths):
            ctx.draw_text(2, 4 + i, ("• " + os.path.basename(path))[:inner])

    def handle_event(self, event):
        # A press arms; the first drag while held fires the OS drag once; a
        # release without dragging disarms. Arming on the press (not a completed
        # click) is what lets a single, natural press-and-drag start the drag —
        # arming on click required an extra click first.
        if event.type is EventType.MOUSE_DOWN and event.button == "left":
            self._armed = True
            return True
        if event.type is EventType.MOUSE_UP and event.button == "left":
            self._armed = False
            return True
        if event.type is EventType.MOUSE_DRAG and event.button == "left" and self._armed:
            self._armed = False
            n = len(self.paths)

            def done(op: str) -> None:
                # The app decides what an operation means. PuiKit never deletes
                # files: on a "move" the file manager would remove the originals
                # here — the demo only reports what the user chose.
                if op == "move":
                    self.status.text = f"Drag ended: MOVE of {n} file(s) (app would delete originals)"
                elif op == "none":
                    self.status.text = "Drag cancelled — nothing dropped"
                else:
                    self.status.text = f"Drag ended: {op.upper()} of {n} file(s)"

            started = self.panel.begin_file_drag(
                self.paths, event, operations=("copy", "move"), on_complete=done
            )
            if started:
                self.status.text = (
                    f"Dragging {n} file(s) — drop onto Finder/an editor "
                    "(hold a modifier to choose copy vs. move)"
                )
            elif self.panel.get_clipboard():
                self.status.text = (
                    f"No OS drag source in a terminal — copied {n} path(s) to "
                    "the clipboard; paste them into the target app"
                )
            return True
        return False


def build_drag_page(panel: Panel) -> VSplit:
    # Dragging files OUT to other apps is an OS-window capability: GUI-Desktop
    # owns a native view and can be an NSDraggingSource; a terminal app cannot,
    # so the Panel falls back to copying the paths to the clipboard. One intent
    # (panel.begin_file_drag), resolved per backend — see docs/drag_drop.md.
    paths = _drag_demo_files()
    status = Label(
        "Drag a well onto another app (GUI), or watch it copy paths to the "
        "clipboard (TUI)",
        DIM,
    )
    one = _DragWell(panel, "One file", paths[:1], status)
    many = _DragWell(panel, "Three files", paths, status)
    explainer = TextBlock(
        "panel.begin_file_drag(paths, event, operations, on_complete) —\n"
        "one intent, two fidelities:\n"
        "\n"
        "  · GUI-Desktop -> a real OS drag session (NSDraggingSource). "
        "Drop the files onto Finder, an editor, a chat.\n"
        "  · TUI / others -> no app can be an OS drag source inside a "
        "terminal, so the Panel copies the paths to the clipboard "
        "instead — paste them into the target.\n"
        "\n"
        "The wells offer copy AND move. PuiKit never deletes files: it "
        "reports the chosen operation through on_complete(op), and the app "
        "performs a move itself (the status line shows what it would do).\n"
        "\n"
        "The page never branches on the backend; it just asks to export "
        "files. Tab focuses a well; press and drag with the mouse to start.",
        wrap=True,
    )
    return VSplit(
        Item(Label("Drag files out to other apps — one intent, two fidelities", DIM), size=1),
        Item(
            HSplit(
                Item(
                    VSplit(
                        Item(one, size="content"),
                        Item(many, size="content"),
                        Item(Label(""), weight=1),
                        gap=1,
                    ),
                    size=30,
                ),
                Item(explainer, weight=1, hints={"surface": "content"}),
                gap=2,
            ),
            weight=1,
        ),
        Item(status, size=1),
        gap=1,
    )


# Each nav entry carries an emoji prefix: the same intent renders as a
# full-color glyph on GUI and as a (wide) text glyph on TUI — the shared
# wide-character accounting (puikit.text) keeps the labels column-aligned on
# both backends, so no page branches on the backend for its own name.
PAGES = [
    ("🏷️ Label", build_label_page),
    ("⌨️ Keys", build_keys_page),
    ("✂️ Truncate", build_truncate_page),
    ("🎛️ Widgets", build_widgets_page),
    ("🔽 ComboBox", build_combo_page),
    ("📊 Progress", build_progress_page),
    ("↔️ Splitter", build_splitter_page),
    ("📋 ListView", build_list_page),
    ("📜 LogView", build_log_page),
    ("📝 Markdown", build_markdown_page),
    ("🎚️ ScrollBar", build_scrollbar_page),
    ("🗂️ Tabs", build_tabs_page),
    ("🌲 Tree", build_tree_page),
    ("📑 Menu", build_menu_page),
    ("💬 MessageBox", build_messagebox_page),
    ("🚪 Drawer", build_drawer_page),
    ("🫳 Drag", build_drag_page),
    ("🎬 Animation", build_animation_page),
    ("🗂️ Layering", build_layer_page),
    ("📐 Layout", build_layout_page),
    ("📏 Intrinsic", build_intrinsic_page),
    ("🔤 Fonts", build_fonts_page),
    ("📜 Wrapping", build_wrap_page),
    ("🎨 Color", build_color_page),
    ("🖼️ Images", build_images_page),
    ("💧 Alpha", build_alpha_page),
    ("🌈 Blending", build_blending_page),
]


# --- application shell -----------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="PuiKit widget catalog")
    parser.add_argument("--backend", default="tui", help="backend name (tui, gui, memory)")
    parser.add_argument(
        "--font-size",
        type=float,
        default=None,
        help="base font size in points (GUI only; sets the base unit grid)",
    )
    args = parser.parse_args()

    kwargs = {}
    if args.font_size is not None and args.backend in ("gui", "macos", "windows", "win32"):
        kwargs["base_font"] = Font(size=args.font_size, monospace=True)
    backend = create_backend(args.backend, **kwargs)
    with backend:
        panel = Panel(backend)
        # The page host is itself a layout (LayoutView), not a coordinate
        # Container: each page is a Split swapped in via set_layout. Its margin
        # gives every page symmetric padding inside the content pane — declared,
        # not hand-placed (8px on GUI, 1 base unit on TUI).
        content = LayoutView(VSplit(), margin_px=8, margin_units=1)
        # A few device pixels of breathing room around the bar text on GUI (the
        # bar grows to fit via size="content"); collapses to nothing on TUI.
        # The title/status label styles are (re)set by apply_theme below.
        title = Label(" PuiKit Demo Catalog", BOLD, padding_px=4)
        status = Label("", padding_px=4)
        # Live curses color-pair usage at the right of the status bar (TUI only;
        # the GUI backend has no color_pair_stats, so it renders nothing).
        pair_meter = PairMeter()

        # Mutable shell state shared by the page/theme switchers.
        page_index = 0
        theme_index = 0

        def update_status() -> None:
            name = PAGES[page_index][0]
            theme_name = DEMO_THEMES[theme_index][0]
            status.text = (
                f" {name} — tab: focus · d: dialog · "
                f"t: theme ({theme_name}) · q: quit"
            )

        def show_page(index: int, name: str) -> None:
            nonlocal page_index
            page_index = index
            content.set_layout(PAGES[index][1](panel))
            update_status()

        def apply_theme(index: int) -> None:
            nonlocal theme_index
            theme_index = index % len(DEMO_THEMES)
            theme = DEMO_THEMES[theme_index][1]
            # One assignment recolors every widget: controls read the active
            # theme at draw time, and the chrome panes' surface roles re-resolve
            # to the new backgrounds on the next render.
            panel.theme = theme
            # A Label carries a fixed Style (it paints its own glyph cells), so
            # the two chrome labels are refreshed to track the theme: the title
            # in the theme text color, the status bar in the accent's contrast
            # color over the accent surface.
            title.style = Style(fg=theme.text, attr=TextAttribute.BOLD)
            status.style = Style(fg=_on_accent_fg(theme.accent), bg=theme.surfaces["status"])
            pair_meter.style = status.style
            update_status()

        nav = ListView([name for name, _ in PAGES], on_change=show_page)

        # The pair meter is a curses-only diagnostic; only give it a slot when
        # the backend actually reports color-pair stats, so GUI doesn't reserve
        # dead status-bar width for it.
        if hasattr(backend, "color_pair_stats"):
            status_bar = HSplit(
                Item(status, weight=1, hints={"surface": "status"}),
                Item(pair_meter, size="content", hints={"surface": "status"}),
            )
            status_item = Item(status_bar, size="content")
        else:
            status_item = Item(status, size="content", hints={"surface": "status"})

        panel.set_layout(
            VSplit(
                Item(title, size="content", hints={"surface": "header"}),
                Item(
                    HSplit(
                        Item(nav, size=18, hints={"min": 12, "surface": "sidebar"}),
                        Item(content, weight=1, hints={"min_px": 300, "surface": "content"}),
                        # A GUI hairline between the nav and the body (zero base
                        # unit cost on TUI, where the sidebar/content surface
                        # contrast does the separating instead).
                        divider="subtle",
                    )
                ),
                status_item,
            ),
        )
        apply_theme(0)

        def close_dialog() -> None:
            panel.pop_layer()

        def open_dialog() -> None:
            dialog = DemoDialog(close_dialog)
            panel.push_layer(
                dialog,
                z=10,
                hints={"shadow": True, "dim_below": False, "w": 36, "h": 7},
            )
            # GUI: fades in over 200ms; TUI: appears immediately.
            panel.animate(dialog, hints={"transition": "fade", "duration_ms": 200})

        def on_event(event) -> None:
            # Pointer movement only updates hover state; re-render so controls
            # under the cursor light up (GUI emits these; TUI does not).
            if event.type is EventType.MOUSE_MOVE:
                panel.dispatch_event(event)
                panel.render()
                return
            # Focused widget gets the event first; a modal dialog takes it
            # exclusively. Tab / Shift+Tab are consumed here too — the Panel
            # walks the whole focus tree (nav -> the page's widgets and back),
            # so the app no longer toggles focus by hand.
            if panel.dispatch_event(event):
                panel.render()
                return
            if event.type is EventType.KEY:
                if event.key in ("q", "escape"):
                    backend.quit()
                    return
                if event.key == "d":
                    open_dialog()
                    panel.render()
                    return
                if event.key == "t":
                    # Cycle the active theme; one re-render recolors the whole
                    # catalog (chrome + every page widget) from the new palette.
                    apply_theme(theme_index + 1)
                    panel.render()
                    return
                if event.key and event.key.isdigit():
                    index = int(event.key) - 1
                    if 0 <= index < len(PAGES):
                        nav.selected = index
                        show_page(index, PAGES[index][0])
                        panel.render()
                        return
            if event.type is EventType.RESIZE:
                panel.render()

        show_page(0, PAGES[0][0])
        panel.render()
        backend.run_event_loop(on_event)


if __name__ == "__main__":
    main()
