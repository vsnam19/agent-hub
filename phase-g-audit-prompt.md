# Phase G — Audit log ("black box") for every agent, director included

You are the director for `~/Workspaces/tools/agent-hub`. Follow the `agent-hub:orchestrate` skill for the whole phase.
Talk to the owner (Nam) in Vietnamese; write every artifact (specs, decisions, briefs, commits, docs) in English.

## 0. Preconditions — check before anything else

1. Read `docs/next-phase.md` (newest update block first), `docs/overview.md`, `docs/decisions.md`, `docs/follow-ups.md`.
2. Phase G starts only after **E8 is merged** (`plan triage` / `plan close` on master through `agent-hub ready`).
   - If E8 is not merged: finish E8 exactly as the resume note says (read the recorded review result files; do not
     re-run finished reviews), merge it through `ready`, then continue here.
3. `master` clean, no plan in flight for this repo (`agent-hub jobs`, `plan status`). Record the starting version.

## 1. Owner decisions already taken (2026-10-18) — record them, do not re-ask

Add these to `docs/decisions.md` from **D-102** on (date 2026-10-18, owner's call unless marked otherwise):

| ID | Decision |
|---|---|
| D-102 | Order of work: E8 → **audit log (Phase G)** → scorecard baseline → spec gate. Audit first: scorecard, any future sentry and audit all need the same event data. |
| D-103 | Audit records **actions plus at most one short quote** (≤ 200 chars) taken from **public output only** (a handoff Status/Summary line, a review finding title, the `description` argument of a tool call, a reply sentence). **Never** record model thinking/reasoning blocks, even when a host returns them. Audit must not depend on any vendor's reasoning output. |
| D-104 | Logging is **fail-open** (a logging failure never stops or slows work; a `gap` event is written when logging recovers). The log is **append-only and tamper-evident** (hash chain). Secrets are redacted **at write time**; file contents are never stored (hashes and paths only). |
| D-105 | Local retention: current month + previous month. A **monthly, deterministic** archival (no agent, no LLM) moves the previous calendar month to the owner's vault, compressed, after checksum verification. The vault path comes **only** from owner config; an agent can read archival status and request a re-run for a given month, never pass a path. |
| D-106 | Deferred until a trigger fires (see §7): sentry (command/egress enforcement), credential broker, spec-review gate for the techlead. Owner direction recorded for when they come: no agent is above policy (director included); the owner alone signs every policy/sentry change; the credential broker is **not** an MCP tool (deterministic local process, agents submit requests as data, owner approves the request hash). |

Further autonomous design choices you make in this phase continue from D-107, each with its reason.

## 2. Write the spec

Create `docs/superpowers/specs/2026-10-18-phase-g-audit-log-design.md`, status "approved (owner, 2026-10-18)".
It must cover everything below; where a choice is left open, decide it in the spec and log a D-xxx.

### 2.1 Goal and questions the log must answer
Every event answers: **who** (role, `agent:model`, director session or job), **when**, **belongs to what** (parent ids),
**what action**, **outcome**, **where the evidence is**. Audit questions to support: "what did the director do since
T", "full timeline of plan X / job Y", "every outward action (push, remote, network to non-model domains)", "every
gap in the log", "did anyone change the log".

### 2.2 Event schema (JSON Lines)
Minimum fields: `ts` (UTC ISO-8601), `seq`, `event_id`, `actor` {`role`, `agent`, `model`, `session_id` | `job_id`},
`parent` {`plan`, `task`, `job`, `attempt`, `director_session`, `turn`} (absent keys allowed), `kind`, `target`,
`outcome` {`status`, `exit_code`, `duration_ms`}, `quote` (optional, ≤ 200 chars, redacted), `evidence` (path or
sha256), `prev_hash`, `hash`. Add a `schema_version`.

`kind` enum and what each records:
- `command`: redacted argv, cwd, exit code, duration; stderr tail (bounded) only on failure. Never full output.
- `file_read` / `file_write`: path, operation, sha256 before/after. Never contents.
- `net`: domain, method, size. Never body or auth headers.
- `git`: operation, ref, sha, remote.
- `job`, `plan`: the lifecycle events agent-hub already knows; `decisions.tsv` rows are mirrored here
  (`decisions.tsv` stays as is for compatibility).
- `outward`: anything leaving the machine that is not a model API call (push, remote creation, upload).
- `gap`: logging was unavailable from–to, and why.
- `archive`: archival runs and their result.

### 2.3 Integrity
- Hash chain: `hash = sha256(prev_hash + canonical JSON of the event without hash)`.
- Several processes write concurrently (director hook, agent-hub runner, jobs). The spec must choose and justify one
  approach (e.g. one chain per day file guarded by an advisory file lock, or one chain per writer stream with a
  daily manifest) and say how ordering and chain continuity are verified. Log the choice as a D-xxx.
- Each day file's first event references the previous day's last hash; archives carry a manifest with first/last
  hash so verification spans local files and archives.

### 2.4 Storage
`$XDG_STATE_HOME/agent-hub/audit/YYYY-MM/YYYY-MM-DD.jsonl` (honour `$AGENT_HUB_STATE`). Standard library only
(project rule: no runtime dependencies).

### 2.5 Sources — staged
- **G1 (this phase):** correlation ids + agent-hub's own events. Every job/plan/gate/review/escalation/autocommit/
  merge emits events with parent ids. Workers receive their trace context through the environment so later stages
  can attach to it.
- **G2 (this phase):** director observation through Claude Code hooks (PreToolUse/PostToolUse), shipped in the
  agent-hub plugin. **Observe only:** the hook never blocks, never alters a tool call, always exits success, and
  must stay fast; if the log is unwritable it does nothing visible except a later `gap` event. Quote source: the
  tool call's own `description` argument when present. Never read or copy reasoning/thinking content from the hook
  payload or the transcript.
- **G3 (later, follow-up):** normalise each worker host's tool-call events (claude stream output, codex JSON events,
  opencode, agy) through the existing adapters, one host per task.
