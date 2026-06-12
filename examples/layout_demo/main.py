"""Layout system demo: one layout definition, two granularities.

A header/body/status arrangement where the body is a 1:2:1 weighted split.
On the TUI backend every boundary snaps to whole cells; on the macOS GUI
backend the same layout resolves at pixel granularity, so weighted regions
get fractional cell widths and the sidebar's min_px hint is honored in real
pixels. Each region displays its own computed geometry — resize the GUI
window and watch the numbers.

Regions are tagged with surface roles and separated by dividers instead of
borders. Header and status use divider="subtle": on GUI a 1-device-pixel
hairline (zero cell cost), on TUI nothing — the themed surface backgrounds
provide the contrast. The body panes use divider="strong": still a hairline
on GUI, but on TUI one cell column is spent on a real │ separator.

    python examples/layout_demo/main.py                  # TUI
    python examples/layout_demo/main.py --backend gui    # macOS GUI

q quits.
"""

import argparse

from puikit import EventType, HSplit, Item, Panel, Style, TextAttribute, VSplit
from puikit.backends import create_backend
from puikit.widgets import Widget


class Region(Widget):
    """Reports its computed geometry. Regions draw no borders of their own:
    the surface backgrounds and the layout dividers are what separate them."""

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


def main() -> None:
    parser = argparse.ArgumentParser(description="PuiKit layout demo")
    parser.add_argument("--backend", default="tui", help="backend name (tui, gui, memory)")
    args = parser.parse_args()

    backend = create_backend(args.backend)
    with backend:
        panel = Panel(backend)
        panel.set_layout(
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
                        # "strong": worth a whole cell on TUI — a real │
                        # column; hairline backends still pay only 1px.
                        divider="strong",
                    )
                ),
                Item(
                    Region("Status  (q: quit, try resizing the window)", (220, 220, 220)),
                    size=1,
                    hints={"surface": "status"},
                ),
                divider="subtle",
            )
        )
        panel.render()

        def on_event(event) -> None:
            if event.type is EventType.KEY and event.key in ("q", "escape"):
                backend.quit()
                return
            panel.dispatch_event(event)
            panel.render()  # layout recomputes from the current backend size

        backend.run_event_loop(on_event)


if __name__ == "__main__":
    main()
