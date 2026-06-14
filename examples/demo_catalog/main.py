"""PuiKit widget catalog with a left navigation pane.

The pages are listed in the navigation on the left; moving the selection
(up/down or mouse) switches the content pane on the right. The whole shell
is built with the layout system, so it follows window resizes; pages are
Containers, so their widgets are clipped and animate as a tree. The Layout
page nests the same layout system inside a page (LayoutView): one split
definition snapped to cells on TUI and resolved at pixel granularity on GUI,
with surface roles and dividers.

Keys: up/down in the nav switch pages, tab moves focus between the nav and
the page, 1..9 jump to a page, d opens a layered dialog, q quits.

    python examples/demo_catalog/main.py                  # TUI
    python examples/demo_catalog/main.py --backend gui    # macOS GUI
"""

import argparse

from puikit import EventType, HSplit, Item, Panel, Style, TextAttribute, VSplit
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


def build_label_page(page: Container, panel: Panel) -> None:
    page.add(Label("Plain label"), x=2, y=1, w=40, h=1)
    page.add(Label("Bold label", BOLD), x=2, y=3, w=40, h=1)
    page.add(Label("Reverse label", Style(attr=TextAttribute.REVERSE)), x=2, y=5, w=40, h=1)
    page.add(Label("Colored label", Style(fg=(13, 188, 121))), x=2, y=7, w=40, h=1)


def build_list_page(page: Container, panel: Panel) -> None:
    items = [f"Item {i:03d}" for i in range(50)]
    status = Label("Use arrows / page keys; enter to select", DIM)
    listview = ListView(
        items, on_select=lambda i, text: setattr(status, "text", f"Selected: {text}")
    )
    page.add(listview, x=2, y=1, w=30, h=12)
    page.add(status, x=2, y=14, w=50, h=1)
    page.focus(listview)


def build_scrollbar_page(page: Container, panel: Panel) -> None:
    page.add(Label("Standalone scroll bars (pos / ratio):"), x=2, y=1, w=50, h=1)
    for i, (pos, ratio) in enumerate([(0.0, 0.3), (0.5, 0.3), (1.0, 0.3), (0.0, 0.8)]):
        page.add(Label(f"{pos:.1f} / {ratio:.1f}"), x=2 + i * 12, y=3, w=10, h=1)
        page.add(ScrollBar(pos, ratio), x=6 + i * 12, y=5, w=1, h=10)


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


def build_animation_page(page: Container, panel: Panel) -> None:
    target = AnimTarget()

    def run(index: int, name: str) -> None:
        target.last_label.text = name
        panel.animate(target, hints=dict(ANIMATIONS[index][1]))

    listview = ListView([name for name, _ in ANIMATIONS], on_select=run)
    page.add(Label("Pick a transition, press enter", DIM), x=2, y=1, w=50, h=1)
    page.add(listview, x=2, y=3, w=24, h=8)
    page.add(target, x=30, y=3, w=28, h=9, hints={"bg": CARD_BG})
    page.focus(listview)


class Region(Widget):
    """A layout region that reports its own computed geometry. Regions draw
    no borders: the surface backgrounds and the layout dividers separate
    them. The cell extent is fractional on pixel-layout backends and whole on
    TUI, so the same layout reads differently per backend."""

    def __init__(self, name: str, color: tuple[int, int, int], note: str = ""):
        self.name = name
        self.color = color
        self.note = note

    def draw(self, ctx) -> None:
        w_cells, h_cells = ctx.size_cells
        cw, ch = ctx.cell_size
        cells_line = f"{w_cells:.2f} x {h_cells:.2f} cells"
        px_line = f"= {w_cells * cw:.0f} x {h_cells * ch:.0f} px"
        if ctx.height >= 5:
            ctx.draw_text(1, 0, self.name, Style(fg=self.color, attr=TextAttribute.BOLD))
            ctx.draw_text(1, 2, cells_line)
            ctx.draw_text(1, 3, px_line)
            if self.note:
                ctx.draw_text(1, 4, self.note, Style(attr=TextAttribute.DIM))
        else:
            line = f"{self.name}  {cells_line} {px_line}" + (
                f"  ({self.note})" if self.note else ""
            )
            ctx.draw_text(1, 0, line, Style(fg=self.color))


def build_layout_page(page: Container, panel: Panel) -> None:
    # One layout definition, resolved at the page's own granularity: every
    # boundary snaps to whole cells on TUI, lands on device pixels on GUI.
    # Header/status use divider="subtle" (a GUI hairline, nothing on TUI —
    # the themed surface backgrounds carry the contrast); the body panes use
    # divider="strong" (a hairline on GUI, one whole │ cell column on TUI).
    page.add(Label("One layout, two granularities — resize the window", DIM), x=2, y=1, w=60, h=1)
    board = LayoutView(
        VSplit(
            Item(
                Region("Header", (229, 229, 16), "fixed: 1 cell"),
                size=1,
                hints={"surface": "header"},
            ),
            Item(
                HSplit(
                    Item(
                        Region("Sidebar", (13, 188, 121), "weight 1, min 220px"),
                        weight=1,
                        hints={"min_px": 220, "min_cells": 18, "surface": "sidebar"},
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
        )
    )
    # stretch: the board fills the page below the caption and tracks resizes.
    page.add(board, x=2, y=3, w=0, h=0, hints={"stretch": True})


def build_intrinsic_page(page: Container, panel: Panel) -> None:
    # Three widgets that size *themselves*; the layout reserves what they
    # report and the rest flexes around them. None of these sizes is named by
    # the app — they come from the widget's own measure().
    page.add(
        Label("Widgets measure themselves; the layout reserves it", DIM),
        x=2, y=1, w=60, h=1,
    )
    board = LayoutView(
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
                    Item(
                        Region("Main", (36, 114, 200), "weight 2 · flexes to fill"),
                        weight=2,
                        hints={"surface": "content"},
                    ),
                    Item(
                        Region("Side", (13, 188, 121), "weight 1 · resize the window"),
                        weight=1,
                        hints={"surface": "sidebar"},
                    ),
                    # Fixed width, reserved before the 2:1 split divides the rest.
                    Item(ScrollBar(0.3, 0.4), size="content"),
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
        )
    )
    page.add(board, x=2, y=3, w=0, h=0, hints={"stretch": True})


PAGES = [
    ("Label", build_label_page),
    ("ListView", build_list_page),
    ("ScrollBar", build_scrollbar_page),
    ("Animation", build_animation_page),
    ("Layout", build_layout_page),
    ("Intrinsic", build_intrinsic_page),
]


# --- application shell -----------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="PuiKit widget catalog")
    parser.add_argument("--backend", default="tui", help="backend name (tui, gui, memory)")
    args = parser.parse_args()

    backend = create_backend(args.backend)
    with backend:
        panel = Panel(backend)
        content = Container()
        status = Label("", DIM)

        def show_page(index: int, name: str) -> None:
            content.clear()
            PAGES[index][1](content, panel)
            status.text = f" {name} — tab: focus page/nav, d: dialog, q: quit"

        nav = ListView([name for name, _ in PAGES], on_change=show_page)

        panel.set_layout(
            VSplit(
                Item(Label(" PuiKit Demo Catalog", BOLD), size=1, hints={"bg": TITLE_BG}),
                Item(
                    HSplit(
                        Item(nav, size=18, hints={"min_cells": 12, "bg": NAV_BG}),
                        Item(content, weight=1, hints={"min_px": 300, "bg": CONTENT_BG}),
                    )
                ),
                Item(status, size=1, hints={"bg": TITLE_BG}),
            )
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
