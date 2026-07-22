# Bundled default fonts

The GUI backends default to **Noto Sans** (proportional) + **Noto Sans Mono**
(monospace) — a designed-together superfamily whose two faces share vertical
metrics, so the base unit (derived from the mono face) fits the proportional
face and text does not clip.

For Japanese (and CJK generally), two **Noto Sans CJK JP** faces are also bundled
— **Noto Sans CJK JP** (proportional) and **Noto Sans Mono CJK JP** (monospace),
Regular weight only. The non-terminal backends use these as a *fallback layer*:
the primary Latin faces still define the base unit and line pitch, and the CJK
faces supply glyphs/advances only for codepoints the Latin faces lack. Bold is
synthesized (Noto CJK advances are weight-invariant), so Regular alone suffices.

These `.ttf` / `.otf` files and their `OFL.txt` / `OFL-CJK.txt` licenses are
**not committed** — they are large binaries under the
[SIL Open Font License 1.1](https://openfontlicense.org/). They are fetched at
build / dev-setup time:

```
make fonts          # or: python scripts/fetch_fonts.py
```

`make venv` (and every run/test target) depends on this, so a normal setup
populates them automatically.

**Required vs optional.** The Latin faces are required — if their download fails
the fetch aborts. The two CJK faces (~16 MB each) are **optional**: a failed
download only warns, and the backends degrade gracefully — Japanese then renders
through the OS's own CJK fallback (Hiragino / Yu Gothic) on the native backends,
and through the web backend's em-width estimate on the web backend. If the Latin
files themselves are absent the backends fall back to the OS fonts entirely
(Consolas / Segoe UI on Windows; the system fonts on macOS).

Sources:
- Latin/Greek/Cyrillic: <https://github.com/notofonts/notofonts.github.io>
- CJK: <https://github.com/notofonts/noto-cjk>
