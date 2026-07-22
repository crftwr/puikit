#!/usr/bin/env python3
"""Download the bundled default fonts into ``puikit/fonts/``.

The GUI backends default to Noto Sans + Noto Sans Mono — a designed-together
superfamily whose proportional and monospace faces share metrics, so the base
unit fits both and text does not clip. The font files are **not committed**
(they are large binaries under their own OFL license); they are fetched here at
build / dev-setup time (``make fonts``, which the venv/run targets depend on),
and the backends fall back to the OS fonts if the files are absent.

Two tiers of files:

* **Required** — the Latin/Greek/Cyrillic Noto faces + their OFL. A download
  failure here is fatal (``main`` returns non-zero), since the GUI backends
  ground their base unit on the mono face.
* **Optional (CJK)** — the large (~16 MB each) Noto Sans CJK JP faces that give
  the non-terminal backends real Japanese glyphs + advances. A failure here only
  *warns*: the backends degrade to the OS's own CJK fallback (and the web
  backend to its em-width estimate), so an offline dev setup still works.

Stdlib only (urllib), so it runs before the venv exists.
"""
from __future__ import annotations

import os
import sys
import urllib.request

_NOTO = "https://github.com/notofonts/notofonts.github.io/raw/main/fonts"
_CJK = "https://github.com/notofonts/noto-cjk/raw/main"

# Required faces + license. A failure fetching any of these is fatal.
_FILES = {
    "NotoSans-Regular.ttf": f"{_NOTO}/NotoSans/hinted/ttf/NotoSans-Regular.ttf",
    "NotoSans-Bold.ttf": f"{_NOTO}/NotoSans/hinted/ttf/NotoSans-Bold.ttf",
    "NotoSansMono-Regular.ttf": f"{_NOTO}/NotoSansMono/hinted/ttf/NotoSansMono-Regular.ttf",
    "NotoSansMono-Bold.ttf": f"{_NOTO}/NotoSansMono/hinted/ttf/NotoSansMono-Bold.ttf",
    "OFL.txt": "https://raw.githubusercontent.com/notofonts/latin-greek-cyrillic/main/OFL.txt",
}

# Optional Noto Sans CJK JP faces (proportional + mono) and their OFL. Regular
# weight only: Noto CJK advances are weight-invariant, so the backends measure
# bold from the Regular table and synthesize the bold look — halving the (~31 MB)
# download. Absence is non-fatal; the backends fall back to OS CJK / em estimate.
_CJK_FILES = {
    "NotoSansCJKjp-Regular.otf": f"{_CJK}/Sans/OTF/Japanese/NotoSansCJKjp-Regular.otf",
    "NotoSansMonoCJKjp-Regular.otf": f"{_CJK}/Sans/Mono/NotoSansMonoCJKjp-Regular.otf",
    "OFL-CJK.txt": f"{_CJK}/Sans/LICENSE",
}

# A valid sfnt file starts with one of these signatures; guard against saving a
# redirect/404 HTML page as a font (which then fails to load silently). ``OTTO``
# is the CFF (OpenType) flavour the Noto CJK .otf files use.
_SFNT_MAGIC = (b"\x00\x01\x00\x00", b"OTTO", b"true", b"ttcf")


def _valid(name: str, data: bytes) -> bool:
    if name.endswith((".ttf", ".otf")):
        return data[:4] in _SFNT_MAGIC
    return b"Open Font License" in data[:4000]  # OFL text


def _fetch_one(dest: str, name: str, url: str) -> bool:
    """Fetch ``name`` into ``dest`` if not already present. Returns True on
    success (or already-present), False on any network / validation failure."""
    path = os.path.join(dest, name)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        print(f"  have  {name}")
        return True
    print(f"  fetch {name}")
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            data = resp.read()
    except Exception as exc:  # network/HTTP error
        print(f"  ERROR fetching {name}: {exc}", file=sys.stderr)
        return False
    if not _valid(name, data):
        print(f"  ERROR {name}: downloaded content is not a valid {name.rsplit('.', 1)[-1]}",
              file=sys.stderr)
        return False
    with open(path, "wb") as f:
        f.write(data)
    return True


def main() -> int:
    dest = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "puikit", "fonts")
    os.makedirs(dest, exist_ok=True)

    # Required faces: a failure is fatal.
    for name, url in _FILES.items():
        if not _fetch_one(dest, name, url):
            return 1

    # Optional CJK faces: a failure only warns, so offline / rate-limited setups
    # still get a working (Latin-exact, CJK-estimated) install.
    for name, url in _CJK_FILES.items():
        if not _fetch_one(dest, name, url):
            print(f"  WARN  {name} unavailable — non-terminal backends will use OS CJK "
                  f"fallback / the web em estimate for Japanese.", file=sys.stderr)

    print(f"fonts ready in {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
