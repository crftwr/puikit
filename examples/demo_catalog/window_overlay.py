"""Styled-window helper for the demo catalog's Window page.

One process = one PuiKit window, so the Window page demonstrates
`WindowStyle` / `activation_policy` by launching this script as a subprocess
with the style under test. The window announces its own style and closes
itself after a few seconds via `panel.call_later` — which also makes this
the smallest end-to-end `call_later` example.

    python window_overlay.py --frameless --topmost --no-activate
    python window_overlay.py --accessory
"""

import argparse

from puikit import Panel, WindowStyle
from puikit.backends import create_backend
from puikit.layout import Item, VSplit
from puikit.widgets import Label

LIFETIME_SECONDS = 3.0


def main() -> None:
    parser = argparse.ArgumentParser(description="PuiKit styled-window overlay demo")
    parser.add_argument("--backend", default="gui")
    parser.add_argument("--frameless", action="store_true")
    parser.add_argument("--topmost", action="store_true")
    parser.add_argument("--no-activate", action="store_true")
    parser.add_argument("--not-resizable", action="store_true")
    parser.add_argument("--tool", action="store_true")
    parser.add_argument("--accessory", action="store_true",
                        help="activation_policy='accessory' (macOS agent app)")
    args = parser.parse_args()

    style = WindowStyle(
        frameless=args.frameless,
        topmost=args.topmost,
        activates=not args.no_activate,
        resizable=not args.not_resizable,
        tool=args.tool,
    )
    fields = [name for name, on in (
        ("frameless", style.frameless), ("topmost", style.topmost),
        ("no-activate", not style.activates), ("fixed-size", not style.resizable),
        ("tool", style.tool), ("accessory", args.accessory),
    ) if on]
    caption = " + ".join(fields) if fields else "default style"

    backend = create_backend(
        args.backend, width=56, height=5, title="PuiKit overlay",
        style=style,
        activation_policy="accessory" if args.accessory else "regular",
    )
    with backend:
        panel = Panel(backend)
        panel.set_layout(VSplit(
            Item(Label(f"  WindowStyle: {caption}"), size="content"),
            Item(Label(f"  closing itself in {LIFETIME_SECONDS:.0f}s (call_later)"),
                 size="content"),
            gap=1,
        ))
        panel.render()
        panel.call_later(LIFETIME_SECONDS, backend.quit)
        backend.run_event_loop(lambda event: None)


if __name__ == "__main__":
    main()
