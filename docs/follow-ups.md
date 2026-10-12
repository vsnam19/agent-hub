# Follow-ups

Open items found by reviews and runs, not yet scheduled. Small, well-specified ones suit `implement-small` (gwen) on a
small repository, or `implement` here (D-079).

| Found | Item | Source |
|---|---|---|
| 2026-10-12 | C: C++ fixture for the gwen bench (B done in v0.9.0, spec `2026-10-12-phase-b-skill-hub-design.md`) | D-077 |
| 2026-10-12 | Owner's call: agy has superpowers imported (2026-09-26): a headless `agy -p` run lists all 15 superpowers skills in its index (e.g. brainstorming: "You MUST use this before any creative work"), verified 2026-10-12; whether its SessionStart hook also runs headless is unverified; the `worker` plugin is now installed in agy (2026-10-12), so agy workers see both `test-first`/`debug-playbook`/`verify-before-done` and superpowers' TDD/debugging/verification skills — consider `agy plugin disable superpowers` | D-087 |
| 2026-10-12 | Repeat the skill checks that could not run: a live `codex exec` with an installed plugin (quota back 2026-11-07) and a gwen run with `skills.paths` (Mac host was down) | D-083 |
| 2026-10-12 | Owner's call: hand-copied `~/.codex/skills/telaf-api` and `telaf-legato` duplicate the installed `telaf` plugin; `agent-hub hub status` reports them as `stray` and never deletes them | D-089 |
