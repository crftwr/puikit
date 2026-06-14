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
    Container,
    Label,
    LayoutView,
    ListView,
    ScrollBar,
    TextBlock,
    Widget,
)

DIM = Style(attr=TextAttribute.DIM)
BOLD = Style(attr=TextAttribute.BOLD)

# Pane background colors: title/status bars, navigation, and content are
# visually separated. GUI renders the exact RGB; TUI approximates via the
# xterm-256 palette.
TITLE_BG = (52, 62, 88)
NAV_BG = (38, 44, 60)
CONTENT_BG = (26, 28, 34)
CARD_BG = (42, 46, 56)
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


PAGES = [
    ("Label", build_label_page),
    ("ListView", build_list_page),
    ("ScrollBar", build_scrollbar_page),
    ("Animation", build_animation_page),
    ("Layout", build_layout_page),
    ("Intrinsic", build_intrinsic_page),
    ("Fonts", build_fonts_page),
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
        status = Label("", DIM)

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
                Item(status, size=1, hints={"bg": TITLE_BG}),
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
