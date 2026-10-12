#pragma once

#include <string>
#include <vector>

namespace minilib {

struct Item {
    std::string name;
    int qty;
    double price;
};

// Reads a CSV file with the header `name,qty,price`. Throws std::runtime_error if it cannot be opened.
std::vector<Item> load_items(const std::string& path);

double total_value(const std::vector<Item>& items);

// Items with qty < threshold.
std::vector<Item> low_stock(const std::vector<Item>& items, int threshold);

}  // namespace minilib
