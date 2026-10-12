# pstack trust mechanics Implementation Plan

> Executed with `agent-hub plan run` (one job per task, role = tier). Each brief points here.

**Goal:** Enforce standing orders where possible, add a finish condition / escape hatch, a decision trail in plan
mode, and verdicts bound to commits.

**Spec:** `docs/superpowers/specs/2026-10-12-pstack-trust-design.md` (D-071..D-076).

## Global Constraints

- Python ≥ 3.12 stdlib only; pytest + ruff; `just lint` and `uv run pytest -q` green at the end of every task.
- Tests never run a real agent, never touch the network or the real Mac, never write into the repo or cwd; the
  autouse fixtures in `tests/conftest.py` stay in force. No test without assertions; never weaken an existing one.
- Backwards compatible: existing pools, briefs, plan files and standing-orders files without the new syntax keep
  working (a standing-orders line without `— enforced by:` is sent as is; only `agent-hub orders` flags it).

## Review Focus

1. A standing-orders file mixing lines with and without `— enforced by:` → all lines are still sent, suffixes
   stripped, `agent-hub orders` exits 1 naming the bare lines — test in `orders`.
2. `--allow` given but the agent changes nothing → no scope violation; a deleted file outside the globs → violation —
   test in `allow-scope`.
3. A test function that asserts only via `self.assertEqual` / `pytest.raises` → not flagged as assertion-free — test in
   `assert-free`.
4. A commit made after the review → `agent-hub ready` says stale and exits 1 — test in `verdict`.
5. A repo `config.toml` with gates plus `--gate` on the command line → both run, repo gates first, duplicates once —
   test in `repo-gates`.

---

### Task `allow-scope` (implement) — single-run scope check

