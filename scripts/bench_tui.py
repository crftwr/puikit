"""Where a TUI frame's time actually goes — the measurements puikit#98 §5 asks for.

Non-interactive: it drives the demo catalog through a fixed script of frames and
exits. Nothing is read from the keyboard, so it is safe to run from a captured
shell.

    python scripts/bench_tui.py                     # curses backend, this terminal
    python scripts/bench_tui.py --backend memory    # draw side only, no terminal
    python scripts/bench_tui.py --json out.json     # machine-readable results

Measurements, numbered as in the discussion:

  M1  Per-frame split: time in draw calls vs time in ``present()``. ``render()``
      is clear -> draws -> present, so wrapping the two calls gives the split
      with no instrumentation inside the backend.
  M2  How often ``present()`` is forced into a full repaint because a recycled
      pair number now carries a different color (``_pair_rgb != _prev_pair_rgb``).
  M3  Run this same script on macOS/Linux and compare the ``present()`` column.
  M4  Idle repaint frequency: how many frames the loop paints with no input.

Run it at least twice and read the SECOND run: the first invocation of a fresh
process is consistently 1.5-2x slower (cold caches, first-touch imports) and has
misled a comparison here already.

The second pass wraps the curses window in a counting proxy to report addstr
calls per frame — the count that maps to console-API round-trips on Windows.
Its own Python overhead inflates the draw side, so the pass-one numbers are the
ones to quote for M1; pass two is for counts and ratios only.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import platform
import statistics
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from puikit import (  # noqa: E402
    Event,
    EventType,
    HSplit,
    Item,
    Panel,
    Style,
    VSplit,
)
from puikit.backends import create_backend  # noqa: E402
from puikit.widgets import Label, LayoutView, ListView  # noqa: E402


def _load_demo():
    """The demo catalog, imported under its own name so ``main`` stays free."""
    path = _ROOT / "examples" / "demo_catalog" / "main.py"
    spec = importlib.util.spec_from_file_location("demo_catalog_main", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["demo_catalog_main"] = module
    spec.loader.exec_module(module)
    return module


@dataclass
class Frame:
    """One ``panel.render()``."""

    label: str
    total_ms: float
    present_ms: float
    pair_changed: bool
    pairs_used: int
    addstr_calls: int = 0
    window_ms: float = 0.0
    # Per curses-window method: {"addstr": (calls, ms), "refresh": (...), ...}
    window_calls: dict = field(default_factory=dict)

    @property
    def draw_ms(self) -> float:
        return self.total_ms - self.present_ms


class _CountingWindow:
    """Forwards every call to the real curses window, counting the ones that
    matter. ``_stdscr`` is only ever used through method calls, so a proxy is
    transparent here."""

    __slots__ = ("_w", "calls", "times")

    def __init__(self, window):
        object.__setattr__(self, "_w", window)
        object.__setattr__(self, "calls", {})
        object.__setattr__(self, "times", {})

    def __getattr__(self, name):
        target = getattr(object.__getattribute__(self, "_w"), name)
        if not callable(target):
            return target
        calls = object.__getattribute__(self, "calls")
        times = object.__getattribute__(self, "times")

        def wrapper(*a, **kw):
            t0 = time.perf_counter()
            try:
                return target(*a, **kw)
            finally:
                dt = time.perf_counter() - t0
                calls[name] = calls.get(name, 0) + 1
                times[name] = times.get(name, 0.0) + dt

        return wrapper

    def reset(self):
        object.__getattribute__(self, "calls").clear()
        object.__getattribute__(self, "times").clear()


class Harness:
    """The demo catalog's shell, driven programmatically.

    Mirrors ``demo_catalog/main.py``'s ``main()``: the same nav list, page host,
    title and status bar, so a frame here costs what a frame there costs.
    """

    def __init__(self, backend, demo):
        self.backend = backend
        self.demo = demo
        self.frames: list[Frame] = []
        self._window = None

        panel = Panel(backend)
        self.panel = panel
        self.content = LayoutView(VSplit(), margin_px=8, margin_units=1)
        self.title = Label(" PuiKit Demo Catalog", demo.BOLD, padding_px=4)
        self.status = Label("", padding_px=4)
        self.theme_index = 0
        self.page_index = 0

        self.nav = ListView([name for name, _ in demo.PAGES], on_change=self.show_page)
        panel.set_layout(
            VSplit(
                Item(self.title, size="content", hints={"surface": "header"}),
                Item(
                    HSplit(
                        Item(self.nav, size=18, hints={"min": 12, "surface": "sidebar"}),
                        Item(self.content, weight=1, hints={"min_px": 300, "surface": "content"}),
                        divider="subtle",
                    )
                ),
                Item(self.status, size="content", hints={"surface": "status"}),
            ),
        )
        self.apply_theme(0)
        self.show_page(0, demo.PAGES[0][0])

    # --- app behaviour (mirrors the demo) ---------------------------------

    def show_page(self, index: int, name: str) -> None:
        self.page_index = index
        self.content.set_layout(self.demo.PAGES[index][1](self.panel))
        self.status.text = f" {name} — tab: focus · d: dialog · t: theme · q: quit"

    def apply_theme(self, index: int) -> None:
        self.theme_index = index % len(self.demo.DEMO_THEMES)
        theme = self.demo.DEMO_THEMES[self.theme_index][1]
        self.panel.theme = theme
        self.title.style = Style(fg=theme.text, attr=self.demo.TextAttribute.BOLD)
        self.status.style = Style(
            fg=self.demo._on_accent_fg(theme.accent), bg=theme.surfaces["status"]
        )

    # --- measurement ------------------------------------------------------

    def install_counter(self) -> None:
        """Wrap the curses window so addstr/refresh calls are counted."""
        if getattr(self.backend, "_stdscr", None) is None:
            return
        self._window = _CountingWindow(self.backend._stdscr)
        self.backend._stdscr = self._window

    def render(self, label: str) -> Frame:
        backend = self.backend
        if self._window is not None:
            self._window.reset()

        # present() decides a full repaint on this comparison; read it before
        # the frame so the same condition is recorded, not a post-hoc guess.
        present_ms = 0.0
        original = type(backend).present

        def timed_present(self_backend):
            nonlocal present_ms
            t0 = time.perf_counter()
            original(self_backend)
            present_ms = (time.perf_counter() - t0) * 1000.0

        type(backend).present = timed_present
        try:
            t0 = time.perf_counter()
            self.panel.render()
            total_ms = (time.perf_counter() - t0) * 1000.0
        finally:
            type(backend).present = original

        # Whether this frame was forced past curses' diff refresh into a full
        # repaint by a pair number changing color. ``_pairs_recolored`` is reset
        # in clear() and set during the draws, so it reads true for this frame.
        pair_changed = bool(getattr(backend, "_pairs_recolored", False))
        pairs_used = len(getattr(backend, "_pair_rgb", ()) or ())
        addstr = 0
        window_ms = 0.0
        per_method: dict = {}
        if self._window is not None:
            calls = object.__getattribute__(self._window, "calls")
            times = object.__getattribute__(self._window, "times")
            addstr = calls.get("addstr", 0)
            window_ms = sum(times.values()) * 1000.0
            per_method = {k: (calls[k], times[k] * 1000.0) for k in calls}

        frame = Frame(label, total_ms, present_ms, pair_changed, pairs_used, addstr, window_ms, per_method)
        self.frames.append(frame)
        return frame

    def key(self, name: str) -> None:
        self.panel.dispatch_event(Event(EventType.KEY, key=name))


def run_scenario(h: Harness, repeats: int) -> None:
    """A scripted session: page switches, cursor movement, theme cycling.

    Cursor movement matters most — the discussion's suspicion is that simply
    moving a selection through a list re-orders pair allocation and forces a
    full repaint every frame.
    """
    demo = h.demo
    h.render("warmup")
    h.frames.clear()  # first frame pays import/layout costs; not representative

    # A) page switches — the heaviest realistic frame
    for _ in range(repeats):
        for i in range(min(8, len(demo.PAGES))):
            h.nav.selected = i
            h.show_page(i, demo.PAGES[i][0])
            h.render(f"page:{demo.PAGES[i][0]}")

    # B) cursor movement in the nav, page content unchanged — the common case
    list_page = next(
        (i for i, (n, _) in enumerate(demo.PAGES) if "ListView" in n), 0
    )
    h.nav.selected = list_page
    h.show_page(list_page, demo.PAGES[list_page][0])
    h.render("settle")
    for _ in range(repeats * 8):
        h.key("down")
        h.render("cursor:down")
    for _ in range(repeats * 8):
        h.key("up")
        h.render("cursor:up")

    # C) static screen, nothing changed at all — the floor
    for _ in range(repeats * 4):
        h.render("static")

    # D) theme cycling — every cell changes color
    for i in range(repeats * 2):
        h.apply_theme(i + 1)
        h.render("theme")


def measure_idle(h: Harness, seconds: float, timeout_ms: int) -> dict:
    """M4 — how many frames does the loop paint with no input at all?

    Parks on the animation page (self-driven ticks) and pumps the real event
    loop. Counts present() calls, which is what a repaint costs.
    """
    demo = h.demo
    anim = next((i for i, (n, _) in enumerate(demo.PAGES) if "Animation" in n), None)
    if anim is None:
        return {"supported": False}
    h.nav.selected = anim
    h.show_page(anim, demo.PAGES[anim][0])
    h.render("idle-settle")

    backend = h.backend
    if not hasattr(backend, "run_event_loop_iteration"):
        return {"supported": False}

    presents = 0
    original = type(backend).present

    def counting_present(self_backend):
        nonlocal presents
        presents += 1
        original(self_backend)

    type(backend).present = counting_present
    iterations = 0
    t0 = time.perf_counter()
    try:
        while time.perf_counter() - t0 < seconds:
            backend.run_event_loop_iteration(lambda e: None, timeout_ms)
            iterations += 1
            if iterations > 100000:  # stdin at EOF: the loop is spinning
                break
    except Exception as exc:  # a dead/redirected stdin can raise here
        type(backend).present = original
        return {"supported": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        type(backend).present = original
    elapsed = time.perf_counter() - t0
    return {
        "supported": True,
        "seconds": round(elapsed, 3),
        "iterations": iterations,
        "repaints": presents,
        "repaints_per_second": round(presents / elapsed, 2) if elapsed else 0,
        "iterations_per_second": round(iterations / elapsed, 2) if elapsed else 0,
        "spinning": iterations > 1000 and elapsed < seconds * 0.9,
    }


def summarize(frames: list[Frame]) -> dict:
    groups: dict[str, list[Frame]] = {}
    for f in frames:
        groups.setdefault(f.label.split(":")[0], []).append(f)

    def stat(fs: list[Frame]) -> dict:
        total = [f.total_ms for f in fs]
        present = [f.present_ms for f in fs]
        draw = [f.draw_ms for f in fs]
        changed = sum(1 for f in fs if f.pair_changed)
        return {
            "frames": len(fs),
            "total_ms_median": round(statistics.median(total), 3),
            "present_ms_median": round(statistics.median(present), 3),
            "draw_ms_median": round(statistics.median(draw), 3),
            "present_share": round(sum(present) / sum(total), 3) if sum(total) else 0,
            "pair_changed_frames": changed,
            "pair_changed_rate": round(changed / len(fs), 3),
            "pairs_used_max": max(f.pairs_used for f in fs),
            "addstr_median": int(statistics.median(f.addstr_calls for f in fs)),
        }

    return {name: stat(fs) for name, fs in groups.items()}


def report(title: str, summary: dict) -> str:
    lines = [f"\n== {title} ==", ""]
    head = f"{'scenario':<10} {'n':>4} {'total':>9} {'draw':>9} {'present':>9} {'present%':>9} {'repaint':>8} {'addstr':>8}"
    lines.append(head)
    lines.append("-" * len(head))
    for name, s in summary.items():
        lines.append(
            f"{name:<10} {s['frames']:>4} "
            f"{s['total_ms_median']:>8.2f}m {s['draw_ms_median']:>8.2f}m {s['present_ms_median']:>8.2f}m "
            f"{s['present_share'] * 100:>8.1f}% {s['pair_changed_rate'] * 100:>7.0f}% {s['addstr_median']:>8}"
        )
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backend", default="tui", help="tui (curses) or memory")
    ap.add_argument(
        "--size",
        default=None,
        metavar="WxH",
        help="grid size for the memory backend, so it can be compared cell-for-cell "
             "against a curses run (e.g. --size 120x30)",
    )
    ap.add_argument("--repeats", type=int, default=3, help="scenario repetitions")
    ap.add_argument("--idle-seconds", type=float, default=2.0)
    ap.add_argument("--idle-timeout-ms", type=int, default=16)
    ap.add_argument("--no-idle", action="store_true", help="skip M4")
    ap.add_argument("--no-count", action="store_true", help="skip the counting pass")
    ap.add_argument("--json", type=Path, default=None)
    args = ap.parse_args()

    demo = _load_demo()
    result = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "backend": args.backend,
    }
    out: list[str] = []

    # --- pass 1: clean timing (M1, M2) ---
    backend = create_backend(args.backend, **_backend_kwargs(args))
    with backend:
        h = Harness(backend, demo)
        result["size"] = list(backend.size)
        result["curses"] = _curses_info(backend)
        run_scenario(h, args.repeats)
        pass1 = summarize(h.frames)
        idle = None
        if not args.no_idle:
            idle = measure_idle(h, args.idle_seconds, args.idle_timeout_ms)
        result["pair_stats_end"] = list(backend.color_pair_stats()) if hasattr(backend, "color_pair_stats") else None
    result["timing"] = pass1
    result["idle"] = idle
    out.append(report("pass 1 — timing (quote these for M1/M2)", pass1))

    # --- pass 2: counted calls (addstr volume) ---
    if not args.no_count and args.backend != "memory":
        backend = create_backend(args.backend)
        with backend:
            h = Harness(backend, demo)
            h.install_counter()
            run_scenario(h, max(1, args.repeats // 2))
            pass2 = summarize(h.frames)
            window_ms = [f.window_ms for f in h.frames]
            total_ms = [f.total_ms for f in h.frames]
        result["counted"] = pass2
        result["window_call_share"] = round(sum(window_ms) / sum(total_ms), 3) if sum(total_ms) else 0
        out.append(report("pass 2 — counted (addstr volume; timings inflated)", pass2))

        # Which curses call the frame's time actually sits in.
        agg: dict[str, list[float]] = {}
        for f in h.frames:
            for name, (n, ms) in f.window_calls.items():
                slot = agg.setdefault(name, [0.0, 0.0])
                slot[0] += n
                slot[1] += ms
        n_frames = max(1, len(h.frames))
        total_all = sum(total_ms)
        result["window_methods"] = {
            k: {
                "calls_per_frame": round(v[0] / n_frames, 1),
                "ms_per_frame": round(v[1] / n_frames, 3),
                "share_of_frame": round(v[1] / total_all, 3) if total_all else 0,
            }
            for k, v in sorted(agg.items(), key=lambda kv: -kv[1][1])
        }
        lines = ["\n== pass 2 — time inside each curses call ==", ""]
        head = f"{'method':<14} {'calls/frame':>12} {'ms/frame':>10} {'share':>8}"
        lines += [head, "-" * len(head)]
        for k, v in result["window_methods"].items():
            lines.append(
                f"{k:<14} {v['calls_per_frame']:>12.1f} {v['ms_per_frame']:>10.2f} "
                f"{v['share_of_frame'] * 100:>7.1f}%"
            )
        out.append("\n".join(lines))

    print("\n".join(out))
    print()
    if idle and idle.get("supported"):
        print(
            f"M4 idle: {idle['repaints']} repaints in {idle['seconds']}s "
            f"({idle['repaints_per_second']}/s) over {idle['iterations']} loop iterations"
            + ("  [stdin at EOF — loop is spinning, treat as unreliable]" if idle.get("spinning") else "")
        )
    elif idle is not None:
        print(f"M4 idle: not measurable here ({idle.get('error', 'unsupported backend')})")
    if result.get("pair_stats_end"):
        used, cap, overflow = result["pair_stats_end"]
        print(f"color pairs at exit: {used}/{cap} used, {overflow} overflow lookups")
    if args.json:
        args.json.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json}")


def _backend_kwargs(args) -> dict:
    if args.size and args.backend == "memory":
        w, h = args.size.lower().split("x")
        return {"width": int(w), "height": int(h)}
    return {}


def _curses_info(backend) -> dict | None:
    try:
        import curses
    except ImportError:
        return None
    if getattr(backend, "_stdscr", None) is None:
        return None
    return {
        "COLORS": getattr(curses, "COLORS", None),
        "COLOR_PAIRS": getattr(curses, "COLOR_PAIRS", None),
        "term": __import__("os").environ.get("TERM"),
        "wt_session": bool(__import__("os").environ.get("WT_SESSION")),
    }


if __name__ == "__main__":
    main()
