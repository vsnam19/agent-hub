# Model routing Implementation Plan

> **For agentic workers:** executed with `agent-hub plan run` — one implement job per task; each brief points here.
> Steps use checkbox (`- [ ]`) syntax.

**Goal:** Tiered roles with escalation, standing orders, review on another model, and the orchestrate skill rewritten
in pstack's style.

**Architecture:** A role becomes `{kind, chain, escalate?}`; every place that branches on the role *name* branches
on its *kind*. Plan mode escalates a failed task one tier. The CLI appends standing orders to every brief. Skills
follow pstack: one mode skill plus short `principle-*` leaf skills.

**Tech Stack:** Python ≥ 3.12 stdlib, pytest, ruff; Markdown skills.

**Spec:** `docs/superpowers/specs/2026-10-11-model-routing-design.md` (D-057..D-064).

## Global Constraints

- No runtime dependencies (D-015). Tests never run a real agent (D-016); autouse fixtures in `tests/conftest.py`
  stay in force; tests never write into the repository or cwd.
- No test without an assertion; no skips; never weaken or delete an existing assertion.
- Backwards compatible: a `pool.toml` with only `[roles.implement]` and `[roles.review]` and no `kind` keeps working
  exactly as today (a role named `implement`/`review` without `kind` has that kind).
- `just lint` and `uv run pytest -q` green at the end of every task.

## Review Focus

1. A user `pool.toml` from v0.4 (roles without `kind`) → behaves as before — test in Task 1.
2. `escalate` naming an unknown role, or a cycle (a → b → a) → `agent-hub pool` / plan load fails with a clear
   error, never an infinite loop — test in Task 1 (unknown) and Task 3 (cycle/one-step bound).
3. Escalation after the agent committed on the task branch → the tree is reset to the start point (not just
   `git checkout .`) — test in Task 3.
4. Standing orders file present but empty, or unreadable → brief unchanged, no crash — test in Task 2.
5. A review chain whose only link is the implementing model → status `exhausted` with a reason, not a self-review —
   test in Task 4.

---

### Task 1: roles have a kind (`roles`)

**Files:** Modify `agenthub/config.py`, `agenthub/pool.py`, `agenthub/cli.py`, `agenthub/brief.py`,
`agenthub/orchestrate.py`, `agenthub/plan.py`, `agenthub/planrun.py`, `agenthub/default_pool.toml`; tests in
`tests/test_config.py`, `tests/test_pool.py`, `tests/test_cli.py`, `tests/test_plan.py`.

**Interfaces — produces:**
```python
@dataclass(frozen=True)
class Role:
    name: str
    kind: str                 # "implement" | "review"
    chain: list[str]          # "agent:model"
    escalate: str | None = None
# the loaded pool exposes roles: dict[str, Role]; helper:
def role_kind(pool, role_name: str) -> str   # raises a config error for an unknown role
```
- `kind` defaults to the role's name when the name is `implement` or `review`; any other role without `kind`, or a
  `kind` outside those two, is a config error. `escalate`, if set, must name another role of kind `implement` and
  only implement roles may escalate.
- Every `== "implement"` / `== "review"` test on a role name in `cli.py`, `brief.py`, `orchestrate.py`, `plan.py`,
  `planrun.py` uses the kind instead. Default timeouts come from `timeout.<kind>` unless `timeout.<role>` exists.
- `run --role` accepts any role in the pool (no fixed `choices`); unknown role → exit 2 with the list of roles.
  Plan task `role` likewise.
- `agent-hub pool` prints each role with `(kind, escalate → x)`.
- `default_pool.toml` = spec §3 verbatim, except `implement-small` = `["claude:haiku"]` until the gwen adapter exists
  (keep the gwen link in a comment).

Tests (names exact): `test_legacy_pool_without_kind_still_works`, `test_custom_role_requires_kind`,
`test_escalate_unknown_role_is_error`, `test_review_role_cannot_escalate`, `test_run_accepts_custom_implement_role`
(fake agent; brief without `## Input` is refused for kind implement), `test_run_unknown_role_exit_2`,
`test_plan_task_custom_role`, `test_pool_shows_kind_and_escalate`, `test_default_pool_roles` (the four roles,
kinds, escalate links).

---

### Task 2: standing orders (`standing-orders`)

**Files:** Modify `agenthub/brief.py` (or where the sent brief is assembled in `orchestrate.py`), `README.md`
(one paragraph); create `docs/standing-orders.example.md`; tests in `tests/test_brief.py` / `tests/test_run.py`.

