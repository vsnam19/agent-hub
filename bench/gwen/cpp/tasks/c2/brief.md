# Brief

## Goal
Add `std::vector<std::pair<std::string, int>> top_words(const std::string& text, int k)` to the `minilib` namespace
(`textstats.hpp` / `textstats.cpp`).
Lower-case the text, words = runs of `[a-z0-9']`, sorted by count descending then word ascending, at most `k` entries,
`k <= 0` -> empty vector.
Write tests in `tests/test_textstats.cpp`.

## Input
- `include/minilib/textstats.hpp`
- `src/textstats.cpp`
- `tests/test_textstats.cpp`

## Expected output
- Implementation of `top_words` and passing tests.

## Gate

Before finishing, run from the repository root and make it pass:

- `cmake -S . -B build -G Ninja && cmake --build build && ctest --test-dir build --output-on-failure`
