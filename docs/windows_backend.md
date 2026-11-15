# PuiKit Windows Backend — Design

`WindowsBackend` is the Windows native GUI backend: a real window, antialiased
vector shapes, and true proportional/sized fonts — the same capability tier as
the macOS backend's CoreGraphics/CoreText.

It is built on **raw `ctypes`**, with no `pywin32` and no `comtypes` dependency.
`user32`/`kernel32` provide the window and message loop; **Direct2D** draws and
**DirectWrite** measures and shapes text; **WIC** decodes images; **Direct3D 11**
runs shader backgrounds. The only real third-party dependency is `numpy`, for
one piece of pixel math (§4).

Files: `puikit/backends/windows_backend.py` (the backend),
`_win32_native.py` (the ctypes/COM shim), `_win32_ime.py`,
`_win32_dragdrop.py`, `_win32_menu.py`, `_d3d_shader.py`.

---

## 1. Shape: a message loop over a double-buffered display list

Like the macOS backend, this one does **not** draw immediately. Widgets fill a
display list of drawing intents (text runs, boxes, scrollbars, icons) in
base-unit coordinates between `clear()` and `present()`:

- `self._back` — the list widgets append to this frame
- `self._front` — the list `WM_PAINT` replays, in pixels

`present()` swaps them. So the same widget code that runs on curses and macOS
gets real rectangles, color text, and emoji icons here.

Window messages arrive at a module-level `_global_wndproc`, which looks the
`hwnd` up in `_hwnd_backends` and forwards to that instance's
`_handle_message`. Cross-thread work is posted back to the UI thread as
`_WM_CALL_ON_MAIN_THREAD` (`WM_APP + 1`).

### The render thread (frame pipeline)

A frame has three stages: the widgets' `draw()` build the display list (①),
`_render` replays it into Direct2D (②), and `EndDraw` + `Present` flush it to
the GPU (③). By default all three run one after another on the UI thread, so
a frame costs their sum. With the **render thread**
(`_render_thread.py`), `WM_PAINT` only hands the presented list over and
returns; the render thread runs ② and ③ while the UI thread goes back to the
queue and builds the next list. A burst of frames (a key held down) then
runs at the pace of the slower side instead of the sum. A frame asked for
while the thread is idle starts at once, so a single key press waits no
longer than before; requests that arrive during a frame fold into one more
frame of the newest list.

`PUIKIT_RENDER_THREAD=1` turns it on, `0` off. Unset, it is on only on a
free-threaded Python, where the two threads really run at the same time; on
a GIL build they would take turns at the interpreter. The choice is made
when the backend is created, because the Direct2D factory is then made
multi-threaded and the D3D device without `SINGLETHREADED`.

