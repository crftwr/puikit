# Task: Exact CJK text measurement for the Web backend

Repo: `crftwr/puikit` — branch off `main`.

## Background

The web backend measures text in Python by summing `hmtx` advances of the
bundled Noto Sans / Noto Sans Mono faces (`puikit/backends/_ttf.py`), with
kerning and ligatures disabled in `client.css` so the browser's rendered width
equals the sum. This is pixel-exact for Latin/Greek/Cyrillic.

CJK glyphs are **not** in the bundled faces. Today
`WebBackend._measure_units` (`puikit/backends/web_backend.py`) estimates a
missing glyph at `display_width(ch) / 2` ems (full-width ≈ 1 em), while the
browser draws it from an OS fallback font whose real advance Python cannot
see. The estimate is close for Han/kana but drifts on halfwidth kana,
punctuation, and mixed runs — see `docs/web_backend.md` §2, "Glyphs the
bundled fonts lack", and "Deferred / future work" item 2. TFM renders
Japanese file names and paths constantly, so this gap matters.

## Goal

Bundle CJK metric faces and wire them into **both** sides of the measurement
seam — the `@font-face` fallback chain the browser draws with, and the `_ttf`
tables Python measures with — so that for any glyph the CJK face covers,
measurement returns to the exact sum-of-advances contract. The em estimate
remains only as the last resort (astral emoji, exotic scripts).

Latin behavior must be bit-identical to today: the CJK face sits **after**
the existing faces in every chain, so any glyph the current fonts cover keeps
its current advance.

## Changes

### 1. `scripts/fetch_fonts.py` — fetch the CJK faces

- Add Noto CJK JP faces to `_FILES`. Source: the `notofonts/noto-cjk` GitHub
  repo (Sans/Mono, Japanese/JP). Verify the exact raw URLs at implementation
  time — do not trust the paths from memory. Prefer static (non-variable)
  files; OTF is fine (see §3).
- Bundle **Regular weight only** for CJK: in Noto CJK the advances do not
  differ between weights (mono is strictly metrics-compatible), so Bold can
  reuse the Regular table for measurement and the browser may synthesize bold.
  This halves the download (~16 MB per face is already large).
- Two files: one mono CJK face, one proportional (sans) CJK face, mirroring
  the existing mono/sans family split.
- Keep the sfnt magic validation; `OTTO` is already accepted.
- CJK files should be **optional at runtime**: if a download fails or the
  file is absent, the backend must degrade to the current em-estimate path,
  not crash. Decide whether the fetch script itself hard-fails (current
  behavior for all files) or warns and continues for the CJK entries; a warn
  is preferred so offline dev setups keep working. Update
  `puikit/fonts/README.md` accordingly.

### 2. `puikit/backends/web/client.css` — fallback chain

- Add `@font-face` rules `PuiMonoCJK` / `PuiSansCJK` for the new files
  (served from `/fonts/`, same as today). Use the correct `format()` for the
  file type actually bundled (`opentype` for `.otf`).
- Declaring the same file for both `font-weight: 400` and `700` is
  acceptable (bold synthesized by the browser); keep whichever the
  measurement model assumes — synthetic bold does not change advances, which
  is what matters.
- Optional: add `unicode-range` covering CJK blocks so a session that never
  draws CJK never downloads the large file. Only do this if it demonstrably
  works with canvas `fillText`; verify, don't assume.

### 3. `puikit/backends/web_backend.py` — measurement chain

- In the `__init__` table loading (around the `_ttf.load(...)` block), load
  the CJK tables when the files exist; store them alongside the primary
  tables. Absent file ⇒ `None`, no error.
- Generalize `_Face` (or its call sites) from one table to an ordered
  fallback list: `[primary, cjk]`. Update the `css_family` strings to insert
  the CJK family between the primary family and the generic keyword, e.g.
  `'"PuiMono", "PuiMonoCJK", monospace'`. Order in CSS must match order in
  Python — that equivalence is the entire correctness argument.
