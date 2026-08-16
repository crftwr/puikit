VENV := .venv

# Most Windows `make`s here run under a Unix-y shell (Git Bash/MSYS2/Cygwin),
# where `uname -s` reports MINGW64_NT-.../CYGWIN_NT-.../MSYS_NT-... reliably —
# unlike `$(OS)`, which some of those `make` builds never expose as a make
# variable even though the shell's own `$OS` is set (verified: GNU Make
# "Built for x86_64-pc-cygwin" silently drops it). Native-Windows GNU Make
# ports without `uname` fall back to checking `$(OS)` directly.
UNAME_S := $(shell uname -s 2>/dev/null)
ifneq (,$(findstring MINGW,$(UNAME_S))$(findstring MSYS,$(UNAME_S))$(findstring CYGWIN,$(UNAME_S)))
    IS_WINDOWS := 1
else ifeq ($(OS),Windows_NT)
    IS_WINDOWS := 1
endif

ifeq ($(IS_WINDOWS),1)
    # Windows: venv scripts live in Scripts/, executables end in .exe. Prefer
    # python3.14 if it's on PATH; otherwise fall back to the `py` launcher.
    # `where.exe` cannot be used for this check: it's a native Windows tool
    # that reads PATH in Windows (semicolon) format, but the shell make spawns
    # here exports a POSIX-style PATH — `where` always finds nothing and silently
    # falls back to `py`, which (confirmed) auto-downloads a fresh Python install
    # into the current directory (a stray `Python/` folder) instead of using the
    # one already on PATH. `command -v` is a shell builtin, so it searches the
    # shell's own PATH correctly instead of re-parsing it as a native Win32 tool.
    PY_ON_PATH := $(shell command -v python3.14 2>/dev/null)
    ifneq ($(strip $(PY_ON_PATH)),)
        PYTHON := python3.14
    else
        PYTHON := py
    endif
    VENV_PYTHON := $(VENV)/Scripts/python.exe
    VENV_PIP := $(VENV)/Scripts/pip.exe
else
    PYTHON := python3.14
    VENV_PYTHON := $(VENV)/bin/python
    VENV_PIP := $(VENV)/bin/pip
endif

# `dev` (test tooling) is the only optional-dependency group. Each backend's own
# requirements — PyObjC (macOS), numpy + windows-curses (Windows) — install
# automatically via platform-marked base deps, so EXTRAS is the same on every OS.
EXTRAS := dev

# Optional base font size for GUI targets, e.g. `make demo-gui FONT_SIZE=18`.
FONT_SIZE :=
FONT_SIZE_ARG := $(if $(FONT_SIZE),--font-size $(FONT_SIZE))

# A file-based stamp, not a phony target: every run/test target depends on
# it, so `make demo-gui` (etc.) auto-creates the venv and installs puikit the
# first time, but only re-installs when pyproject.toml actually changes —
# unlike depending on the phony `install` target directly, which make would
# always re-run (pip install -e on every single invocation). This closes the
# footgun where `make venv` alone leaves puikit un-installed and any run
# target fails with `ModuleNotFoundError: No module named 'puikit'`.
VENV_STAMP := $(VENV)/.installed

# Bundled default fonts (Noto), fetched at build time rather than committed
# (large binaries under their own license). One fetched file stands in for all;
# fetch_fonts.py is idempotent and fills in any that are missing. The install
# stamp depends on this so `make venv` / any run target populates the fonts.
FONTS := puikit/fonts/NotoSans-Regular.ttf

.PHONY: help venv install test fonts hello demo demo-vt demo-curses layout bg3d hello-gui demo-gui layout-gui bg3d-gui hello-web demo-web build publish-testpypi tag release-github release-whl release-status clean

