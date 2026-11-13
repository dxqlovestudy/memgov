.PHONY: install test lint demo

install:
	pip install -e ".[dev]"

test:
	pytest -q

lint:
	ruff check .

demo:
	memgov demo
