# PuiKit Web Backend — Design

Status: **describes the implemented v1** (`puikit/backends/web_backend.py`,
`puikit/backends/_web_server.py`, `puikit/backends/_ttf.py`,
`puikit/backends/web/`).

The web backend is the pixel/vector GUI backend for a **web browser** — the
`CanvasBackend` slot in the roadmap (`CLAUDE.md`, Planned Backends). A PuiKit app
runs unchanged on it: `create_backend("web")` (aliases `"webbrowser"` /
`"browser"`) launches the user's browser and renders the same widget code that
runs on curses / macOS / Windows.

    python examples/demo_catalog/main.py --backend web
    python examples/demo_catalog/main.py --backend web --font-size 18
    make demo-web            # or hello-web

It advertises the web GUI capability profile (`PROFILE_GUI_WEB`): pixel layout,
vector control faces, **proportional fonts**, layering, transparency, shadows,
images, hover, and a native cursor.

---

## 1. Shape: a local server + a canvas replayer

```
Python process                                  Browser tab
──────────────                                  ───────────
WebBackend                                       index.html
  display list (base-unit ops)                     <canvas>
  ── serialize to pixels + CSS ──▶  WebSocket  ──▶  client.js replays ops
  event loop  ◀── normalized JSON ── WebSocket ◀──  keydown / mouse / wheel
_web_server (HTTP + WebSocket)                    client.css (@font-face)
```

- **`open()`** starts a local HTTP + WebSocket server (`_web_server.WebServer`)
  on an ephemeral `127.0.0.1` port, then opens the browser at it with the stdlib
  `webbrowser` module — the module the backend is named after. It blocks until
  the tab connects and reports its canvas size (or a 15 s timeout), so the app's
  first `render()` sizes to the real window.
- **`present()`** serializes the frame's display list and sends it as one JSON
  text frame. The client clears the canvas and replays each op.
- **Input** (`keydown` / mouse / `wheel`) is streamed back as small JSON
  messages; a reader thread turns each into a PuiKit `Event` on a queue that the
  event loop drains.

The client is deliberately dumb: **all** base-unit→pixel math and font
resolution happen in Python, so each op is already in CSS pixels with a
ready-made CSS font/color string. `client.js` knows nothing about widgets, base
units, or layout — it is a canvas command interpreter plus an input reporter.

### Transport (hand-rolled WebSocket)

`_web_server` implements the RFC 6455 subset a single trusted localhost client
needs — handshake (`Sec-WebSocket-Accept`), unfragmented masked text frames in,
unmasked text frames out, ping/pong, close — with `socket` + `http.server` +
`hashlib` only. This keeps PuiKit dependency-free, the same choice the Windows
backend makes with raw ctypes. One HTTP server serves both the client assets
(`/`, `/client.js`, `/client.css`, `/fonts/*.ttf`) and the `/ws` upgrade; there
is exactly one client (the launched tab), and a later connection replaces an
earlier one. `tests/test_web_backend.py` exercises the framing both ways with a
tiny hand-rolled client over a real socket.

---

## 2. Text is measured in Python

This is the backend's defining constraint. The layout/measurement seam
(`measure_text`, `measure_line_height`, `font_metrics`, `measure_font_size`)
runs **synchronously inside `panel.render()`**, before anything reaches the
browser — a widget sized to its label, a wrapping paragraph, and a
content-sized region all need a width *now*, not after a round-trip. So the
backend cannot ask the canvas how wide a run is; it must **predict** the
browser's rendering.

It does that by measuring the **same font files the browser draws with**:

- `client.css` binds the bundled Noto faces (`puikit/fonts`, served at `/fonts`)
  as `@font-face` families `PuiMono` / `PuiSans`, and sets `font-kerning: none`
  and `font-variant-ligatures: none` on the canvas.
- `_ttf.py` is a from-scratch TrueType reader (`struct` only) that parses just
  the horizontal-metrics tables — `head` (unitsPerEm), `hhea`, `hmtx` (advance
  widths), `cmap` (format 4 for the BMP, format 12 for astral) — and returns
  advances and ascent/descent as **em fractions**.

