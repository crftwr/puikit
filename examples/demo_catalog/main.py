"""PuiKit widget catalog: one screen per widget type.

Switch screens with the left/right arrow keys (or 1..9), quit with q.

    python examples/demo_catalog/main.py
"""

import argparse

from puikit import EventType, Panel, Style, TextAttribute
from puikit.backends import create_backend
from puikit.widgets import Label, ListView, ScrollBar


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


def build_scrollbar_screen(panel: Panel) -> None:
    panel.add(Label("Standalone scroll bars (pos / ratio):"), x=4, y=4, w=50, h=1)
    for i, (pos, ratio) in enumerate([(0.0, 0.3), (0.5, 0.3), (1.0, 0.3), (0.0, 0.8)]):
        panel.add(Label(f"{pos:.1f} / {ratio:.1f}"), x=4 + i * 12, y=6, w=10, h=1)
        panel.add(ScrollBar(pos, ratio), x=8 + i * 12, y=8, w=1, h=10)


SCREENS = [
    ("Label", build_label_screen),
    ("ListView", build_list_screen),
    ("ScrollBar", build_scrollbar_screen),
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
            panel.add(Label("left/right: switch screen, q: quit"), x=2, y=2, w=70, h=1)
            builder(panel)
            panel.render()

        def on_event(event) -> None:
            nonlocal current
            if event.type is EventType.KEY:
                if event.key in ("q", "escape"):
                    backend.quit()
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
            panel.dispatch_event(event)
            panel.render()

        show_screen(current)
        backend.run_event_loop(on_event)


if __name__ == "__main__":
    main()
