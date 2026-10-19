# Model ids from the host + incident log (D-121, D-122) — design

Status: approved (owner decisions D-121, D-122, 2026-10-19; director design 2026-10-20). Version target: v0.13.

## 1. Why

- **Model ids (D-121).** agy 1.4.2 silently removed `claude-sonnet-4-6` / `claude-opus-4-6-thinking`. Nobody noticed:
  the links sat on cooldown, and a Phase G task blocked because three test files hard-coded default-pool ids. Model
  ids are copied in four places (default pool, owner pool, tests, docs).
- **Incidents (D-122).** Process failures (director, techlead, tool — not ordinary review findings) were lessons in
  chat. Since 2026-10-19 they are logged by hand in `<state>/incidents.jsonl`; `skills weekly` should count them and
  turn a repeated kind into a `learned-director` lesson.

Non-goals: changing routing policy; validating models at every job start over the network; editing the owner's
`pool.toml` automatically.

## 2. Model catalog (D-121)

### 2.1 Sources
| Agent | Command | Parse |
|---|---|---|
| agy | `agy models` with `SSH_CONNECTION`, `SSH_CLIENT`, `SSH_TTY` removed from the env (keyring auth) | lines `<id>\t<display name>`; skip lines without a tab (e.g. `Fetching available models...`) |
| codex | `codex debug models` | JSON `{"models": [{"slug": …}]}` → slugs |
| claude | none (no list command) | static aliases from `agents/claude/agent.toml` `known_models = ["haiku", "sonnet", "opus", "fable"]`; any id starting `claude-` is accepted |
| gwen | none | not checked (`unknown` never reported) |

Each adapter gets `list_models() -> list[str] | None` (None = this agent has no catalog). Commands run with a 30 s
timeout through the existing process helper; any failure → None plus a reason, never an exception.

### 2.2 Cache
`<state>/models.json`: `{"<agent>": {"fetched_at": iso, "models": [...], "error": null | "<reason>"}}`, mode 600,
written atomically. Fresh for 24 h. Refreshed by `agent-hub pool --refresh-models` and lazily by `agent-hub pool` when
missing or stale (only for installed agents). Job runs never fetch: they only read the cache.

### 2.3 Behaviour
- `agent-hub pool` prints `unknown-model` next to a link whose agent has a fresh catalog that does not contain the
  model (`--json`: `"model_known": true|false|null`). A stale or missing catalog → `null`, printed as nothing.
- Chains skip an unknown link at once: in the orchestrate loop, before the cooldown check, a link whose agent's cached
  catalog is fresh and lacks the model is recorded `outcome="skipped", reason="unknown-model"` and the next link runs.
  Missing/stale cache → no skip (fail-open: never block work on a catalog problem).
- `agent-hub pool` also prints, per owner role, `(same as default)` when the owner's chain equals the default chain,
  so the owner can drop it (D-121 part 3: the owner pool lists only overrides; the director does not edit it).

### 2.4 Tests never hard-code real ids
- Tests that check the default chains read them from `default_pool.toml` via `load_pool()` instead of literals.
- A guard test (`tests/test_no_real_model_ids.py`) scans `tests/**/*.py` (excluding itself and fixture files under
  `tests/fixtures/`) and fails on string literals matching `agy:(gemini|claude|gpt)-[\w.-]+` or `codex:gpt-[\w.-]+`.
  Claude aliases (`claude:haiku` etc.) are allowed: they are stable. Arbitrary links in tests use fake ids
  (`agy:m1`, `codex:m2`, `fake:model-a` style).

## 3. Incident log (D-122)

### 3.1 Record
One JSON line per incident in `<state>/incidents.jsonl` (mode 600, `O_APPEND`):
`ts` (UTC, set by the tool), `kind`, `source` (`director` | `techlead` | `tool` | `owner`), optional `plan`, `task`,
`job`, optional `cost` {`attempts`, `rounds`, `minutes`} (ints), `what` (≤ 500 chars), `lesson` (≤ 500 chars,
optional). Text fields go through `agenthub.redact.redact`. Known kinds (free text allowed, printed as-is):
`brief-scope`, `stale-model-id`, `spec-issue`, `process-skip`, `wait-hang`, `quota`, `tool-break`, `review-loop`,
`test-time-bomb`, `handoff-status-ignored`, `external-tool-unavailable`. The hand-written lines from 2026-10-19 use
`summary`/`fix` instead of `what` in some rows: readers accept `summary` as `what`.

### 3.2 Commands
- `agent-hub incident add --kind K --what TEXT [--lesson TEXT] [--source S] [--plan P] [--task T] [--job J]
  [--attempts N] [--rounds N] [--minutes N] [--json]` — appends the line and emits an audit event
  (`agenthub.audit.emit`, kind `incident`, `target` = kind, `quote` = what, `detail` = the record without text
  duplication). Deterministic; no agent. Exit 2 on a missing `--kind`/`--what`.
- `agent-hub incident list [--since T] [--kind K] [--json]` — newest last; `--since` takes the same forms as
  `audit show`.
- MCP: read-only `incidents(since?, kind?)`, `readOnlyHint=True`. No write tool (an agent cannot log on the
  director's behalf without the CLI; the director uses the CLI).

### 3.3 Weekly
- `skillmine.mine` reads incidents with `ts >= since` into `Extract.incidents` (list of records) and
  `Extract.incident_counts` ({kind: n}).
- `render_md` adds `## Incidents` (counts per kind, the top 3 by cost = attempts + rounds + minutes/10, each with its
  lesson).
- `skills weekly` treats ≥ 1 incident as something to learn (it no longer stops with "nothing to learn" when only
  incidents exist).
- `templates/learned-brief.md`: when one kind has ≥ 3 incidents in the window, draft or update one
  `learned-director` skill for it from the lessons; fewer than 3 → mention in the report only.

### 3.4 Orchestrate skill
`skills/orchestrate/SKILL.md` gets one non-negotiable: "A task blocks or fails for a reason that is not the
implementer's (brief, spec, tool, quota, process) → `agent-hub incident add`."

## 4. Testing
Fake agents only; no real `agy`/`codex` calls in tests (patch the process helper). Cases: parse agy text and codex
JSON; cache freshness; unknown-model shown and skipped; stale cache does not skip; `same as default`; guard test
catches a planted literal; `incident add` writes the line + audit event and redacts; `list --since/--kind`; mine reads
old (`summary`) and new rows; render shows counts/top cost; weekly does not stop when only incidents exist.

## 5. Suggested split (techlead decides)
catalog parsing per adapter → cache module → pool display/refresh → orchestrate skip → test literal migration (one
task per file group) + guard test → incident module + CLI → MCP tool → skillmine/render → weekly + template → skill
text + docs.
