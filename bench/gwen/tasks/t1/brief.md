# Brief

## Goal
Fix the off-by-one bug in `paginate()` so the visible tests pass.

## Input
- `src/minilib/paging.py`
- `tests/test_paging.py`

## Expected output
- Passing tests for `paginate()`

## Constraints
- Do not change `page_count`.

## Gate

Before finishing, run from the repository root and make both pass:

- `ruff check .`
- `python -m pytest -q`
