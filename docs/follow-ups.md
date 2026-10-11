# Follow-ups

Open items found by reviews and runs, not yet scheduled. Small, well-specified ones suit `implement-small` (gwen) on a
small repository, or `implement` here (D-079).

| Found | Item | Source |
|---|---|---|
| 2026-10-12 | `run_gate_job` has no SIGTERM/SIGINT handling: an interrupted `agent-hub gate` leaves its job `running` | review of D-080 (low) |
| 2026-10-12 | gate-guard's re-added-test check keys on the test name per file, ignoring the enclosing class | review of D-080 (low) |
| 2026-10-12 | `touched_findings` takes the first passing gate with findings, not the newest | review of D-080 (low) |
| 2026-10-12 | `ready --accept-touched` appends an acceptance on every run, even when other problems remain | review of D-080 (low) |
| 2026-10-12 | `plan cancel` crashes on a corrupt `state.json` instead of a usage error | review of D-080 (low) |
| 2026-10-12 | `--exclude` has no direct `run` test; a missing implementer record should block a plan review; a pinned review agent equal to the implementer should be blocked | handoff of review-other-model |
| 2026-10-12 | `agy` adapter: Gemini returned an empty review on the agent-hub repo | review run 2026-10-12 |
| 2026-10-12 | B: install skills (incl. `telaf`, `aspice`) for agy/codex/opencode — the hub; C: C++ fixture for the gwen bench | D-077 |
