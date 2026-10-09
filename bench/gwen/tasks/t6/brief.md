# Brief

## Goal
Add `src/minilib/report.py` with `summarize(items) -> dict`. It returns:
`count`, `total_qty`, `total_value` (rounded to 2), `low_stock` (names with qty < 5 sorted).
Add a `report FILE` subcommand to cli that prints the dict as sorted JSON.
Write tests in `tests/test_report.py`.

## Input
- `src/minilib/cli.py`
- `tests/test_cli.py`

## Expected output
- `report.py` and `test_report.py` created.
- `report` subcommand works.

## Gate

Before finishing, run from the repository root and make both pass:

- `ruff check .`
- `python -m pytest -q`
