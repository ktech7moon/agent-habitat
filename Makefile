.PHONY: install test lint fmt type smoke check

install:
	pip install -e ".[dev]"

test:
	pytest -v

lint:
	ruff check src tests

fmt:
	ruff format src tests

type:
	mypy src

smoke:
	pytest tests/test_smoke.py -v

check: lint type test
