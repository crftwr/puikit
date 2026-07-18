"""Shader background: an animated GPU scene behind a sparse UI.

A feasibility demo for ``backend.set_background(...)``. The GUI backend compiles
the fragment shader and paints it on its own layer *behind* the display list; the
terminal backend has no pixels, so the call no-ops and you just see the text.

Run from the repository root:

    python examples/background_shader/main.py --backend gui --font-size 18   # GUI (see it move)
    python examples/background_shader/main.py                                # TUI (no-op background)
    python examples/background_shader/main.py --backend memory               # headless smoke test

Keys: q / esc quit · +/- speed · space ink color · o cycle surface opacity
(how opaque the panel surface is; lower lets the scene show *through* it).
"""

import argparse

from puikit import EventType, Font, Panel, Shader, Style, TextAttribute
from puikit.backends import create_backend
from puikit.widgets import Label

# Concentric rings travelling outward, built from the `ink` uniform so one source
# follows whatever color the app passes. Deliberately tiny: the point of the demo
# is the wiring, not the scene. Ships both dialects, since MSL and HLSL are
# different languages and each backend compiles the one it speaks.
_MSL = """
fragment float4 puikit_bg_fragment(float4 pos [[position]],
                                   constant BackgroundUniforms &u [[buffer(0)]]) {
    float2 uv = (pos.xy - u.resolution * 0.5) / u.resolution.y;
    float d = length(uv);
    float wave = 0.5 + 0.5 * sin(d * 24.0 - u.time * 2.0);
    float ring = pow(wave, 6.0) * smoothstep(0.75, 0.05, d);
    return float4(mix(u.backdrop.rgb, u.ink.rgb, ring * u.opacity), 1.0);
}
"""

_HLSL = """
float4 puikit_bg_fragment(float4 pos : SV_Position) : SV_Target {
    float2 uv = (pos.xy - resolution * 0.5) / resolution.y;
    float d = length(uv);
    float wave = 0.5 + 0.5 * sin(d * 24.0 - time * 2.0);
    float ring = pow(wave, 6.0) * smoothstep(0.75, 0.05, d);
    return float4(lerp(backdrop.rgb, ink.rgb, ring * opacity), 1.0);
}
"""

# A few ink colors to cycle with space, so the on-palette `ink=` path is
# exercised (None would let the backend fill in the theme foreground).
_COLORS = [
    (90, 140, 200),   # soft blue
    (80, 220, 140),   # phosphor green
    (230, 150, 70),   # amber
    (210, 90, 160),   # magenta
]


def main() -> None:
    parser = argparse.ArgumentParser(description="PuiKit shader background demo")
    parser.add_argument("--backend", default="tui", help="backend name (tui, gui, memory)")
    parser.add_argument("--font-size", type=float, default=None,
                        help="base font size in points (GUI only)")
    args = parser.parse_args()

    kwargs = {}
    if args.font_size is not None and args.backend in ("gui", "macos", "windows", "win32"):
        kwargs["base_font"] = Font(size=args.font_size, monospace=True)
    backend = create_backend(args.backend, **kwargs)

    # Mutable speed/color/UI-opacity state the key handler drives.
    _OPACITIES = [1.0, 0.65, 0.35, 0.1]
    state = {"speed": 1.0, "color_ix": 0, "opacity_ix": 2}

    def apply_background() -> None:
        backend.set_background(Shader(
            source=_MSL,
            source_hlsl=_HLSL,
            ink=_COLORS[state["color_ix"]],
            backdrop=(12, 14, 20),
            speed=state["speed"],
            opacity=0.7,
        ))

    def apply_opacity() -> None:
        # How opaque the UI is, is a backend-wide, background-agnostic knob set
        # separately from the scene — the same value would dissolve any wallpaper.
        backend.set_surface_opacity(_OPACITIES[state["opacity_ix"]])

    with backend:
        cols, rows = backend.size_units
        panel = Panel(backend)
        # A full-window slot with a solid "bg" fills the whole surface, so lowering
        # its opacity has something to dissolve — press o to watch the scene emerge.
        panel.add(Label(""), x=0, y=0, w=cols, h=rows, hints={"bg": (22, 24, 30)})
        panel.add(Label("Shader background demo", Style(attr=TextAttribute.BOLD)), x=2, y=1, w=40, h=1)
        panel.add(Label("A GPU scene animates behind the panel."), x=2, y=3, w=48, h=1)
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
