---
name: puikit-release
description: Release a new version of PuiKit to PyPI and GitHub — pick the version bump, verify downstream compatibility, run the Makefile pipeline, and write the hand-written release note the Makefile does not produce. Use when the user says "release PuiKit", "release a patch/minor/major version of PuiKit", or "ship puikit X.Y.Z".
---

# Releasing PuiKit

Run every command from the root of the puikit checkout. The pipeline is three
Makefile targets plus **one manual step the Makefile does not do: the
hand-written release note** (step 4). Downstream checkouts (`../keyhac`,
`../xefm`) are assumed to be siblings, per the ecosystem convention.

## 1. Decide the version

The single source of truth is `__version__` in `puikit/__init__.py`
(pyproject derives from it via `dynamic = ["version"]`). Map the request:

- "patch" → bump Z (1.0.11 → 1.0.12)
- "minor" → bump Y, reset Z (1.0.11 → 1.1.0)
- "major" → bump X, reset Y and Z
- An explicit version in the request wins.

**Confirm the number with the user before proceeding.** State the mapping
explicitly — "current is X.Y.Z, this releases X.Y.Z′" — and wait for a yes
(AskUserQuestion in interactive sessions) before the step-2 checks, which cost
minutes, and long before `make tag`, which publishes: a pushed tag, and later a
PyPI version that can never be re-uploaded. A misread request — a "patch" that
should have been "minor", an explicit version with a typo — must be caught
here, not discovered on PyPI.

Do not edit `__version__` yourself — `make tag` bumps, commits, and tags it.

## 2. Judgment checks before tagging

`scripts/release_preflight.py` (run by `make tag`) already enforces the
mechanics: on `main`, clean tree, not behind origin, version strictly ahead,
tag free, pyproject still dynamic; it warns if `gh` is unauthenticated. Before
invoking it, do the checks it cannot:

- `git log v<current>..origin/main --oneline` — everything meant for this
  release is merged, nothing unexpected rode along. This list is also the raw
  material for the release note.
- **Backward compatibility.** PuiKit is released and pinned; per its CLAUDE.md
  policy every change must be additive. For anything behavior-affecting, run
  the downstream suites against the local checkout, not just puikit's own:
  - puikit: `make test`
  - keyhac: `../keyhac/.venv/bin/python -m pytest -q` (its dev venv imports
    the local puikit checkout when its `Makefile.local` sets `PUIKIT_DIR` —
    verify with `import puikit; puikit.__file__` first)
  - xefm: `(cd ../xefm && PYTHONPATH=../puikit .venv/bin/python -m pytest -q)`
    (puikit is pure Python, so `PYTHONPATH` shadows the venv's PyPI copy)
- **Draft the release note now**, before tagging (style in step 4). Writing it
  forces a review of what is actually shipping while the tag is still
  retractable.

## 3. The Makefile pipeline

```
make tag VERSION=x.y.z   # preflight → tests → bump __version__ → commit
                         # "Releasing x.y.z" → tag vx.y.z → build gate
                         # (sdist+wheel+twine check) → push commit and tag
make release-github      # create the GitHub Release (auto-generated body —
                         # replaced in step 4)
make release-whl         # HEAD must sit exactly on the tag; uploads sdist +
                         # wheel to PyPI and attaches both to the Release
make release-status      # read-only: GitHub assets + PyPI published?
```

- `make tag` commits directly to puikit's `main`. That is the sanctioned
  exception to the "puikit changes always go through a PR" rule — release
  commits only.
- Each target is independently re-runnable; on failure, fix and re-run from
  the failed step. `release-github` is idempotent.
- `release-whl` needs a `[pypi]` token in `~/.pypirc` and an authenticated
  `gh`. A PyPI version can never be re-uploaded — that is why the target
  refuses to run unless HEAD is on the tag.

## 4. The hand-written release note (not in the Makefile)

`make release-github` creates the Release with GitHub's `--generate-notes` PR
list. That body is a placeholder — replace it with a hand-written note. The
model is v1.0.10 (v1.0.11 shipped with only the auto list; don't repeat that).

Style:

- Body starts `## PuiKit X.Y.Z`. Leave the Release title as `vX.Y.Z`.
- One scoping sentence up front when it applies ("Everything in this release
  is Windows. On macOS, Linux and the web backend nothing changes.").
- One bullet per user-visible change: a **bold headline in user terms**, then
  prose — the symptom as an app author saw it, the cause in a clause, and what
  now happens. Written for people building on PuiKit, not repo archaeologists.
  PR/issue numbers are optional; prose comes first.
- Minor items in one trailing "Also:" sentence.
- End with
  `**Full Changelog**: https://github.com/crftwr/puikit/compare/vPREV...vNEW`.

Write the note to a scratch location outside the repo (it is never committed),
then:

```
gh release edit vX.Y.Z --notes-file <scratch>/release-note-x.y.z.md
```

## 5. After the release

- `make release-status` must show both GitHub assets and "PyPI: published".
- Check what downstream was waiting on this release — typically a keyhac PR
  pinning `puikit>=X.Y.Z` (`gh pr list --repo crftwr/keyhac`). Report it;
  merge it only if the request covered that too.
