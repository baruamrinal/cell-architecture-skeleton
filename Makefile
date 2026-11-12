# Local development tasks (macOS/Linux). On Windows use .\make <target> instead.
# Thin wrapper around scripts/dev.py, so both stay identical.

PYTHON := $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)
TARGETS := up down clean status build test setup help

.PHONY: $(TARGETS)
.DEFAULT_GOAL := help

$(TARGETS):
	@$(PYTHON) scripts/dev.py $@
	@if [ "$@" = "up" ] && [ -z "$$VIRTUAL_ENV" ]; then \
		echo "Tip: run 'source .venv/bin/activate' to use cellctl in this terminal."; fi