With kerning and ligatures disabled, a run's rendered width equals the plain sum
of its glyphs' `hmtx` advances — exactly what `_ttf` returns. So Python's
`measure_text` matches the canvas to the pixel, and a proportional label,
button, or wrapped paragraph lands where the layout placed it. The base unit is
grounded on the mono face's advance × the base point size, kept as a float so
`measure_line_height(font=None)` is exactly `1.0` and the drawing path stays
crisp.

### Glyphs the bundled fonts lack (CJK, emoji)

The bundled Noto Sans/Mono do **not** contain CJK or astral emoji glyphs, and a
missing glyph's `.notdef` box is far narrower than the full-width glyph the
browser's fallback font actually draws. So `WebBackend._measure_units` estimates
a glyph the font *lacks* (`_ttf.has_glyph` is False) by its **em width**: a
full-width (wide) glyph is ~1 em, a half-width one ~0.5 em (`display_width/2`
ems), which is what the browser draws a fallback ideograph at. Two earlier
estimates were both wrong: the `.notdef` box (near-zero — Japanese "wrapped"
without visibly wrapping) and the terminal column count (2 base units per wide
glyph, but one base unit is only 0.6 em, so 2 columns ≈ 1.67 em — too wide,
Japanese wrapped early). Latin/Greek/Cyrillic (the vast majority of any UI)
still measure exactly from their advances.

The em estimate is close, not pixel-exact (the fallback font's real advance is
unknown to Python), so a wide-glyph run can still wrap a hair off. Bundling a
CJK metrics face would make it exact; deferred for size.

---

## 3. Rendering: the op vocabulary

`present()` walks the display list and emits a flat `ops` array. Each op is a
small JSON list `[kind, ...pixel args]`; `client.js` switches on `kind`:

| op | canvas realization |
|----|--------------------|
| `fill` | `fillRect` |
| `box` | optional `fillRect` + inset `strokeRect` |
| `rrect` | `roundRect` path, fill and/or stroke (the vector control face) |
| `check` / `chevron` | stroked check / disclosure path |
| `text` | `fillText` at a Python-computed baseline; underline/strike drawn as a rule |
| `dim` | translucent black `fillRect` (the modal scrim) |
| `shadow` | `roundRect` fill with `shadowBlur` (drop shadow under a layer) |
| `sbar` | track + rounded thumb |
| `img` | `drawImage(src-rect → dest-rect)` at a global alpha |
| `clip` / `unclip` | `save` + `rect` + `clip` / `restore` |

Colors arrive as ready `rgba(...)` strings (RGBA folds its alpha in Python);
text attributes are resolved Python-side — `REVERSE` swaps fg/bg, `DIM` scales
the foreground alpha, `BOLD`/`ITALIC` fold into the CSS font, underline /
strikethrough become flags. Coordinates are multiplied by the base unit (float
CSS px) before serialization; the client scales the canvas backing store by
`devicePixelRatio` and draws in CSS px, so output is crisp on HiDPI.

**Images** are sent once each as a base64 `data:` URL `asset` message (keyed by
path) before the first frame that references them; the client caches the decoded
`Image` by id. A reconnecting tab (a reload) re-requests them because
`_on_connect` clears the sent set.

---

## 4. Events and motion

- **Keyboard** — `client.js` forwards `KeyboardEvent.key` + modifier flags;
  `translate_key` (module-level, tested without a browser) maps named keys and
  routes printable glyphs through the shared `char_key_event`, so `Shift-A` is
  `key="a"` + `{shift}` and `Shift-1` is `("!", {})`, identical to every other
  backend (`docs/keyboard_contract.md`). Browser `Meta` maps to the `cmd`
  command modifier. A small allowlist (reload / close / new-tab / devtools) is
  left to the browser; everything else is `preventDefault`ed and delivered.
