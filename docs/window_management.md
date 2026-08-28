# Window management: `WindowStyle`, activation policy, `call_later`

Three additive extensions for apps whose windows are not one classic
resizable document window — the first customer is Keyhac 2 (a menu-bar-only
keyboard tool whose UI is a console window, a chooser popup, and a balloon
tooltip). All three follow the additive recipe: every default reproduces the
pre-extension behavior exactly.

## `WindowStyle` (capability `window_styles`)

```python
from puikit import WindowStyle
from puikit.backends import create_backend

backend = create_backend(
    "gui", width=40, height=8, title="Balloon",
    style=WindowStyle(frameless=True, topmost=True, activates=False,
                      resizable=False, tool=True),
)
```

| Field | Default | macOS | Windows |
|---|---|---|---|
| `frameless` | `False` | borderless `NSWindow` (no title bar; cannot become key) | `WS_POPUP` |
| `topmost` | `False` | `NSFloatingWindowLevel` | `WS_EX_TOPMOST` |
| `activates` | `True` | `False`: `orderFrontRegardless()` — shown without key status or app activation | `False`: `WS_EX_NOACTIVATE` + `SW_SHOWNA` |
| `resizable` | `True` | drops `NSWindowStyleMaskResizable` | drops `WS_THICKFRAME \| WS_MAXIMIZEBOX` |
| `tool` | `False` | no-op today | `WS_EX_TOOLWINDOW` (out of taskbar / Alt-Tab) |
| `overlay_input` | `"none"` | with `activates=False`: `"mouse"` / `"keyboard"` build an `NSPanel` + `NSWindowStyleMaskNonactivatingPanel \| Titled \| UtilityWindow`; `"mouse"` adds `becomesKeyOnlyIfNeeded` | no equivalent; ignored |

- `style=None` (the default) ≡ `WindowStyle()` ≡ the classic window.
- Backends without the `window_styles` capability (curses, web, memory)
  accept the parameter and ignore it; `MemoryBackend` records it
  (`backend.window_style`) for tests.
- `activates=False` **on its own** is for display-only overlays (a balloon, a
  toast): no keyboard focus, and on macOS a *click* still activates the
  application even though a borderless window cannot become key.
- `overlay_input` opens the axis `activates=False` leaves closed: **what input
  reaches the window while its application is not active.** One field rather
  than a flag per mechanism, because the values are a ladder and no
  combination of them would be meaningful.

  | value | reaches the window | the target window |
  |---|---|---|
  | `"none"` (default) | nothing | untouched |
  | `"mouse"` | clicks | keeps its focus, caret and selection |
  | `"keyboard"` | clicks and keys | loses key status |

  `"keyboard"` is the **command palette** shape — Spotlight and the launcher
  apps. Because the window becomes key, `NSTextInputClient` serves it, so an
  input method composes in it, which no other value can offer. `"mouse"` is
  the shape for a picker driven from *elsewhere* — a global hotkey, an input
  hook — which must not disturb what it is acting on.

  macOS-only in effect. Windows couples keyboard focus to activation and
  `WS_EX_NOACTIVATE` refuses focus by design, so both values degrade there to
  plain `activates=False`; a Windows app that needs to be typed into has to
  activate. An unrecognized value degrades the same way.

  The mask needs a *titled* panel (a borderless panel cannot become key
  either), so the window carries a title bar — add `frameless=True` to hide it
  (full-size content view + transparent titlebar + hidden title), which also
  puts the content rect back to the frame rect so it measures like any other
  frameless window. `hidesOnDeactivate` is turned off, or a utility panel would
  hide itself whenever the owning application is not active, which for this
  window is always.
- The **pointer shape is per window**. Each window's `Panel` pushes a shape
  once per frame from its own hover state, so one shared slot let two open
  windows overwrite each other every frame — a popup's I-beam against a
  background window's arrow, seen as a flickering pointer. A shape is applied
  immediately only for the window the pointer is inside; a background window's
  request is recorded and takes effect when the pointer arrives.
- A window with `activates=False` also tracks the mouse **always** rather than
  only while key — it is never key, and under `NSTrackingActiveInKeyWindow` it
  would get no `mouseMoved` (no hover) and no `cursorUpdate`, leaving the
  pointer shape to be fought over between it and the application underneath.

## Activation policy (macOS agent apps)

```python
MacOSBackend(..., activation_policy="accessory")
```

- `"regular"` (default): Dock icon; opening the window activates the app —
  identical to the old behavior.
