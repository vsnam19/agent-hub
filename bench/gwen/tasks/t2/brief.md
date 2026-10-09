# Brief

## Goal
Add `top_words(text: str, k: int) -> list[tuple[str, int]]` to `textstats.py`.
Lower-case, words = `[a-z0-9']+`, sorted by count desc then word asc, `k <= 0` -> `[]`.
Write tests in `tests/test_textstats.py`.

## Input
- `src/minilib/textstats.py`
- `tests/test_textstats.py`

## Expected output
- Implementation of `top_words` and passing tests.

## Gate

Before finishing, run from the repository root and make both pass:

- `ruff check .`
- `python -m pytest -q`
