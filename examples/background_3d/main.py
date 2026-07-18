"""Animated 3D background: a spinning wireframe cube behind a sparse UI.

A feasibility demo for ``backend.set_background(...)``. The GUI (macOS) backend
strokes the projected cube under the display list every frame; the terminal
backend has no sub-cell pixels, so the call no-ops and you just see the text.

Run from the repository root:

    python examples/background_3d/main.py --backend gui --font-size 18   # GUI (see it spin)
    python examples/background_3d/main.py                                # TUI (no-op background)
    python examples/background_3d/main.py --backend memory               # headless smoke test

Keys: q / esc quit · +/- spin speed · space line color · o cycle surface opacity
(how opaque the panel surface is; lower lets the cube show *through* it).
"""

import argparse

from puikit import Background3D, EventType, Font, Panel, Style, TextAttribute
from puikit.backends import create_backend
from puikit.widgets import Label

# A few line colors to cycle with space, so the on-palette `color=` path is
# exercised (None would let the backend pick its default blue).
_COLORS = [
    (90, 140, 200),   # soft blue
    (80, 220, 140),   # phosphor green
    (230, 150, 70),   # amber
    (210, 90, 160),   # magenta
]


def main() -> None:
    parser = argparse.ArgumentParser(description="PuiKit 3D background demo")
    parser.add_argument("--backend", default="tui", help="backend name (tui, gui, memory)")
    parser.add_argument("--font-size", type=float, default=None,
                        help="base font size in points (GUI only)")
    args = parser.parse_args()

    kwargs = {}
    if args.font_size is not None and args.backend in ("gui", "macos", "windows", "win32"):
        kwargs["base_font"] = Font(size=args.font_size, monospace=True)
    backend = create_backend(args.backend, **kwargs)

    # Mutable spin/color/UI-opacity state the key handler drives.
    _OPACITIES = [1.0, 0.65, 0.35, 0.1]
    state = {"speed": 1.0, "color_ix": 0, "opacity_ix": 2}

    def apply_background() -> None:
        backend.set_background(Background3D(
            kind="cube",
            color=_COLORS[state["color_ix"]],
            speed=state["speed"],
            opacity=0.7,
        ))

    def apply_opacity() -> None:
        # How opaque the UI is, is a backend-wide, wallpaper-agnostic knob set
        # separately from the scene — the same value would dissolve any wallpaper.
        backend.set_surface_opacity(_OPACITIES[state["opacity_ix"]])

    with backend:
        cols, rows = backend.size_units
        panel = Panel(backend)
        # A full-window slot with a solid "bg" fills the whole surface, so lowering
        # its opacity has something to dissolve — press o to watch the cube emerge.
        panel.add(Label(""), x=0, y=0, w=cols, h=rows, hints={"bg": (22, 24, 30)})
        panel.add(Label("3D background demo", Style(attr=TextAttribute.BOLD)), x=2, y=1, w=40, h=1)
        panel.add(Label("A wireframe cube spins behind the panel."), x=2, y=3, w=48, h=1)
        panel.add(Label("q quit · +/- speed · space color · o opacity"), x=2, y=5, w=48, h=1)
        panel.render()
        apply_background()
        apply_opacity()

        def on_event(event) -> None:
            if event.type is EventType.KEY:
                if event.key in ("q", "escape"):
                    backend.quit()
                    return
                if event.key in ("+", "="):
                    state["speed"] = min(state["speed"] + 0.5, 8.0)
                    apply_background()
                    return
                if event.key in ("-", "_"):
                    state["speed"] = max(state["speed"] - 0.5, 0.0)
                    apply_background()
                    return
                if event.key == "space":
                    state["color_ix"] = (state["color_ix"] + 1) % len(_COLORS)
                    apply_background()
                    return
                if event.key == "o":
                    state["opacity_ix"] = (state["opacity_ix"] + 1) % len(_OPACITIES)
                    apply_opacity()
                    return
            panel.dispatch_event(event)
            panel.render()

        backend.run_event_loop(on_event)

    if args.backend == "memory":
        # Headless: prove the wiring runs end-to-end without a window.
        for line in backend.snapshot()[:6]:
            print(line.rstrip())


if __name__ == "__main__":
    main()
