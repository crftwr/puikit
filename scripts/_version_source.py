"""Locate and rewrite PuiKit's single version literal.

The version lives in exactly one place — ``puikit/__init__.py``'s
``__version__`` — and pyproject.toml derives it through setuptools' dynamic
``version = { attr = "puikit.__version__" }``. Both release scripts go through
this module so they can never disagree about where the literal is. They once
did: bump_version.py rewrote pyproject.toml only, which is how 1.0.2 shipped
with ``__version__`` left behind at 1.0.1.

The literal is read statically (regex, no ``import puikit``) so the release
tooling never needs the runtime deps — pillow, pyobjc, windows-curses — merely
to learn the version. That is the same static approach setuptools itself uses
to resolve the ``attr`` at build time.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INIT = REPO_ROOT / "puikit" / "__init__.py"

#: Anchored to a whole line so nothing else in the file can match.
PATTERN = re.compile(r'^__version__\s*=\s*"([^"]+)"\s*$', re.M)


def read_version() -> str:
    """Return the current version literal.

    Raises SystemExit if it is absent or duplicated — either means the single
    source of truth has been disturbed, which a release must not paper over.
    """
    found = PATTERN.findall(INIT.read_text(encoding="utf-8"))
    if len(found) != 1:
        raise SystemExit(
            f'ERROR: expected exactly one `__version__ = "..."` line in {INIT}, '
            f"found {len(found)}"
        )
    return found[0]


def write_version(new: str) -> str:
    """Rewrite the literal to ``new``. Returns the previous value."""
    old = read_version()
    text = INIT.read_text(encoding="utf-8")
    # A lambda replacement, so backslashes/group refs in `new` stay literal.
    new_text, count = PATTERN.subn(lambda _m: f'__version__ = "{new}"', text)
    if count != 1:
        raise SystemExit(f"ERROR: expected 1 substitution in {INIT}, made {count}")
    INIT.write_text(new_text, encoding="utf-8")
    return old
