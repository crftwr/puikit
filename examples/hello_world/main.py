"""Minimal PuiKit app: a single text label.

Run from the repository root:

    python examples/hello_world/main.py            # TUI (curses)
    python examples/hello_world/main.py --backend memory   # headless smoke test
"""

import argparse

from puikit import EventType, Panel, Style, TextAttribute
from puikit.backends import create_backend
from puikit.widgets import Label


def main() -> None:
    parser = argparse.ArgumentParser(description="PuiKit hello world")
    parser.add_argument("--backend", default="tui", help="backend name (tui, memory)")
    args = parser.parse_args()

    backend = create_backend(args.backend)
    with backend:
        panel = Panel(backend)
        panel.add(Label("Hello, PuiKit!", Style(attr=TextAttribute.BOLD)), x=2, y=1, w=30, h=1)
        panel.add(Label("Press q to quit."), x=2, y=3, w=30, h=1)
        panel.render()

        def on_event(event) -> None:
            if event.type is EventType.KEY and event.key in ("q", "escape"):
                backend.quit()
                return
            panel.dispatch_event(event)
            panel.render()

        backend.run_event_loop(on_event)

    if args.backend == "memory":
        for line in backend.snapshot()[:5]:
            print(line.rstrip())


if __name__ == "__main__":
    main()
