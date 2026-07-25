"""Rewrite the `__version__ = "..."` literal in puikit/__init__.py.

Used by `make release VERSION=x.y.z`. That literal is the single source of
truth for the version: pyproject.toml derives it via setuptools' dynamic
``version = { attr = "puikit.__version__" }``, so bumping this one line moves
the packaging metadata and ``puikit.__version__`` together and they cannot
drift apart. Prints `old -> new` so the release recipe echoes what changed.

Kept surgical (see scripts/_version_source.py): it replaces exactly one
whole-line match, and refuses if the file holds anything other than a single
``__version__`` assignment.
"""

import sys

from _version_source import write_version


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: bump_version.py <new-version>", file=sys.stderr)
        return 2
    new = sys.argv[1]

    old = write_version(new)
    print(f"{old} -> {new}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
