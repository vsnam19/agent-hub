#include "minilib/cli.hpp"

#include <fstream>
#include <ostream>
#include <sstream>
#include <stdexcept>

#include "minilib/inventory.hpp"
#include "minilib/textstats.hpp"

namespace minilib {

namespace {

constexpr const char* kUsage = "usage: minilib stats FILE | inventory FILE [--min-qty N]";

int usage(std::ostream& err) {
    err << kUsage << "\n";
    return 2;
}

int run_stats(const std::vector<std::string>& args, std::ostream& out, std::ostream& err) {
    if (args.size() != 2) {
        return usage(err);
    }
    std::ifstream in(args[1]);
    if (!in) {
        err << "cannot open " << args[1] << "\n";
        return 1;
    }
    std::stringstream buffer;
    buffer << in.rdbuf();
    const std::string text = buffer.str();
    out << "words: " << word_count(text) << "\n";
    out << "chars: " << text.size() << "\n";
    return 0;
}

int run_inventory(const std::vector<std::string>& args, std::ostream& out, std::ostream& err) {
    if (args.size() != 2 && args.size() != 4) {
        return usage(err);
    }
    int min_qty = 5;
    if (args.size() == 4) {
        if (args[2] != "--min-qty") {
            return usage(err);
        }
        try {
            min_qty = std::stoi(args[3]);
        } catch (const std::logic_error&) {
            return usage(err);
        }
    }
    for (const Item& item : load_items(args[1])) {
        if (item.qty < min_qty) {
            out << item.name << " " << item.qty << "\n";
        }
    }
    return 0;
}

}  // namespace

int run_cli(const std::vector<std::string>& args, std::ostream& out, std::ostream& err) {
    if (args.empty()) {
        return usage(err);
    }
    if (args[0] == "stats") {
        return run_stats(args, out, err);
    }
    if (args[0] == "inventory") {
        return run_inventory(args, out, err);
    }
    return usage(err);
}

}  // namespace minilib
