# Follow-ups

Open items found by reviews and runs, not yet scheduled. Small, well-specified ones suit `implement-small` (gwen) on a
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
| 2026-10-13 | A network outage (`API Error: Can't reach the API server … EAI_AGAIN`) is classified `failed` (jobs 20261012-181839-implement-1cfb, 20261012-182120-review-b33c); it should be a transient outcome that falls through to the next link (or retries) like quota, never consume a review round. Small task: claude adapter classify + test | jobs 1cfb, b33c |
