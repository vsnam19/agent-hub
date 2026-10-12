#include <gtest/gtest.h>

#include <filesystem>
#include <fstream>
#include <sstream>
#include <string>

#include "minilib/cli.hpp"
#include "minilib/inventory.hpp"

namespace fs = std::filesystem;

TEST(C5Hidden, QuotedNameWithCommaIsOneField) {
    const auto items = minilib::load_items(std::string(MINILIB_SOURCE_DIR) + "/data/items.csv");
    ASSERT_EQ(items.size(), 3u);
    EXPECT_EQ(items[2].name, "Bolt, large");
    EXPECT_EQ(items[2].qty, 12);
    EXPECT_DOUBLE_EQ(items[2].price, 0.30);
}

TEST(C5Hidden, PlainRowsStillParse) {
    const fs::path path = fs::temp_directory_path() / "minilib_c5_hidden.csv";
    std::ofstream(path) << "name,qty,price\nA,2,1.5\n\"B, C\",10,5.0\n";
    const auto items = minilib::load_items(path.string());
    fs::remove(path);
    ASSERT_EQ(items.size(), 2u);
    EXPECT_EQ(items[0].name, "A");
    EXPECT_EQ(items[1].name, "B, C");
    EXPECT_EQ(items[1].qty, 10);
}

TEST(C5Hidden, InventoryCommandWorksOnTheDataFile) {
    std::ostringstream out, err;
    const std::string data = std::string(MINILIB_SOURCE_DIR) + "/data/items.csv";
    EXPECT_EQ(minilib::run_cli({"inventory", data}, out, err), 0);
    EXPECT_EQ(out.str(), "Screw 3\n");
    std::ostringstream wide;
    EXPECT_EQ(minilib::run_cli({"inventory", data, "--min-qty", "20"}, wide, err), 0);
    EXPECT_EQ(wide.str(), "Screw 3\nBolt, large 12\n");
}
