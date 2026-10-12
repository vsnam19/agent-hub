#pragma once

#include <string>
#include <vector>

namespace minilib {

// Items of the 1-based `page`; empty past the end. Throws std::invalid_argument when page < 1.
std::vector<std::string> paginate(const std::vector<std::string>& items, int page, int size);

// Number of pages needed for n items.
int page_count(int n, int size);

}  // namespace minilib
