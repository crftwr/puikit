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
    # pyobjc (the macos extra) only installs on macOS.
    EXTRAS := dev
else
    PYTHON := python3.14
    VENV_PYTHON := $(VENV)/bin/python
    VENV_PIP := $(VENV)/bin/pip
    ifeq ($(UNAME_S),Darwin)
        EXTRAS := dev,macos
    else
        EXTRAS := dev
    endif
endif

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

.PHONY: help venv install test hello demo layout hello-gui demo-gui layout-gui clean

help:
	@echo "PuiKit utility commands:"
	@echo "  make venv      - create the virtualenv and install puikit ($(VENV)/, $(PYTHON))"
	@echo "  make install   - (re)install puikit into the venv (editable, with dev deps; +macos on macOS)"
	@echo "  make test      - run the test suite"
	@echo "  make hello     - run the hello_world example (TUI)"
	@echo "  make demo      - run the demo_catalog example (TUI)"
	@echo "  make layout    - run the layout demo (TUI)"
	@echo "  make hello-gui - run the hello_world example (native GUI: macOS or Windows)"
	@echo "  make demo-gui  - run the demo_catalog example (native GUI: macOS or Windows)"
	@echo "  make layout-gui - run the layout demo (native GUI, pixel layout)"
	@echo "  make clean     - remove build artifacts and caches"
	@echo ""
	@echo "  Run/test targets create the venv and install puikit automatically"
	@echo "  if needed. GUI targets accept FONT_SIZE, e.g. make demo-gui FONT_SIZE=18"

$(VENV_STAMP): pyproject.toml
	$(PYTHON) -m venv $(VENV)
	$(VENV_PIP) install -e ".[$(EXTRAS)]"
	@touch $(VENV_STAMP)

venv: $(VENV_STAMP)

install: $(VENV_STAMP)

test: $(VENV_STAMP)
	$(VENV_PYTHON) -m pytest

hello: $(VENV_STAMP)
	$(VENV_PYTHON) examples/hello_world/main.py

demo: $(VENV_STAMP)
	$(VENV_PYTHON) examples/demo_catalog/main.py

hello-gui: $(VENV_STAMP)
	$(VENV_PYTHON) examples/hello_world/main.py --backend gui $(FONT_SIZE_ARG)

demo-gui: $(VENV_STAMP)
	$(VENV_PYTHON) examples/demo_catalog/main.py --backend gui $(FONT_SIZE_ARG)

layout: $(VENV_STAMP)
	$(VENV_PYTHON) examples/layout_demo/main.py

layout-gui: $(VENV_STAMP)
	$(VENV_PYTHON) examples/layout_demo/main.py --backend gui

clean:
	rm -rf build dist *.egg-info
	find . -name __pycache__ -type d -not -path "./$(VENV)/*" -exec rm -rf {} +
	rm -rf .pytest_cache