Files: `agenthub/cli.py`, `agenthub/orchestrate.py`; tests in `tests/test_run.py`.
- `run --allow <glob>` (repeatable, fnmatch on repo-relative paths; `**` handled like plan mode's
  `paths_allowed`). After an `ok` implement attempt, changed files (`git diff --name-only <start>` + untracked) outside
  every glob → status `scope-violation`, exit code 10, `result.json.scope_violation = [paths]`; gates still recorded.
  No `--allow` → no check (today's behaviour). Plan mode keeps its own check.
- Tests: `test_allow_scope_ok`, `test_allow_scope_violation_exit_10`, `test_allow_scope_deleted_file_counts`,
  `test_no_allow_no_scope_check`.

### Task `orders` (implement) — rules paired with enforcement; depends on `allow-scope`

Files: `agenthub/brief.py`, `agenthub/cli.py`, `docs/standing-orders.example.md`, `README.md`; tests in
`tests/test_brief.py`, `tests/test_cli.py`.
- Parse each standing-orders line `N. <rule> — enforced by: <mechanism>` (em dash or ` -- `); the text appended to
  briefs strips the suffix. `agent-hub orders [--json]` prints rule / enforced by / source for the user file then the
  repo file (`-d <dir>` for the repo, default cwd); exit 1 if any non-empty numbered line lacks the suffix, listing
  them. Mechanism names used in the example file: `gate`, `gate-guard`, `scope`, `handoff-parser`, `none`.
- Update `docs/standing-orders.example.md` to the table in spec §1 (seven rules with their mechanism).
- Tests: `test_orders_suffix_stripped_in_brief`, `test_orders_table_lists_rules_and_mechanisms`,
  `test_orders_exit_1_on_bare_line`, `test_orders_mixed_lines_all_sent`.

### Task `repo-gates` (implement-small) — repository default gates; depends on `orders`

Files: `agenthub/orchestrate.py` (or `agenthub/config.py`); tests in `tests/test_run.py`.
- `<repo root>/.agent-hub/config.toml` with `gates = ["cmd", ...]` → those gates run on every implement job of that
  repo, before any `--gate` given on the command line; a command appearing in both runs once.
- Tests: `test_repo_default_gates_run`, `test_repo_and_cli_gates_merged_once`, `test_no_repo_config_no_extra_gates`.

### Task `assert-free` (implement-small) — assertion-free test finding

Files: `agenthub/gateguard.py`; tests in `tests/test_gateguard.py`.
- For each changed `.py` test file (existing `_is_test_file`), parse the new file content with `ast`; every function
  named `test_*` that is new in this diff (its `def` line is an added line) and whose body contains no `ast.Assert`,
  no call whose dotted name ends in `raises`/`warns`/`fail`, and no call to an attribute or name starting with
  `assert` → finding `{"file", "kind": "assertion-free-test", "lines": [def line]}`. A syntax error → skip the file.
- Tests: `test_assertion_free_test_flagged`, `test_pytest_raises_counts_as_assertion`,
  `test_unittest_assert_method_counts`, `test_existing_test_not_flagged`.

### Task `escape-hatch` (implement-small) — finish condition and escape hatch

Files: `agenthub/handoff.py`, `skills/orchestrate/brief-template.md`; tests in `tests/test_handoff.py`.
- Footer gains (before "When you are done…"): `If you cannot meet the brief's checks, stop: do not weaken a test, a
  gate or the brief's conditions to get green. Report Status "blocked" and say exactly what stopped you.`
- `## Verification` accepts `inconclusive`; the template line becomes
  `unit-test-verified | type-check-only | not-verified | inconclusive`.
- `brief-template.md`: the Expected output paragraph starts with "Done means: the checks that prove the task is
  finished (exact test names, commands and their expected results)."
- Tests: `test_footer_has_escape_hatch`, `test_parse_inconclusive_verification`.

### Task `trail` (implement) — decision trail in plan mode

Files: `agenthub/planrun.py`, `agenthub/cli.py` (`plan status --trail`); tests in `tests/test_planrun.py`,
`tests/test_plan_cli.py`.
- Runner appends rows to `<plan dir>/decisions.tsv` (tab-separated, header
  `time\ttask\tphase\tdecision\treason\tevidence\tresult` written once): task start, attempt end, retry, escalation,
  autocommit, scope violation, final status. Values have tabs/newlines replaced by spaces. Only the runner writes it.
- `plan status <plan> --trail` prints the TSV as an aligned table.
- Tests: `test_trail_records_attempt_and_done`, `test_trail_records_retry_and_escalation`,
  `test_trail_header_written_once_across_resume`, `test_plan_status_trail_prints`.

### Task `verdict` (implement-hard) — verdicts bound to commits; depends on `repo-gates`, `escape-hatch`

Files: `agenthub/orchestrate.py`, `agenthub/handoff.py`, `agenthub/cli.py`, `agenthub/jobs.py` (if needed),
`skills/orchestrate/SKILL.md` (one line: run `agent-hub ready` before merging); tests in `tests/test_ready.py`,
`tests/test_handoff.py`.
- `result.json.head` = work tree `HEAD` sha at job end; each gate entry records `sha`.
- Review jobs: a review footer asks for `## Verdict` with `approve | request-changes`; parsed into
  `result.json.verdict` (null when absent).
- `agent-hub ready -d <worktree> [--json]`: finds the jobs for this work tree; ready (exit 0) only if the current
  `HEAD` has (a) an implement job whose gates all passed with `sha == HEAD` and (b) a review job with
  `verdict == "approve"`, `head == HEAD`, and `agent:model` different from (a)'s. Otherwise exit 1 with each missing
  condition (`no gated implement job on <sha>`, `review is stale (reviewed <sha>)`, `review verdict request-changes`,
  `reviewer is the implementer`).
- Tests: `test_ready_when_gated_and_approved_on_head`, `test_not_ready_review_stale_after_commit`,
  `test_not_ready_request_changes`, `test_not_ready_reviewer_is_implementer`, `test_parse_review_verdict`,
  `test_result_records_head_and_gate_sha`.

---

### Task `scoped-cancel` (implement) — a project cancels only its own jobs (D-078); after the trust plan

Files: `agenthub/orchestrate.py` (record `repo`), `agenthub/cli.py` (`cancel`, `cancel --all`, `jobs --all`,
`plan cancel`), `agenthub/planrun.py` / `agenthub/planstate.py` (runner pid + start time); tests in
`tests/test_cancel.py` (new), `tests/test_plan_cli.py`.
- `request.json`/`result.json` gain `repo` = the resolved git common dir of the work tree at job start.
- Ownership check: the caller's repository = git common dir of `-d <dir>` (default cwd); a job without `repo` (older
  record) derives it from its `workdir` if that still exists, else it is treated as foreign.
