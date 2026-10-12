#include <gtest/gtest.h>

#include <string>
#include <utility>
#include <vector>

#include "minilib/textstats.hpp"

using Ranked = std::vector<std::pair<std::string, int>>;

TEST(TextStatsHidden, RanksByCountThenWord) {
    EXPECT_EQ(minilib::top_words("b a b c a b", 3), (Ranked{{"b", 3}, {"a", 2}, {"c", 1}}));
    EXPECT_EQ(minilib::top_words("b a c", 3), (Ranked{{"a", 1}, {"b", 1}, {"c", 1}}));
}

TEST(TextStatsHidden, LimitsToK) {
    EXPECT_EQ(minilib::top_words("a a b b c", 1), (Ranked{{"a", 2}}));
    EXPECT_EQ(minilib::top_words("a a b b c", 10).size(), 3u);
}

TEST(TextStatsHidden, LowerCasesAndKeepsApostropheAndDigits) {
    EXPECT_EQ(minilib::top_words("Don't don't DON'T, 42 42 x1", 2), (Ranked{{"don't", 3}, {"42", 2}}));
}

TEST(TextStatsHidden, NonPositiveKAndEmptyText) {
    EXPECT_TRUE(minilib::top_words("a b", 0).empty());
    EXPECT_TRUE(minilib::top_words("a b", -3).empty());
    EXPECT_TRUE(minilib::top_words("", 5).empty());
    EXPECT_TRUE(minilib::top_words("!!! ...", 5).empty());
}
