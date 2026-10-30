.PHONY: check test format

check:
	uv run --frozen ruff check .
	uv run --frozen ruff format --check .
	uv run --frozen pytest

test:
	uv run --frozen pytest --cov=probe --cov=manage --cov-report=term-missing

format:
	uv run --frozen ruff check --fix .
	uv run --frozen ruff format .
