#pragma once

#include <iosfwd>
#include <string>
#include <vector>

namespace minilib {

// Runs a command (args exclude the program name). Returns the exit code; usage errors go to `err` and return 2.
int run_cli(const std::vector<std::string>& args, std::ostream& out, std::ostream& err);

}  // namespace minilib
