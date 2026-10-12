#include <gtest/gtest.h>

#include <stdexcept>
#include <string>
#include <vector>

#include "minilib/paging.hpp"

using Strings = std::vector<std::string>;

TEST(PagingHidden, Paginate) {
    const Strings items = {"0", "1", "2", "3", "4", "5", "6", "7", "8", "9"};
    EXPECT_EQ(minilib::paginate(items, 1, 3), (Strings{"0", "1", "2"}));
    EXPECT_EQ(minilib::paginate(items, 2, 3), (Strings{"3", "4", "5"}));
    EXPECT_EQ(minilib::paginate(items, 4, 3), (Strings{"9"}));
    EXPECT_TRUE(minilib::paginate(items, 5, 3).empty());
}

TEST(PagingHidden, BadPageThrows) {
    EXPECT_THROW(minilib::paginate({"a"}, 0, 3), std::invalid_argument);
}

TEST(PagingHidden, PageCount) {
    EXPECT_EQ(minilib::page_count(0, 3), 0);
    EXPECT_EQ(minilib::page_count(9, 3), 3);
    EXPECT_EQ(minilib::page_count(10, 3), 4);
}
