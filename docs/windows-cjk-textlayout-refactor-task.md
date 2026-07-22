# Task: Shape each Windows text run in one IDWriteTextLayout (fallback + baseline for free)

Repo: `crftwr/puikit`. Windows-only backend work; **must be verified on real Windows**
(the authoring session runs on macOS and cannot exercise DirectWrite).

## Background — how we got here

The GUI backends now embed **Noto Sans CJK JP** (proportional + mono, Regular only)
as a fallback layer so Japanese renders in one bundled typeface. The three backends
reach that goal very differently:

- **Web** (`web_backend.py` + `web/client.js`): draws each run in **one**
  `ctx.fillText` whose CSS `font` names the whole `@font-face` chain
  (`"PuiSans", "PuiSansCJK", sans-serif`). The **browser** does font substitution
  *and* baseline alignment inside that one call.
- **macOS** (`macos_backend.py`): draws each run as **one** `NSAttributedString`
  whose font carries a `kCTFontCascadeListAttribute` cascade. **Core Text** shapes
  the whole run and aligns baselines in one pass.
- **Windows** (`windows_backend.py`): DirectWrite's `ID2D1RenderTarget::DrawText`
  is **format-based — one font family per call** and cannot be handed a custom font
  fallback. So the backend **splits** each run into per-font segments
  (`text.cjk_segments`) and draws each segment with its own `IDWriteTextFormat`
  (the primary Latin format, or a Noto CJK JP format).

That split-and-draw model forces the Windows backend to **manually reconcile**
everything DirectWrite would otherwise do in a single shaping pass. Two commits
already patched symptoms:

- `9a80bea` — grid-vs-flow now gated on `grid_aligned(font)` (was `font is None`).
- `105583b` — `_cjk_baseline_dy` nudges CJK segments up onto the primary baseline
  (Noto CJK's ascent 116 > Latin 106.9; each `DrawText` seats its baseline at
  `rect_top + that_font.ascent`, so a shared rect top dropped the CJK segment).

**Known latent bug the patches left open:** `_render_flow_text._draw_run` applies
the baseline nudge only in the multi-segment loop, **not** in the
`len(segments) == 1` fast path. A **pure-CJK proportional run** (an all-Japanese
label/dialog/button in the UI font) is one segment with the CJK format, so it still
drops below the baseline.

## Goal

Route each **flow** (proportional) run through **one `IDWriteTextLayout`** with
per-range font overrides on the CJK ranges, then `DrawTextLayout`. DirectWrite then
does fallback shaping **and** baseline alignment in a single pass — like the browser
and Core Text — which removes, in one move:

- `_cjk_baseline_dy` / `_cjk_dy_cache` and the whole baseline-nudge concern
  (including the single-segment gap above),
- the per-segment measurement in `measure_text` / `_flow_segments`,
- the per-segment draw loop in `_render_flow_text`.

**Out of scope:** the **grid** path (`grid_aligned(font)` — `font=None` or an
unsized/unnamed monospace font). Grid text is *deliberately* column-locked (one glyph
per base-unit cell), not fallback-shaped, and must stay per-cell. It can keep its
current CJK grid format + baseline nudge, or migrate to one-cell layouts later
(optional, lower value). Do not fold grid into this change.

## Mechanism

All the DirectWrite pieces already exist or are one adjacent vtable slot away, and
every index is anchored to one the file already verifies.

1. **Build the layout** — `dwrite_create_text_layout(factory, text, primary_format)`
   already exists (`_win32_native.py`).
