# Phase C — a C++ suite for the gwen bench

Status: in progress, 2026-10-12. Decision D-090.

## Goal

Measure gwen on C++ before any C++ task is routed to it (D-077). The Python bench (fixture `minilib`, tasks t1–t6,
hidden pytest tests) says nothing about CMake, headers, compile errors or gtest. Add a second **suite**: a C++17 fixture
built with CMake + Ninja and tested with GoogleTest, six tasks c1–c6 with hidden tests, run by the same
`bench.gwen.run` with the same scoring (all gate steps pass, no scope violation, no infra error).

## Machine facts (2026-10-12)

This Linux host runs the harness and the gate; only the model is remote (gwen on the Mac, `GWEN_BASE_URL`). Installed:
cmake, ninja, g++ 15.2, clang++, clang-tidy, clang-format, `libgtest-dev` 1.17 (`find_package(GTest)` works). No
cppcheck. The gwen host did not answer at 07:20 UTC, so a live run waits for the Mac (as in Phase B).

## Design

### Suites (task `suite-runner`)

- `bench/gwen/suites.toml` defines each suite: `fixture` and `tasks` directories (relative to `bench/gwen`), the
  ordered `[<suite>.gate]` steps (name → argv), and `[<suite>.harness]` values that harness argv may use.
- Gate argv placeholders: `{python}` (the bench's interpreter), `{build_dir}` (a fresh directory inside the run
  directory, outside the repository, so the agent's own `build/` never counts), `{visible_tests}` (expands to zero or
  more separate arguments from the task's `visible_tests`).
- Harness placeholders `{lint_cmd}` and `{test_cmd}` come from the suite (aider's `--lint-cmd`/`--test-cmd` are
  Python-only today).
- The Python suite keeps its paths (`fixture/`, `tasks/t1..t6`) and its gate names (`ruff`, `visible`, `hidden`), so
  existing results and reports stay comparable.
- `load_tasks()` discovers every task directory of every suite (no hard-coded `t1..t6`); task ids are unique across
  suites (`t*` Python, `c*` C++). `Task` carries its `suite`. `run.py` gains `--suite NAME` (repeatable) to filter;
  `--task all` keeps meaning every task. Each result line records `suite`.
- The hidden tests directory is `tests/hidden` in both suites, removed and re-copied before the gate (as today), so an
  agent cannot pre-empt it.

### C++ fixture and tasks (task `cpp-fixture`)

- `bench/gwen/cpp/fixture`: a C++17 library `minilib` mirroring the Python one — `paging`, `textstats`,
  `inventory` (CSV), and a CLI `minilib` with `stats FILE` and `inventory FILE [--min-qty N]`. Headers in
  `include/minilib/*.hpp`, sources in `src/*.cpp`, CLI logic in a testable `int run_cli(const std::vector<std::string>&
  args, std::ostream& out, std::ostream& err)`, `main.cpp` only forwards. Visible tests in `tests/*.cpp` (one gtest
  executable, ctest label `visible`); when `tests/hidden/` exists, its `*.cpp` build a second executable labelled
  `hidden`. Warnings: `-Wall -Wextra -Wpedantic` and `CMAKE_COMPILE_WARNING_AS_ERROR` (the lint of this suite;
  clang-tidy is too slow and noisy for a 9B model's loop and is a later option). `.gitignore` ignores `build/`.
  About the size of the Python fixture (≈ 300 lines in all).
- Gate of the suite: `configure` (`cmake -S . -B {build_dir} -G Ninja -DCMAKE_BUILD_TYPE=Debug`), `build`,
  `visible` (`ctest --test-dir {build_dir} -L visible --no-tests=error --output-on-failure`), `hidden` (same with `-L
  hidden`).
- Tasks mirror the Python ones, so the two suites compare one language against the other on the same kinds of work:

| Id | Kind | Task |
|---|---|---|
| c1 | bug fix | off-by-one in `paginate()`; a visible test fails |
| c2 | feature + tests | `top_words(text, k)` → `std::vector<std::pair<std::string, int>>`, same rules as t2 |
| c3 | refactor across header/source/callers | rename `load_items` → `read_items`, no alias left |
| c4 | CLI flag | `stats --json` prints `{"chars": N, "words": N}` (sorted keys, no JSON library) |
| c5 | crash from a trace | `minilib inventory data/items.csv` ends with `std::invalid_argument` from `std::stoi` on a quoted CSV field; make it work without changing the data |
| c6 | new module + build change | `report.hpp/.cpp` with `summarize(items)`, a `report FILE` subcommand printing sorted JSON; the agent must add the source to `CMakeLists.txt` |

- Briefs follow the Python ones (Goal / Input / Expected output / Constraints / Gate), name exact signatures (hidden
  tests compile against them), never mention hidden tests, and give the gate as
  `cmake -S . -B build -G Ninja && cmake --build build && ctest --test-dir build --output-on-failure`.
- Tests (same guarantees as `tests/bench/test_gwen_tasks.py`): every C++ task's hidden tests fail on the setup state
  and pass with `solution.patch`; the solution passes every gate step and stays inside `paths_allowed`; briefs carry the
  sections and gate and never say "hidden"; the fixture alone builds and passes. These tests need cmake and gtest and
  fail (not skip) when they are missing: the bench runs on this machine.

## Not in this phase

clang-tidy/MISRA-subset gates, cross-compilation, telaf-sim — those belong to a TelAF repository's own
`.agent-hub/config.toml` (D-077), not to a bench that measures the model.

## Done means

Both tasks merged through `agent-hub ready`; README documents suites; then, when the Mac is up, Stage A on the C++ suite
(`--suite cpp`, qwen3.5-9b-4bit, opencode) and a decision whether gwen takes C++ tasks (`implement-small`).
