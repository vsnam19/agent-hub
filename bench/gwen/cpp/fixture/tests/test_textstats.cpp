#include <gtest/gtest.h>

#include <map>
#include <string>

#include "minilib/textstats.hpp"

using minilib::char_freq;
using minilib::word_count;

TEST(TextStats, CountsWords) {
    EXPECT_EQ(word_count("hello world"), 2u);
    EXPECT_EQ(word_count("  spaced \t out\nlines  "), 3u);
}

TEST(TextStats, EmptyTextHasNoWords) {
    EXPECT_EQ(word_count(""), 0u);
}

TEST(TextStats, CharFreq) {
    const std::map<char, int> expected = {{'a', 2}, {'b', 1}};
    EXPECT_EQ(char_freq("aba"), expected);
}
