#include <gtest/gtest.h>

#include <stdexcept>
#include <string>
#include <vector>

#include "minilib/paging.hpp"

using minilib::page_count;
using minilib::paginate;

TEST(Paging, ReturnsEachPage) {
    const std::vector<std::string> items = {"a", "b", "c", "d", "e"};
    EXPECT_EQ(paginate(items, 1, 2), (std::vector<std::string>{"a", "b"}));
    EXPECT_EQ(paginate(items, 2, 2), (std::vector<std::string>{"c", "d"}));
    EXPECT_EQ(paginate(items, 3, 2), (std::vector<std::string>{"e"}));
}

TEST(Paging, BadPageThrows) {
    EXPECT_THROW(paginate({}, 0, 10), std::invalid_argument);
}

TEST(Paging, PageCount) {
    EXPECT_EQ(page_count(5, 2), 3);
    EXPECT_EQ(page_count(4, 2), 2);
    EXPECT_EQ(page_count(0, 2), 0);
}
