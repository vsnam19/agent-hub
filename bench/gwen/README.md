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

Task ids are unique across suites: the Python suite uses `t1`..`t6`.

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
5. Add `hidden/test_*.py` for hidden assertions.

## How to add a suite

1. Add a `[<suite>]` table to `bench/gwen/suites.toml` with `fixture` and `tasks`, its `[<suite>.gate]` steps and its `[<suite>.harness]` values.
2. Create the fixture and the tasks directories it names.
