PYTHON := python3.14
VENV := .venv
VENV_PYTHON := $(VENV)/bin/python
VENV_PIP := $(VENV)/bin/pip

.PHONY: help venv install test hello demo layout hello-gui demo-gui layout-gui clean

help:
	@echo "PuiKit utility commands:"
	@echo "  make venv      - create the virtualenv ($(VENV)/, $(PYTHON))"
	@echo "  make install   - install puikit into the venv (editable, with dev/macos deps)"
	@echo "  make test      - run the test suite"
	@echo "  make hello     - run the hello_world example (TUI)"
	@echo "  make demo      - run the demo_catalog example (TUI)"
	@echo "  make layout    - run the layout demo (TUI)"
	@echo "  make hello-gui - run the hello_world example (macOS GUI)"
	@echo "  make demo-gui  - run the demo_catalog example (macOS GUI)"
	@echo "  make layout-gui - run the layout demo (macOS GUI, pixel layout)"
	@echo "  make clean     - remove build artifacts and caches"

venv:
	$(PYTHON) -m venv $(VENV)

install:
	$(VENV_PIP) install -e ".[dev,macos]"

test:
	$(VENV_PYTHON) -m pytest

hello:
	$(VENV_PYTHON) examples/hello_world/main.py

demo:
	$(VENV_PYTHON) examples/demo_catalog/main.py

hello-gui:
	$(VENV_PYTHON) examples/hello_world/main.py --backend gui

demo-gui:
	$(VENV_PYTHON) examples/demo_catalog/main.py --backend gui

layout:
	$(VENV_PYTHON) examples/layout_demo/main.py

layout-gui:
	$(VENV_PYTHON) examples/layout_demo/main.py --backend gui

clean:
	rm -rf build dist *.egg-info
	find . -name __pycache__ -type d -not -path "./$(VENV)/*" -exec rm -rf {} +
	rm -rf .pytest_cache
