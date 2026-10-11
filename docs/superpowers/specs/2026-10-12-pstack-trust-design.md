# Trust mechanics from pstack — enforced rules, finish conditions, decision trail, commit-bound verdicts

Status: approved (owner: "toi dong y" to items 1–3, 2026-10-12; autonomous mode for the rest). Decisions D-071..D-076.

Source: Lauren Tan's (poteto) pstack, `cursor/plugins/pstack` — read 2026-10-12: the guide (`docs/guide/01..10`),
`/correct`, `show-me-your-work`, the overnight contract, Autopilot's verdict rule. Earlier work (D-057..D-068) took
pstack's *shape* from a Claude Code port; this spec takes its *trust mechanics* from the original.

## 1. Rules climb the enforcement ladder (D-071)

pstack's `/correct`: fix a repeated mistake at the highest level that works — (1) architecture makes it impossible,
(2) types / lint / CI block it, (3) a test catches it, (4) a written rule — last, because "nothing fails when an agent
skips a rule". A class counts once it has happened twice; keep a table pairing each rule with what enforces it, so an
unenforced rule shows up.

agent-hub's standing orders are all level 4. This spec moves them up where possible and makes the table explicit.

**Format.** Every line of a standing-orders file (user and repo, D-064) is
`N. <rule> — enforced by: <mechanism>` where `<mechanism>` is a named check, or `none`. `agent-hub orders`
prints the merged table (rule, enforcement, source file) and exits 1 if a line lacks the `— enforced by:` suffix. The
text sent to agents is unchanged except the suffix is stripped (agents need the rule, not our bookkeeping).

**New enforcement** (each a deterministic check, reported in `result.json`, never a model call):

| Standing order | Enforced by (new or existing) |
|---|---|
| Run full lint + tests of touched files | `gate` — and the repo can now declare default gates in `<repo>/.agent-hub/config.toml` `gates = [...]`, run on every implement job even when the orchestrator forgets `--gate` (D-072) |
| No test without assertions; never weaken asserts | `gate-guard` (D-038) **plus a new finding `assertion-free-test`**: a test function added in a `.py` test file whose body (AST) contains no `assert`, no `pytest.raises`/`pytest.warns`, and no call to a name starting `assert` |
| Never write into the repo root from tests | `none` (not enforceable without sandboxing — stays a rule) |
| Never edit gate configuration | `gate-guard` (D-038) |
| No real network/agents from tests | `none` |
| English, end with the handoff | `handoff-parser` (`handoff_missing` already recorded) |
| Change only the files the brief allows | `scope` — **new for single runs**: `run --allow <glob>` (repeatable); changed files outside the globs → status `scope-violation` (exit 10), as plan mode's `paths_allowed` (D-034) |

## 2. Finish condition and escape hatch (D-073)

pstack's overnight contract: the goal, *done means*, permissions, an escape hatch; "the finish condition never quietly
relaxes to declare victory"; a reply without evidence is a red flag, and "inconclusive" is an honest answer.

- The implement footer (D-031) gains, before the handoff template:
  `If you cannot meet the brief's checks, stop: do not weaken a test, a gate or the brief's conditions to get green.
  Report Status "blocked" and say exactly what stopped you.`
- `## Verification` accepts a fourth value `inconclusive` (checks could not run); parsed like the others.
- `brief-template.md` renames the `## Expected output` guidance to lead with **Done means** (the checks), keeping the
  heading `## Expected output` for D-011 compatibility.
- Plan-mode retries and escalations append the same escape-hatch sentence to their notes.

## 3. Decision trail per plan (D-074)

pstack's `show-me-your-work`: one row per decision — time, phase, decision, reason, evidence pointer, result — audited
later instead of re-reading transcripts.

`agent-hub plan run` appends to `<plan dir>/decisions.tsv` (header once): `time  task  phase  decision  reason
evidence  result`. Rows: task started (role, start ref), attempt finished (status, job id), retry (cause), escalation
(from → to, reason), autocommit (sha), scope violation, task done/failed/blocked, review verdict. `plan status
--trail` prints it. Written by the runner only (one writer, pstack's store rule).

## 4. Verdicts bound to commits (D-075)

pstack's Autopilot: "no owner merges on its own verdict… only a clean verdict on the patch that merges authorizes the
merge".

- Every job's `result.json` records `head`: the work tree's `HEAD` sha when the job ended (after autocommit in plan
  mode); gate results record the sha they ran on.
- Review jobs get a footer asking for `## Verdict` → `approve | request-changes`, parsed into `result.json.verdict`
  (`null` if missing).
- `agent-hub ready -d <worktree>` (exit 0/1, prints why): ready only if, for the current `HEAD`: an implement job's
  gates passed **on that sha** (or the repo default gates, D-072), and a review job with verdict `approve` ran **on
  that sha** with an `agent:model` different from the implementer's (D-062). Any later commit makes both stale.
- The orchestrate skill: run `agent-hub ready` before every merge.

## 5. Out of scope (later)

`explain-the-number` checklist for the bench; `agent-hub pause` (pause-safely); reviewer audit of the trail
("Attention" section); `/reflect`-style session review.

## 6. Tiers (D-076)

| Task | Content | Tier |
|---|---|---|
| `orders` | `— enforced by:` format, `agent-hub orders`, strip suffix when sending, seed files updated | implement |
| `assert-free` | gate-guard `assertion-free-test` finding | implement-small |
| `allow-scope` | `run --allow` + `scope-violation` for single runs | implement |
| `repo-gates` | `<repo>/.agent-hub/config.toml` default gates | implement-small |
| `escape-hatch` | footer sentence, `inconclusive`, template wording, plan notes | implement-small |
| `trail` | `decisions.tsv` in plan mode, `plan status --trail` | implement |
| `verdict` | `head` sha, review `## Verdict`, `agent-hub ready` | implement-hard |