- `_measure_units`: walk the table chain with `has_glyph`; first table that
  has the glyph supplies the advance via `table.advance(cp) * em_units`.
  Note the em conversion: `advance()` returns em fractions of *that* table's
  own `units_per_em`-normalized em, and both faces render at the same CSS px
  size, so the existing `em_units = face.px / self._base_w` factor applies
  unchanged to whichever table matched. Only when no table has the glyph,
  fall back to the current `display_width(ch) / 2.0` estimate.
- Bold styles: measurement for bold CJK uses the Regular CJK table
  (weight-invariant advances). Make sure the `_face_cache` key still
  distinguishes what it needs to.
- Line metrics (`measure_line_height`, `font_metrics`) must continue to come
  from the **primary** face only. Do not mix CJK vertical metrics in; the
  base unit and line pitch are defined by the primary faces.
- Update the module docstring lines that currently describe the em estimate.

### 4. `puikit/backends/_ttf.py` — verify OTF

`_parse` reads the sfnt table directory without checking the version tag, so
CFF-flavoured (`OTTO`) files should already parse for `head`/`hhea`/`hmtx`/
`cmap`. Verify with the real downloaded file; if anything trips (e.g. a
`cmap` subtable format the reader skips), extend minimally. Do **not** add
glyph-outline parsing — metrics only, as today.

### 5. `puikit/backends/_web_server.py` — MIME type

Add `.otf` → `font/otf` next to the existing `.ttf` entry if OTF files are
bundled.

### 6. `puikit/backends/web/client.js` — font readiness

The client already awaits `document.fonts.load(...)` for the declared faces
before reporting ready. Extend the spec list to the CJK families so the first
frame is not drawn (and mismeasured visually) with an OS fallback while the
16 MB face is still loading. If `unicode-range` lazy loading is adopted in
§2, rethink this: preloading defeats it — pick one strategy and document it.

### 7. Docs

Update `docs/web_backend.md`: rewrite the "Glyphs the bundled fonts lack"
subsection to describe the chain (primary → CJK → em estimate), and strike
item 2 from "Deferred / future work". Note the Regular-only-bold decision.

## Tests

Extend `tests/test_web_backend.py` (or a new `tests/test_web_cjk.py`):

1. **Exactness**: with the CJK font present, `measure_text` of a
   Japanese string (mix Han, hiragana, katakana, halfwidth katakana, CJK
   punctuation `、。「」`) equals the sum of per-char advances read directly
   from the CJK table via `_ttf` — no em-estimate term. Skip the test with a
   clear reason if the font file is absent.
2. **Chain order**: a Latin string measures identically with and without the
   CJK tables loaded (byte-for-byte equal floats).
3. **Graceful absence**: with CJK tables forced to `None`, a Japanese string
   still measures via the em estimate (current behavior preserved).
4. **Bold invariance**: bold and regular CJK runs measure equal widths.
5. If `_ttf` needed OTF changes, a direct `_ttf.load` test on the OTF file
   (units_per_em sane, `has_glyph(ord('あ'))` true, advance ≈ 1.0 em for a
   full-width glyph).

Run the full suite; nothing outside the web backend may change behavior.
`make demo-web` with a directory of Japanese file names is the manual check:
wrapped Japanese text must land flush, not a hair short or long.

## Consistency check (do not skip)

`puikit/text.py::char_width` treats East Asian **Ambiguous** characters as
width 1. The em-estimate path inherits that. Once real CJK advances are used,
an Ambiguous glyph covered by the CJK face will measure at that face's true
advance instead — this is more correct, but confirm no TUI/Web layout test
encodes the old assumption. If any widget aligns columns by `display_width`
while the web backend measures by advance, note the discrepancy in
`docs/web_backend.md` rather than papering over it.

## Out of scope

- Emoji metrics (stay on the em estimate).
- Frame diffing, clipboard, drag-in, composited `animate()` (separate
  deferred items).
- Any change to native macOS/Windows backends — they already measure via OS
  font fallback and are correct.

## Acceptance criteria

- Japanese text on the web backend is measured from real font advances,
  matching the browser's rendering pixel-exactly for glyphs the CJK face
  covers.
- Latin measurement and all existing tests unchanged.
- Missing CJK font files degrade to today's behavior with no error.
- Docs and tests updated as above.