help:
	@echo "PuiKit utility commands:"
	@echo "  make venv      - create the virtualenv and install puikit ($(VENV)/, $(PYTHON))"
	@echo "  make install   - (re)install puikit into the venv (editable, with dev deps)"
	@echo "  make fonts     - download the bundled default fonts into puikit/fonts/"
	@echo "  make test      - run the test suite"
	@echo "  make hello     - run the hello_world example (TUI)"
	@echo "  make demo      - run the demo_catalog example (TUI: VT on Windows, curses elsewhere)"
	@echo "  make demo-vt   - run the demo_catalog example (force the VT backend)"
	@echo "  make demo-curses - run the demo_catalog example (force the curses backend)"
	@echo "  make layout    - run the layout demo (TUI)"
	@echo "  make bg3d      - run the background_3d example (TUI)"
	@echo "  make hello-gui - run the hello_world example (native GUI: macOS or Windows)"
	@echo "  make demo-gui  - run the demo_catalog example (native GUI: macOS or Windows)"
	@echo "  make hello-web - run the hello_world example (web backend, in a browser)"
	@echo "  make demo-web  - run the demo_catalog example (web backend, in a browser)"
	@echo "  make layout-gui - run the layout demo (native GUI, pixel layout)"
	@echo "  make bg3d-gui  - run the background_3d example (native GUI: macOS or Windows)"
	@echo "  make clean     - remove build artifacts and caches"
	@echo ""
	@echo "Release (run in this order):"
	@echo "  make tag VERSION=x.y.z - bump __version__, commit, tag, push (no publishing)"
	@echo "  make release-github    - open the GitHub Release at that tag"
	@echo "  make release-whl       - upload sdist + wheel to PyPI, and to the Release"
	@echo "  make release-status    - show which artifacts have landed so far"
	@echo ""
	@echo "  The release-* targets are re-runnable. Supporting targets:"
	@echo "  make build             - build the sdist + wheel into dist/ (installs build/twine as needed)"
	@echo "  make publish-testpypi  - rehearsal: upload dist/* to TestPyPI ([testpypi] token in ~/.pypirc)"
	@echo ""
	@echo "  Run/test targets create the venv and install puikit automatically"
	@echo "  if needed. GUI targets accept FONT_SIZE, e.g. make demo-gui FONT_SIZE=18"

$(VENV_STAMP): pyproject.toml $(FONTS)
	$(PYTHON) -m venv $(VENV)
	$(VENV_PIP) install -e ".[$(EXTRAS)]"
	@touch $(VENV_STAMP)

$(FONTS): scripts/fetch_fonts.py
	$(PYTHON) scripts/fetch_fonts.py

fonts: $(FONTS)

venv: $(VENV_STAMP)

install: $(VENV_STAMP)

test: $(VENV_STAMP)
	$(VENV_PYTHON) -m pytest

hello: $(VENV_STAMP)
	$(VENV_PYTHON) examples/hello_world/main.py

demo: $(VENV_STAMP)
	$(VENV_PYTHON) examples/demo_catalog/main.py

# `make demo` already picks the VT backend on Windows. These two force one or
# the other, for comparing them side by side on the same machine.
demo-vt: $(VENV_STAMP)
	$(VENV_PYTHON) examples/demo_catalog/main.py --backend vt

demo-curses: $(VENV_STAMP)
	$(VENV_PYTHON) examples/demo_catalog/main.py --backend curses

hello-gui: $(VENV_STAMP)
	$(VENV_PYTHON) examples/hello_world/main.py --backend gui $(FONT_SIZE_ARG)

demo-gui: $(VENV_STAMP)
	$(VENV_PYTHON) examples/demo_catalog/main.py --backend gui $(FONT_SIZE_ARG)

hello-web: $(VENV_STAMP)
	$(VENV_PYTHON) examples/hello_world/main.py --backend web $(FONT_SIZE_ARG)

demo-web: $(VENV_STAMP)
	$(VENV_PYTHON) examples/demo_catalog/main.py --backend web $(FONT_SIZE_ARG)

layout: $(VENV_STAMP)
	$(VENV_PYTHON) examples/layout_demo/main.py

