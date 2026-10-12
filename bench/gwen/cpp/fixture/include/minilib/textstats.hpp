#pragma once

#include <cstddef>
#include <map>
#include <string>

namespace minilib {

// Number of whitespace-separated words.
std::size_t word_count(const std::string& text);

// How often each character occurs.
std::map<char, int> char_freq(const std::string& text);

}  // namespace minilib
