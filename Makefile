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
    PY_ON_PATH := $(shell where python3.14 2>/dev/null)
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

.PHONY: help venv install test hello demo layout hello-gui demo-gui layout-gui clean

help:
	@echo "PuiKit utility commands:"
	@echo "  make venv      - create the virtualenv ($(VENV)/, $(PYTHON))"
	@echo "  make install   - install puikit into the venv (editable, with dev deps; +macos on macOS)"
	@echo "  make test      - run the test suite"
	@echo "  make hello     - run the hello_world example (TUI)"
	@echo "  make demo      - run the demo_catalog example (TUI)"
	@echo "  make layout    - run the layout demo (TUI)"
	@echo "  make hello-gui - run the hello_world example (native GUI: macOS or Windows)"
	@echo "  make demo-gui  - run the demo_catalog example (native GUI: macOS or Windows)"
	@echo "  make layout-gui - run the layout demo (native GUI, pixel layout)"
	@echo "  make clean     - remove build artifacts and caches"
	@echo ""
	@echo "  GUI targets accept FONT_SIZE, e.g. make demo-gui FONT_SIZE=18"

venv:
	$(PYTHON) -m venv $(VENV)

install:
	$(VENV_PIP) install -e ".[$(EXTRAS)]"

test:
	$(VENV_PYTHON) -m pytest

hello:
	$(VENV_PYTHON) examples/hello_world/main.py

demo:
	$(VENV_PYTHON) examples/demo_catalog/main.py

hello-gui:
	$(VENV_PYTHON) examples/hello_world/main.py --backend gui $(FONT_SIZE_ARG)

demo-gui:
	$(VENV_PYTHON) examples/demo_catalog/main.py --backend gui $(FONT_SIZE_ARG)

layout:
	$(VENV_PYTHON) examples/layout_demo/main.py

layout-gui:
	$(VENV_PYTHON) examples/layout_demo/main.py --backend gui

clean:
	rm -rf build dist *.egg-info
	find . -name __pycache__ -type d -not -path "./$(VENV)/*" -exec rm -rf {} +
	rm -rf .pytest_cache