- `cancel <id>`: foreign job → exit 3, message `job <id> belongs to <repo>; run cancel from that repository`.
  Own job → today's behaviour (verify pid start time, SIGTERM).
- `cancel --all [-d <dir>]`: every running job of the caller's repository; prints one line per job; exit 0.
- `jobs` lists only the caller's repository's jobs; `jobs --all` lists all (read-only).
- `plan run` records `runner: {pid, start_time}` in the plan state; `plan cancel <plan.json>`: verifies the pid's start
  time, SIGTERMs the runner (which already cancels its running job on SIGTERM — check; add if not), and cancels the
  plan's running task jobs; state tasks running → `cancelled`.
- Tests: `test_cancel_foreign_job_refused_exit_3`, `test_cancel_own_job_from_other_worktree_same_repo`,
  `test_cancel_all_only_own_repo`, `test_jobs_default_scoped_all_flag_lists_everything`,
  `test_legacy_job_without_repo_derived_from_workdir`, `test_plan_cancel_stops_runner_and_jobs`,
  `test_plan_cancel_wrong_pid_start_time_not_signalled`.

---

### Task `touched-review` (implement-hard) — gate-touched findings judged by the reviewer (D-081)

Files: `agenthub/orchestrate.py`, `agenthub/handoff.py`, `agenthub/cli.py`, `skills/orchestrate/SKILL.md`,
`README.md`; tests in `tests/test_ready.py`, `tests/test_handoff.py`, `tests/test_run.py`.
- Remove `ready --accept-touched` and the `accepted-touched.jsonl` writer (and their tests; say so in the handoff —
  this removal is the decision, not a weakening).
- Review jobs: before running, find the newest passing gate evidence (implement or gate job) on the work tree's HEAD;
  if it has `gate_touched`, append to the review brief a section `## Gate-touched findings to judge` listing each
  finding as `<file> <kind> lines <lines>` and asking, in the final message, for
  `## Gate-touched` with one line per finding: `<file> <kind> — legitimate|weakening — <reason>`.
- `handoff.parse_touched_review(reply) -> dict[(file, kind), "legitimate"|"weakening"]`; stored in
  `result.json.touched_review` (list of `{file, kind, judgement, reason}`).
- `readiness`: when the gate evidence has findings, require an approving review on HEAD by a non-implementer whose
  `touched_review` covers every finding with `legitimate`; otherwise problems `gate touched: <n> findings not judged
  by the reviewer` or `reviewer judged <file> <kind> as weakening`.
- Skill: replace the accept-touched sentence with "gate-touched findings are judged by the reviewer, never by you".
- Tests: `test_review_brief_lists_touched_findings`, `test_parse_touched_review`,
  `test_ready_requires_reviewer_to_judge_every_finding`, `test_ready_blocked_on_weakening_judgement`,
  `test_ready_passes_when_all_legitimate`, `test_accept_touched_flag_removed`.

### Task `guard-precision` (implement) — fewer false findings, remaining review fixes; depends on `touched-review`

Files: `agenthub/gateguard.py`, `agenthub/orchestrate.py`, `agenthub/cli.py`; tests in `tests/test_gateguard.py`,
`tests/test_gate_cmd.py`, `tests/test_plan_cli.py`, `tests/test_ready.py`.
- gate-guard for `.py` test files: parse the base and new versions with `ast`; identify tests as `Class.test_name`
  (or `test_name` at module level); `assert-removed` when a test's assertion count (asserts + `pytest.raises/warns` +
  `self.assert*` calls) drops; `test-deleted` when an identity disappears; non-Python test files keep the line rules.
  A syntax error falls back to the line rules.
- `run_gate_job`: SIGTERM/SIGINT/exceptions mark the job `cancelled`/`failed`, never left `running`.
- `touched_findings`: use the newest passing gate evidence on HEAD.
- `plan cancel`: a corrupt `state.json` → exit 2 with `plan state unreadable: <path>`, and still stops a verified
  runner.
- Tests: `test_assert_replaced_by_stronger_not_flagged`, `test_assert_count_drop_flagged`,
  `test_same_name_other_class_deleted_flagged`, `test_gate_job_sigterm_marks_cancelled`,
  `test_touched_uses_newest_gate`, `test_plan_cancel_corrupt_state`.
