# Brief

## Goal
Fix the off-by-one bug in `paginate()` so the visible tests pass.

## Input
- `src/paging.cpp`
- `tests/test_paging.cpp`
- `include/minilib/paging.hpp`: `std::vector<std::string> paginate(const std::vector<std::string>& items, int page, int size)`

## Expected output
- Passing tests for `paginate()`

## Constraints
- Do not change `page_count`.

## Gate

Before finishing, run from the repository root and make it pass:

- `cmake -S . -B build -G Ninja && cmake --build build && ctest --test-dir build --output-on-failure`