- `"accessory"`: an agent app — no Dock icon, and opening a window never
  steals focus from the frontmost app (the window still becomes key when
  clicked). Pair with `LSUIElement` in the app bundle for a menu-bar-only
  app.
- Unrecognized values degrade to `"regular"`. `WindowsBackend` accepts the
  parameter for signature parity and ignores it (no Dock on Windows; taskbar
  presence is `WindowStyle.tool`).

## Multi-window (capability `multi_window`)

**Status: shipped on macOS, Windows and MemoryBackend; Web and TUI planned.**

```python
win = backend.create_window(34, 4, title="Balloon",
                            style=WindowStyle(frameless=True, topmost=True,
                                              activates=False))
panel = Panel(backend, window=win)      # renders into win, receives its events
panel.add(Label("hello"), 0, 0, 20, 1)
panel.render()
win.hide(); win.show(); win.close()     # closing a secondary never quits the app
win.on_close = ...                       # user clicked close
```

- `create_window` is UI-thread-only and requires the backend to be open
  (secondary windows share the main window's base unit and fonts).
- A bound Panel installs itself as `win.on_event` (dispatch + render) unless
  the app set its own handler first.
- Per-window fidelity today: each secondary window has its own display list,
  input routing and — on Windows — its own IME input context (mode-gated at
  creation, inline preedit routed to its Panel; see
  [`windows_backend.md`](windows_backend.md) §6). Backgrounds and post effects
  remain main-window features for now.
- On Windows each secondary window is a real `HWND` with its own DXGI swap
  chain, but they all share the backend's one D3D11 device, Direct2D device
  context, fonts and base unit — rendering retargets that context at the
  window's swap-chain bitmap. A device-loss recreate rebuilds every window's
  surface.
- `MemoryBackend` windows record everything (`win.snapshot()`,
  `win.style_at()`), so multi-window UIs are testable headlessly.

**Decided fidelity mapping** (2026-07): secondary windows are **real windows
on every backend that has them** — native OS windows on GUI-Desktop, real
browser windows on Web — and degrade to **layers only on TUI**, where the
emulator owns the single terminal surface and no other kind of window can
exist.

| Intent | GUI-Desktop | Web | TUI |
|---|---|---|---|
| `create_window(...)` | a real OS window (`NSWindow` / `HWND`) | a real browser window (`window.open` companion page, same server session, own canvas + socket) | a framed, z-ordered layer on the one terminal surface |
| `topmost` | window level / `WS_EX_TOPMOST` | best-effort (`window.focus` on show; browsers do not expose true always-on-top) | a higher layer `z` |
| `frameless` | borderless window | browser-chrome-limited (popup features) | no frame box around the layer |
| `activates=False` | no focus stealing | open without `focus()` | non-interactive layer; keys keep flowing below |
| `overlay_input` | clicks, or keys, without app activation | — | — (macOS-only in effect) |
| position/size | screen coordinates | `window.open` features (best-effort; browser-gated) | a rect on the terminal surface |
| z-order between windows | OS compositor | browser window manager | layer `z`; topmost *interactive* layer is modal |
| screen geometry | `screen_frames()` | `window.screen` (per browser window) | the terminal size — one "screen" |

The app keeps issuing one intent — "give me a small topmost surface next to
X" — and the backend realizes it at its own fidelity, the same fallback
philosophy as menus (native `NSMenu`/`HMENU` vs. the widget-rendered menu):
widget and app code never branches.

Consequences of the mapping:

- Web fidelity is browser-gated and honest about it: popup blockers can
  require the open to happen inside a user gesture, and `topmost`/geometry
  are best-effort. Where a popup cannot open, the backend degrades that
  window to a layer in the main canvas rather than failing the app.
- A TUI "window" cannot escape the terminal: a balloon that would float over
  *another app's* window on GUI can only float over PuiKit's own surface —
  a documented fidelity limit (like `os_drag_drop` falling back to the
  clipboard), not something to emulate around.
- Modality stays consistent everywhere: the topmost interactive surface owns
  input, so a chooser popup behaves identically whether it is an OS window,
  a browser window, or a layer.

## `Backend.call_later(delay_seconds, callback) -> cancel`

One-shot timer on the UI thread; returns a zero-argument cancel function
(calling it after firing is a no-op).

- Base implementation rides `request_animation_ticks`, so any backend with
  `animation_ticks` gets a working timer at tick granularity — no capability
  flag needed.
- `MacOSBackend`: real one-shot `NSTimer`. `WindowsBackend`: `WM_TIMER` with
  its own id range (never collides with the animation timer). Both avoid
  dragging the 60 fps tick alive during the wait.
- `MemoryBackend`: records into `backend.later_timers`; tests fire pending
  timers deterministically with `backend.fire_timers()`.
- **UI-thread-only, and enforced.** Once the backend is open, calling
  `call_later` (or the returned cancel) from another thread raises
  `RuntimeError` — identically on every backend. Without the guard the
  failure diverged per platform: an `NSTimer` scheduled from a worker thread
  attaches to that thread's non-running run loop and *silently never fires*,
  while the same mistake happened to work on Windows and the tick-fallback
  backends. From a worker thread, hand the schedule over:
  `panel.call_on_main_thread(lambda: panel.call_later(1.0, fn))`. (The guard
  arms in `open()`; before it, headless construction stays unrestricted.)


## Screen marks (capability `screen_markers`)

```python
outline = backend.mark_screen(x, y, w, h, style=Style(fg=(255, 90, 90)),
                              line_width=3.0, radius=8.0)
label = backend.mark_screen(x, y, text="A", fill=True,
                            style=Style(fg=(255, 255, 255), bg=(30, 90, 200)))
tip = backend.mark_screen(x, y, text=long_text, max_width=220, fill=True,
                          timeout=3.0, style=Style(bg=(250, 240, 170)))
```

A rectangle drawn **on the screen**, over whatever is already there, for
*marking* something rather than being used — an outline around the control
something is pointing at, a label over each of them, a tooltip beside the
caret. Never interactive: clicks, scrolls and hovers pass through to the
application underneath, which is the point of a mark that points at something
clickable.

**Why an intent and not a window.** The obvious shape is a transparent,
click-through window an app fills with widgets, and it was built that way
first. It makes the feature macOS-only: a see-through window on Windows needs
`WS_EX_LAYERED` with per-pixel alpha, which is a change to how the backend
presents every frame rather than a style flag. "Outline this rectangle" has an
implementation on both — Windows can stroke with thin opaque windows and fill
with one — so the request is the intent and the mechanism is the backend's.
The cost is generality: a mark is a rectangle, some text and nothing else. No
consumer wanted more, and one that does can be answered without breaking
anyone, which a published window flag could not be.

| | |
|---|---|
| `x`, `y` | top-left, in the coordinates `screen_frames()` reports |
| `w`, `h` | the size, or `None` to fit `text` |
| `text` | `\n` separates lines; a mark with none is an outline or a wash |
| `style` | `fg` strokes and draws text, `bg` fills — as in `draw_round_rect` |
| `radius`, `fill`, `line_width` | the same vocabulary again |
| `max_width` | **opts into wrapping** when sizing to content |
| `timeout` | close after N seconds |
| `flash` | come up bright and settle to `style` |

`ScreenMarker.set_rect()` moves and resizes — and re-wraps, because a width is
a width whenever it arrives: a mark made narrower otherwise keeps lines that no
longer fit inside it. It re-flows only when the width actually changed, since
this is what an animation calls every frame. **Animating a mark is that in a
loop**, driven by `request_animation_ticks` like any other frame. Nothing
fades — opacity over time is exactly what a backend without per-pixel alpha
cannot do, so putting it in the vocabulary would undo the portability the
shape exists for.

`flash=True` is the one animation the primitive does itself, and it is a
**colour** transition for that same reason: puikit already calls a colour
flash a "highlight" and already tweens colour on a backend that cannot
composite, so it asks for nothing an opacity fade would have asked for. It
lifts the mark's colours toward white and settles over 200 ms, the Panel's own
default transition length. Use it when the mark appears somewhere the user is
not already looking, which is most of the time — the screen was not chosen by
whoever put the mark on it.

A backend without the capability returns a mark that is **already closed**, so
a caller needs no branch and an unsupported request costs nothing.

**macOS** draws one borderless floating window per mark:
`ignoresMouseEvents`, `setOpaque_(False)` with a clear background, and no
shadow — macOS derives the shadow from the alpha channel, so a window painting
nothing has one that must be invalidated by hand on every content change and a
mark being moved would trail the previous frame. It joins all Spaces, because
what it points at is on the screen in front of the user. Closing the backend
closes every mark: they are floating windows of their own, and stopping
without them would leave rectangles painted over the screen with nothing left
to remove them.
