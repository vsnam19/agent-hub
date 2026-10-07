default:
    @just --list

lint:
    uv run ruff check . && uv run ruff format --check .

test:
    uv run pytest -q

smoke:
    scripts/smoke.sh
