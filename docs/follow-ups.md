# Follow-ups

Open items found by reviews and runs, not yet scheduled. Audited against master on 2026-10-18: struck-through items are fixed. Small, well-specified ones suit `implement-small` (gwen) on a
small repository, or `implement` here (D-079).

| Found | Item | Source |
|---|---|---|
| 2026-10-12 | C: C++ fixture for the gwen bench (B done in v0.9.0, spec `2026-10-12-phase-b-skill-hub-design.md`) | D-077 |
| 2026-10-12 | Owner's call: agy has superpowers imported (2026-09-26): a headless `agy -p` run lists all 15 superpowers skills in its index (e.g. brainstorming: "You MUST use this before any creative work"), verified 2026-10-12; whether its SessionStart hook also runs headless is unverified; the `worker` plugin is now installed in agy (2026-10-12), so agy workers see both `test-first`/`debug-playbook`/`verify-before-done` and superpowers' TDD/debugging/verification skills — consider `agy plugin disable superpowers` | D-087 |
| 2026-10-12 | Repeat the skill checks that could not run: a live `codex exec` with an installed plugin (quota back 2026-11-07) and a gwen run with `skills.paths` (Mac host was down) | D-083 |
| 2026-10-12 | Owner's call: hand-copied `~/.codex/skills/telaf-api` and `telaf-legato` duplicate the installed `telaf` plugin; `agent-hub hub status` reports them as `stray` and never deletes them | D-089 |
| 2026-10-12 | Phase C: a live `--suite cpp` Stage A run (opencode × qwen3.5-9b-4bit) when the Mac host answers, then decide whether gwen takes C++ tasks (D-077, D-090) | D-090 |
| 2026-10-12 | Phase E E5 must also: sort tasks topologically before writing, treat `"reason": null` as empty, and reject a task whose brief fails `tasksize.check` headings (from review ff04) | review ff04 |
| 2026-10-13 | C++ bench: c4 (1782 s) and c6 (3519 s) report wall times above the 1200 s timeout with `timed_out: false`; the log has a 41-min gap (host likely suspended). Check `run_one`'s wall/timeout accounting across a suspend (monotonic clock), then repeat Stage A with `--repeat 2` | D-095 |
| 2026-10-13 | **Done 2026-10-19 (plan `followups-1`, D-103).** A network outage (`API Error: Can't reach the API server … EAI_AGAIN`) is classified `failed` (jobs 20261012-181839-implement-1cfb, 20261012-182120-review-b33c); it should be a transient outcome that falls through to the next link (or retries) like quota, never consume a review round. Small task: claude adapter classify + test | jobs 1cfb, b33c |
| 2026-10-13 | **Done 2026-10-19 (plan `followups-1`, D-103).** agy 1.3.4 no longer offers `gemini-3.1-pro-high` (`agy models`: gemini-3.8-flash-high/medium/low, claude-sonnet-4-6, claude-opus-4-6-thinking, gpt-oss-120b-medium); attempts fail as `model-unavailable`. The user pool override now uses `agy:gemini-3.8-flash-high`; `agenthub/default_pool.toml` and agy `profile.md` still name the old id — small task to update them | job 20261013-142316-implement-94bc |
| 2026-10-13 | First real `skills weekly` (report `~/.local/state/agent-hub/skills/2026-10-13/report.md`, branch `learned/2026-10-13` kept in agent-plugins): stopped correctly at eval (gate-before-review: with 1.00 = without 1.00). Small tasks (1, 3, 4 done 2026-10-19 in `followups-1`; 2 and 5 open): (1) report lists bumped versions under "Released" although nothing was released → "bumped (not released)"; (2) 189 unparsed findings — parse `- **medium**: \`file:line\` …` and similar formats; (3) drop the 1× categories from metrics, show groups; (4) drafting brief: each skill's positive eval case must be one the no-plugin baseline fails (state it in the case), and eval every drafted skill before deciding; (5) MCP proposal `ast-guard` for the owner | weekly 2026-10-13 |
| 2026-10-13 | **Done 2026-10-19 (plan `followups-1`, D-103).** `agent-hub run --role review -d <wt>` does not skip links that implemented the work tree (only plan mode passes `--exclude`); `ready` catches it afterwards ("reviewer is the implementer"), wasting a review. Small task: a review job excludes every implementer of the work tree by default (`agent:model`, effort ignored) | plan merge on plan-close-fix2 |
| 2026-10-14 | **Done 2026-10-19 (plan `followups-1`, D-103).** Phase F lows (review on `orch/phasef-fix10/gate-pid`, approve): (1) `run_gate_job`/`run_job` announce before `with _Cancel()` installs SIGTERM handlers — a cancel in that gap kills without `_finish`; (2) `_finish` read-check-write of `result.json` is not atomic against a fallback cancel write; (3) `tests/test_gate_cmd.py` cancel test sends SIGTERM to the pytest process itself and reads capsys from a thread — flaky risk, rewrite with a child process. Small tasks for the techlead loop (E8) | Phase F review |
| 2026-10-14 | Phase F F4/F5 lows (review on `orch/phasef2-fix3/mcp-manifest`, approve): ~~`plan_review` parent "running" write races the child's `--result` write; `plan_review` child not reaped; `plan_wait` does not detect a dead runner~~ (fixed: plan `deadlock`, E8 `_spawn_result_cli` writes the info file with `open(..., "x")`); done 2026-10-19 (`followups-1`): `plan_merge` with no done tasks returns the same shape as "nothing left" while the CLI exits 1. Small tasks for E8 | Phase F review |
- **Done 2026-10-19 (plan `followups-1`, D-103).** **MCP serverInfo version is empty** (2026-10-14): `initialize` returns `{"name": "agent-hub", "version": ""}`; pass the package version to `MCPServer`. Low.
- **Done 2026-10-19 (plan `followups-1`, D-103).** **tasksize counts dotted module names as files** (2026-10-14): a brief line "Stdlib plus `agenthub.triage` and `agenthub.planstate`" under Expected output counted 4 files for a 2-file task (E8 draft, `triage-apply`). Count only backticked spans with a `/` or a file extension. Low.
- **Done 2026-10-19 (plan `followups-1`, D-103).** **gateguard false positives** (2026-10-14, E8 review): (1) `skip-added` fires on the formatter comment `# fmt: skip`
  (`gateguard.py:442` regex `\bskip\b`) — ignore `# fmt: skip`/`# fmt: off` comments; (2) `assert-removed` fired on
  `test_server_lists_tools` in `tests/test_mcpserver.py` when the task only added two names to a `set(...) == {…}`
  literal — `_weakened` should not treat a grown expected-literal as weakening. Low; both were read and judged benign.
