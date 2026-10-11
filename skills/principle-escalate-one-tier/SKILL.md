---
name: principle-escalate-one-tier
description: Apply when a task failed its gate or ended failed, to move it one tier up instead of retrying at the same tier or jumping to the strongest.
disable-model-invocation: true
---

# Escalate one tier

A task that failed on proven ability goes one role up (the role's `escalate` target), from a clean tree, with the failure shown.

**Why:** D-061. A bigger model only earns its cost where a smaller one failed. Retrying the same model repeats the failure; jumping straight to the top wastes quota.

**Pattern:**
- Plan mode does it for you: after retries the tree is reset to the task's start point and the task runs once at the escalated role with the failing gate output in the brief. An escalated run uses the escalated role's chain, not a pinned agent.
- In a single `run` you do it: reset the worktree to the start commit, re-run at the `escalate` role named by `agent-hub pool`, and add the last gate tail to the brief.

**Boundaries:** `exhausted` and `refused` never escalate; that is quota or configuration, not ability. One step per failure; small → normal → hard is at most two. A role with no `escalate` target stops there: split the task or rewrite the brief and run it again; you still do not write the code.
