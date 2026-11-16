"""Fetch libghostty-vt for the TerminalView prototype (xefm#900).

Downloads the official release from release.files.ghostty.org into the
per-user cache that ``puikit._ghostty`` searches (~/.cache/puikit/libghostty/):

- macOS: the xcframework ships only a static universal ``libghostty-vt.a``;
  it is linked into ``libghostty-vt.dylib`` with the system clang.
- Windows: the ``*-windows-gnu.zip`` carries ``ghostty-vt.dll`` as is.
- Linux: no binary is published; build from the source tarball with zig.

    python scripts/fetch_libghostty.py [--version 1.0.1]
"""

from __future__ import annotations

import argparse
import io
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

BASE = "https://release.files.ghostty.org/libghostty-vt"


def cache_dir() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "puikit", "libghostty")


def fetch(url: str) -> bytes:
    print(f"fetching {url}")
    # The host refuses urllib's default User-Agent.
    req = urllib.request.Request(url, headers={"User-Agent": "puikit-fetch-libghostty"})
    with urllib.request.urlopen(req) as resp:
        return resp.read()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default="1.0.1")
    args = ap.parse_args()
    v = args.version
    out = cache_dir()
    os.makedirs(out, exist_ok=True)
    if sys.platform == "darwin":
        data = fetch(f"{BASE}/{v}/ghostty-vt-{v}.xcframework.zip")
        with tempfile.TemporaryDirectory() as tmp:
            zipfile.ZipFile(io.BytesIO(data)).extractall(tmp)
            lib = os.path.join(tmp, "ghostty-vt.xcframework", "macos-arm64_x86_64", "libghostty-vt.a")
            dest = os.path.join(out, "libghostty-vt.dylib")
            subprocess.run(["clang", "-dynamiclib", "-arch", "arm64", "-arch", "x86_64",
                            "-Wl,-all_load", lib, "-o", dest,
                            "-install_name", "@rpath/libghostty-vt.dylib"], check=True)
    elif sys.platform == "win32":
        arch = "aarch64" if platform.machine().lower() in ("arm64", "aarch64") else "x86_64"
        name = f"ghostty-vt-{v}-{arch}-windows-gnu"
        data = fetch(f"{BASE}/{v}/{name}.zip")
        with zipfile.ZipFile(io.BytesIO(data)) as z, \
                z.open(f"{name}/bin/ghostty-vt.dll") as src, \
                open(os.path.join(out, "ghostty-vt.dll"), "wb") as dst:
            shutil.copyfileobj(src, dst)
        dest = os.path.join(out, "ghostty-vt.dll")
    else:
        print("No prebuilt libghostty-vt for this platform; build it from "
              f"{BASE}/{v}/libghostty-vt-{v}.tar.gz with zig and set PUIKIT_LIBGHOSTTY.")
        return 1
    print(f"installed {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
