#include "minilib/paging.hpp"

#include <algorithm>
#include <cstddef>
#include <stdexcept>

namespace minilib {

std::vector<std::string> paginate(const std::vector<std::string>& items, int page, int size) {
    if (page < 1) {
        throw std::invalid_argument("page must be >= 1");
    }
    if (size < 1) {
        return {};
    }
    const std::size_t start = static_cast<std::size_t>(page - 1) * static_cast<std::size_t>(size);
    if (start >= items.size()) {
        return {};
    }
    const std::size_t end = std::min(start + static_cast<std::size_t>(size), items.size());
    return {items.begin() + static_cast<std::ptrdiff_t>(start), items.begin() + static_cast<std::ptrdiff_t>(end)};
}

int page_count(int n, int size) {
    if (n == 0 || size < 1) {
        return 0;
    }
    return (n + size - 1) / size;
}

}  // namespace minilib
