# Brief

## Goal

Building the project and running `./build/minilib inventory data/items.csv` from the repository root crashes with the
output below. Make this command work.

## Input

```
$ ./build/minilib inventory data/items.csv
terminate called after throwing an instance of 'std::invalid_argument'
  what():  stoi
Aborted (core dumped)
```

The reading code is `std::vector<Item> load_items(const std::string& path)` in `src/inventory.cpp` (declared in
`include/minilib/inventory.hpp`).

## Expected output

- The command above exits 0 and prints the low-stock items.
- The existing tests still pass: `ctest --test-dir build --output-on-failure`.

## Constraints

- Only `src/inventory.cpp` may change.
- Do not modify `data/items.csv`.

## Gate

Before finishing, run from the repository root and make it pass:

- `cmake -S . -B build -G Ninja && cmake --build build && ctest --test-dir build --output-on-failure`
