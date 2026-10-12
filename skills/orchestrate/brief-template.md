# Brief: <short title>

## Goal

One or two sentences: the outcome wanted and why, with no implementation detail.

## Input

Name the files to read, the current behaviour, and the interfaces the change must respect.

## Expected output

Done means: the checks that prove the task is finished (exact test names, commands and their expected results).

Name the files to create or change, the exact test names that must pass, and the exact commands with their expected result.

## Constraints

State what must not change, the style rules, and anything forbidden (for example no new dependencies, no tests without assertions).

## Rationalizations to refuse

Optional. The rows below are examples only: replace them with the excuses this task invites and the answer to each, or delete this section when none apply (for example, a docs-only task has no failing test to write).

| Excuse | Answer |
| --- | --- |
| I know the bug, I'll just fix it | Reproduce it first with a failing test |
| The test is flaky, skip it | Find the cause; never add a skip to get green |
| It's a small change, no test needed | Every behaviour change gets a test that fails without it |
| The gate is too strict | Stop and report blocked; never weaken a check |

## Gate

List the commands the agent must run before finishing: the full lint, the whole test suite, and the tests of every touched file.