- ~~**`plan_review` unreaped child hangs the director for hours — HIGH**~~ **Fixed 2026-10-14 (plan `deadlock`).** (2026-10-14): the MCP server starts
  `agenthub plan review` without a wait thread, so the finished runner stays a zombie until the server exits. A director
  waiting with `while kill -0 <pid>` (zombies answer `kill -0`) never wakes. Seen at least 3 times: E8 review finished
  13:20, session idle until /exit at 21:12 (session 4040f7e7). Fix: reap the child (wait thread as in `plan_run`) and
  write the final `--result` from the child only; directors wait on the result file's `status`, not on the pid.

## Director deadlock avoidance (2026-10-14)

A director that ends its turn while waiting on something that never signals is idle until the user notices (lost ~8 h,
three times, at E8 `plan review`). Rule: **every wait must end on success, failure, and a dead/zombie/stalled
process**, and the director keeps a harness-tracked waiter (a `run_in_background` command that exits) before it ends
a turn. Items:

- **Fixed 2026-10-14 (plan `deadlock`, merged):** (1) `plan_review` reaps its child and marks a runner that died without a result
  `failed`; `plan_review_wait` treats zombies as dead (`procinfo.is_running`). (2) `plan_wait` returns `stalled` when
  the recorded runner (`runner.pid` + start time) is gone while tasks are still `pending`/`running`.
- **Done 2026-10-19 (plan `followups-1`, D-103).** **`plan review` ignores SIGTERM/SIGINT:** the per-job signal handler (`orchestrate.py` ~231) stops the current
  review, and the review loop moves on to the next task; `agent-hub cancel` and `kill -INT` did not stop it, only
  SIGKILL did. The review loop should stop after a signal. Medium.
- **Done 2026-10-19 (plan `followups-1`, D-103).** **Wait-tool timeout vs the Claude Code MCP client:** `plan_review_wait`/`plan_wait` accept up to 300 s, but Claude
  Code moves an MCP call to the background after ~120 s and its later notification still says `running`. Cap the
  default below 120 s and document "call again" in the tool description. Low.
