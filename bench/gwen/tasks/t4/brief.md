# Brief

## Goal
Add `--json` flag to `stats` command.
It should print `{"words": N, "chars": N}` using `json.dumps(..., sort_keys=True)`.
Without the flag, output is unchanged.
Add a test.

## Input
- `src/minilib/cli.py`
- `tests/test_cli.py`

## Expected output
- Command supports `--json`.

## Gate

Before finishing, run from the repository root and make both pass:

- `ruff check .`
- `python -m pytest -q`