- Order of the text sent for implement and review jobs: the brief → upstream handoffs / retry notes (existing) →
  `## Standing orders` (user file `$AGENT_HUB_CONFIG/standing-orders.md` or `~/.config/agent-hub/standing-orders.md`,
  then `<repo root>/.agent-hub/standing-orders.md`, each only if it exists and is non-empty after strip) → handoff
  footer (implement only). The recorded `brief.md` equals what was sent.
- Unreadable file → skipped with a one-line warning on stderr.
- `docs/standing-orders.example.md`: the seed text from spec §7.

Tests: `test_standing_orders_user_and_repo_appended_in_order`, `test_no_standing_orders_when_files_absent_or_empty`,
`test_unreadable_standing_orders_warns_and_continues`, `test_review_brief_gets_standing_orders_but_no_handoff_footer`.

---

### Task 3: escalation in plan mode (`escalate`) — depends on `roles`

**Files:** Modify `agenthub/planrun.py`, `agenthub/planstate.py`; tests in `tests/test_planrun.py`.

- After a task's retries (D-034) end in `gate-failed` or `failed`, and its role has `escalate`: reset the task's
  work tree with `git reset --hard <start_ref>` and `git clean -fdx` (same safe git invocation helper the runner
  already uses), then run the task once at the escalated role. The brief gets an extra section
  `## Previous attempt failed` with the last gate output tail (or the failure reason). The task state records
  `escalated_from: <role>` and the new attempts. The escalated run itself may escalate again (its role's
  `escalate`), so small → implement → hard is reachable; a visited set prevents cycles.
- `exhausted`, `refused`, `scope-violation`, `cancelled` never escalate.
- `plan status` shows `escalated from <role>`.

Tests: `test_gate_failed_task_escalates_once_with_gate_tail`, `test_escalation_resets_committed_work`,
`test_exhausted_does_not_escalate`, `test_escalation_chain_two_steps_then_stops`, `test_escalation_cycle_guard`.

---

### Task 4: review never on the implementing model (`review-other-model`) — depends on `roles`

**Files:** Modify `agenthub/planrun.py` (and `agenthub/orchestrate.py` if the exclusion is passed down); tests in
`tests/test_planrun.py`.

- For a plan `review` task with `verifies: <task>`, the chain skips every link equal to the `agent:model` that
  produced the verified task's final successful attempt (read from its job `result.json`). If no link remains →
  task `blocked` with reason `no reviewer different from <agent:model>`.

Tests: `test_review_skips_implementing_model`, `test_review_blocked_when_only_implementing_model_left`.

---

### Task 5: skills in pstack's style (`skills`) — depends on `roles`, `standing-orders`, `escalate`, `review-other-model`

**Files:** Rewrite `skills/orchestrate/SKILL.md`; create `skills/principle-director-never-codes/SKILL.md`,
`skills/principle-route-by-tier/SKILL.md`, `skills/principle-escalate-one-tier/SKILL.md`,
`skills/principle-quota-first/SKILL.md`, `skills/principle-prove-it-works/SKILL.md`,
`skills/principle-review-on-another-tier/SKILL.md`, `skills/principle-guard-the-context-window/SKILL.md`;
update `tests/test_skill.py` and `tests/test_plugin.py` if they enumerate skills.

- Style reference: pstack (spec §6). Mode skill sections: *Non-negotiables* (trigger → action, one line each),
  *Routing* (tier table with criteria from spec §2 and default chains; "`agent-hub pool` is the live truth"),
  *Principles* (one line each → leaf skill), *The loop* (existing steps kept), *Plan mode*, *Long sessions*,
  *Statuses* (existing table kept), *Autonomy* (proceed on reversible work; pause for push, remote creation,
  deletion). Must keep every rule the current skill has (diff the old and new rule lists in the handoff).
- Each leaf skill: frontmatter `name`, `description` ("Apply when …"), `disable-model-invocation: true`; body
  ≤ 30 lines: the rule, **Why** (cite the D-entry), **Pattern**, **Boundaries**.

Tests: `tests/test_skill.py` gains `test_principle_skills_have_frontmatter_and_are_model_invocation_disabled` and
`test_orchestrate_lists_every_principle` (every `skills/principle-*` directory is named in the mode skill and vice
versa). Existing skill tests keep passing (update expected strings only where a heading was renamed — list each in
the handoff).
