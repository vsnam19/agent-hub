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

## How to add a harness

1. Edit `bench/gwen/harnesses.toml`.
2. Add a new `[harness-name]` table.
3. Configure `argv` and `env`. Available placeholders: `{model}`, `{base_url}`, `{api_key}`, `{brief_path}`, `{brief_text}`, `{home}`.
4. If the harness requires a configuration file, specify it under the `files` key.

## How to add a task

1. Create a new directory `bench/gwen/tasks/t<N>`.
2. Add `task.toml` defining the `title`, `paths_allowed`, and `visible_tests`.
3. Add `brief.md` for the agent.
4. Add `setup.patch` (applied before the run) and `solution.patch` (reference solution).
5. Add `hidden/test_*.py` for hidden assertions.