- **G4 (later, follow-up):** daily digest to the owner.

### 2.6 Read side
- `agent-hub audit show [--plan P | --job J | --session S | --since T] [--kind K]... [--json]`: timeline view,
  reading local files and archives transparently.
- `agent-hub audit verify [--month YYYY-MM]`: checks the chain across local files and archive manifests; exit 1 on
  any break, printing where.
- `agent-hub audit outward [--since T]`: outward actions only.
- MCP: **read-only** tools only (`audit_status`, `audit_show`), `readOnlyHint=True`. No write or delete tool.

### 2.7 Monthly archival
- `agent-hub audit archive --month YYYY-MM` (default: previous calendar month): compress (stdlib `tarfile` with
  lzma or gzip), copy to the vault, read the copy back and verify sha256, write the manifest (count, first/last
  hash, sha256), and **only then** delete the local month. Idempotent: a re-run for an archived month is a no-op
  that reports "already archived".
- Vault path from owner config only (e.g. `~/.config/agent-hub/audit.toml`, key `vault`). Missing config, vault not
  mounted, not writable, or checksum mismatch → keep everything, exit non-zero, write an `archive` event with the
  reason. Never delete without a verified copy.
- `agent-hub audit schedule [--apply] [--remove] [--at "<calendar>"]`, same pattern as `skills schedule`
  (print by default; `--apply` writes and enables a systemd user timer; `--remove` deletes only its own units).
  Default: the **first Sunday of each month, after `skills weekly`** (weekly runs Sunday 20:17), e.g. 21:17 local.
  Check the calendar expression with `systemd-analyze calendar`. The two timers must never overlap.
- MCP may expose `audit_archive_status` and `audit_archive_rerun(month)`; the re-run takes only a month, never a path.

### 2.8 Redaction
Reuse `skillmine`'s redaction (extend it if needed, with tests) and apply it to argv, quotes, stderr tails and
targets before hashing and writing.

### 2.9 Out of scope (record in `follow-ups.md`)
G3, G4, scorecard baseline, sentry, credential broker, spec-review gate — with the triggers in §7.

## 3. Plan and run the work

1. Have the techlead split the spec (use `agent-hub plan draft` if available in this version, otherwise a
   techlead job of kind `plan` as the orchestrate skill describes). Size limits as usual: one concern, ≤ 3 files,
   ≤ 4 deliverables, ≤ 6 named tests per task. Suggested ordering, the techlead decides the final split:
   schema + writer + chain + lock → redaction reuse → agent-hub event emission + trace context → `audit verify` →
   `audit show` / `outward` → director hook (plugin) → archive command → `audit schedule` → MCP read-only tools →
   README / overview / orchestrate skill updates.
2. Read the draft plan yourself (tiers, dependencies, file paths exist), then `plan run` → `plan review` →
   `plan triage` / `plan close` → `plan merge` through `agent-hub ready`. Every review on a model other than the
   implementer's. No gate bypass.
3. Required tests (fake agents only, never real agents or real network):
   - chain verification passes on a clean log and fails at the exact tampered/removed/reordered event;
   - concurrent writers keep a valid chain;
   - redaction removes secrets from argv, quotes and stderr tails before hashing;
   - a quote is truncated to 200 chars; a hook payload/transcript containing a thinking/reasoning block produces an
     event without any of that content;
   - the hook exits success and does not block when the audit directory is unwritable, and a `gap` event appears
     once writable again;
   - archive: success path; vault missing; vault unwritable; checksum mismatch; re-run is a no-op; local data is
     never deleted on any failure;
   - `audit verify` spans a month that was archived;
   - `audit schedule` prints without changing anything; `--apply`/`--remove` touch only their own units (mock
     systemctl as `skills schedule` tests do).
4. Real check before closing the phase (`principle-prove-it-works`): run one small real job and one plan task, then
   `agent-hub audit show --plan …` and `audit verify` on the real state directory; run `audit archive` against a
   temporary vault directory (never the owner's vault) for a synthetic past month and verify it.

## 4. Autonomy and limits for this phase
- Proceed without asking on reversible local work (worktrees, jobs, reviews, local commits, local merges of verified
  `orch/*` branches, local release/version bump as in previous phases).
- Never push, create remotes or PRs, or touch the owner's real vault; never run `audit schedule --apply` — print
  the units and leave applying to the owner.
- Never edit product code yourself; you write the spec, decisions, follow-ups, briefs and resume notes.

## 5. Finish
- Update `README.md` (commands), `docs/overview.md` (pieces, job record/audit section, decision index),
  `docs/follow-ups.md`, and write a new update block at the top of `docs/next-phase.md` naming the plan file,
  review result files and anything left open.
- Report to the owner in Vietnamese: what merged, version, how to apply the archival timer, open items.

## 6. Done means
- `agent-hub audit verify` exits 0 on the real state directory after the real check.
- A director session's tool calls and a plan's full lifecycle are visible in `agent-hub audit show`.
- Archival proven against a temporary vault, including a failure case that kept local data.
- All merged branches passed `agent-hub ready`; lint and tests green on master.

## 7. Triggers for the deferred items (write them into `follow-ups.md`)
Revisit sentry / broker when any of these happens: unattended overnight runs become routine; agents read untrusted
content (web pages, external issues) regularly; the repo gets a GitHub remote and pushes become frequent; the audit
log shows a dangerous action actually happened. Revisit the spec-review gate after the scorecard baseline exists, so
its effect can be measured (fix rounds and spec-caused `blocked` tasks with vs without the gate).
