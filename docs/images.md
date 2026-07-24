# PuiKit Images — Design

Images follow the same rule as everything else: the app states an **intent** —
this file, fitted this way — and the backend decides how, or whether, to realize
it. What makes images interesting is that "whether" has three answers, not two:
a GUI backend draws real pixels, *some terminals* draw real pixels through an
out-of-band protocol, and the rest stamp an alt glyph.

`puikit/image.py` (geometry) · `puikit/widgets/image.py` (`ImageView`) ·
`puikit/backends/_terminal_graphics.py` (terminal protocols) · capability
`images`

---

## 1. `ImageView` and the five fits

```python
ImageView(path, fit="cover", alt="🖼", alpha=1.0)
```

| Fit | Behavior |
|---|---|
| `fill` | Stretch to the target rect, ignoring aspect ratio |
| `contain` | Largest aspect-preserving box inside the rect (bands may show around it) |
| `cover` | Cover the rect with aspect preserved (the image is cropped) |
| `width` | The target width is given; **height follows** the aspect ratio |
| `height` | The target height is given; **width follows** the aspect ratio |

`width`/`height` are the *intrinsic* fits: they size the widget itself, resolved
in `measure()`, so they belong in an intrinsic layout slot —
`Item(ImageView(p, fit="width"), size="content")` in a vertical stack,
`fit="height"` in a horizontal split. By draw time the rect is already
aspect-correct, so they render as `fill`. Only `fill`/`contain`/`cover` carry a
draw-time fit.

An unknown fit raises at construction — this one is a programming error, not
config.

---

## 2. Geometry lives in `puikit.image`, once

A file's pixel dimensions and the way an image fits a rect are
backend-independent facts, so they live in one module that no backend owns:

| Function | Answers |
|---|---|
| `image_size(path)` | Natural `(w, h)` in pixels, or `None` |
| `aspect_extent(...)` | The dependent extent, in base units, that locks the on-screen aspect ratio |
| `contain_box(...)` | The largest aspect-preserving box inside a target, centered |
| `cover_source(...)` | The centered source crop whose aspect matches the target |
| `zoom_window(...)` | The normalized source window a pan/zoom viewer is looking at |

`image_size` is a **dependency-free header parse** (PNG IHDR, GIF logical screen
descriptor, BMP `BITMAPINFOHEADER`, and a JPEG SOF marker scan). No Pillow
required — which is the point: the aspect ratio must be available on *every*
backend, TUI included, where it shapes the placeholder footprint and the layout
exactly as it does on GUI.

`contain_box` and `cover_source` are ratio-only, so the same code works in
pixels (GUI draw) or base units (TUI placeholder).

> **The non-square base unit.** `aspect_extent` takes `base_w`/`base_h` — the
> pixel size of one base unit — and solves in *pixels*, so the on-screen aspect
> ratio is right on a GUI backend whose base unit is taller than it is wide.
> Assuming a square cell makes the intrinsic extent roughly 2× too large.

---

## 3. `zoom_window`: the crop is **normalized**, and that is load-bearing

`zoom_window(zoom, cx, cy)` returns `(x, y, w, h)` as **fractions of the image,
0..1, top-left origin** — the form the `src` hint of `draw_image` accepts.

It is normalized on purpose, because backends do not agree on what unit an image
is measured in: a macOS `NSImage` reports **points**, derived from the file's
DPI, while Direct2D and Pillow use **pixels**. Each backend multiplies these
fractions by its own idea of the image size, so a Retina image — whose point
size is half its pixel size — crops correctly everywhere. Passing pixels here
would silently halve the crop on macOS.

Two behaviors worth knowing:

- The window is square in *fraction* space (`w == h == 1/zoom`), so scaling both
  axes by the same factor preserves the image's aspect at every zoom. Paired
  with `contain` — whose destination box is aspect-locked to the image — the
  view is undistorted throughout, and magnification changes only how much of the
  source is sampled.
- Panning past an edge **slides** the window back inside the image rather than
  shrinking it, so the zoom level survives a clamp.

---

## 4. Terminals that really do draw images

A character grid has no pixels, so `CursesBackend` normally reports
`images=False` and the Panel substitutes the `alt` emoji (a neutral `●` when
none is given). But several emulators accept pixel data out-of-band, through an
escape sequence the grid never sees. `_terminal_graphics.py` detects which one
is available and encodes for it, letting the curses backend flip `images` **on**
and draw genuine pictures in a terminal.

Three protocols, in preference order:

| Protocol | Emulators | Notes |
|---|---|---|
| **kitty** | kitty, Ghostty, WezTerm, konsole | Transmits PNG bytes, places them in a cell box, and can **delete** placements by id — the only one with real erase semantics |
| **iTerm2** | iTerm.app, WezTerm, mintty | OSC 1337 carrying the image file verbatim. No delete verb; a placement is cleared by overwriting the cells it covers |
| **sixel** | `xterm -ti vt340`, foot, contour, mlterm | Oldest and most widely implemented. Six vertical pixels per band per byte, from a quantized palette |

**Detection is environment-only, deliberately.** The alternative — a Device
Attributes query (`\x1b[c`) — means writing to the tty and blocking on a reply
that a non-supporting emulator never sends, risking a startup hang inside
curses' raw mode for what is a cosmetic capability. Env vars are unambiguous for
every emulator implementing these protocols, so the trade is worth it.
`PUIKIT_TERM_GRAPHICS` overrides the guess either way — a protocol name, or
`none` to force the alt-glyph fallback.

**Pillow is optional.** It is what crops (for the pan/zoom `src` hint) and
re-encodes; without it, a terminal falls back to what it can do unaided.

---

## 5. Per-backend decode

| Backend | Path |
|---|---|
| macOS | `NSImage` — reports **points**, not pixels (see §3) |
| Windows | WIC decode, then **manual alpha premultiply** with numpy — neither WIC's converter nor `CreateBitmap` will do it. See [`windows_backend.md`](windows_backend.md) §4 |
| Web | The browser decodes; the replayer draws to canvas |
| Curses | `_terminal_graphics.py`, or the alt glyph |

---

## 6. Relationship to other systems

- [`layout_system.md`](layout_system.md) — intrinsic sizing is what `fit="width"`
  and `fit="height"` plug into
- [`rendering_system.md`](rendering_system.md) — `draw_image` and its `src` hint
- [`widget_catalog.md`](widget_catalog.md) — `ImageView` in context
- `examples/demo_catalog/main.py` — the **Images**, **Alpha**, and **Blending**
  pages exercise all five fits, per-pixel alpha, and compositing
