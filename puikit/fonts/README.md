# Bundled default fonts

The GUI backends default to **Noto Sans** (proportional) + **Noto Sans Mono**
(monospace) — a designed-together superfamily whose two faces share vertical
metrics, so the base unit (derived from the mono face) fits the proportional
face and text does not clip.

These `.ttf` files and their `OFL.txt` license are **not committed** — they are
large binaries under the [SIL Open Font License 1.1](https://openfontlicense.org/).
They are fetched at build / dev-setup time:

```
make fonts          # or: python scripts/fetch_fonts.py
```

`make venv` (and every run/test target) depends on this, so a normal setup
populates them automatically. If the files are absent, the backends fall back to
the OS fonts (Consolas / Segoe UI on Windows; the system fonts on macOS).

Source: <https://github.com/notofonts/notofonts.github.io>