layout-gui: $(VENV_STAMP)
	$(VENV_PYTHON) examples/layout_demo/main.py --backend gui

bg3d: $(VENV_STAMP)
	$(VENV_PYTHON) examples/background_3d/main.py

bg3d-gui: $(VENV_STAMP)
	$(VENV_PYTHON) examples/background_3d/main.py --backend gui $(FONT_SIZE_ARG)

# --- Packaging / release ----------------------------------------------------
# `build` and `twine` are release-time tooling, not needed to run or develop
# PuiKit, so they are installed on demand here rather than bloating the base
# venv. Invoked as `python -m ...` (not the venv's console scripts) so the same
# recipe works on Windows, where those scripts live in Scripts/ and end in .exe.
# The PyPI long description (README.pypi.md) is generated here on the fly and
# never committed: README.md keeps repo-relative image/link targets for GitHub,
# and gen_pypi_readme.py rewrites them to version-tagged GitHub URLs so they
# render on the PyPI page. `twine check --strict` promotes twine's
# "description missing" warning to a failure, so a build that somehow skipped
# generation can never upload an empty description.
build: $(VENV_STAMP)
	$(VENV_PIP) install --quiet build twine
	rm -rf dist build puikit.egg-info
	$(VENV_PYTHON) scripts/gen_pypi_readme.py
	$(VENV_PYTHON) -m build
	$(VENV_PYTHON) -m twine check --strict dist/*

# The safe rehearsal for release-whl: same build and upload path, but a bad
# TestPyPI version costs nothing. Deliberately NOT named release-* — it needs
# neither a tag nor a GitHub Release and publishes nothing permanent, so it is
# a pre-release smoke test rather than a step of the pipeline. Depends on
# `build` (not on the file target below) so it always builds fresh.
publish-testpypi: build
	$(VENV_PYTHON) -m twine upload -r testpypi dist/*

# --- Release pipeline (mirrors XeFM's) ---------------------------------------
# Releasing is one target per step, each independently re-runnable:
#
#   make tag VERSION=x.y.z   bump __version__, commit, tag, push
#   make release-github      open the GitHub Release at that tag
#   make release-whl         sdist + wheel -> PyPI (+ the Release)
#   make release-status      what has landed so far
#
# Order matters only twice: `tag` first (everything else names the tag it
# creates), then `release-github` (release-whl uploads into the Release it
# opens). PuiKit builds a single artifact (the Python distributions), so this
# is XeFM's pipeline minus the per-platform release-<artifact> targets.
#
# The version's single source of truth is puikit/__init__.py's __version__;
# pyproject.toml derives it (dynamic version = attr). PUIKIT_VERSION below
# reads that same literal, so every release-* target acts on the release the
# checkout is actually on — only `tag` takes a VERSION=. Override it on the
# others to target a different release (e.g. re-uploading an asset for an
# older tag).
PUIKIT_VERSION := $(if $(VERSION),$(VERSION),$(shell sed -nE 's/^__version__[[:space:]]*=[[:space:]]*"([^"]+)".*/\1/p' puikit/__init__.py 2>/dev/null | head -1))

# Guards shared by the release-* targets, kept in one place so they cannot
# drift into checking different things. Used as $(call ...) inside a recipe;
# each expands to a single multi-line shell test.
#
# check_gh:             a resolvable version and a usable `gh`.
# check_release_exists: the above, plus the GitHub Release to upload into.
define check_gh
test -n "$(PUIKIT_VERSION)" || { echo "ERROR: could not determine version; pass VERSION=x.y.z"; exit 1; }; \
command -v gh >/dev/null 2>&1 || { echo "ERROR: 'gh' not found. Install the GitHub CLI first."; exit 1; }; \
gh auth status >/dev/null 2>&1 || { echo "ERROR: 'gh' is not authenticated. Run 'gh auth login'."; exit 1; }
endef

