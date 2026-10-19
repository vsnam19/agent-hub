# Phase G — audit log ("black box") for every agent, director included

Status: **approved (owner, 2026-10-18; scope extended 2026-10-19)**. Source brief: `phase-g-audit-prompt.md`.
Starting version: v0.11.0 (`0d5b3fe`). Target release: v0.12.0 (local, not pushed).
Decisions: owner D-105..D-110, director D-111..D-118 (`docs/decisions.md`).

## 1. Goal

Every action taken by an agent on this machine — the director's own session included — leaves one event in an
append-only, tamper-evident log that answers: **who** (role, `agent:model`, session or job), **when**, **belongs to
what** (parent ids), **what action**, **outcome**, **where the evidence is**.

Questions the log must answer, each with a read command (§7):

- what did the director do since T (`audit show --session S` / `--since T`);
- the full timeline of plan X or job Y (`audit show --plan X` / `--job Y`);
- every outward action: push, remote creation, upload, network to non-model domains (`audit outward`);
- every gap in the log (`audit show --kind gap`);
- did anyone change the log (`audit verify`).

Scope (owner, D-110): **every interactive agent session on this machine, in every project**, on all three hosts that
have an official hook mechanism — Claude Code, codex and agy — plus agent-hub's own job/plan lifecycle.

### Non-goals and anti-goals

- No enforcement: nothing in this phase blocks, slows or alters a tool call (sentry is deferred, D-109).
- Never record model thinking/reasoning, even when a host payload carries it (D-106). Never read the transcript.
- Never store file contents, command output, request bodies or auth headers — hashes, paths, sizes only (D-107).
- A logging failure must never fail or slow down work (fail-open, D-107). Failing this is a bug, not a trade-off.
- No runtime dependency (project rule): standard library only.

## 2. Event schema (JSON Lines, `schema_version: 1`)

One JSON object per line, UTF-8, keys sorted when hashed (§3).

| Field | Type | Notes |
|---|---|---|
| `schema_version` | int | `1` |
| `ts` | str | UTC ISO-8601 with milliseconds and `Z` |
| `seq` | int | position in the day file, from 1 |
| `event_id` | str | `uuid4().hex` |
| `actor` | obj | `role` (`director` \| `worker` \| `agent-hub` \| `archiver`), `host` (`claude`\|`codex`\|`agy`\|`agent-hub`), `agent` (`agent:model` when known), `model`, and `session_id` or `job_id` |
| `parent` | obj | any of `plan`, `task`, `job`, `attempt`, `director_session`, `turn`; absent keys allowed |
| `kind` | str | enum below |
| `target` | str | redacted: argv string, path, domain, ref, plan slug … (≤ 500 chars) |
| `outcome` | obj | `status` (`ok`\|`error`\|`denied`\|kind-specific), `exit_code`, `duration_ms` (each optional) |
| `quote` | str? | ≤ 200 chars after redaction, public output only (§5.3) |
| `evidence` | str? | a path or `sha256:<hex>` |
| `detail` | obj? | kind-specific fields below, all redacted |
| `prev_hash` | str | hash of the previous event in the chain (§3) |
| `hash` | str | this event's hash |

`kind` and its `detail`:

- `command`: `argv` (redacted list), `cwd`; `stderr_tail` (≤ 2 000 chars, redacted) **only** when the exit code is
  non-zero. Never full output.
- `file_read` / `file_write`: `path`, `op` (`read`\|`write`\|`edit`\|`delete`), `sha256_before`, `sha256_after`
  (each when computable by stat+hash of the path within 50 ms; else absent). Never contents.
- `net`: `domain`, `method`, `size` (bytes, when known). Never body or headers.
- `git`: `op`, `ref`, `sha`, `remote`.
- `tool`: any other host tool call (`detail.tool` = host tool name), e.g. a search or an MCP call.
- `job`, `plan`: lifecycle events agent-hub already knows (`detail.event`: `start`, `attempt`, `fallback`, `gate`,
  `review`, `escalate`, `autocommit`, `merge`, `triage`, `close`, `finish` …). Each `decisions.tsv` row is mirrored as
  a `plan` event; `decisions.tsv` stays as it is.
- `outward`: anything leaving the machine that is not a model API call: `git push`, remote creation, `gh pr create`,
  uploads, `curl`/`wget` to a non-model domain. Classified from argv by one deterministic function (§5.4).
