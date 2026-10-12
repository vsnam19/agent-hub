# Brief

## Goal
Add `include/minilib/report.hpp` and `src/report.cpp` (namespace `minilib`):

```cpp
struct Summary {
    int count;
    int total_qty;
    double total_value;
    std::vector<std::string> low_stock;
};
Summary summarize(const std::vector<Item>& items);
```

`count` is the number of items, `total_qty` the sum of the quantities, `total_value` the sum of `qty * price`, and
`low_stock` the names of the items with `qty < 5`, sorted.

Add a `report FILE` subcommand to the CLI that prints one line, with sorted keys and two decimals for the value:
`{"count": N, "low_stock": ["a", "b"], "total_qty": N, "total_value": X.XX}` followed by a newline.
Write tests in `tests/test_report.cpp`.

## Input
- `include/minilib/inventory.hpp`: `struct Item { std::string name; int qty; double price; }`, `std::vector<Item> load_items(const std::string& path)`
- `include/minilib/cli.hpp`: `int run_cli(const std::vector<std::string>& args, std::ostream& out, std::ostream& err)`
- `src/cli.cpp`
- `tests/test_cli.cpp`
- `CMakeLists.txt` (the library's source list is explicit)

## Expected output
- `report.hpp`, `report.cpp` and `tests/test_report.cpp` created, and the new source built into the library.
- The `report` subcommand works.

## Gate

Before finishing, run from the repository root and make it pass:

- `cmake -S . -B build -G Ninja && cmake --build build && ctest --test-dir build --output-on-failure`
