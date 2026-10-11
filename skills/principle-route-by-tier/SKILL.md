---
name: principle-route-by-tier
description: Apply when writing a task or choosing `--role`, to pick the cheapest tier whose criteria the brief actually meets.
disable-model-invocation: true
---

# Route by tier

Choose the tier from the criteria in the mode skill's Routing table, not from a feeling about difficulty. Pick the cheapest tier the brief honestly qualifies for.

**Why:** D-059. "Hard", "normal" and "small" are only decidable with numbers: files touched, changed lines, whether the design is settled, brief size against the small model's context. A wrong guess either wastes strong-model quota or fails a gate.

**Pattern:**
- Count the files and lines the change needs. Over ~400 lines → split the task before routing.
- Unsettled design, a shared interface, concurrency, security, migration or an unknown-cause bug → `implement-hard`.
- Fully specified and tiny → `implement-small`; otherwise `implement`.
- Reviews always use `review`.

**Boundaries:** The CLI never guesses a tier for you. When two tiers fit, take the cheaper one: escalation corrects an underestimate for the price of one failed run.