Measured in an ARM Windows VM (x64 Python, a flat view of thousands of files
at font size 8, PageDown held at 30 Hz under an animated background): on
3.15t the frames that show new content went from about 16 a second to about
20, and the median key lag from about 30 ms to 0. Each replay costs more
while the UI thread runs beside it (about 45 ms against 25), so the gain is
the overlap, not a faster replay. On the 3.14 GIL build the same pipeline
made replays three to seven times slower, which is why it stays off there.
By hand on the same VM under the Shinagawa background, 3.15t went from about
19 new frames a second to 31 (the key repeat's limit), while the sequential
GIL and free-threaded builds were alike at about 20: the gain is the
pipeline, which needs the GIL off.

② and ③ share the thread rather than taking one each. Most of ③ is
`EndDraw`, Direct2D turning its batch into D3D commands on the CPU, and a
multi-threaded factory serializes every call on its objects under one lock,
so a third thread's `EndDraw` would stall the next frame's ② anyway.
`Present` returns at once (sync interval 0) unless the GPU's queue is full,
and in the measurements above it never was: the GPU is not what limits the
frame rate there, the render thread's CPU time is. Submitting the shader
background with an early `Flush` was tried and changed nothing. What did cut
that time is drawing less: the UI bitmap (§9), a brush per colour (§3) and a
cheaper text shadow (§3, "The text drop shadow"). By hand under Shinagawa in
a wider window, the first three took the render thread's frame from about 30
ms to 20, and the frames drawn from about 30 a second to 50. New frames stayed
at the key repeat's 30.

`PUIKIT_FPS=1` logs a line a second (`_frame_stats.py`) to check this on a
machine: frames drawn and new, the UI pass, the render thread's time, the
latency from `present()` to the Present that shows the list, the time in
`Present`, frames still on the GPU, and the GPU's own time from timestamp
queries where the driver gives them (the VMware adapter reports every
interval disjoint, so it shows none).

The hand-over goes through `WM_PAINT`, which Windows delivers only after
input and posted messages. With a key held, a list could have waited there
while the render thread idled. Measured, it does not: from `present()` to
the frame starting took 0.1-0.2 ms, and the latency was the frame's own
draw time (about 19 ms, Down and PageDown held, xefm#901). A `present()` that
asked the render thread directly was tried and measured the same, so the
paint message stays the one path.

What keeps the two threads apart:

- **The display list is the hand-over.** `present()` swaps in a fresh `_back`
  and never appends to a presented list again, so the render thread reads
  `_front` without a copy. `_render` takes the list once at its start and
  passes it down (`_prepare_shadows`, `_acquire_ui_cache`,
  `_render_display_list`).
- **The window scope is per thread.** `_active_win` (which secondary window
  `_window_scope` has scoped) is a thread-local, so while the UI thread
  handles a popup's messages the render thread still sees the main window's
  list and size.
- **The UI bitmap is checked, not dropped.** Without a render thread
  `present()` marks the layer (§9) stale; with one, the render thread may be
  drawing it right then, so the layer remembers the list it holds and
  `_acquire_ui_cache` draws it again when that is not the list being drawn.
- **One lock for everything else.** The render thread draws holding
  `RenderThread.lock` (reentrant). The UI thread takes it, through
  `_exclusive()` or the `_holds_render_lock` decorator, around whatever
  changes what a frame reads and does not swap atomically: the swap chain on
  `WM_SIZE`, the text formats on a DPI or base-font change, the effect chain
  and the background, the image cache (a decoded picture arriving, dead
  rasters drained), a secondary window's render, screen marks. A direct
  `_render()` call holds it too.
- **Text formats are created once.** `_resolve_style_font` is check-then-create
  on both threads (measuring on one, drawing on the other); `_format_lock`
  stops two creations racing, which would leak a format and key caches by an
  address then freed.

---

## 2. COM by vtable index, not by declared interface

Most ctypes COM wrappers declare a full `ctypes.Structure` vtable per interface,
which means getting *every inherited method's* signature right — including ones
that are never called. This backend doesn't.

A vtable is just a flat array of function pointers, so `ComPtr.call(index, ...)`
needs only the target method's **position** in that array (inherited methods
counted first, per the COM ABI) and that one method's real signature:

```python
ptr.call(_IDX_RT_FILL_RECTANGLE, None, [ctypes.POINTER(D2D1_RECT_F), ctypes.c_void_p],
         ctypes.byref(rect), brush.addr)
```

Every `_IDX_*` constant in `_win32_native.py` is annotated with the
interface/method it names, derived from the public `d2d1.h` / `dwrite.h`
declaration order. `release()` is just index 2 — `IUnknown::Release`.

> **Performance note.** `ctypes.WINFUNCTYPE(...)` builds a *new ctypes type* on
> every call — metaclass work, and it once ran on every D2D/DWrite call.
> Profiling put **>65% of a list's render time** inside that single line. The
> `(restype, argtypes)` signature is constant per method, so the constructed
> type is cached in `_functype_cache` and only the lightweight bind to the call's
> function pointer happens per call. Keep it that way.

WIC's factory is the one interface here obtained through a real
`CoCreateInstance` rather than a plain DLL export.

---

## 3. Text: DirectWrite measures *and* draws

**Do not measure text with GDI on this backend.** GDI's metrics for the same
font and text can disagree with DirectWrite's actual layout by a wide margin —
verified at ~40% wider for a proportional UI font, which invisibly widened a
text background fill (a reverse-styled label) far past the glyphs it was meant
to sit behind. Measurement goes through DirectWrite's own layout engine
(`IDWriteTextLayout::GetMetrics`), the same system that renders the run. GDI is
used **only** for the monospace base/grid font's cell size, which doesn't have
to agree with anything, since each grid glyph gets its own backend-declared clip
cell.

There are two text paths, chosen by `grid_aligned(style.font)`:

- **Grid path** — `font=None`, or an unsized/unnamed monospace request. One
  glyph per base-unit column, so it measures in **columns**
  (`display_width`), not by DirectWrite's natural advance. This mirrors the
  macOS backend's `_is_grid_font` test. Measuring an explicit
  `Font(monospace=True)` by natural advance instead made a wide (CJK) glyph
  1.67 columns rather than 2, so it rendered and wrapped unlike the same run on
  macOS.
- **Flow path** — a real proportional or sized font. Measured and drawn from
  **one** `IDWriteTextLayout` built by `_build_flow_layout`.

### CJK in the flow path

`_build_flow_layout` sets the bundled Noto CJK JP family on each CJK range of
the run (`text_layout_set_font_family` / `set_font_collection`, with
`DWRITE_TEXT_RANGE` offsets in **UTF-16 code units** — astral CJK is 2 units).
DirectWrite then shapes the whole run in a single pass, handling font fallback
*and* baseline alignment, so `GetMetrics` (measure) and `DrawTextLayout` (draw)
agree **by construction** — no per-segment summation, no reconciliation.

DirectWrite takes a line's baseline from its **tallest** run, so a run
containing a taller face has every glyph, the primary font's included, pushed
down by `that face's ascent − primary_ascent`. The taller face is Noto CJK on a
CJK range, or whatever system font DirectWrite falls back to for a glyph the
primary font lacks (Segoe UI Symbol / Emoji are 1.079 em tall, so with a named
`MS Gothic` a name holding `⧸` dropped 9 px at 40 DIP — xefm#788).
`_flow_baseline_fix` reads the laid-out line's `GetLineMetrics().baseline` and
moves the run so that baseline lands where it belongs (`_baseline_px`): the
shared ascent for a default face (`docs/font_system.md` §3.1), the font's own
ascent otherwise. It covers both without knowing which face fallback chose. The grid path's per-cell CJK
nudge keeps its own `_cjk_baseline_dy`.

### Text layouts are kept between frames

Building an `IDWriteTextLayout` shapes the run, and a frame draws the same file
names as the one before, so both paths keep their layouts in an LRU of 4,096:
the grid path in `_grid_segment_layout`, the flow path in `_flow_layout`
(xefm#866). A layout holds glyphs and advances from the run's own origin, not
where it is drawn or in what colour — those are `DrawTextLayout`'s arguments —
so a name that scrolls to another row, or becomes the cursor row, reuses it.
The flow key is the text, the primary format's address and whether the CJK
faces are in; the line's baseline is read once and kept beside the layout.
`_drop_text_layouts` releases both caches before `_rebuild_fonts` and
`_close_impl` release the formats the keys name.

Measuring does not use the flow cache. Eliding a name measures every growing
prefix of it, dozens of strings that are never drawn, so `_measure_text_uncached`
builds a layout of its own, reads the width, and releases it; the widths are
kept instead (`_width_cache`, `_measure_memo`).

Measured on XeFM's file panes holding Down (Windows 11 on Arm VM, x64
Python, 200 % scale, 37 rows a pane): the Direct2D pass went from 36 to 20 ms
a frame, the layouts built a frame from 113 to about one, and a held key's
list stopped 0.15–0.5 s after release instead of 1.5 s. The panes' pixels
were the same before and after. Details in xefm#866.

### The text drop shadow

macOS draws a theme's `drop_shadow` with the glyphs (an `NSShadow`
attribute); Direct2D has no per-draw shadow. Each run of text (consecutive
text commands, broken by any other command and by a text with a background of
its own; `_text_shadow_runs`) is drawn once, into a text buffer: a
transparent bitmap the size of the window (`_TextBuffer`).

1. Before the frame opens a batch, `_prepare_text_shadows` draws the list's
   runs into the buffer, as many as fit (a page, `_pack_text_page`; the rest
   below), packed in rows (tallest first), each in a slot of
   its own, in one batch of the buffer's own device context (a second
   context on the same Direct2D device; it shares the bitmaps and brushes).
   Then that context blurs the whole buffer once, through a `GaussianBlur`
   and a `ColorMatrix` that keeps only alpha times the shadow's peak, into an
   alpha-only (A8) bitmap of the same size: every run's shadow at once.
