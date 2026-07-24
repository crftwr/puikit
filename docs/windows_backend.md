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
containing CJK (Noto CJK's ascent exceeds the Latin face's) has every glyph,
Latin included, pushed down by `cjk_ascent − primary_ascent`.
`_flow_baseline_fix` shifts it back up; the flow re-anchor and the grid path's
per-cell nudge share the one `_cjk_baseline_dy` metric.

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
| `clipboard_rich` | `False` | unused by any PuiKit app to date |
| `native_file_dialog` | `False` | " |
| `system_tray` | `False` | " |
| `media_keys` | `False` | " |

`MacOSBackend.PROFILE` leaves the same four `False`; neither is on a punch list.

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
