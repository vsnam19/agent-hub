---
name: principle-prove-it-works
description: Apply when delegated work is about to be accepted or merged — run the gate and the result yourself on real data; a worker reply and a green gate alone are not proof.
disable-model-invocation: true
---

# Prove it works

Verify delegated work against the real artifact. The worker's claim is not evidence, and a passing gate only proves what the gate checks.

**Why:** D-065 and every earlier job record. Replies said "all tests pass" over trees whose gate failed, and a gate can be weakened by the same change it guards.

**Pattern:**
- Run the repository's lint and tests in the worktree yourself.
- Read the whole diff; read every hunk in `gate_touched` and scan new tests for missing assertions.
- Run the changed behaviour on real input (the command, the CLI path, a real file), not only the unit tests.
- When a check fails, suspect your observation before the system, then fix the cause.

**Boundaries:** Do not re-derive facts a job record already proves (exit code, changed files). A review's findings are input to this check, not a replacement for it.
