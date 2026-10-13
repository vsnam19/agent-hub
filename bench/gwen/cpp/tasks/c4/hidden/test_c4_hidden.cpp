#include <gtest/gtest.h>

#include <filesystem>
#include <fstream>
#include <sstream>
#include <string>
#include <unistd.h>

#include "minilib/cli.hpp"

namespace fs = std::filesystem;

namespace {

class StatsJson : public ::testing::Test {
protected:
    void SetUp() override {
        path_ = (fs::temp_directory_path() / ("minilib_c4_hidden_" + std::to_string(::getpid()) + ".txt")).string();
        std::ofstream(path_) << "one two  three\nfour";
    }
    void TearDown() override { fs::remove(path_); }
    std::string path_;
};

}  // namespace

TEST_F(StatsJson, FlagBeforeFile) {
    std::ostringstream out, err;
    EXPECT_EQ(minilib::run_cli({"stats", "--json", path_}, out, err), 0);
    EXPECT_EQ(out.str(), "{\"chars\": 19, \"words\": 4}\n");
}

TEST_F(StatsJson, FlagAfterFile) {
    std::ostringstream out, err;
    EXPECT_EQ(minilib::run_cli({"stats", path_, "--json"}, out, err), 0);
    EXPECT_EQ(out.str(), "{\"chars\": 19, \"words\": 4}\n");
}

TEST_F(StatsJson, PlainOutputUnchanged) {
    std::ostringstream out, err;
    EXPECT_EQ(minilib::run_cli({"stats", path_}, out, err), 0);
    EXPECT_EQ(out.str(), "words: 4\nchars: 19\n");
}

TEST_F(StatsJson, MissingFileArgumentIsUsageError) {
    std::ostringstream out, err;
    EXPECT_EQ(minilib::run_cli({"stats", "--json"}, out, err), 2);
    EXPECT_FALSE(err.str().empty());
}
