#!/usr/bin/env python3
"""Download the bundled default fonts into ``puikit/fonts/``.

The GUI backends default to Noto Sans + Noto Sans Mono — a designed-together
superfamily whose proportional and monospace faces share metrics, so the base
unit fits both and text does not clip. The font files are **not committed**
(they are large binaries under their own OFL license); they are fetched here at
build / dev-setup time (``make fonts``, which the venv/run targets depend on),
and the backends fall back to the OS fonts if the files are absent.

Stdlib only (urllib), so it runs before the venv exists.
"""
from __future__ import annotations

import os
import sys
import urllib.request

_NOTO = "https://github.com/notofonts/notofonts.github.io/raw/main/fonts"
_FILES = {
    "NotoSans-Regular.ttf": f"{_NOTO}/NotoSans/hinted/ttf/NotoSans-Regular.ttf",
    "NotoSans-Bold.ttf": f"{_NOTO}/NotoSans/hinted/ttf/NotoSans-Bold.ttf",
    "NotoSansMono-Regular.ttf": f"{_NOTO}/NotoSansMono/hinted/ttf/NotoSansMono-Regular.ttf",
    "NotoSansMono-Bold.ttf": f"{_NOTO}/NotoSansMono/hinted/ttf/NotoSansMono-Bold.ttf",
    "OFL.txt": "https://raw.githubusercontent.com/notofonts/latin-greek-cyrillic/main/OFL.txt",
}

# A valid sfnt/TrueType file starts with one of these signatures; guard against
# saving a redirect/404 HTML page as a .ttf (which then fails to load silently).
_SFNT_MAGIC = (b"\x00\x01\x00\x00", b"OTTO", b"true", b"ttcf")


def _valid(name: str, data: bytes) -> bool:
    if name.endswith(".ttf"):
        return data[:4] in _SFNT_MAGIC
    return b"Open Font License" in data[:4000]  # OFL.txt


def main() -> int:
    dest = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "puikit", "fonts")
    os.makedirs(dest, exist_ok=True)
    for name, url in _FILES.items():
        path = os.path.join(dest, name)
        if os.path.exists(path) and os.path.getsize(path) > 0:
            print(f"  have  {name}")
            continue
        print(f"  fetch {name}")
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                data = resp.read()
        except Exception as exc:  # network/HTTP error
            print(f"  ERROR fetching {name}: {exc}", file=sys.stderr)
            return 1
        if not _valid(name, data):
            print(f"  ERROR {name}: downloaded content is not a valid {name.rsplit('.', 1)[-1]}", file=sys.stderr)
            return 1
        with open(path, "wb") as f:
            f.write(data)
    print(f"fonts ready in {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
