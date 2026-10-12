#include <gtest/gtest.h>

#include <filesystem>
#include <fstream>
#include <iterator>
#include <sstream>
#include <stdexcept>
#include <string>
#include <unistd.h>

#include "minilib/inventory.hpp"

namespace fs = std::filesystem;

TEST(C3Hidden, ReadItemsWorks) {
    const fs::path path = fs::temp_directory_path() / ("minilib_c3_hidden_" + std::to_string(::getpid()) + ".csv");
    std::ofstream(path) << "name,qty,price\nX,1,1.0\n";
    const auto items = minilib::read_items(path.string());
    fs::remove(path);
    ASSERT_EQ(items.size(), 1u);
    EXPECT_EQ(items[0].name, "X");
}

TEST(C3Hidden, MissingFileStillThrows) {
    EXPECT_THROW(minilib::read_items("/nonexistent/items.csv"), std::runtime_error);
}

TEST(C3Hidden, NoAliasLeftInTheSources) {
    const fs::path root = MINILIB_SOURCE_DIR;
    int checked = 0;
    for (const char* dir : {"include/minilib", "src", "tests"}) {
        for (const auto& entry : fs::directory_iterator(root / dir)) {
            if (!entry.is_regular_file()) {
                continue;
            }
            std::ifstream in(entry.path());
            const std::string text((std::istreambuf_iterator<char>(in)), std::istreambuf_iterator<char>());
            EXPECT_EQ(text.find("load_items"), std::string::npos) << entry.path();
            ++checked;
        }
    }
    EXPECT_GT(checked, 5);
}
