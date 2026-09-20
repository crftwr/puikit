# PuiKit Images — Design

Images follow the same rule as everything else: the app states an **intent** —
this picture, fitted this way — and the backend decides how, or whether, to
realize it. What makes images interesting is that "whether" has three answers,
not two: a GUI backend draws real pixels, *some terminals* draw real pixels
through an out-of-band protocol, and the rest stamp an alt glyph.

A source is a **path** or a **`RasterImage`** (§8), and `image_formats()` (§5) is
how an application learns which of the two a given file wants to be.

`puikit/image.py` (geometry, `RasterImage`, `source_key`) ·
`puikit/widgets/image.py` (`ImageView`) ·
`puikit/backends/_terminal_graphics.py` (terminal protocols) ·
`puikit/backends/_image_cache.py` (the GUI backends' budgeted LRU) · capability
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

## 5. Per-backend decode, and `image_formats()`

| Backend | Decoder | `image_formats()` comes from |
|---|---|---|
| macOS | `NSImage` / ImageIO — `NSImage.size` reports **points**, not pixels (see §3), so `image_size` reads the stored pixel size from `CGImageSourceCopyPropertiesAtIndex` instead | `NSImage.imageFileTypes()`, minus the Classic OSType codes it mixes in |
| Windows | WIC decode, then **manual alpha premultiply** with numpy — neither WIC's converter nor `CreateBitmap` will do it. See [`windows_backend.md`](windows_backend.md) §4 | the installed WIC decoders, enumerated (`CreateComponentEnumerator`) |
| Web | The browser decodes; the replayer draws to canvas | a fixed list — the decoder is on the far end of the socket and cannot be asked |
| VT / curses | `_terminal_graphics.py`, or the alt glyph | Pillow's own registry, plugins included |

`image_formats()` answers **"which extensions do you draw from a path?"** and is
empty by default — which claims nothing, and must never be read as "nothing
works".

It exists because the answer is a property of the *running system*, not of the
format. ImageIO reads HEIC, camera RAW and JPEG XL on any current macOS. On
Windows the same formats arrive as Microsoft Store extensions the user may not
have, so the honest answer differs between two machines running the same build.
On a terminal it is whatever Pillow plugins happen to be installed. An
application that can decode a picture itself asks rather than assumes: a listed
suffix travels as a path, with no decode on its side and no pixels copied, and an
unlisted one is its own to open and hand over as a `RasterImage`.

---

## 6. Relationship to other systems

- [`layout_system.md`](layout_system.md) — intrinsic sizing is what `fit="width"`
  and `fit="height"` plug into
- [`rendering_system.md`](rendering_system.md) — `draw_image` and its `src` hint
- [`widget_catalog.md`](widget_catalog.md) — `ImageView` in context
- `examples/demo_catalog/main.py` — the **Images**, **Alpha**, and **Blending**
  pages exercise all five fits, per-pixel alpha, and compositing

---

## 7. Document sources: `base_dir` and `http(s)://` (`MarkdownView`)

A backend opens image *files*; the path string travels from widget to backend
untouched, and it is what every cache in the pipeline is keyed through
(`source_key`, §8). Both facts pin where document-level sources get resolved:
**in the widget, before layout**, so `measure_image` and `draw_image` read the
same real file and no backend learns URLs exist.

- **Relative paths.** `MarkdownView(..., base_dir=...)` resolves a relative
  `![alt](docs/a.png)` against the document's own directory — `from_file`
  defaults it — the way the same file renders on GitHub. No `base_dir` keeps
  the old meaning: relative to the process CWD.
- **Remote URLs.** `puikit/_remote_image.py` (internal) downloads an
  `http(s)://` source once into `<tempdir>/puikit-remote-images/<digest>`,
  on a daemon thread, and the widget substitutes that local path. The write
  is atomic (a `.part` renamed into place), so a path the pipeline ever sees
  always names a complete file — the caches above never pin a half-written
  read. While the fetch is in flight the document shows the image's alt text;
  when it settles the view invalidates its layout and repaints — handed to
  the UI thread via `call_on_main_thread` on `main_thread_dispatch` backends,
  drained by a `request_animation_ticks` callback elsewhere (curses, web),
  and picked up by the next natural draw as a last resort. A failed download
  is remembered for the life of the process, like the Windows backend's
  negative decode cache, not re-attempted every layout pass.

---

## 8. `RasterImage`: pixels without a file

A path is the fast lane — every backend has a decoder behind it — but it is only
ever a *file*. An application that produced its pixels some other way (a format
the backend's decoder does not know, a frame it rendered, bytes that never
touched a disk) used to have exactly one way to hand them over: write a PNG back
out and pass its name. That is an encode and a decode to move data the backend
was about to be given.

`RasterImage(width, height, data)` is the other thing every `draw_image`,
`image_size` and `measure_image` accepts.

**The buffer is straight-alpha RGBA8**, tightly packed, top-left origin, stride
`width * 4`. Straight rather than premultiplied because that is what a decoder
hands over — Pillow's `tobytes()` on an `RGBA` image is already exactly this, and
so is every other library's. Windows needs premultiplied BGRA and pays a swizzle
for it; that is the cheaper trade than making every producer premultiply for a
backend it cannot see.

**Identity, not content, is what caches key on.** `source_key(source)` returns
`("raster", identity, revision)` for a raster and `("path", path, mtime, size)`
for a file. Content hashing is the obvious identity for a raster and the wrong
one: an application that paints into its buffer changes it every frame, and
hashing megabytes per frame costs more than the work the cache exists to avoid.
So a raster names itself, and says when it changed — `update()` or `touch()`. A
producer that writes through `data` without calling `touch()` is the one way to
get a stale picture on screen.

Keying the *path* case by `(mtime, size)` rather than by the name alone closed a
bug of its own: the same path holds different pixels after a rebuild or a
thumbnail refresh, and a name-keyed cache kept serving the old picture until it
happened to be evicted.

**Per backend.** macOS builds an `NSBitmapImageRep` with
`NSBitmapFormatAlphaNonpremultiplied` and pins the `NSImage`'s size to pixels, so
a raster measures the same here as everywhere else. Windows skips WIC and goes
straight to the RGBA→BGRA swizzle, the premultiply and `CreateBitmap` — the
second half of the path it already ran. The terminals wrap the buffer with
`Image.frombuffer` instead of `Image.open`, which makes a re-crop of a magnified
picture *cheaper* than the path case, where every zoom step re-decoded the file.
The web backend is the one place a PNG encode is unavoidable: it is the wire
format the browser reads.

**The caches are budgeted.** The macOS and Windows decoded-image dicts used to be
unbounded, which was defensible while only files could land in them. A raster
admits a 24-megapixel photo as ~100MB of RGBA, so both now use
`_image_cache.ImageCache`: an LRU spending a byte budget, evicting least-recently
used first, telling the backend so it can release a native handle, and never
evicting an entry to make room for itself — an image larger than the whole budget
is still drawn, and still cached, alone.
