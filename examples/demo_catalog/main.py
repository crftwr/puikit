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

Keys: up/down in the nav switch pages, tab moves focus between the nav and
the page, 1..9 jump to a page, d opens a layered dialog, q quits.

    python examples/demo_catalog/main.py                  # TUI
    python examples/demo_catalog/main.py --backend gui    # macOS GUI
"""

import argparse
import colorsys
import os

from puikit import (
    EventType,
    Font,
    FontSlant,
    FontWeight,
    HSplit,
    Item,
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
    ImageButton,
    ImageView,
    Label,
    LayoutView,
    ListView,
    RadioGroup,
    ScrollBar,
    ScrollView,
    TextBlock,
    TextEdit,
    Widget,
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
    # (the ScrollView cycles its focusable children and scrolls them into view);
    # space / enter activates, arrows move within the focused control.
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
            (color, 1),
            (heading("Text edit"), 1),
            (name, 1),
            (heading("Button"), 1),
            (action, 1),
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
    def row(label: str, style: Style = Style(), size: float = 1) -> Item:
        return Item(Label(label, style), size=size)

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
        row("Named family: Georgia, 16pt", Style(font=Font(family="Georgia", size=16)), size=2),
        # Decorative size: it needs layout room to fit (size=3), else it clips
        # at the pane edge like any overflow — size never reshapes implicitly.
        row("Big Title — sized text", Style(font=Font(size=28, weight=FontWeight.SEMI_BOLD)), size=3),
        Item(Label(""), weight=1),
        gap=0,
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
    # Panel layer frames each (fit-shaped) footprint with its alt text. The
    # page never branches on the backend. The ImageButton clicks like any
    # button. The asset is a 16:9 scene, so the fits read distinctly.
    status = Label("Resize the window to watch each fit re-resolve", DIM)
    plays = {"n": 0}

    def on_play() -> None:
        plays["n"] += 1
        status.text = f"ImageButton clicked ×{plays['n']}"

    scene = os.path.join(ASSETS, "scene.png")
    play = os.path.join(ASSETS, "play.png")

    def fit_cell(title: str, fit: str) -> Item:
        # Each cell hands the image the same square-ish pane; the fit decides
        # how the 16:9 scene relates to it (stretch / letterbox / crop).
        return Item(
            VSplit(
                Item(Label(title, BOLD), size=1),
                Item(ImageView(scene, fit=fit, alt=f"scene ({fit})"), weight=1),
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
                Item(ImageView(scene, fit="width", alt="scene (width)"), size="content"),
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
                        Item(ImageView(scene, fit="height", alt="scene (height)"), size="content"),
                        Item(Label(""), weight=1),
                    ),
                    weight=1,
                ),
                gap=0,
            ),
            weight=1,
        ),
        # An image-faced button, sized to a fixed square card.
        Item(
            VSplit(
                Item(Label("ImageButton", BOLD), size=1),
                Item(
                    HSplit(Item(ImageButton(play, on_click=on_play, alt="▶"), size=8), Item(Label(""), weight=1)),
                    size=4,
                ),
                Item(Label(""), weight=1),
                gap=0,
            ),
            weight=1,
        ),
        gap=2,
    )

    return VSplit(
        Item(Label("ImageView object-fits — GUI draws them, TUI frames them", DIM), size=1),
        Item(box_fits, weight=1, hints={"surface": "content"}),
        Item(aspect_fits, weight=1, hints={"surface": "sidebar"}),
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
    ("🎬 Animation", build_animation_page),
    ("🗂️ Layering", build_layer_page),
    ("📐 Layout", build_layout_page),
    ("📏 Intrinsic", build_intrinsic_page),
    ("🔤 Fonts", build_fonts_page),
    ("🎨 Color", build_color_page),
    ("🖼️ Images", build_images_page),
]


# --- application shell -----------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="PuiKit widget catalog")
    parser.add_argument("--backend", default="tui", help="backend name (tui, gui, memory)")
    args = parser.parse_args()

    backend = create_backend(args.backend)
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
            status.text = f" {name} — tab: focus page/nav, d: dialog, q: quit"

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
            # Focused widget (nav or page) gets the event first; a modal
            # dialog takes it exclusively.
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
                if event.key == "tab":
                    panel.focus(content if panel.focused is nav else nav)
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
