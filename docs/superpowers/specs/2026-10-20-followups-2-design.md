# Follow-ups 2 — design

Status: approved (director, 2026-10-20; items from `docs/follow-ups.md` "From plan ids-incidents" and "From the Phase G
final review"). Version target: v0.13.1.

## 1. Runner honours a blocked handoff (priority; occurred twice)
`agenthub/planrun.py`, outcome step for implement tasks (~line 440): today a job with `status == "ok"` makes the task
`done` (after scope check and autocommit) whatever the agent's handoff said. Plans `phase-g` (`audit-codex-fixture`)
and `ids-incidents` (`pool-refresh-models`, no change at all, three dependents built on it) were marked done with
`## Status blocked`.
Rule: when the job's `result.json` has `handoff.status == "blocked"` (the parsed handoff, `orchestrate.py:614`), the
task becomes `blocked` with `reason = "handoff blocked: " + first non-empty line of handoff.notes (≤ 300 chars)`, no
autocommit, a `_trail` entry `handoff-blocked`, and dependents are skipped as for any blocked task. `partial`,
`success` or a missing handoff keep today's behaviour. No escalation (a block is a brief/spec problem, not ability).
`plan status` already prints the handoff status. Tests in `tests/test_planrun.py` style with fake agents.

## 2. `skills weekly --dry-run` mine line counts incidents
`agenthub/skillweekly.py` (the "mine" step, ~line 262): the line reads `N findings, M corrections since …`; add
`, K incidents` from `Extract.incident_counts` (sum). Test in the existing skills weekly tests.

## 3. Test name
`tests/test_pool_same_as_default.py`: the test whose name says `does_not_mark` but asserts the marker is present gets
a name matching its body (`test_pool_marks_role_with_owner_override`), body unchanged; no other file.

## 4. Audit writer: unreadable claim restored
`agenthub/audit/writer.py` `_claim_pending_and_emit_gap`: if the gap append fails before the gap line is written,
only the readable claims (`raw`) go back to the pending file, then `_release_claims` unlinks every claim including
unreadable ones. Rule: on that failure path, a claim that could not be read is left in place (not unlinked); it is
picked up by the orphan rule later. Test in `tests/test_audit_failopen.py`.

## 5. Audit writer: `_redact_target` must not leave a quoted copy
`_redact_target` checks only that each changed token matches somewhere in the original text. If the secret's own
occurrence is quoted (`user:'pass'`, `p\!x`) and a plain copy exists elsewhere, the plain copy is replaced and the
quoted one survives. Rule: after replacement, re-tokenise the result (same fallback tokeniser) and run `redact_argv`;
if any token still changes, fall back to the joined redacted tokens. Test in `tests/test_audit_redaction.py`.

## 6. Redaction: curl `-E/--cert <file>:<password>`
`agenthub/redact.py` curl branch: `-E`, `--cert`, `--proxy-cert` (separate item, `=` form, attached `-Efile:pw`) keep
the part before the LAST `:` that is not part of a Windows drive (`C:`) and redact the rest; the value then goes
through `redact()` like every item. Test in `tests/test_redact.py`.

## 7. Audit plan id is the plan slug
`planrun.py`, `planclose.py`, `triagerun.py`: `plan_name = plan.path.parent.name if plan.path else …` → use
`plan.slug` everywhere a plan id goes into audit events or trace env. Tests: an event emitted for a plan file at
`<tmp>/x/plan.json` with slug `s` has `parent.plan == "s"`.

## Testing
Fake agents only; gates `just lint` and `uv run pytest -q`; no real model ids in tests (guard test).
