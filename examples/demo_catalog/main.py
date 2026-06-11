"""PuiKit widget catalog with a left navigation pane.

The pages are listed in the navigation on the left; moving the selection
(up/down or mouse) switches the content pane on the right. The whole shell
is built with the layout system, so it follows window resizes; pages are
Containers, so their widgets are clipped and animate as a tree.

Keys: up/down in the nav switch pages, tab moves focus between the nav and
the page, 1..9 jump to a page, d opens a layered dialog, q quits.

    python examples/demo_catalog/main.py                  # TUI
    python examples/demo_catalog/main.py --backend gui    # macOS GUI
"""

import argparse

from puikit import EventType, HSplit, Item, Panel, Style, TextAttribute, VSplit
from puikit.backends import create_backend
from puikit.widgets import Container, Label, ListView, ScrollBar, Widget

DIM = Style(attr=TextAttribute.DIM)
BOLD = Style(attr=TextAttribute.BOLD)

# Pane background colors: title/status bars, navigation, and content are
# visually separated. GUI renders the exact RGB; TUI approximates via the
# xterm-256 palette.
TITLE_BG = (52, 62, 88)
NAV_BG = (38, 44, 60)
CONTENT_BG = (26, 28, 34)
CARD_BG = (42, 46, 56)


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


PAGES = [
    ("Label", build_label_page),
    ("ListView", build_list_page),
    ("ScrollBar", build_scrollbar_page),
    ("Animation", build_animation_page),
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
