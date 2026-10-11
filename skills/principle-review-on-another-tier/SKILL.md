---
name: principle-review-on-another-tier
description: Apply when reviewing a delegated change, so the reviewer is never the model that implemented it and risky changes get a second lens.
disable-model-invocation: true
---

# Review on another tier

Every change you merge gets one review, and the reviewer is never the `agent:model` that wrote it.

**Why:** D-062. A model reviewing its own output shares its blind spots. Seats that differ by model and by lens find different defects.

**Pattern:**
- Run `--role review` on the worktree with `--base <default branch>`; in plan mode a `verifies` task skips the implementing link on its own.
- Outside plan mode pass `--exclude <agent>:<model>` for the model that wrote the change.
- For security, concurrency, data or a public interface add a second review from a different vendor with a different lens, for example "does it actually run and do what it claims — check error paths and inputs that break it".

**Boundaries:** If the only review link is the implementer, the job is `exhausted` with a reason; take the review yourself or clear another link, never accept a self-review. Findings are input to your judgement, not a verdict.