- `session`: a host session started or stopped (Claude Code `SessionStart`/`Stop`, agy `SessionStart`/`Stop`, codex
  `SessionStart`).
- `gap`: `detail.from`, `detail.to`, `detail.reason`, `detail.lost` (count when known).
- `archive`: an archival run and its result (§8).

## 3. Integrity (D-111)

- `hash = sha256(prev_hash + canonical_json(event without "hash"))`, where `canonical_json` is
  `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)` encoded UTF-8.
- **One chain per day file, guarded by an advisory lock.** Every writer (director hooks of all hosts, the agent-hub
  runner, jobs, the archiver) appends through one function that:
  1. opens `<audit>/YYYY-MM/YYYY-MM-DD.jsonl.lock` and takes `fcntl.flock(LOCK_EX | LOCK_NB)`, retrying every 5 ms up
     to 200 ms; on timeout it gives up (fail-open) and records the miss in the pending-gap file (§3.1);
  2. under the lock, computes the day from the event's `ts` (so two writers at UTC midnight agree), reads the last
     line of that day file to get `seq` and `prev_hash` (seek from the end; no full read);
  3. if the day file does not exist, `prev_hash` is the last hash of the most recent earlier day file (local) or,
     if that month is archived, the `last_hash` of its manifest (§8); for the very first event ever, 64 zeros;
  4. appends the line with one `os.write` on an `O_APPEND` descriptor, then `fsync`s, then releases the lock.
- Why one chain per day rather than one per writer stream: a single total order makes `verify` a sequential read, a
  removed or reordered line breaks the chain at that exact `seq`, and the write rate (one line per tool call) is far
  below what a per-append lock costs (< 1 ms on a local disk). Per-stream chains need a daily manifest to recover the
  order and still cannot detect a whole stream removed.
- `verify` checks, per line: JSON parses, `schema_version` known, `seq` = previous + 1, `prev_hash` = previous `hash`,
  `hash` recomputes, `ts` not earlier than the previous `ts` by more than 2 s (clock-skew tolerance). Across days and
  archives it checks the link in step 3. It prints the first break as `<file>:<line> seq=<n>: <reason>` and exits 1.

### 3.1 Fail-open and `gap`

- Any exception inside the writer is caught; the caller never sees it and never waits more than ~250 ms in total.
- On failure the writer tries to append `{ts, reason}` to `<state>/audit/pending-gap.jsonl` (outside the chain). If
  even that fails (directory unwritable), it does nothing.
