"""Rewrite the hardcoded `version = "..."` line in pyproject.toml.

Used by `make release VERSION=x.y.z`. Kept surgical: it reads the current
version from the [project] table, then replaces only that exact line, so it
can never touch a `version` key in another table (build-system, tool.*, etc.).
Prints `old -> new` so the release recipe echoes what changed.
"""

import re
import sys
import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: bump_version.py <new-version>", file=sys.stderr)
        return 2
    new = sys.argv[1]

    text = PYPROJECT.read_text(encoding="utf-8")
    data = tomllib.loads(text)
    old = data.get("project", {}).get("version")
    if old is None:
        print("ERROR: no [project].version found in pyproject.toml", file=sys.stderr)
        return 1

    # Match the exact project version line (any surrounding whitespace), anchored
    # to the value we just parsed so no other `version =` line can match.
    pattern = re.compile(r'^version\s*=\s*"' + re.escape(old) + r'"\s*$', re.M)
    new_text, count = pattern.subn(f'version = "{new}"', text)
    if count != 1:
        print(
            f"ERROR: expected exactly one `version = \"{old}\"` line, found {count}",
            file=sys.stderr,
        )
        return 1

    PYPROJECT.write_text(new_text, encoding="utf-8")
    print(f"{old} -> {new}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