2. **Per-range CJK overrides** — for each maximal CJK range (from
   `text.cjk_segments`, converted to **UTF-16** offsets), call on the layout:
   - `IDWriteTextLayout::SetFontCollection(collection, DWRITE_TEXT_RANGE)` — **vtable index 30**
   - `IDWriteTextLayout::SetFontFamilyName(wchar_p, DWRITE_TEXT_RANGE)` — **index 31**

   These are the CJK family names already registered in the custom collection
   (`"Noto Sans CJK JP"` / `"Noto Sans Mono CJK JP"`). Both indices are already
   listed in the file's `IDWriteTextLayout` vtable comment (SetFontCollection/
   FamilyName are the range setters at 30/31); `GetMetrics[60]` anchors that count.
   `DWRITE_TEXT_RANGE = { UINT32 startPosition; UINT32 length }`, positions in UTF-16
   code units (astral chars are 2 units — mirror the existing UTF-16 length handling
   in `rt_draw_text` / `dwrite_create_text_layout`).
3. **Draw** — `ID2D1RenderTarget::DrawTextLayout(D2D1_POINT_2F origin, layout, brush,
   options)` — **index 28**, explicitly documented in the file adjacent to
   `DrawText[27]`.
4. **Measure** — `IDWriteTextLayout::GetMetrics` (**index 60**, already used by
   `measure_text_dwrite`) now reflects the per-range fonts, so `measure_text` returns
   the layout width directly — no per-segment summation, and measure == draw by
   construction.

Because DirectWrite shapes the entire run, CJK glyph baselines align to the run's
baseline automatically → no manual `dy`, and the single-segment case is covered for
free.

## ABI risks — the reason this was deferred (verify live on Windows)

- `DrawTextLayout` takes `D2D1_POINT_2F` **by value** (2 floats / 8 bytes).
  `SetFontCollection` / `SetFontFamilyName` take `DWRITE_TEXT_RANGE` **by value**
  (2×uint32 / 8 bytes). `ComPtr.call` builds a `WINFUNCTYPE`, which *does* marshal
  structs by value, but the x64 aggregate-in-register ABI for these must be confirmed
  live — a wrong marshalling can **crash** (access violation), not merely error, and
  a Python `try/except` will not catch it.
- Cross-check every index against the file's verified anchors before running:
  `DrawText[27]` / `DrawTextLayout[28]`, `SetFontCollection[30]` /
  `SetFontFamilyName[31]`, `GetMetrics[60]`.
- Gate the new path behind the existing `self._cjk_available` and keep the current
  per-segment path as a fallback so a failure degrades instead of breaking Latin.

## What to remove once the layout path is verified

- `_cjk_baseline_dy`, `_cjk_dy_cache`, and their call sites in the **flow** path.
- The per-segment loop in `_render_flow_text._draw_run` and the per-segment sum in
  `measure_text` (keep the single-format-per-run fast path — it is now the norm).
- `_flow_segments` may shrink to "does this run contain CJK?" (whether to attach the
  overrides), since the split into `(segment, format)` pairs is no longer needed.

Keep `text.is_cjk` / `text.cjk_segments` — they still identify the ranges to override.

## Tests / acceptance

- Mixed CJK/Latin proportional run: one baseline, correct width, embedded Noto CJK —
  confirmed visually on Windows.
- **Pure-CJK proportional run: baseline-aligned** (the single-segment gap is gone).
- Latin-only runs: unchanged.
- `measure_text(flow)` equals the layout's `GetMetrics` width; measure and draw agree
  (no manual reconciliation).
- Grid path (file panes) unchanged.
- `test_windows_backend` + `test_cjk_fonts` + `test_text` green on Windows.

## Smaller alternative (if the refactor is too much for one pass)

Just apply `_cjk_baseline_dy` in the `len(segments) == 1` branch of
`_render_flow_text._draw_run`. That closes the known pure-CJK baseline bug with **no
new ABI surface**. The layout refactor is the "stop reconciling by hand" option; this
one-liner is the safe stopgap.

## Reference implementations (the target model, already shipped)

- Web single-pass shaping: `web_backend.py::_ser_text` / `web/client.js` `case "text"`.
- macOS cascade: `macos_backend.py::_with_cjk_cascade`.
Both prove the "hand the whole run to one shaping pass" model; this task brings
Windows to it.
