PYTHON := python3.14
VENV := .venv
VENV_PYTHON := $(VENV)/bin/python
VENV_PIP := $(VENV)/bin/pip

.PHONY: help venv install test hello demo clean

help:
	@echo "PuiKit utility commands:"
	@echo "  make venv     - create the virtualenv ($(VENV)/, $(PYTHON))"
	@echo "  make install  - install puikit into the venv (editable, with dev deps)"
	@echo "  make test     - run the test suite"
	@echo "  make hello    - run the hello_world example (TUI)"
	@echo "  make demo     - run the demo_catalog example (TUI)"
	@echo "  make clean    - remove build artifacts and caches"

venv:
	$(PYTHON) -m venv $(VENV)

install:
	$(VENV_PIP) install -e ".[dev]"

test:
	$(VENV_PYTHON) -m pytest

hello:
	$(VENV_PYTHON) examples/hello_world/main.py

demo:
	$(VENV_PYTHON) examples/demo_catalog/main.py

clean:
	rm -rf build dist *.egg-info
	find . -name __pycache__ -type d -not -path "./$(VENV)/*" -exec rm -rf {} +
	rm -rf .pytest_cache
