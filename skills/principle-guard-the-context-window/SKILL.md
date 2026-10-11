---
name: principle-guard-the-context-window
description: Apply when job output, logs or replies grow large, to read handoffs and result records instead of full replies and keep bulk out of the main thread.
disable-model-invocation: true
---

# Guard the context window

Read the smallest artifact that answers the question. Every request re-reads the whole conversation, so bulk text taxes every later call.

**Why:** D-063 and the long-session cost of this skill. A full reply or log pasted into the chat is paid for again on each turn.

**Pattern:**
- Start from `result.json`, `agent-hub show <id>` and `handoffs/`; open the full reply or attempt stderr only when the handoff is not enough.
- Use `plan status` for a plan's state instead of reading every job.
- Read diffs by file or hunk once you know which matter.
- At a phase boundary make sure the plan file, handoffs, decisions and commits are on disk, then tell the user to `/compact` or start a new session.

**Boundaries:** Never compact in the middle of a task; file paths and partial state are lost. Do not skip reading something that decides acceptance, such as the gate tail of a failure.