- On the next successful write, the writer first appends one `gap` event (`from` = first pending ts, or the last
  chained event's ts when the pending file is missing; `to` = now; `reason`; `lost` count) and removes the pending
  file. If the state directory was unwritable throughout, the gap's `from` is the last chained event's ts and
  `reason` is `unwritable (no pending record)`.

## 4. Storage

`<state>/audit/YYYY-MM/YYYY-MM-DD.jsonl`, where `<state>` is resolved exactly as `agenthub/config.py` does
(`$AGENT_HUB_STATE`, else `$XDG_STATE_HOME/agent-hub`, else `~/.local/state/agent-hub`). Local manifests of archived
months: `<state>/audit/manifests/YYYY-MM.json` (a copy of the vault manifest, §8). Files are created `0600`,
directories `0700`.

## 5. Sources

### 5.1 G1 — agent-hub's own events and trace context

- Every job, attempt, fallback, gate, review, escalation, autocommit, merge, plan run/review/triage/close/merge emits a
  `job` or `plan` event with parent ids, at the places that already write `result.json` / `decisions.tsv`.
- Workers get their trace context through the environment (D-115): `AGENT_HUB_TRACE_PLAN`, `AGENT_HUB_TRACE_TASK`,
  `AGENT_HUB_TRACE_JOB`, `AGENT_HUB_TRACE_ATTEMPT` (absent when not applicable), and `AGENT_HUB_TRACE_ROLE` (the pool
  role). Because the host hooks of §5.2 are installed machine-wide, a worker's own tool calls are logged by those hooks
  with these parent ids and `actor.role = "worker"` — this gives part of G3 for free. A session without
  `AGENT_HUB_TRACE_JOB` is a `director` session.

### 5.2 G2 — host hooks (observe only)

One hook entry point for all hosts: `hooks/audit-hook.py --host claude|codex|agy <event>`, run as
`python3 -I <root>/hooks/audit-hook.py …` — **not** through `bin/agent-hub` (that goes through `uv run`, hundreds of
ms per call; D-112). It imports only standard-library modules and the stdlib-only leaf modules `agenthub/audit/*` and
`agenthub/redact.py`, with `<root>` put on `sys.path` by the script itself. A test asserts the import closure.

Rules for every host: never block, never print anything that alters a tool call, always exit 0, finish in < 100 ms
typical (hard budget 2 s via `signal.alarm`, then exit 0 silently). Read the payload from stdin; never open the
transcript (`transcript_path`, `transcriptPath`). Drop any payload key named or containing `thinking`, `reasoning`,
`signature` before normalising (defence in depth: the normaliser also only picks whitelisted keys).

| Host | Registration | Events | Stdout |
|---|---|---|---|
| Claude Code | plugin `hooks/hooks.json` (next to the existing SessionStart/Stop entries), `matcher` `*`, timeout 5 s | `PostToolUse`, `PostToolUseFailure` (Claude Code fires PostToolUse only on success), `SessionStart`, `Stop` | nothing |
| codex 0.152.1 | `~/.codex/hooks.json` via `hub install` (needs `[features] hooks = true`, already set) | `PostToolUse`, `SessionStart` | nothing |
| agy 1.4.2 | `~/.gemini/config/hooks.json` via `hub install`, named group `"agent-hub"`, `matcher` `*` | `PostToolUse`, `SessionStart`, `Stop` | exactly `{}` |

No `PreToolUse` on any host (D-113): the post event carries the input and the outcome, so one write per tool call.
The cost: a tool call that crashes the host mid-call leaves no event (a host crash is visible as a session with no
`Stop`).

Host payload facts (each normaliser brief must copy a **real captured payload** as a test fixture, not invent one):

- Claude Code: `session_id`, `cwd`, `hook_event_name`, `tool_name`, `tool_input` (Bash: `command`, `description`),
  `tool_response`; failure event carries the error.
- agy (`~/.gemini/antigravity-cli/builtin/skills/agy-customizations/docs/hooks.md`): camelCase keys —
  `toolCall{name,args}`, `error` (present on failure), `conversationId`, `modelName`; no tool response; no
  `description` argument; `transcriptPath` must be ignored; a group with `"enabled": false` is the owner's choice.
- codex: event names `PreToolUse`/`PostToolUse`/`SessionStart`/`UserPromptSubmit`/`PermissionRequest` exist in the
  0.152.1 binary; **payload keys and which tools fire are not yet verified**. The codex normaliser task starts by
  capturing a real payload (§10.4) and is planned after the claude/agy normalisers.

### 5.3 Quote source (D-106)

At most one quote per event, ≤ 200 chars after redaction, cut at a character boundary with a trailing `…`:

- Claude Code / codex tool events: the tool call's own `description` argument when present; otherwise none.
- agy tool events: none.
- agent-hub events: a handoff `Status`/`Summary` line, a review verdict or finding title, a gate summary line.

### 5.4 Normalising a tool call into a kind

One deterministic function `classify(host, tool_name, tool_input) -> (kind, target, detail)`:

- shell tools (Claude `Bash`, agy `run_command`, codex shell/exec) → `command`; argv split with `shlex`; then
  `git push|remote add|remote set-url`, `gh pr create|repo create|release create`, `scp`, `rsync` to a host,
  `curl`/`wget` to a domain not in the model-API allowlist (`api.anthropic.com`, `*.googleapis.com`,
  `api.openai.com`, `chatgpt.com`, `localhost`) → also `outward` (a second event with the same parent);
  other `git …` → `git`.
- read/write/edit tools → `file_read` / `file_write` with path and hashes.
- web fetch/search tools → `net` with domain (fetch to a non-allowlisted domain is also `outward`).
- anything else → `tool`.

### 5.5 Later (follow-ups, not this phase)

- **G3:** normalise worker hosts' streamed tool events (claude stream-json, codex JSON events, opencode, agy) through
  the adapters for workers whose host hooks do not fire in headless mode, one host per task.
- **G4:** daily digest to the owner.

## 6. Redaction (D-112)

`skillmine.redact` moves to a new stdlib-only leaf module `agenthub/redact.py` (`redact(text) -> str`, plus
`redact_argv(list[str]) -> list[str]`, which also redacts the value after `--token`, `--password`, `-p` style flags
and `KEY=value` env prefixes); `skillmine` imports it so its tests keep passing unchanged. The writer applies it to
`target`, `quote`, `detail.argv`, `detail.stderr_tail` and every string in `detail` **before** hashing and writing.

## 7. Read side

- `agent-hub audit show [--plan P | --job J | --session S | --since T] [--kind K]... [--json]`: timeline, one line
  per event (`ts actor kind target outcome quote`), oldest first; reads local day files and, transparently, archived
  months from the vault (§8) when the range reaches them and the vault is reachable (else prints which months were
  skipped).
- `agent-hub audit verify [--month YYYY-MM]`: §3; default all local months plus the links to archive manifests; with
  `--month` of an archived month, verifies the archive content itself (vault required).
- `agent-hub audit outward [--since T]`: `outward` events only.
- MCP, read-only only (`readOnlyHint=True`): `audit_status` (current day file, last seq/hash, pending gap, last
  archive), `audit_show` (same filters as the CLI, bounded to 500 events), `audit_archive_status`,
  `audit_archive_rerun(month)` (takes a month string only, never a path; runs §8 for that month). No write or delete
  tool.

## 8. Monthly archival (D-108, D-117)

`agent-hub audit archive [--month YYYY-MM]` (default: previous calendar month; the current month is refused):

1. Load the vault path from `~/.config/agent-hub/audit.toml` (`vault = "…"`, honour `$AGENT_HUB_CONFIG`). No other
   source: no flag, no environment variable, no MCP argument.
2. If `<vault>/agent-hub-audit/YYYY-MM.tar.xz` and its manifest exist and the manifest's sha256 matches the file →
   "already archived", exit 0, write an `archive` event `status=noop`.
3. Take every day lock of the month in turn (a writer for that month would be a clock bug, but stay safe), run
   `verify --month` on the local files; a break → exit 1, keep everything.
4. Build `YYYY-MM.tar.xz` (stdlib `tarfile`, `w:xz`) in `<state>/audit/tmp/`, compute sha256.
5. Copy to `<vault>/agent-hub-audit/YYYY-MM.tar.xz.part`, `fsync`, rename; read the copy back and compare sha256.
6. Write the manifest `<vault>/agent-hub-audit/YYYY-MM.json`: `month`, `files`, `count`, `first_hash`,
   `first_prev_hash`, `last_hash`, `sha256`, `created`; copy it to `<state>/audit/manifests/YYYY-MM.json`.
7. **Only then** delete the local month directory, and write an `archive` event `status=ok`.

Any failure (config missing, vault not mounted/not a directory, not writable, checksum mismatch, disk full) → keep
every local file, remove only our own `.part`/tmp files, exit non-zero, write an `archive` event with `status=error`
and the reason. Never delete without a verified copy.

`agent-hub audit schedule [--apply] [--remove] [--at "<calendar>"]`, same pattern and helpers as `skills schedule`
(`agenthub/cli.py`, `_schedule_run`): print the units by default; `--apply` writes and enables
`agent-hub-audit-archive.{service,timer}`; `--remove` disables and deletes only those two units. Default calendar
`Sun *-*-01..07 21:17:00` (first Sunday of the month, one hour after `skills weekly` at `Sun *-*-* 20:17`;
checked with `systemd-analyze calendar`; next elapse 2026-11-01 21:17 local). `--at` is validated with
`systemd-analyze calendar` before writing. The director never runs `--apply` (owner step).

## 9. Out of scope (recorded in `docs/follow-ups.md` with triggers)

G3, G4, scorecard baseline, sentry, credential broker, spec-review gate (D-109, triggers in the follow-ups).

## 10. Testing

Fake agents and fake payloads only; never a real agent or real network in tests. Required tests, each owned by a task:

1. chain verification passes on a clean log and fails at the exact tampered / removed / reordered event (`seq` named);
2. concurrent writers (≥ 4 **processes**, `multiprocessing`) keep a valid chain with no lost or duplicated `seq`;
3. redaction removes secrets from argv, quotes and stderr tails before hashing (assert the raw secret is absent from
   the file bytes);
4. a quote is truncated to 200 chars; a payload containing `thinking`/`reasoning` blocks (and a `transcript_path`
   pointing to a file with secrets) produces an event without any of that content;
5. the hook exits 0 with no stdout (agy: `{}`) when the audit directory is unwritable, and a `gap` event appears on the
   next write once it is writable again;
6. archive: success; vault missing; vault unwritable; checksum mismatch (patched copy); re-run is a no-op; local data
   never deleted on any failure;
7. `audit verify` spans an archived month (local manifest link);
8. `audit schedule` prints without changing anything; `--apply`/`--remove` touch only their own units (mock systemctl
   as the `skills schedule` tests do);
9. `hub install` hook registration keeps third-party entries (`herdr`) byte-identical in meaning, is idempotent,
   appends to codex's list (never reorders: codex keys trust by index), and never writes `trusted_hash`.

### 10.4 Real check before closing (principle-prove-it-works)

- Capture real payloads for fixtures: Claude Code from this session's hook, agy and codex from a temporary
  `HOME`-scoped config with a hook that only dumps stdin (codex with `--dangerously-bypass-hook-trust` in that temp
  config only).
- Run one small real job and one plan task; then `agent-hub audit show --plan …`, `audit show --session <director>`
  and `audit verify` on the real state directory (exit 0).
- `audit archive` against a temporary vault (`AGENT_HUB_CONFIG` pointing at a temp `audit.toml`) for a synthetic past
  month in a temporary state dir; verify it; then a failure case (unwritable vault) that keeps local data.
- Never the owner's real vault; never `audit schedule --apply`.

## 11. Owner steps after release

1. Restart Claude Code (plugin hooks load). 2. `agent-hub hub install --apply` (codex + agy hook entries).
3. Trust the codex hook (codex prompts on next start, or `/hooks`). 4. Write `~/.config/agent-hub/audit.toml` with the
vault path. 5. `agent-hub audit schedule --apply`.

## 12. Suggested split (the techlead decides the final split)

writer + chain + lock + gap (`agenthub/audit/writer.py`) → redaction leaf (`agenthub/redact.py`) → classify →
agent-hub job events + trace env → plan events + `decisions.tsv` mirror → verify → show/outward → claude+agy
normalisers → hook entry point + plugin `hooks.json` → codex normaliser (after payload capture) → `hub install`
hook registration (after the pending `agy plugin list` parser task in `agenthub/hub.py`) → archive → schedule → CLI
wiring → MCP read-only tools → README / overview / orchestrate skill. Keep each `audit` command in its own
`agenthub/audit/*.py`; serialise tasks that edit `agenthub/planrun.py`, `agenthub/cli.py`, `agenthub/hub.py`,
`agenthub/mcpserver.py` or `hooks/hooks.json`.

## 13. Corrections from the techlead review (job `20261018-235854-techlead-0689`, D-120)

These override the sections above where they disagree.

1. §3.1: with no pending record nothing is known to be lost, so no `gap` event is written in that case; the
   pending-gap file lives **outside** the audit directory, at `<state>/audit-pending-gap.jsonl`.
2. §2 `file_write`: only `sha256_after` (post events only, D-113); `sha256_before` is absent.
3. §7: `audit_archive_rerun` writes and deletes, so it has `readOnlyHint=False`; it is still the only non-read tool
   and still takes a month only.
4. §6: `-p` values are redacted only for `mysql`, `mysqldump`, `mysqladmin`, `sshpass` (else `mkdir -p dir` breaks).
5. §5.2: the hook's import closure may include `agenthub/config.py` (stdlib-only) for the state directory.
6. §5.1: the plan runner sets `AGENT_HUB_TRACE_PLAN`/`TASK` around its `spawn` call in `agenthub/planrun.py`;
   `run_job` sets `JOB`, `ROLE`, `ATTEMPT` (link number) per attempt. `actor` gains `pool_role`.
7. §5.4: `classify` returns a list of events; it scans every `&&`, `;`, `|` segment (`cd x && git push`).
8. §5.2: agy `SessionStart`/`Stop` are flat handler lists without `matcher`; only `PostToolUse` is grouped. agy tool
   names other than `run_command` are undocumented and map to kind `tool`.
9. Emitters take the state directory from the caller (`Paths`), never from the global, so tests stay isolated.
10. The codex payload fixture is captured in its own task before the codex normaliser.
