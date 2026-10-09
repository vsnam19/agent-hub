# Brief

## Goal
Rename `load_items` to `read_items` everywhere.
Behaviour unchanged. No alias left.

## Input
- `src/minilib/inventory.py`
- `src/minilib/cli.py`
- `tests/test_inventory.py`

## Expected output
- `read_items` is the only function name used.

## Gate

Before finishing, run from the repository root and make both pass:

- `ruff check .`
- `python -m pytest -q`
