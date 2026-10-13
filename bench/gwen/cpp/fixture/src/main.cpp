#include <iostream>
#include <string>
#include <vector>

#include "minilib/cli.hpp"

int main(int argc, char** argv) {
    const std::vector<std::string> args(argv + 1, argv + argc);
    return minilib::run_cli(args, std::cout, std::cerr);
}