2. Where a run stands in the display list, the frame draws its own
   background, then its shadow (`FillOpacityMask` of its slot of the A8
   bitmap, black, at the shadow's offset, clipped to the run's rect), then
   the run out of its slot on top (`_shadow_run`). All of it lands under the
   clip, transform and group opacity in force at the run, so the shadow keeps
   its place in the display list.

A run's rect (`_text_shadow_plan`) is its cells padded by the shadow's reach
(offset plus three sigmas) and the glyphs' overhang, cut to the clip in force;
without the cut, a file name past its pane's edge reaches into the other pane.
The padding also keeps the blur of one slot from picking up the next.

The runs go in before the frame because Direct2D flushes one context's batch
whenever another context on the device draws. Drawing each run into the
buffer as the frame reached it took a flush of both batches a run, about 7 ms
a frame of a file list in the ARM VM; blurring each run in the frame (an
effect draw a run) cost about 3 ms more than the one blur. None of this
waits for the GPU: Direct2D queues a batch's draws when it flushes or ends,
and the buffer's batches end before the frame's begins. Measured in the ARM
VM at 2400x2000 (Shinagawa, a frame with a new file list), the shadow costs
about 3-4 ms over none (8.5-10.5 ms against 5.7-6.3).

A list whose runs do not fit in the buffer at once (a dialog over the file
list, or a cursor row splitting a pane's run, can make the runs together
larger than the window) is drawn in pages. At the first run the page does
not hold, the frame flushes its batch, which queues its reads of the page so
far, and the buffer is filled again from that run on, blurred again in one
pass (`_pack_text_page`); a secondary window's list starts that way. So there
is no limit past which the shadow changes, and a list that does not fit
costs a flush and a blur a page, not a run. The buffer's batches always end
before the frame draws what they drew: an earlier version drew each run that
did not fit as the frame reached it, with the buffer's batch left open, and
the frame then read slots still holding the runs drawn there before (file
names landed on other rows, xefm#901).

The two bitmaps (the window's size, 5 bytes a pixel together: about 24 MB
at 2400x2000) exist only while a shadow is on: `set_post_effect` releases
them when the shadow goes, as does device loss, and they grow to the largest
window drawn. The runs are kept while the list is, so a background tick or a
transition draws nothing into them. Blits under a fractional transform (a
transition) are resampled, which softens the text for those frames.

Two earlier versions drew the text twice: a recorded silhouette per run, each
through its own blur (over half of a scrolling frame, xefm#901), and then an
unblurred offset copy.

### The Python side of a draw call

With the layouts kept, the Direct2D pass is mostly Python, not Direct2D: in
the same bench, the native calls themselves took about 3.6 ms of the 20. Four
things take that Python cost out, and none changes what is drawn:

- **`ComPtr.call` keeps the bound function** per pointer and method index.
  Reading the vtable, reading the slot and binding a ctypes function to it
  were three ctypes round trips on every call, about 700 calls a frame
  (puikit#203). Functions bound to one slot are shared between objects of an
  interface (`_bound_cache`), and `release` drops the pointer's own.
- **A brush per colour.** `_set_brush` keeps a solid brush for each colour and
  alpha it is asked for (up to `_BRUSH_CACHE`) and only points `_brush` at it,
  so switching colour costs no `SetColor`; a run of one colour does not even
  look the brush up. It used to re-colour one shared brush, a few hundred
  `SetColor` calls a frame on a file list (about 1.5 ms in the ARM VM,
  xefm#901). The table goes with the render target, and is emptied between
  frames if an animating alpha has grown it past the limit.
- **A font resolves by identity first.** Every text command looks its font
  up (`_resolve_style_font`, `_resolve_style_cjk_font`, and for a grid run its
  baselines, `_grid_run_fonts`), and the lookup by value hashes the `Font`, a
  dataclass whose enum fields hash in Python, several times a command: about a
  tenth of a file list's pass (xefm#901). A widget's `Font` is usually one
  long-lived object, so `_font_by_identity` finds it by `id()` first; the
  entry holds the `Font`, so the id cannot be reused while it lives, and is
  checked against it anyway. Emptied wherever the formats go. A flow run also
  measures its width and line height only when a background or a rule needs
  them.
- **Attribute bits are tested on an int.** `style.attr & TextAttribute.BOLD`
  goes through the enum's Python-level `__and__`; `_render_text` and the font
  resolvers convert `style.attr` once and test module-level ints.
- **The grid walk is kept** (`_grid_plan`, puikit#202). Where a grid run
  splits into batched segments and lone cells depends only on its text and on
  whether a CJK face is in, so the plan is kept on those two and holds no
  format or layout: a font rebuild leaves it valid.

One more is not in the backend. `DrawContext.measure_text` keeps
default-style widths in a memo on the Panel, keyed by the text alone, which
skips the two `Style` hashes of the general path. The width of default text
follows the UI font, so the memo is emptied whenever the backend's
`_measure_epoch` moves; the Windows and macOS backends move it where they
reset `_measure_memo`. A backend without the counter does not take the fast
path.

Measured with the same bench as above, before and after, all five together:
the Direct2D pass went from 20 to 7 ms a frame (median of three runs each).
Under the profiler, `DrawContext.measure_text` fell from 1.25 to 0.73 s over
the run, and the Python pass from 52 to 42 ms a frame (the profiler inflates
it). After release, the held key's list kept moving for 0.33-0.49 s before;
after, it stopped at once in two runs of three and moved 0.33 s in the third.
The panes' pixels were byte-identical. Details in xefm#884.

`measure_line_height` has a related trap: `font=None` does **not** mean "one
grid row". `Panel._resolve()` substitutes the proportional UI font before
anything is drawn, so a content-sized default-font widget must be *measured* as
that UI font too — otherwise the pane is under-sized and the container clip
trims the taller font's descenders. Measure the font that will draw.

---

## 4. Images: WIC decode, then premultiply by hand

`IWICImagingFactory` decodes to BGRA and `CopyPixels` hands over raw pixels.
Then comes the part neither Windows API will do for you: Direct2D wants
**alpha-premultiplied** pixels, and neither WIC's format converter nor
`ID2D1RenderTarget::CreateBitmap` will premultiply them.

So `_premultiply_bgra` does it, vectorized with `numpy` (this is why `numpy` is
a mandatory dependency, not an optional one). The result goes to `CreateBitmap`
**directly** — not `CreateBitmapFromWicBitmap`, which would re-introduce the
unpremultiplied path.

**A file is decoded on a worker thread** (xefm#732; the shared design is
[`images.md`](images.md) §11). `wic_decode_pixels` does everything slow there:
it opens the file, puts an `IWICBitmapScaler` (Fant) right on the frame so the
picture comes out at the size it is shown, converts, turns it upright and
premultiplies. The worker uses a WIC factory of its own (`wic_thread_factory`),
because COM apartments are per thread. The UI thread only copies the finished
buffer into an `ID2D1Bitmap` (`rt_create_bitmap_from_premultiplied`), since the
bitmap belongs to the render target. A result that arrives late marks the UI
layer (§9) stale and repaints every window. Measured in the ARM VM on
a 6000x4000 photo, the UI thread went from 2.2 s (JPEG) and 3.0 s (PNG) to
under 30 ms.

---

## 5. DPI

Per-monitor DPI awareness must be set **before the first window is created**
(`set_process_dpi_awareness()` in `open()`); otherwise Windows bitmap-stretches
a 96-DPI surface and text renders blurry.

The window is created at a provisional size first, so its real monitor DPI can
be read (`get_dpi_for_window` / 96.0 → `_dpi_scale`) before the final size is
computed from the base unit for *that* monitor. Pixel-space constants in the
module are quoted at 96 dpi and multiplied by `_dpi_scale` at use, so the look
holds its physical size on a hi-dpi display.

On `WM_DPICHANGED`, every cached text format is recreated at the new scale and
the CJK fonts and recorded-glyph caches are dropped — cached glyphs are at the
old density.

---

## 6. IME (IMM32) — mode-gated, inline preedit

Mirrors the macOS `NSTextInputClient` contract: gated on focus so a CJK input
source never swallows a command-mode single-letter binding (`j`/`f`/`v`) into
composition, positioned at the focused field's caret, and rendered **inline** by
the widget rather than through the OS's floating composition box.

Three IMM32 mechanisms, each verified live before being wired in:

1. **Mode gate.** `ImmAssociateContext(hwnd, NULL)` fully detaches the window's
   input context — the technique games use to disable IME outright — so keys
   pass through as plain `WM_KEYDOWN`/`WM_CHAR` and a bare `j` dispatches as a
   command even with a Japanese IME selected. Re-associating the *same* handle
   re-enables it; the handle returned by the first detach **is** the window's
   default context, so one saved handle serves the window's lifetime.
2. **Position.** `ImmSetCompositionWindow` with `CFS_POINT` moves the composition
   anchor to the live caret. That alone does **not** move the candidate list —
   IMM32 treats it as a separate window — so `ImmSetCandidateWindow` with
   `CFS_CANDIDATEPOS` is always set alongside it. Skip the second and the
   candidate popup stays pinned at the IME's default (observed: bottom-right of
   the screen).
3. **Inline preedit.** `WM_IME_SETCONTEXT` is intercepted to clear
   `ISC_SHOWUICOMPOSITIONWINDOW`, suppressing the OS's composition box (the
   candidate popup is deliberately left alone — a widget draws the composition
   *string* inline, not a conversion list). `WM_IME_COMPOSITION`'s
   `GCS_COMPSTR`/`GCS_CURSORPOS` become an `IME_COMPOSITION` event, the same
   way `setMarkedText:` does on macOS.

Because the `WM_IME_COMPOSITION` handler returns 0 instead of forwarding to
`DefWindowProc`, Windows never synthesizes the `WM_CHAR` it would normally
produce for a commit — so the commit's `GCS_RESULTSTR` is read here and turned
into the KEY event itself.

**All three are per-window.** IMM32 associations are per-`HWND`, so a secondary
window (`create_window`) needs its own: `create_window` detaches its context at
creation and saves the handle on the `WinWindowHandle`, and the three IMM
entry points (`begin_text_input` / `end_text_input` / `request_text_input`)
plus `_handle_ime_message` address `_target_hwnd()` — the window the calling
Panel is scoped to (`Panel.render` enters `_window_scope` before
`_sync_text_input`), not `self._hwnd`. Addressing the main window instead left
a popup's field composing into a window the user was not typing in, which is
what made a chooser's filter box accept ASCII but never compose Japanese.

See [`keyboard_contract.md`](keyboard_contract.md) §6 for the focus gating.

---

## 7. Drag & drop — both directions, hand-built COM

Both directions need a real OLE drag session. An earlier version tried the
classic `DragAcceptFiles` / `WM_DROPFILES` shortcut for drop-in, to avoid
hand-building a COM object at all — **it does not work** for an arbitrary OLE
source (verified live: a cross-window drop from a plain `DoDragDrop` source
never produced a `WM_DROPFILES` message, regardless of `OleInitialize`
ordering). Real `IDropTarget` registration is what Explorer and every other
OLE-aware app rely on.

- **Drag-out** (`os_drag_drop`) — `DoDragDrop` with `FileDataObject`, a
  hand-built `IDataObject` exposing exactly one format (CF_HDROP over a real
  `DROPFILES` global memory block), plus an `IDropSource` answering "keep
  going?" as the mouse moves. `begin_file_drag` calls `DoDragDrop`
  **synchronously from the `WM_MOUSEMOVE` handler**, with mouse capture granted.
- **Drop-in** (`drag_and_drop`) — `RegisterDragDrop` with a hand-built
  `IDropTarget` that checks whether the incoming object offers CF_HDROP
  (`QueryGetData`) and, on drop, reads it with `GetData` + `DragQueryFileW`
  (which happily reads an `HDROP` from any source, not just a `WM_DROPFILES`
  message).

All three COM objects — `IDropSource` (2 real methods), `IDropTarget` (4),
`FileDataObject` (2) — are small enough to author their vtables directly in
ctypes: a `Structure` of `WINFUNCTYPE` callback pointers standing in for the
vtable, wrapping an instance whose only field is a pointer to it.

See [`drag_drop.md`](drag_drop.md) for the capability model.

---

## 8. Menus

`_win32_menu.py` turns a backend-agnostic `puikit.menu.Menu` into a real
`HMENU` — the window's menu bar and right-click context menus — mirroring
`_macos_menu.py`'s responder/tag pattern: every actionable item gets a unique
command id, and `WM_COMMAND`/`WM_INITMENUPOPUP` route back to it.

One platform difference drives the design: AppKit fires a menu item's callback
synchronously while a popup is tracking, but `TrackPopupMenu` only **posts** the
resulting `WM_COMMAND` — it arrives after `TrackPopupMenu` has already returned.
A fresh per-popup responder could be torn down before that posted message is
pumped, so the backend keeps a **single** `MenuResponder` for its whole
lifetime, shared by the menu bar and every popup.

---

## 9. Shader backgrounds (D3D11 + HLSL)

`_d3d_shader.py` is the twin of macOS's `_metal.py`. Like the Metal path it is
geometry-free: the vertex stage covers the viewport with one triangle from
`SV_VertexID`, so every frame is a single three-vertex `Draw` and all the work
is the app's fragment function. Per-frame CPU cost is writing a small constant
buffer, so the background costs the same whether it draws ten particles or a
million.

Two forced differences from Metal:

- **Language.** MSL is not HLSL, so a scene's `source` cannot be compiled here;
  the app supplies `Shader.source_hlsl`. This is the one place a background is
  genuinely backend-specific.
- **Compositing.** macOS gives the shader its own `CAMetalLayer` behind a
  transparent view, so it can advance without repainting the UI. Here the shader
  renders into an offscreen texture that the Direct2D device context wraps as a
  bitmap and draws as the frame's backdrop (`_render_shader_backdrop`). The
  texture is created on the backend's **own** D3D device precisely so D2D can
  wrap it with no copy.

**The UI bitmap** keeps the rest of the macOS arrangement. There the view's
backing store stays put while the shader layer behind it advances; here the
display list is drawn into an offscreen bitmap (`_acquire_ui_cache`, a
premultiplied `D2D1_BITMAP_OPTIONS_TARGET` bitmap at the client size), and
every frame draws the backdrop and then that bitmap with one `DrawImage`. The
layer is drawn again only when the list changes, a transition is running
(the pass then depends on the time), or the target or DPI changes.

It replaced a recorded `ID2D1CommandList`. Replaying a command list re-runs
every draw in it, text and blurs included, so a background-only tick still
cost most of a full pass. Measured in the ARM VM (2400x2000, Shinagawa, a flat
file list), a tick fell from about 12 ms to 1.5. A frame with a new list costs
the same as before: the replay's work had only moved from `EndDraw` into the
draw calls. Details in xefm#901.

Text in the layer is grayscale-antialiased. ClearType needs the pixels under
a glyph, and the layer is transparent. The swap-chain target is premultiplied
and was getting grayscale text already: a readback of the default mode
matched forced grayscale pixel for pixel. The device context now asks for
grayscale outright, so a transition frame, drawn straight into the frame,
matches the layer. macOS has drawn grayscale text since 10.14.

The `background_shader` capability is declared only when the shader-compile path
is actually usable (`HAVE_D3D_SHADER` — `d3dcompiler` present), the same gate
the macOS Metal path uses.

---

## 10. Animation

Fade is realized with `ID2D1RenderTarget::PushLayer` +
`D2D1_LAYER_PARAMETERS.opacity` — the Direct2D counterpart of a Core Graphics
transparency layer — so an animating group composites **once** at the group
opacity. An earlier implementation multiplied *each primitive's* alpha by the
group opacity, which double-blends overlapping content.

Full derivation, math, and the macOS/Windows symbol table:
[`animation.md`](animation.md) §3 and §6.

### The 60 fps tick is paced by the event loop, not by `WM_TIMER`

`WM_TIMER` is the **lowest-priority message on Windows**: `GetMessageW` /
`PeekMessageW` hand back sent messages, posted messages and input first, then
`WM_PAINT`, and only report a timer when the queue has nothing else at all. A
held-down arrow key is enough to starve it — autorepeat delivers a `WM_KEYDOWN`
every ~33 ms, each one re-rendering and invalidating, so the queue is never
empty and the timer never arrives. An animated background (whose clock advances
only in `_background_tick`) then freezes for as long as the key is held, while
every repaint re-renders the same shader frame. macOS has no such ordering: its
`NSTimer` is serviced by the run loop alongside key events, which is why the
symptom is Windows-only.

So `run_event_loop` peek-dispatches **one message per turn** and bounds its wait
by the next frame deadline (`MsgWaitForMultipleObjectsEx` with `QS_ALLINPUT |
MWMO_INPUTAVAILABLE`, or `INFINITE` when nothing is animating). A saturated
queue therefore still yields a tick between dispatches, and an idle app still
sleeps in the wait instead of spinning.

The `SetTimer` / `WM_TIMER` source is kept as a *second* source, because a
native modal loop (a tracked popup menu, `DoDragDrop`) pumps messages while
`run_event_loop` is not running, and there the timer is all that keeps ticking.
Both sources go through `_pump_animation_tick`, which fires at most one frame
per `_ANIM_FRAME_SECONDS` and never leaves the next deadline in the past — so
they cannot double-tick a frame, and a slow frame cannot turn the bounded wait
into a busy loop.

---

## 11. Capability profile

`PROFILE_GUI_DESKTOP`, with these overrides (`WindowsBackend.PROFILE`):

| Capability | Value | Why |
|---|---|---|
| `drag_and_drop` | `True` | drop-in: `IDropTarget` + `RegisterDragDrop` |
| `os_drag_drop` | `True` | drag-out: `IDropSource` + `DoDragDrop` |
| `ime` | `True` | mode-gated, inline preedit |
| `post_effects` | `True` | Direct2D-effects CRT composite |
| `background` | `True` | wallpaper image drawn under the UI |
| `background_shader` | `True`\* | D3D11 + HLSL; \*gated on `HAVE_D3D_SHADER` at runtime |
| `system_tray` | `True` | `set_tray`: `Shell_NotifyIconW` icon (from the host exe or a `.ico` path) + click menu |
| `system_notifications` | `True` | `post_notification`: the tray icon's info balloon (`NIF_INFO`), shown as a toast on Windows 10 and later. Returns False with no tray icon. `on_click` runs on `NIN_BALLOONUSERCLICK`, which names no notification, so only the latest one's runs |
| `clipboard_files` | `True` | `CF_HDROP` + "Preferred DropEffect", as Explorer's Copy / Cut (see [`drag_drop.md`](drag_drop.md)) |
| `clipboard_rich` | `False` | unused by any PuiKit app to date |
| `native_file_dialog` | `False` | " |
| `media_keys` | `False` | " |

`MacOSBackend.PROFILE` also leaves `native_file_dialog` and `media_keys`
`False`; neither is on a punch list.

---

## 12. Relationship to other systems

- [`rendering_system.md`](rendering_system.md) — the primitive floor this
  backend implements
- [`font_system.md`](font_system.md) — the grid-vs-flow distinction in §3 is
  this backend's half of the font seam
- [`animation.md`](animation.md) — group compositing, both GUI backends
- [`drag_drop.md`](drag_drop.md) — the two-capability model behind §7
- [`keyboard_contract.md`](keyboard_contract.md) — key normalization and the
  focus-gated IME contract behind §6
