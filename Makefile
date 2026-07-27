.PHONY: help test check verify ui lint clean icon release

PY ?= python3

help:
	@echo "make test     run the Houdini-free test suite"
	@echo "make check    everything provable without Houdini"
	@echo "make verify   probe a real Houdini install (needs \$$HFS)"
	@echo "make ui       open the launcher window"
	@echo "make icon     redraw hsl/assets/hsl.ico"
	@echo "make release  build dist/hsl-<version>.zip to hand out"

test:
	$(PY) -m unittest discover -s tests -v

check:
	@echo "== tests =="              && $(PY) -m unittest discover -s tests -q
	@echo "== boundary =="           && $(PY) .claude/hooks/boundary_guard.py --check-tree .
	@echo "== agent config drift ==" && $(PY) scripts/check_drift.py
	@echo "== ui imports =="         && $(PY) scripts/check_ui_imports.py

verify:
	$(PY) scripts/verify_environment.py --report

ui:
	$(PY) -m hsl.cli ui

icon:
	$(PY) scripts/make_icon.py

release: check icon
	$(PY) scripts/make_release.py

lint:
	ruff check hsl tests scripts

clean:
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	find . -name '*.pyc' -delete
