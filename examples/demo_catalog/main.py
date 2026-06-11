"""PuiKit widget catalog: one screen per widget type.

Switch screens with the left/right arrow keys (or 1..9), open a layered
dialog with d, quit with q.

    python examples/demo_catalog/main.py
"""

import argparse

from puikit import EventType, Panel, Style, TextAttribute
from puikit.backends import create_backend
from puikit.widgets import Container, Label, ListView, ScrollBar, Widget


class DemoDialog(Widget):
    """A modal dialog layer. Pushed with shadow/dim_below hints: GUI backends
    render a drop shadow and a translucent dim overlay, TUI approximates the
    dim with dark attributes and skips the shadow."""

    def __init__(self, on_close):
        self.on_close = on_close

    def draw(self, ctx):
        ctx.draw_box(0, 0, ctx.width, ctx.height, hints={"fill": True})
        ctx.draw_icon(2, 1, "info")
        ctx.draw_text(5, 1, "A layered dialog", Style(attr=TextAttribute.BOLD))
        ctx.draw_text(2, 3, "The content below is dimmed.")
        ctx.draw_text(2, ctx.height - 2, "esc / enter: close", Style(attr=TextAttribute.DIM))

    def handle_event(self, event):
        if event.type is EventType.KEY and event.key in ("escape", "enter"):
            self.on_close()
        return True  # modal: swallow everything


def build_label_screen(panel: Panel) -> None:
    panel.add(Label("Plain label"), x=4, y=4, w=40, h=1)
    panel.add(Label("Bold label", Style(attr=TextAttribute.BOLD)), x=4, y=6, w=40, h=1)
    panel.add(
        Label("Reverse label", Style(attr=TextAttribute.REVERSE)), x=4, y=8, w=40, h=1
    )
    panel.add(
        Label("Colored label", Style(fg=(13, 188, 121))), x=4, y=10, w=40, h=1
    )


def build_list_screen(panel: Panel) -> None:
    items = [f"Item {i:03d}" for i in range(50)]
    status = Label("Use arrows / page keys; enter to select")
    listview = ListView(
        items, on_select=lambda i, text: setattr(status, "text", f"Selected: {text}")
    )
    panel.add(listview, x=4, y=4, w=30, h=12)
    panel.add(status, x=4, y=17, w=50, h=1)
    panel.focus(listview)


class AnimTarget(Container):
    """The animated target is a widget tree: transitions on the container
    cascade to all children, and the overflowing child shows clipping."""

    def __init__(self):
        super().__init__()
        self.last_label = Label("none yet", Style(fg=(13, 188, 121)))
        self.add(Label("Target", Style(attr=TextAttribute.BOLD)), x=5, y=1, w=12, h=1)
        self.add(Label("Last transition:"), x=2, y=3, w=20, h=1)
        self.add(self.last_label, x=2, y=4, w=22, h=1)
        # Wider than the card on purpose: clipped at the container edge.
        self.add(
            Label("This long child label is clipped at the card edge",
                  Style(attr=TextAttribute.DIM)),
            x=2, y=6, w=50, h=1,
        )

    def draw(self, ctx) -> None:
        ctx.draw_border(Style(fg=(36, 114, 200)), hints={"fill": True})
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


def build_animation_screen(panel: Panel) -> None:
    target = AnimTarget()

    def run(index: int, name: str) -> None:
        target.last_label.text = name
        panel.animate(target, hints=dict(ANIMATIONS[index][1]))

    listview = ListView([name for name, _ in ANIMATIONS], on_select=run)
    panel.add(
        Label("Pick a transition, press enter (GUI animates; TUI switches instantly)"),
        x=4, y=4, w=70, h=1,
    )
    panel.add(listview, x=4, y=6, w=24, h=8)
    panel.add(target, x=34, y=6, w=26, h=8)
    panel.focus(listview)


def build_scrollbar_screen(panel: Panel) -> None:
    panel.add(Label("Standalone scroll bars (pos / ratio):"), x=4, y=4, w=50, h=1)
    for i, (pos, ratio) in enumerate([(0.0, 0.3), (0.5, 0.3), (1.0, 0.3), (0.0, 0.8)]):
        panel.add(Label(f"{pos:.1f} / {ratio:.1f}"), x=4 + i * 12, y=6, w=10, h=1)
        panel.add(ScrollBar(pos, ratio), x=8 + i * 12, y=8, w=1, h=10)


SCREENS = [
    ("Label", build_label_screen),
    ("ListView", build_list_screen),
    ("ScrollBar", build_scrollbar_screen),
    ("Animation", build_animation_screen),
]


def main() -> None:
    parser = argparse.ArgumentParser(description="PuiKit widget catalog")
    parser.add_argument("--backend", default="tui", help="backend name (tui, memory)")
    args = parser.parse_args()

    backend = create_backend(args.backend)
    current = 0

    with backend:
        panel = Panel(backend)

        def show_screen(index: int) -> None:
            panel.clear()
            name, builder = SCREENS[index]
            tabs = "  ".join(
                f"[{n}]" if i == index else f" {n} " for i, (n, _) in enumerate(SCREENS)
            )
            panel.add(Label(tabs, Style(attr=TextAttribute.BOLD)), x=2, y=1, w=70, h=1)
            panel.add(
                Label("left/right: switch screen, d: dialog, q: quit"), x=2, y=2, w=70, h=1
            )
            builder(panel)
            panel.render()

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
            nonlocal current
            # Let an open dialog take the event first (modal).
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
                if event.key == "right":
                    current = (current + 1) % len(SCREENS)
                    show_screen(current)
                    return
                if event.key == "left":
                    current = (current - 1) % len(SCREENS)
                    show_screen(current)
                    return
                if event.key and event.key.isdigit():
                    index = int(event.key) - 1
                    if 0 <= index < len(SCREENS):
                        current = index
                        show_screen(current)
                        return

        show_screen(current)
        backend.run_event_loop(on_event)


if __name__ == "__main__":
    main()