define check_release_exists
$(check_gh); \
gh release view v$(PUIKIT_VERSION) >/dev/null 2>&1 || { \
	echo "ERROR: GitHub Release v$(PUIKIT_VERSION) does not exist."; \
	echo "       Open it first with 'make release-github'."; \
	exit 1; \
}
endef

# --- tag: the one target that changes the version ---------------------------
# Usage: make tag VERSION=1.0.11
#
# Pure version + git work: bump __version__, commit, tag, push. It publishes
# nothing and needs no `gh` and no PyPI token — the release-* targets do the
# publishing, each with its own credentials.
#
# bump_version.py rewrites the single __version__ line, which is why the commit
# stages __init__.py rather than pyproject.toml.
#
# release_preflight.py runs FIRST and aborts before any mutation if the tree is
# dirty, the version is stale, or the tag exists — so a failed precondition
# never leaves a half-cut release. The test suite must pass before anything is
# built.
#
# `make build` runs before the pushes purely as a gate: it proves the sdist and
# wheel build and pass `twine check` while the tag is still local and
# retractable. It also leaves dist/ ready for `make release-whl`.
tag: $(VENV_STAMP)
	@test -n "$(VERSION)" || { echo "ERROR: set VERSION, e.g. make tag VERSION=1.0.11"; exit 1; }
	$(VENV_PYTHON) scripts/release_preflight.py "$(VERSION)"
	$(MAKE) test
	$(VENV_PYTHON) scripts/bump_version.py "$(VERSION)"
	git add puikit/__init__.py
	git commit -m "Releasing $(VERSION)"
	git tag -a v$(VERSION) -m "$(VERSION)"
	$(MAKE) build
	git push
	git push origin v$(VERSION)
	@echo ""
	@echo "Tagged $(VERSION): commit + tag v$(VERSION), both pushed ✓"
	@echo "Next:"
	@echo "  make release-github    # open the GitHub Release at v$(VERSION)"
	@echo "  make release-whl       # sdist + wheel -> PyPI + the Release"

# --- release-github: open the Release the artifacts upload into -------------
# Reads the version from the checkout, so the usual path is `make tag` then
# `make release-github` with no arguments. --verify-tag refuses to invent a tag
# GitHub does not already have, which is why `tag` pushes it first.
#
# Idempotent on purpose: an existing Release is reported and left alone rather
# than erroring, so re-running the pipeline from the top costs nothing.
release-github:
	@$(call check_gh)
	@git ls-remote --exit-code --tags origin "v$(PUIKIT_VERSION)" >/dev/null 2>&1 || { \
		echo "ERROR: tag v$(PUIKIT_VERSION) is not on origin."; \
		echo "       Push it with 'make tag VERSION=$(PUIKIT_VERSION)' (or 'git push origin v$(PUIKIT_VERSION)')."; \
		exit 1; \
	}
	@if gh release view v$(PUIKIT_VERSION) >/dev/null 2>&1; then \
		echo "GitHub Release v$(PUIKIT_VERSION) already exists; leaving it as is."; \
	else \
		gh release create v$(PUIKIT_VERSION) --title "v$(PUIKIT_VERSION)" --generate-notes --verify-tag && \
		echo "Opened GitHub Release v$(PUIKIT_VERSION) ✓"; \
	fi

# The filenames setuptools gives the sdist + wheel, derived from the same
# version literal as PUIKIT_VERSION. Naming them explicitly (rather than
# globbing dist/*) means a stale artifact left from an earlier version can
# never be swept into an upload.
PYPI_SDIST := dist/puikit-$(PUIKIT_VERSION).tar.gz
PYPI_WHEEL := dist/puikit-$(PUIKIT_VERSION)-py3-none-any.whl

# File target so release-whl builds the distributions on demand when they are
# missing (e.g. after `make clean`). `make build` wipes dist/ and writes both
# files, so the sdist alone is enough of a prerequisite to trigger it; the
# recipe below then asserts the wheel landed too. Existing artifacts are NOT
# rebuilt — publishing the exact bytes that were verified is the point.
$(PYPI_SDIST):
	@echo "Python distributions for $(PUIKIT_VERSION) not found; building them first..."
	@$(MAKE) build