- **Done 2026-10-19 (plan `followups-1`, D-103).** **`wait_job` / `agent-hub wait` never detect a dead job (verified):** both loop on `result.json` `status ==
  "running"` with no liveness check (`mcpserver.py` `wait_job`, `cli.py` `_cmd_wait`); a SIGKILLed job waits forever.
  Fixed 2026-10-14 (`fix/wait-job-dead`). Review lows: `agent-hub wait --json` prints the dead-job reason as plain
  text (like the existing timeout line); the dead-job result is built in three copies (sync/async MCP, CLI) — share
  one helper.
- **Orchestrate skill:** state the rule above in the Non-negotiables ("never wait on `kill -0 <pid>`; wait on the
  result file or the wait tool, and cover dead processes"). The skill lives in `agent-plugins`.
- **Resume notes:** a resume note must name the last review/plan result file, so a new session reads finished
  results instead of re-running (E8 review was re-run although its result had been on disk since 13:20).
- ~~**agy OAuth expiry is classified `failed`, not auth**~~ **Fixed 2026-10-14 (`fix/agy-ssh-auth`, `agents/agy` maps the markers to AUTH).** (2026-10-14): with an expired agy login every run prints an
  OAuth URL, `Waiting for authentication (timeout 60s)...`, `Error: authentication timed out.` and is recorded as
  `failed` — so plan tasks retry twice and escalate, and reviews end `failed` (3 E8 reviews, every `implement`
  attempt of plan `deadlock`). Map "authentication failed or timed out" to the auth outcome (cooldown the agy links,
  fall through the chain, no escalation). **Root cause (verified):** under SSH (`SSH_CONNECTION`/`SSH_TTY` set) agy
  uses empty file-based token storage instead of the keyring; blanking those vars makes `agy -p` work. Fix task
  `agy-ssh-auth` (adapter blanks them + auth markers). No re-login needed.
- ~~**Race in stalled detection**~~ **Fixed (plan `deadlock`, `plan-run-claims`; E8: `plan_run` refuses while a verified runner lives).** MCP `plan_run` returned before the new runner
  wrote `runner.pid`, so `plan_wait` on a resumed plan saw the previous runner's dead pid → false `stalled`. Fix
  task `plan-run-claims` writes `runner.pid` in `plan_run` right after spawn.
- **Done 2026-10-19 (plan `followups-1`, D-103).** **agy keyring override lows (review `20261014-154835-review-85cf`, approve):** SSH vars are blanked, not removed
  (`Invocation.env` cannot unset; works with agy 1.3.5's truthiness check — breaks if agy switches to a presence
  check); `_secret_service_running` is cached per process (a keyring started later is missed until restart); the
  existing `run_fake` agy tests call the real `dbus-send` when the test run itself is under SSH — patch the probe in
  an autouse fixture. Low.
- **Done 2026-10-19 (plan `followups-1`, D-103).** **Per-branch fixes on stacked plan branches need a cascade** (2026-10-18, E8): fixing review findings on the branch
  that introduced the code leaves descendants with the old code; their reviews (base `master` for multi-dependency
  tasks) re-raise the ancestors' findings, and `plan merge` hits conflicts (E8: `agenthub/triage.py` touched by 4
  stacked branches, `tests/test_mcpserver.py` changed on master meanwhile). The director merged each fixed branch down
  the DAG with a per-definition 3-way merge script, then re-reviewed every changed head (≈60 review jobs). `plan close`
  should do this itself: after a fix round, merge the task's branch into its `done` dependents and re-review them
  (today it only flags them, `CloseResult.stale`). Medium.
- **Done 2026-10-19 (plan `followups-1`, D-103).** **`plan triage`/`plan close` count merged tasks as skipped** (2026-10-18): after `plan merge` removes the work trees,
  `agent-hub plan triage <e8 plan>` prints `skipped 16 task(s)` and exits 1. Tasks whose branch is already merged into
  the base (or deleted) should be left out silently. Low.
- **Done 2026-10-19 (plan `followups-1`, D-103).** **`plan review` lets the fix author approve** (2026-10-18): `review_task` excludes only the task's first implementer;
  after a fix round by another model (agy Gemini here) the review chain picked that same model, and `ready` refused
  (`reviewer is the implementer`). Exclude every model that implemented a commit on the branch. Medium.
