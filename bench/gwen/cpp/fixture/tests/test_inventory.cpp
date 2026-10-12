#include <gtest/gtest.h>

#include <stdexcept>
#include <string>
#include <vector>

#include "minilib/inventory.hpp"

using minilib::Item;

namespace {
const std::vector<Item> kItems = {{"A", 2, 1.5}, {"B", 10, 5.0}};
}

TEST(Inventory, LoadsTheDataFile) {
    const auto items = minilib::load_items(std::string(MINILIB_SOURCE_DIR) + "/data/items.csv");
    ASSERT_GE(items.size(), 2u);
    EXPECT_EQ(items[0].name, "Nail");
    EXPECT_EQ(items[0].qty, 100);
    EXPECT_EQ(items[1].name, "Screw");
    EXPECT_DOUBLE_EQ(items[1].price, 0.10);
}

TEST(Inventory, MissingFileThrows) {
    EXPECT_THROW(minilib::load_items("/nonexistent/items.csv"), std::runtime_error);
}

TEST(Inventory, TotalValue) {
    EXPECT_DOUBLE_EQ(minilib::total_value(kItems), 53.0);
}

TEST(Inventory, LowStock) {
    const auto low = minilib::low_stock(kItems, 5);
    ASSERT_EQ(low.size(), 1u);
    EXPECT_EQ(low[0].name, "A");
}
