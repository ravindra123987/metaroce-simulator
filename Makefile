PY ?= python3

.PHONY: test lint check

test:
	$(PY) -m pytest -q

lint:
	$(PY) -m ruff check .

check: lint test
