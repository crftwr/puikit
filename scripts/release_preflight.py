"""Fail-fast checks run before `make release` mutates anything.

The release recipe does irreversible things (pushes a tag, uploads to PyPI —
a PyPI version can never be reused). This script runs FIRST and refuses the
release unless every precondition holds, so a dirty tree, a stale checkout, a
duplicate version, or a missing/​unauthenticated `gh` fails loudly *before* any
commit, tag, upload, or push happens. It collects all problems and reports them
together rather than stopping at the first.

Usage: release_preflight.py <new-version>
"""

import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"

# X.Y.Z core, with an optional PEP 440-ish pre/post/dev suffix (e.g. 1.2.0rc1).
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[.-]?(?:a|b|rc|alpha|beta|post|dev)\d+)?$")


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, cwd=PYPROJECT.parent
    )


def core(version: str) -> tuple[int, int, int]:
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)", version)
    return tuple(int(g) for g in m.groups()) if m else (0, 0, 0)


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: release_preflight.py <new-version>", file=sys.stderr)
        return 2
    new = sys.argv[1]
    problems: list[str] = []

    # 1. Version string is well-formed.
    if not VERSION_RE.match(new):
        problems.append(f"VERSION '{new}' is not X.Y.Z (optionally +rc1/.post1/…)")

    # 2. New version is strictly ahead of the current one (no re-release / rollback).
    current = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]
    if new == current:
        problems.append(f"VERSION {new} equals the current version in pyproject.toml")
    elif core(new) < core(current):
        problems.append(f"VERSION {new} is older than the current {current}")

    # 3. On the main branch.
    branch = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if branch != "main":
        problems.append(f"on branch '{branch}', not 'main'")

    # 4. Working tree is clean.
    if git("status", "--porcelain").stdout.strip():
        problems.append("working tree is dirty — commit or stash first")

    # 5. The tag does not already exist.
    if git("tag", "--list", f"v{new}").stdout.strip():
        problems.append(f"tag v{new} already exists")

    # 6. Local main is not behind its upstream (a non-fast-forward push would
    #    otherwise fail mid-release). Skipped cleanly if there is no upstream.
    git("fetch", "--quiet")
    upstream = git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if upstream.returncode == 0:
        behind = git("rev-list", "--count", "HEAD..@{u}").stdout.strip()
        if behind and behind != "0":
            problems.append(f"local branch is {behind} commit(s) behind {upstream.stdout.strip()} — pull first")

    # 7. gh is installed and authenticated (this release creates a GitHub Release).
    if shutil.which("gh") is None:
        problems.append("`gh` not found — install it (`brew install gh`) and run `gh auth login`")
    elif subprocess.run(["gh", "auth", "status"], capture_output=True).returncode != 0:
        problems.append("`gh` is not authenticated — run `gh auth login`")

    if problems:
        print("Release preflight failed:", file=sys.stderr)
        for p in problems:
            print(f"  ✗ {p}", file=sys.stderr)
        return 1

    print(f"Preflight OK: {current} -> {new} on {branch}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
