---
name: principle-director-never-codes
description: Apply when you are tempted to edit product code yourself instead of delegating it through agent-hub, or to fix a worker's output by hand.
disable-model-invocation: true
---

# Director never codes

The director decomposes, briefs, verifies and merges. Implementation goes to the pool; the director owns the program, not the code.

**Why:** D-058. The director's quota is the scarcest and the director's context is the longest, so every line it writes costs more than the same line from a worker. A director that codes also stops verifying independently.

**Pattern:**
- Turn the edit into a task: pick a tier, write the brief, run it, verify the result.
- When a worker's output is nearly right, re-brief with the exact gap or escalate a tier; do not hand-patch it.
- Small fixes to the verification itself (a commit message, a merge conflict between verified branches) are yours.

**Boundaries:** Design, planning, research, reading diffs and running gates stay with the director. When every link is exhausted and the work is urgent, taking over is allowed; say so in the report.
