#include "minilib/textstats.hpp"

#include <sstream>

namespace minilib {

std::size_t word_count(const std::string& text) {
    std::istringstream in(text);
    std::string word;
    std::size_t n = 0;
    while (in >> word) {
        ++n;
    }
    return n;
}

std::map<char, int> char_freq(const std::string& text) {
    std::map<char, int> freq;
    for (char c : text) {
        ++freq[c];
    }
    return freq;
}

}  // namespace minilib
