#include <gtest/gtest.h>

#include <sstream>
#include <string>
#include <vector>

#include "minilib/cli.hpp"
#include "minilib/inventory.hpp"
#include "minilib/report.hpp"

TEST(C6Hidden, Summarize) {
    const std::vector<minilib::Item> items = {{"Zip", 4, 2.0}, {"Nail", 100, 0.05}, {"Awl", 0, 9.99}, {"Bolt", 5, 1.0}};
    const minilib::Summary s = minilib::summarize(items);
    EXPECT_EQ(s.count, 4);
    EXPECT_EQ(s.total_qty, 109);
    EXPECT_NEAR(s.total_value, 18.0, 1e-9);
    EXPECT_EQ(s.low_stock, (std::vector<std::string>{"Awl", "Zip"}));
}

TEST(C6Hidden, ReportSubcommandOnTheDataFile) {
    std::ostringstream out, err;
    const std::string data = std::string(MINILIB_SOURCE_DIR) + "/data/items.csv";
    EXPECT_EQ(minilib::run_cli({"report", data}, out, err), 0);
    EXPECT_EQ(out.str(), "{\"count\": 2, \"low_stock\": [\"Screw\"], \"total_qty\": 103, \"total_value\": 5.30}\n");
}

TEST(C6Hidden, ReportWithoutFileIsUsageError) {
    std::ostringstream out, err;
    EXPECT_EQ(minilib::run_cli({"report"}, out, err), 2);
    EXPECT_FALSE(err.str().empty());
}
