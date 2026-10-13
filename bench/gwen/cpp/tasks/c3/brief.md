# Brief

## Goal
Rename `load_items` to `read_items` everywhere: header, source, the CLI and the tests.
Behaviour unchanged. No alias left.

## Input
- `include/minilib/inventory.hpp`
- `src/inventory.cpp`
- `src/cli.cpp`
- `tests/test_inventory.cpp`

## Expected output
- `read_items(const std::string& path)` is the only reading function name used.

## Gate

Before finishing, run from the repository root and make it pass:

- `cmake -S . -B build -G Ninja && cmake --build build && ctest --test-dir build --output-on-failure`