- **IME / composition** — a hidden, caret-positioned `<input>` engages the OS
  IME while a text widget is focused. `begin_text_input` / `end_text_input`
  focus / blur it (the browser equivalent of `NSTextInputClient`), and
  `request_text_input` moves it to the field's caret so the candidate window
  appears there. `compositionupdate` streams the preedit as `IME_COMPOSITION`
  (the widget draws it; the input's own text is transparent); `compositionend`
  and direct typing commit as one `char_key_event` per character — the same
  event shapes the macOS backend produces. While the input holds focus the
  window-level key handler stands down and the input's own handler forwards
  command keys and chords (arrows, copy/paste), so navigation still works.
- **Mouse** — down / up, hover `move` vs. `drag` (decided by the tracked press),
  and `wheel` → `MOUSE_SCROLL` with a notch plus precise `scroll_units` (base
  units) for smooth trackpad scrolling. Moves are coalesced to one per animation
  frame. The Panel synthesizes `MOUSE_CLICK` from down/up itself.
- **Resize** — the client reports canvas CSS size on load and on window resize;
  the backend recomputes `size` and enqueues a `RESIZE` event so layouts reflow.
- **Animation ticks** — `request_animation_ticks` is driven by a timer thread
  that enqueues a tick sentinel at **30 fps** while callbacks remain; the
  event-loop thread runs them (they re-render), so caret blink, the busy
  spinner, and geometry/color transitions animate. The ticker stops when the
  last callback unregisters. Because a tick re-renders and sends a whole frame,
  the loop **coalesces**: each drain of the queue collapses any number of queued
  ticks into a single re-render (dropping stale frames), so a perpetual
  animation — the Progress page's busy indicators — can't saturate the socket
  and starve input.

---

## 5. Capability profile (`PROFILE_WEB`)

`PROFILE_GUI_WEB` with a few axes turned **off** for v1, so the Panel substitutes
its documented fallback and never calls a primitive this backend does not serve:

| axis | v1 | note |
|------|----|------|
| `pixel_layout`, `hairline`, `vector_shapes`, `fonts`, `proportional_text` | on | full pixel/vector/text fidelity |
| `layering`, `transparency`, `shadow`, `images`, `hover`, `pointer_shape`, `os_open` | on | |
| `ime` | on | composition via a hidden, caret-positioned page `<input>` |
| `animation` | **off** | composited fade/scale apply immediately; geometry/blink still animate via `animation_ticks` |
| `animation_ticks` | on | timer-driven re-render, 30 fps, coalesced |
| `drag_and_drop` | **off** | no OS file drop-*in* |
| `icons` | **off** | `draw_icon` falls back to a text/emoji glyph (no icon set bundled) |

Turning a capability off is the framework-sanctioned way to defer it: the app
never branches on the backend, and each fallback is the Panel's existing one.

### Deferred / future work

1. **Composited `animate()`** — real alpha/transform fade/scale/slide via CSS or
   per-group canvas compositing, to flip `animation` on.
2. **CJK/emoji metrics** — bundle a CJK metrics face (or a server-side measure of
   the browser's fallback) to make wide-glyph fit/wrap exact (§2).
3. **IME clause highlight** — the browser reports the whole preedit but not the
   converting clause, so `target_start/end` collapse (no thick target underline);
   deriving it would need a richer composition source.
4. **Clipboard / drop-in** — the browser clipboard and DataTransfer APIs behind
   `clipboard_rich` / `drag_and_drop`.
5. **Frame diffing** — v1 sends the whole display list each frame; a dirty-rect
   or op-diff protocol would cut bandwidth for large windows.

---

## 6. Relationship to other systems

- **Rendering** (`docs/rendering_system.md`) — the web backend implements the
  core primitive floor plus the `vector_shapes` / `images` / `shadow` extended
  primitives; everything else the Panel resolves before it reaches here.
- **Fonts** (`docs/font_system.md`) — the measurement seam is served from `_ttf`
  advances; a `Style.font` renders a real face/size/weight/slant, folding
  bold/italic from `attr` as the doc specifies.
- **Layout** (`docs/layout_system.md`) — `pixel_layout` is on, so boundaries land
  on device pixels (fractional base units), re-resolved from the canvas size on
  every resize.
- **Capabilities** (`puikit/capability.py`) — `PROFILE_WEB` derives from
  `PROFILE_GUI_WEB`; the app never reads it.
