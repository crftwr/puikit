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
    VSplit,
)
from puikit.backends import create_backend
from puikit.widgets import (
    Button,
    Checkbox,
    Container,
    DropDown,
    ImageView,
    Label,
    LayoutView,
    ListView,
    MenuBar,
    RadioGroup,
    ScrollBar,
    ScrollView,
    Tabs,
    TextBlock,
    TextEdit,
    TreeNode,
    TreeView,
    Widget,
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
CARD_BG = (45, 45, 45)
STATUS_BG = (0, 122, 204)    # status bar      #007ACC (accent)
STATUS_FG = Style(fg=(255, 255, 255), bg=STATUS_BG)
# Button face: a lighter fill so buttons read as raised against a footer bar.
BUTTON_FACE = Style(fg=(232, 234, 240), bg=(74, 88, 124))


class DemoDialog(Widget):
    """A modal dialog layer. Pushed with shadow/dim_below hints: GUI backends
    render a drop shadow and a translucent dim overlay, TUI approximates the
    dim with dark attributes and skips the shadow."""

    def __init__(self, on_close):
        self.on_close = on_close

    def draw(self, ctx):
        ctx.draw_box(0, 0, ctx.width, ctx.height, hints={"fill": True})
        ctx.draw_icon(2, 1, "info")
        ctx.draw_text(5, 1, "A layered dialog", BOLD)
        ctx.draw_text(2, 3, "The content below is dimmed.")
        ctx.draw_text(2, ctx.height - 2, "esc / enter: close", DIM)

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
        ctx.draw_box(0, 0, ctx.width, ctx.height, hints={"fill": True})
        # Content sits at x=2; keep every line inside the right border (at
        # column width-1) with a one-column pad, so text never overwrites the
        # frame. The Panel clips to the full rect — the border included — so
        # the inset is the card's own responsibility.
        inner = max(0, ctx.width - 2 - 2)

        def line(x, y, text, style=None):
            avail = max(0, inner - (x - 2))
            ctx.draw_text(x, y, text[:avail], style) if style else ctx.draw_text(x, y, text[:avail])

        ctx.draw_icon(2, 1, "info")
        line(5, 1, self.title, BOLD)
        for i, note in enumerate(self.notes):
            line(2, 3 + i, note)
        hint = "esc / enter: close top layer"
        if self.on_open is not None:
            hint = "o: open another, " + hint
        line(2, ctx.height - 2, hint, DIM)

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

    heading = lambda text: Label(text, BOLD)  # noqa: E731 - tiny local helper
    scroller = ScrollView(
        [
            (heading("Check boxes"), 1),
            (feature, 1),
            (hidden, 1),
            (heading("Radio buttons"), 1),
            (size, 3),
            (heading("Drop-down (opens a floating popup over the page)"), 1),
            (color, "content"),
            (heading("Text edit"), 1),
            (name, "content"),
            (heading("Button"), 1),
            (action, "content"),
            (heading("Static text — single line (Label)"), 1),
            (Label("The quick brown fox jumps over the lazy dog."), 1),
            (heading("Static text — multi line (TextBlock)"), 1),
            (
                TextBlock(
                    "TextBlock reserves one row per line:\n"
                    "  · it never reflows on the backend,\n"
                    "  · it just clips at the pane edge,\n"
                    "  · and the ScrollView scrolls past it.",
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


def build_list_page(panel: Panel) -> VSplit:
    items = [f"Item {i:03d}" for i in range(50)]
    status = Label("Use arrows / page keys; enter to select", DIM)
    listview = ListView(
        items, on_select=lambda i, text: setattr(status, "text", f"Selected: {text}")
    )
    return VSplit(
        # List capped to 30 base units wide; it flexes to fill the height.
        Item(HSplit(Item(listview, size=30), Item(Label(""), weight=1)), weight=1),
        Item(status, size=1),
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
        # The card's pane background comes from its slot's "bg" hint, which
        # children inherit; the border just frames it.
        ctx.draw_border(Style(fg=(36, 114, 200)))
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
                        Item(target, size=12, hints={"bg": CARD_BG}),
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
        if ctx.height >= 5:
            ctx.draw_text(1, 0, self.name, Style(fg=self.color, attr=TextAttribute.BOLD))
            ctx.draw_text(1, 2, units_line)
            ctx.draw_text(1, 3, px_line)
            if self.note:
                ctx.draw_text(1, 4, self.note, Style(attr=TextAttribute.DIM))
        else:
            line = f"{self.name}  {units_line} {px_line}" + (
                f"  ({self.note})" if self.note else ""
            )
            ctx.draw_text(1, 0, line, Style(fg=self.color))


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
        # font=None -> the base monospaced grid font (unchanged everywhere).
        row("Base grid font (font=None) — monospaced, column-aligned"),
        # Font() is the proportional system UI font on GUI; mono on TUI.
        row("Proportional UI font — flows by natural advances", Style(font=Font())),
        row("Monospaced UI font — fixed advance", Style(font=Font(monospace=True))),
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

    scroller = ScrollView(
        [
            (heading("Word wrap — base grid font (column-aligned)"), 1),
            (TextBlock(en, wrap=True), "content"),
            (heading("Word wrap — proportional font (GUI flows by advances)"), 1),
            # Same text, same width: GUI wraps at different points than the
            # monospaced block above because the glyph advances differ; TUI
            # folds the font to the grid and wraps identically. One intent.
            (TextBlock(en, style=PROP, wrap=True), "content"),
            (heading("Japanese — wraps between glyphs (no spaces)"), 1),
            (TextBlock(ja, wrap=True), "content"),
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


class Swatch(Widget):
    """A filled color block labeled with its RGB. The app passes one RGB
    intent; GUI paints the exact channel values, TUI approximates them through
    the xterm-256 palette — the same color, two fidelities. No swatch branches
    on the backend; the fill carries the difference."""

    def __init__(self, color: tuple[int, int, int], name: str = ""):
        self.color = color
        self.name = name

    def draw(self, ctx) -> None:
        ctx.fill_rect(0, 0, ctx.width, ctx.height, Style(bg=self.color))
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


def build_color_page(panel: Panel) -> VSplit:
    # One RGB intent per swatch; GUI paints exact channels, TUI snaps each to
    # the nearest xterm-256 cell — same color, two fidelities (curses_backend
    # ._xterm256_index). A 2D color table shows the difference plainly: hue
    # sweeps across columns, brightness down rows. The grid reads as a smooth
    # field on GUI and as discrete xterm-256 bands on TUI, where the palette
    # quantizes it. The grid is built from the layout system — a VSplit of
    # HSplit rows — so it re-resolves to fill the pane on every resize.
    cols, rows = 32, 12

    def cell(cx: int, cy: int) -> Swatch:
        hue = cx / cols
        value = 1.0 - 0.9 * (cy / (rows - 1))  # bright at top, dark at bottom
        return Swatch(_hsv_rgb(hue, 1.0, value))

    grid = VSplit(
        *[
            Item(HSplit(*[Item(cell(cx, cy), weight=1) for cx in range(cols)]), weight=1)
            for cy in range(rows)
        ]
    )
    return VSplit(
        Item(Label("GUI paints exact RGB; TUI snaps to the xterm-256 palette", DIM), size=1),
        # Named palette: each swatch is wide enough to label with its RGB.
        Item(HSplit(*[Item(Swatch(c, n), weight=1) for n, c in PALETTE], gap=1), size=4),
        Item(Label("Hue x brightness table (smooth on GUI, banded on TUI):", DIM), size=1),
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
        "approximates dim with attributes, and skips shadows.\n"
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
    # One image intent, two fidelities — and five object-fits, all resolved by
    # the layout, never by the page. GUI renders the real picture (scaled,
    # letterboxed, or cropped per fit); TUI has no `images` capability, so the
    # Panel layer stamps each image's alt emoji in its place. The page never
    # branches on the backend. The image-faced Button clicks like any button.
    # The asset is a 16:9 scene, so the fits read distinctly on GUI.
    status = Label("Resize the window to watch each fit re-resolve", DIM)
    plays = {"n": 0}

    def on_play() -> None:
        plays["n"] += 1
        status.text = f"Button clicked ×{plays['n']}"

    scene = os.path.join(ASSETS, "scene.png")
    play = os.path.join(ASSETS, "play.png")

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
        # The one Button class, three faces: an image-only tile, and an
        # icon+label action button sized to its content.
        Item(
            VSplit(
                Item(Label("Button: image / image+text", BOLD), size=1),
                Item(
                    HSplit(Item(Button(image=play, on_click=on_play, alt="▶"), size=8), Item(Label(""), weight=1)),
                    size=4,
                ),
                Item(
                    HSplit(
                        Item(Button("Play", image=play, on_click=on_play, alt="▶"), size="content"),
                        Item(Label(""), weight=1),
                    ),
                    size=3,
                ),
                Item(Label(""), weight=1),
                gap=1,
            ),
            weight=1,
        ),
        gap=2,
    )

    return VSplit(
        Item(Label("ImageView object-fits — GUI draws them, TUI shows alt emoji", DIM), size=1),
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


# Each nav entry carries an emoji prefix: the same intent renders as a
# full-color glyph on GUI and as a (wide) text glyph on TUI — the shared
# wide-character accounting (puikit.text) keeps the labels column-aligned on
# both backends, so no page branches on the backend for its own name.
PAGES = [
    ("🏷️ Label", build_label_page),
    ("🎛️ Widgets", build_widgets_page),
    ("📋 ListView", build_list_page),
    ("🎚️ ScrollBar", build_scrollbar_page),
    ("🗂️ Tabs", build_tabs_page),
    ("🌲 Tree", build_tree_page),
    ("📑 Menu", build_menu_page),
    ("💬 MessageBox", build_messagebox_page),
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
    if args.font_size is not None and args.backend in ("gui", "macos"):
        kwargs["base_font"] = Font(size=args.font_size, monospace=True)
    backend = create_backend(args.backend, **kwargs)
    with backend:
        panel = Panel(backend)
        # The page host is itself a layout (LayoutView), not a coordinate
        # Container: each page is a Split swapped in via set_layout. Its margin
        # gives every page symmetric padding inside the content pane — declared,
        # not hand-placed (8px on GUI, 1 base unit on TUI).
        content = LayoutView(VSplit(), margin_px=8, margin_units=1)
        status = Label("", STATUS_FG)

        def show_page(index: int, name: str) -> None:
            content.set_layout(PAGES[index][1](panel))
            status.text = f" {name} — tab: move focus, d: dialog, q: quit"

        nav = ListView([name for name, _ in PAGES], on_change=show_page)

        panel.set_layout(
            VSplit(
                Item(Label(" PuiKit Demo Catalog", BOLD), size=1, hints={"bg": TITLE_BG}),
                Item(
                    HSplit(
                        Item(nav, size=18, hints={"min": 12, "bg": NAV_BG}),
                        Item(content, weight=1, hints={"min_px": 300, "bg": CONTENT_BG}),
                    )
                ),
                Item(status, size=1, hints={"bg": STATUS_BG}),
            ),
            # GUI: inset the whole layout 4px from the window frame. Edge panes
            # bleed their backgrounds across the margin, so it reads as padding,
            # not a bare frame. Ignored on TUI (a px margin would cost base units).
            margin_px=4,
        )

        def close_dialog() -> None:
            panel.pop_layer()

        def open_dialog() -> None:
            dialog = DemoDialog(close_dialog)
            panel.push_layer(
                dialog,
                z=10,
                hints={"shadow": True, "dim_below": True, "w": 36, "h": 7},
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
