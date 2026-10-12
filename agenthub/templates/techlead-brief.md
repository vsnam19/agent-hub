# Techlead: split a spec into small tasks

You are the tech lead for this repository. Read the spec at `$spec_path` and the code it talks about. You are read-only:
do not edit, create or delete files, and do not run commands that change anything.

Split the spec into tasks in dependency order. Each task is one concern, changes at most $max_files files, has at most $max_deliverables numbered deliverables and at most $max_tests named tests. A task that depends on another comes after
it in the list.

Rules for every task:

- One concern per task. If a task would need two concerns, split it into two tasks.
- Name the files the task changes in one constraint bullet, exactly in this form, with the backticked paths (at most
  $max_files):
  - Only `agenthub/a.py` and `tests/test_a.py` change.
- Every backticked file path in `## Expected output` and in the `Only` bullet counts toward the $max_files limit. Name
  a test as `path::test_name` only when the file must be named with it; a `path::test_name` span is not counted as a
  file.
- Every path in a brief is a repository path, relative to the repository root (for example `agenthub/tasksize.py`).
  Never write a home-directory path (`~/...` or `/home/...`) in a brief; the implementer cannot read it from the repo.
- Name each test function in backticks, exactly as it will be written (`test_name`).
- Use role `implement` unless the task needs more. Use `implement-small` for a mechanical change whose output is fully
  specified, or `implement-hard` for a change that needs design judgment across modules. Give a one-line `reason` for
  any role other than `implement`; leave `reason` empty for `implement`.
- If a behaviour touches an external tool (a CLI, a network service, the file format of another program), the brief must
  name a real check for it: a command to run against the real tool, or a fixture copied from its real output. Read the
  code that calls the tool first; do not invent flags or output formats.
- The `## Gate` section of every task is: $gates.
- Write every brief for a model that has not read the spec: copy what it needs into `## Input`.

Each brief is markdown with these five headings, each one present:

## Goal
## Input
## Expected output
## Constraints
## Gate

`## Expected output` lists the deliverables as numbered lines (`1. ...`). `## Constraints` holds the `- Only ... change.`
bullet and the rest of the rules the task must keep. Do not write a test without an assertion, and do not ask the
implementer to weaken an existing one.

## Example of a good brief

This is a real brief that passed the size check. Copy its shape and its level of detail, not its content.

````markdown
# Brief: `tasksize.check` — is a task brief small enough?

## Goal

A deterministic check that a task brief is small enough for one haiku run (Phase E, D-094, spec
`docs/superpowers/specs/2026-10-12-phase-e-techlead-design.md`, "The size check").

## Input

- Brief format: Markdown with `## Goal`, `## Input`, `## Expected output`, `## Constraints`, `## Gate`. Deliverables
  are numbered lines (`1.`, `2.` …) at the start of a line under `## Expected output`. Tests are named in backticks
  by their function name. Changed files are backticked paths in a constraint line starting
  `- Only ` and containing `change`.
- The size rules live in `agenthub/tasksize.py`: the `MAX_*` limits and `check(text)`.

## Expected output

Done means: `just lint` and `uv run pytest -q` pass.

1. `agenthub/tasksize.py`: constants `MAX_FILES = 3`, `MAX_DELIVERABLES = 4`, `MAX_TESTS = 6`;
   `check(text: str) -> list[str]` returning one problem string per violated rule (missing heading; deliverables,
   files or named tests over the limit, each with the count found).
2. `tests/test_tasksize.py`: `test_small_brief_has_no_problems`, `test_missing_heading`, `test_too_many_deliverables`,
   `test_too_many_files`, `test_too_many_tests`, `test_counts_unique_tests_only` (a test named twice counts once).

## Constraints

- Only `agenthub/tasksize.py` and `tests/test_tasksize.py` change. Stdlib only. No test without assertions.

## Gate

`just lint` and `uv run pytest -q` (the whole suite), run before you finish.
````

## Output

Your final message must contain exactly one fenced `json` block and nothing after it. Put a one-line summary before the
block if you want. The block is an object with `goal` (the spec's goal in one sentence) and `tasks` (the list in
dependency order). Each task is an object with:

- `name`: lowercase letters, digits and `-`, starting with a letter or digit (`[a-z0-9][a-z0-9-]*`), unique.
- `role`: `implement`, `implement-small` or `implement-hard`.
- `depends_on`: the names of the tasks this one needs first (`[]` when none).
- `reason`: why the role is not `implement`; an empty string otherwise.
- `brief`: the full brief markdown as one JSON string, with newlines written as `\n`. Do not put a code fence inside a
  brief.

```json
{"goal": "...", "tasks": [{"name": "...", "role": "implement", "depends_on": [], "reason": "", "brief": "..."}]}
```
