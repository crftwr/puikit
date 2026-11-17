"""Fetch libghostty-vt into this checkout's ``puikit/_bin/`` (xefm#900).

``puikit._ghostty`` looks for the library there (then on the system's library
path, and nowhere else). This script is for a development checkout, and
``make venv`` runs it on macOS and Windows; an application bundle ships its own
copy in the bundled package's ``_bin/`` (XeFM: tools/fetch_libghostty.py).

It fetches exactly the release the binding is written for -
``LIBGHOSTTY_VERSION`` in ``puikit/_ghostty.py``, read from the source so
PuiKit need not be importable yet - from release.files.ghostty.org, and checks
the download against its SHA-256 in ``PINS``. A library already in place that
reports that version is left alone, so running it again costs nothing.

- macOS: the xcframework ships only a static universal ``libghostty-vt.a``;
  it is linked into ``libghostty-vt.dylib`` with the system clang.
- Windows: the ``*-windows-gnu.zip`` carries ``ghostty-vt.dll`` as is.
- Linux: no binary is published; build from the source tarball with zig and
  put it in ``puikit/_bin/`` or on the library path. Not needed for a TUI.

    python scripts/fetch_libghostty.py [--dest DIR] [--force]
"""

from __future__ import annotations

import argparse
import ast
import ctypes
import hashlib
import io
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: version -> target -> (asset, SHA-256). A new LIBGHOSTTY_VERSION needs its
#: row here before this script will fetch it.
PINS = {
    "1.0.1": {
        "macos": ("ghostty-vt-1.0.1.xcframework.zip",
                  "c0f9631277f726154c8f521954d17cdc41aac4626fc0df8cf925a19ee58e829b"),
        "windows-x64": ("ghostty-vt-1.0.1-x86_64-windows-gnu.zip",
                        "8e2eb9e1822e85d786b456c8beb86f49d09c318c91f0c2913f91e7ffa12cda29"),
        "windows-arm64": ("ghostty-vt-1.0.1-aarch64-windows-gnu.zip",
                          "cc61da5f968ea0b08639bdf019c6b7da46bad1d985774fd8a2de57af32862892"),
    },
}

#: The oldest macOS the xcframework's objects were built for. Linked with no
#: target, clang stamps the host's macOS on the dylib and older systems refuse it.
MACOS_MIN = "13.0"


def declared_version() -> str:
    """``LIBGHOSTTY_VERSION`` from puikit/_ghostty.py, without importing it."""
    with open(os.path.join(ROOT, "puikit", "_ghostty.py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and getattr(node.targets[0], "id", None) == "LIBGHOSTTY_VERSION"):
            return ast.literal_eval(node.value)
    raise SystemExit("LIBGHOSTTY_VERSION not found in puikit/_ghostty.py")


def target() -> str | None:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform == "win32":
        arm = platform.machine().lower() in ("arm64", "aarch64")
        return "windows-arm64" if arm else "windows-x64"
    return None


def library_name(tgt: str) -> str:
    return "libghostty-vt.dylib" if tgt == "macos" else "ghostty-vt.dll"


def reported_version(path: str) -> str | None:
    """What the library at ``path`` says it is, or None if it does not load."""
    try:
        fn = ctypes.CDLL(path).ghostty_version
    except (OSError, AttributeError):
        return None
    fn.restype = ctypes.c_char_p
    fn.argtypes = []
    return (fn() or b"").decode("utf-8", "replace")


def fetch(url: str, sha256: str) -> bytes:
    print(f"fetching {url}")
    # The host refuses urllib's default User-Agent.
    req = urllib.request.Request(url, headers={"User-Agent": "puikit-fetch-libghostty"})
    with urllib.request.urlopen(req) as resp:
        data = resp.read()
    got = hashlib.sha256(data).hexdigest()
    if got != sha256:
        raise SystemExit(f"SHA-256 mismatch for {url}\n  expected {sha256}\n  got      {got}")
    return data


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dest", default=os.path.join(ROOT, "puikit", "_bin"),
                    help="directory to put the library in (default: puikit/_bin/)")
    ap.add_argument("--force", action="store_true", help="fetch even if it is in place")
    args = ap.parse_args()
    tgt = target()
    if tgt is None:
        print("No prebuilt libghostty-vt for this platform, and TerminalView is not "
              "needed for a TUI; skipping.")
        return 0
    version = declared_version()
    if version not in PINS:
        raise SystemExit(f"No pinned hashes for libghostty-vt {version}; add them to PINS.")
    asset, sha256 = PINS[version][tgt]
    dest = os.path.join(args.dest, library_name(tgt))
    if not args.force and reported_version(dest) == version:
        print(f"libghostty-vt {version} already in {dest}")
        return 0
    data = fetch(f"https://release.files.ghostty.org/libghostty-vt/{version}/{asset}", sha256)
    os.makedirs(args.dest, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as z, tempfile.TemporaryDirectory() as tmp:
        if tgt == "macos":
            lib = z.extract("ghostty-vt.xcframework/macos-arm64_x86_64/libghostty-vt.a", tmp)
            subprocess.run(["clang", "-dynamiclib", "-arch", "arm64", "-arch", "x86_64",
                            f"-mmacosx-version-min={MACOS_MIN}",
                            "-Wl,-all_load", lib, "-o", dest,
                            "-install_name", "@rpath/libghostty-vt.dylib"], check=True)
        else:
            stem = asset[:-len(".zip")]
            with z.open(f"{stem}/bin/ghostty-vt.dll") as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
    got = reported_version(dest)
    if got != version:
        raise SystemExit(f"{dest} reports {got!r}, not libghostty-vt {version}")
    print(f"installed libghostty-vt {version} in {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
