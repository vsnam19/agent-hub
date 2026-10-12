# Gwen Bake-off Benchmark

This directory contains the benchmark suite for evaluating coding agent harnesses against the local `gwen` model.

## What it measures

The bench measures the performance of a given `harness × model × task` combination. For each run, it records:
- **Pass rate**: Evaluated strictly using hidden tests and scoped git diffs.
- **Wall time**: Time taken to complete the task.
- **Token usage**: Prompt, completion, and cached tokens (intercepted transparently by a local proxy).
- **Memory impact**: Mac memory pressure and swap usage (sampled over SSH).

## How to run

The benchmark is run in stages to keep machine time bounded.

### Stage A (Quick Evaluation)
Runs every harness against `qwen3.5-9b-4bit` on tasks t1, t2, and t3 (18 runs total).
```bash
python -m bench.gwen.run \
  --model qwen3.5-9b-4bit \
  --task t1 --task t2 --task t3
```

### Stage B (Full Overnight Run)
Runs the best two harnesses against three models on all six tasks, with 2 repeats (72 runs total, overnight).
```bash
python -m bench.gwen.run \
  --harness <winner1> --harness <winner2> \
  --model qwen3.5-9b-4bit --model <model2> --model <model3> \
  --repeat 2
```

### Generating the Report
Produce a markdown report from the results:
```bash
python -m bench.gwen.report bench/gwen/results/*.jsonl -o docs/gwen-bakeoff-YYYY-MM-DD.md
```

## Outputs

- **Results JSONL**: Each run appends a JSON line to `bench/gwen/results/<YYYY-MM-DD>.jsonl`.
- **Run Directories**: The full workspace (including harness home, git repo, stdout, stderr, and brief) is kept under `~/.local/state/agent-hub/bench/` (or `$XDG_STATE_HOME/agent-hub/bench/`).

## Suites

The bench holds one or more **suites**, one per language. A suite is a fixture (the repository the agent works in), a
set of tasks, and a gate (the checks run after the agent stops). Each task belongs to exactly one suite.

`bench/gwen/suites.toml` defines the suites, one table per suite:
- `fixture` and `tasks`: directories relative to `bench/gwen`.
- `[<suite>.gate]`: the gate steps, in file order, as name → argv. Every step runs, even after one fails.
- `[<suite>.harness]`: values that harness argv may use (`lint_cmd`, `test_cmd`).

Gate argv placeholders:
- `{python}`: the bench's own interpreter.
- `{build_dir}`: a fresh directory in the run directory, outside the repository.
- `{visible_tests}`: as a whole argument, expands to the task's `visible_tests` (zero or more arguments).

Harness placeholders `{lint_cmd}` and `{test_cmd}` come from the task's suite (empty when the suite omits them).

Run one suite with `--suite NAME` (repeatable); an unknown suite exits with 2. Without `--suite`, every suite runs.
`--task all` (or no `--task`) means every task of the selected suites. Each result line records its `suite`.

Task ids are unique across suites: the Python suite uses `t1`..`t6`, the C++ suite `c1`..`c6`.

### The C++ suite

`--suite cpp` runs the C++ suite (needs `cmake`, `ninja`, a C++17 compiler and the system GoogleTest, no network):
```bash
python -m bench.gwen.run --suite cpp --model qwen3.5-9b-4bit
```
It mirrors the Python suite, so the two compare one language against the other on the same kinds of work:

| Id | Mirrors | Task |
|---|---|---|
| c1 | t1 | off-by-one in `paginate()` |
| c2 | t2 | add `top_words` |
| c3 | t3 | rename `load_items` → `read_items` |
| c4 | t4 | `stats --json` |
| c5 | t5 | crash (`std::stoi`) on a quoted CSV field |
| c6 | t6 | `report.hpp/.cpp`, a `report FILE` subcommand, `CMakeLists.txt` edit |

Layout: `bench/gwen/cpp/fixture/` (C++17 library `minilib`: `include/minilib/*.hpp`, `src/*.cpp`, `data/items.csv`,
`tests/test_*.cpp`, `CMakeLists.txt` with an explicit source list) and `bench/gwen/cpp/tasks/c1..c6/` (the same files as
the Python tasks, with `hidden/*.cpp`). The build uses `-Wall -Wextra -Wpedantic` with warnings as errors. The visible
tests build into one executable labelled `visible`; if `tests/hidden/` exists, its `*.cpp` build into a second one
labelled `hidden`. Tests that read `data/` get the source directory from `MINILIB_SOURCE_DIR`.

Gate steps, in order (all must exit 0), each shown with its full argv as in `bench/gwen/suites.toml`:
```
configure    cmake -S . -B {build_dir} -G Ninja -DCMAKE_BUILD_TYPE=Debug
build        cmake --build {build_dir}
visible      ctest --test-dir {build_dir} -L visible --no-tests=error --output-on-failure
build-hidden cmake --build {build_dir} --target minilib_hidden_tests
hidden       ctest --test-dir {build_dir} -L hidden --no-tests=error --output-on-failure
```
Hidden tests are
`EXCLUDE_FROM_ALL`, so only `build-hidden` builds them; `build` and `visible` never do. The agent's briefs ask it to run `cmake -S . -B build -G Ninja && cmake --build build && ctest
--test-dir build --output-on-failure`. For aider, `lint_cmd` is a build and `test_cmd` is the build plus `ctest`.

## How to add a harness

1. Edit `bench/gwen/harnesses.toml`.
2. Add a new `[harness-name]` table.
3. Configure `argv` and `env`. Available placeholders: `{model}`, `{base_url}`, `{api_key}`, `{brief_path}`, `{brief_text}`, `{home}`, `{lint_cmd}`, `{test_cmd}`.
4. If the harness requires a configuration file, specify it under the `files` key.

## How to add a task

1. Create a new directory `t<N>` under the `tasks` directory of its suite (for the Python suite, `bench/gwen/tasks/t<N>`).
2. Add `task.toml` defining the `title`, `paths_allowed`, and `visible_tests`.
3. Add `brief.md` for the agent.
4. Add `setup.patch` (applied before the run) and `solution.patch` (reference solution).
5. Add `hidden/test_*.py` (C++: `hidden/*.cpp`) for hidden assertions.

## How to add a suite

1. Add a `[<suite>]` table to `bench/gwen/suites.toml` with `fixture` and `tasks`, its `[<suite>.gate]` steps and its `[<suite>.harness]` values.
2. Create the fixture and the tasks directories it names.