# --- release-whl: publish the Python distributions --------------------------
# Uploads BOTH the sdist and the wheel — the target is named for the headline
# artifact, not the whole payload.
#
# A PyPI version can never be re-uploaded, so this refuses to publish a build
# that is not the tagged one: HEAD must sit exactly on vX.Y.Z. `make tag`
# leaves the checkout there, so the usual path is `make tag` then
# `make release-whl`; publishing an older release means checking out its tag
# first.
#
# Also attaches both files to the GitHub Release, so the release page lists
# every artifact. --clobber replaces same-named assets on a re-run.
# Prereqs: a [pypi] token in ~/.pypirc and an authenticated `gh`.
release-whl: $(PYPI_SDIST)
	@$(call check_release_exists)
	@git rev-parse -q --verify "v$(PUIKIT_VERSION)^{commit}" >/dev/null || { \
		echo "ERROR: tag v$(PUIKIT_VERSION) not found locally. Cut it with 'make tag VERSION=$(PUIKIT_VERSION)' or fetch it."; \
		exit 1; \
	}
	@test "$$(git rev-parse HEAD)" = "$$(git rev-parse "v$(PUIKIT_VERSION)^{commit}")" || { \
		echo "ERROR: HEAD is not at tag v$(PUIKIT_VERSION); the upload would not match the tag."; \
		echo "       Check the tag out first: git checkout v$(PUIKIT_VERSION)"; \
		exit 1; \
	}
	@# Both files, not just the sdist that triggered the build: a VERSION= override
	@# that disagrees with __version__ builds different filenames entirely, and
	@# this is where that shows up as a clear error instead of a twine traceback.
	@for f in "$(PYPI_SDIST)" "$(PYPI_WHEEL)"; do \
		test -f "$$f" || { echo "ERROR: $$f missing; run 'make build' from a checkout at v$(PUIKIT_VERSION)."; exit 1; }; \
	done
	@echo "Uploading $(notdir $(PYPI_SDIST)) + $(notdir $(PYPI_WHEEL)) to PyPI..."
	$(VENV_PYTHON) -m twine upload "$(PYPI_SDIST)" "$(PYPI_WHEEL)"
	gh release upload v$(PUIKIT_VERSION) "$(PYPI_SDIST)" "$(PYPI_WHEEL)" --clobber
	@echo "Published $(PUIKIT_VERSION) to PyPI and attached both distributions to release v$(PUIKIT_VERSION) ✓"

# --- release-status: read-only progress check -------------------------------
# One place to see which artifacts have landed for the version the checkout is
# on.
release-status:
	@test -n "$(PUIKIT_VERSION)" || { echo "ERROR: could not determine version; pass VERSION=x.y.z"; exit 1; }
	@echo "Release v$(PUIKIT_VERSION):"
	@# Asset names only: gh renders JSON numbers in Go's default float format, so
	@# {{.size}} would print sizes as 8.8917854e+07.
	@gh release view v$(PUIKIT_VERSION) --json assets \
		--template '{{range .assets}}  GitHub asset: {{.name}}{{"\n"}}{{end}}' \
		2>/dev/null || echo "  (no GitHub Release yet — run 'make release-github')"
	@$(VENV_PYTHON) -c "import json,urllib.request as u; \
		v='$(PUIKIT_VERSION)'; \
		d=json.load(u.urlopen('https://pypi.org/pypi/puikit/json')); \
		print('  PyPI: ' + ('published' if v in d['releases'] else 'NOT published'))" \
		2>/dev/null || echo "  PyPI: unknown (needs the venv and network access)"

clean:
	rm -rf build dist *.egg-info
	rm -f README.pypi.md
	find . -name __pycache__ -type d -not -path "./$(VENV)/*" -exec rm -rf {} +
	rm -rf .pytest_cache
