# Brief

## Goal
Add a `--json` flag to the `stats` command; the flag may come before or after `FILE`.
It prints exactly `{"chars": N, "words": N}` followed by a newline (keys in this order, one space after each colon
and comma).
Without the flag, output is unchanged.
Add a test.

## Input
- `src/cli.cpp`: `int run_cli(const std::vector<std::string>& args, std::ostream& out, std::ostream& err)` (`args` exclude the program name)
- `tests/test_cli.cpp`

## Expected output
- `stats` supports `--json`, and a test for it passes.

## Gate

Before finishing, run from the repository root and make it pass:

- `cmake -S . -B build -G Ninja && cmake --build build && ctest --test-dir build --output-on-failure`