- **Done 2026-10-19 (plan `followups-1`, D-103).** **`test_child_does_not_import_cwd_agenthub` flakes under parallel gates** (2026-10-15): with 4–6 `uv run pytest`
  gates at once it failed once with `git add` → `index.lock: File exists` in its own temp repo (job
  20261014-200442-implement-5046); passes alone and on rerun. Low.
- **Gemini handoff claims** (2026-10-18): job 20261014-232223-implement-c3e1 reported "backwards-compatible aliases"
  for renamed tests that did not exist. Handoffs remain unverified claims; the director's diff read caught it. Info.

## From plan `followups-1` (2026-10-19)

- **Done 2026-10-19 (plan `f6`, D-104).** **Gate-touched judgement must match the finding group exactly:** `orchestrate._touched_problems` keys a judgement by
  (file, kind, all lines); a reviewer who splits one group (`lines 235,…,1410`) into two lines leaves it "not judged"
  and `ready` refuses (wait-timeout-cap needed a targeted re-review). Accept legitimate entries whose lines together
  cover the group. Low.
- **Real cancel gap in gates:** a SIGTERM after `process.run` creates the gate's stdout file but before `on_start`
  records the process group only sets `cancelled`; the gate command runs on until it ends or `GATE_TIMEOUT_S`
  (review 20261018-154041-review-09d2). Low.
- **README still says the MCP wait tools cap at 300 s** (now 100 s, `MAX_WAIT_S`). Low, docs.
- **Quote `${CLAUDE_PLUGIN_ROOT}` in `hooks/hooks.json`** (`claude plugin validate` warns: a path with a space splits).
  Low.
- **Network-transient only for the Claude adapter:** agy/codex DNS or connection failures still end `failed`; the 3-minute
  transient cooldown is hard-coded (review 20261018-145747-review-a609). Low.
- **Review loops on edge cases:** gateguard took 4 rounds and review-excludes 3 because each review found the inverse
  of the last fix; the director ended both by narrowing the rule (D-103). When a round re-raises the opposite trade-off,
  decide and record a rejection instead of another round.
- **Agent tests that spawn real agents:** a `plan close` test written by Gemini ran a real `plan run` and hung the job
  30 min; the brief now forbids it, but a conftest guard (fail when a test execs `agy`/`claude`/`codex`) would catch it.
  Medium.
- **agent-plugins repo:** add to `skills/orchestrate/SKILL.md` Non-negotiables "never wait on `kill -0 <pid>`; wait on
  the result file or a wait tool that also ends on a dead/zombie process" (still open).
- **F6 lows (review of `orch/f6/mcp-register`, approve):** a stale codex entry is replaced by `mcp remove` then `mcp add`;
  if the add fails the server is gone until the next `hub install`. A non-string codex `transport.command` would make
  `hub status` raise. Low.
- **uv-tool `agent-hub` lacks the `mcp` extra:** `agent-hub mcp` on PATH fails ("mcp SDK not installed"); reinstall
  with `uv tool install -e '.[mcp]'` at release, or keep registering `bin/agent-hub`. Low.
- **agy 1.4.2 changed `agy plugin list` to text** (`Installed plugins:` / `- name (enabled, 1.0.0)` / `Source:` …;
  no JSON `imports`): `hub status` reports `agy - error: agy list output not understood` and every agy plugin as
  `missing`, so each `hub install` reinstalls them (harmless). Parse the text form (keep the JSON form). Seen at the
  v0.11.0 `hub install --apply` (2026-10-19), where the plugins were in fact installed. Small task, next.

## Deferred from Phase G (2026-10-19, D-109)

- **G3 — worker host events through the adapters** (claude stream-json, codex JSON events, opencode, agy), one host
  per task, for workers whose host hooks do not fire in headless mode. Trigger: Phase G real check shows worker tool
  calls missing from `audit show --job`.
- **G4 — daily audit digest to the owner.** Trigger: after G has run a week on real work.
- **Scorecard baseline** — next phase after G (D-105), built on the audit events.
- **Sentry (command/egress enforcement) and credential broker.** Revisit when any of these happens: unattended
  overnight runs become routine; agents read untrusted content (web pages, external issues) regularly; the repo gets
  a GitHub remote and pushes become frequent; the audit log shows a dangerous action actually happened. Direction
  (D-109): no agent above policy, owner signs every policy change, broker is a local deterministic process (not MCP),
  owner approves the request hash.
