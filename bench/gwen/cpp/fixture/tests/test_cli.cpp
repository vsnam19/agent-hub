#include <gtest/gtest.h>

#include <cstdio>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <string>
#include <unistd.h>
#include <vector>

#include "minilib/cli.hpp"

namespace {

std::string write_temp(const std::string& name, const std::string& content) {
    const auto path = std::filesystem::temp_directory_path() / ("minilib_cli_" + std::to_string(::getpid()) + "_" + name);
    std::ofstream(path) << content;
    return path.string();
}

}  // namespace

TEST(Cli, Stats) {
    const std::string path = write_temp("stats.txt", "hello world");
    std::ostringstream out, err;
    EXPECT_EQ(minilib::run_cli({"stats", path}, out, err), 0);
    EXPECT_EQ(out.str(), "words: 2\nchars: 11\n");
    std::remove(path.c_str());
}

TEST(Cli, InventoryListsLowStock) {
    const std::string path = write_temp("items.csv", "name,qty,price\nA,2,1.5\nB,10,5.0\n");
    std::ostringstream out, err;
    EXPECT_EQ(minilib::run_cli({"inventory", path}, out, err), 0);
    EXPECT_EQ(out.str(), "A 2\n");
    std::remove(path.c_str());
}

TEST(Cli, UsageErrorReturnsTwo) {
    std::ostringstream out, err;
    EXPECT_EQ(minilib::run_cli({}, out, err), 2);
    EXPECT_NE(err.str().find("usage"), std::string::npos);
    EXPECT_EQ(minilib::run_cli({"stats"}, out, err), 2);
}
