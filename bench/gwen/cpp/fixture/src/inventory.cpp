#include "minilib/inventory.hpp"

#include <fstream>
#include <sstream>
#include <stdexcept>

namespace minilib {

std::vector<Item> load_items(const std::string& path) {
    std::ifstream in(path);
    if (!in) {
        throw std::runtime_error("cannot open " + path);
    }
    std::vector<Item> items;
    std::string line;
    std::getline(in, line);  // header
    while (std::getline(in, line)) {
        if (!line.empty() && line.back() == '\r') {
            line.pop_back();
        }
        if (line.empty()) {
            continue;
        }
        std::vector<std::string> parts;
        std::istringstream fields(line);
        std::string field;
        while (std::getline(fields, field, ',')) {
            parts.push_back(field);
        }
        if (parts.size() < 3) {
            throw std::runtime_error("bad row: " + line);
        }
        items.push_back(Item{parts[0], std::stoi(parts[1]), std::stod(parts[2])});
    }
    return items;
}

double total_value(const std::vector<Item>& items) {
    double total = 0.0;
    for (const Item& item : items) {
        total += item.qty * item.price;
    }
    return total;
}

std::vector<Item> low_stock(const std::vector<Item>& items, int threshold) {
    std::vector<Item> out;
    for (const Item& item : items) {
        if (item.qty < threshold) {
            out.push_back(item);
        }
    }
    return out;
}

}  // namespace minilib