- **Spec-review gate for the techlead.** Revisit after the scorecard baseline exists, so its effect can be measured
  (fix rounds and spec-caused `blocked` tasks with vs without the gate).

## Single source of truth for model ids (owner, 2026-10-19, D-121) — next plan after Phase G

agy 1.4.2 removed `claude-*-4-6`; nothing noticed (cooldowns hid it) and the Phase G task `pool-agy-model-ids` blocked
because three test files hard-code default-pool ids. Fix: (1) each agent's model list comes from its host CLI
(`agy models`, …) cached in state; `agent-hub pool` marks `unknown-model` links and chains skip them at once;
(2) tests read expected chains from `default_pool.toml` via `load_pool()` and use fake ids (`fake:model-a`) for
arbitrary links; a test fails on real `agy:`/`codex:`/`claude:` literals in `tests/`; (3) the owner pool lists only
overrides.

## Incident log for `skills weekly` (owner, 2026-10-19, D-122) — with the D-121 plan

Every process failure (director, techlead or tool cause — not ordinary review findings) is one line in
`<state>/incidents.jsonl`: `ts`, `kind` (`brief-scope`, `stale-model-id`, `spec-issue`, `process-skip`, `wait-hang`,
`quota`, `tool-break`, …), `source`, `plan`/`task`/`job`, `cost` (attempts, rounds, minutes), `what`, `lesson`.
Started by hand on 2026-10-19 (4 entries). To build: `agent-hub incident add …` (deterministic, also an audit event
of kind `incident`); `skillmine.mine` reads it; the weekly report shows counts per kind per week and the top cost,
and drafts a `learned-director` lesson when one kind repeats ≥ 3 times; the orchestrate skill tells the director to
log one whenever a task blocks or fails for a reason that is not the implementer's.

## From plan `phase-g` (2026-10-19)

- **Plan runner ignores a `blocked` handoff:** `audit-codex-fixture` ended `done` (gate passed, nothing committed)
  while its handoff said `Status: blocked`. A blocked handoff should make the task `blocked`.
- **Real codex `PostToolUse` payload fixture:** not captured (temp `CODEX_HOME` → 401; codex monthly limit until
  2026-11-06). The codex normaliser uses the schema embedded in the codex 0.152.1 binary. Capture a real payload when
  quota returns and replace/extend the fixture.

## From plan phase-g-hardening (accepted limitations, 2026-10-19)
- **Signal windows over-count loss.** A SIGALRM landing between `os.write` and the "written" flag (writer) or between
  `write(ev)` returning and the loop index advancing (hook) records an already-written event as pending, so a later
  gap reports it as lost. Errs on the safe side (never hides a loss). Revisit if gaps with `lost` show up in real logs
  next to the same `event_id`.
- **`audit verify --month M` for an archived month with no vault configured** exits 0 with "not local and was not
  checked". Chosen so verify works on machines without the vault; a script that needs a real check must pass
  `--vault`/configure one. Revisit with G4 (digest).
- **Pending-line race.** A `record_pending` that opened the pending file before another process claimed it and writes
  after that process's last re-read of the claim lands in an unlinked inode and is lost uncounted. Window is a few
  microseconds between two processes' syscalls; fixing needs `record_pending` under the chain lock (costs the fail-open
  path its lock-free property). Revisit if gaps ever disagree with host-side counts.
- Final review covered `4562504...HEAD` (hardening and its fix rounds); the 34 Phase G branches were each reviewed once
  in plan `phase-g` (D-124).

## From the Phase G final review (2026-10-19, review job 20261019-225727-review-b4ea, verdict approve)
- **Unreadable claim file can vanish** when the gap append fails before the gap line is written: only readable claims
  are restored, `_release_claims` unlinks all (writer `_claim_pending_and_emit_gap`).
- **Target keeps a quoted secret** when the same value also appears plain elsewhere in the command (`user:'pass'`);
  the fallback check only asks whether each changed token matches somewhere (writer `_redact_target`).
- **curl `-E/--cert <file>:<password>`** is not redacted (only the `-u` family is).
- **Plan id in audit events is the plan file's directory name** (`plan.path.parent.name` in `planrun.py`,
  `planclose.py`, `triagerun.py`), not `plan.slug`; equal only under the standard `plans/<slug>/plan.json` layout.
- **Real codex `PostToolUse` fixture** still missing (codex quota until 2026-11-06).
