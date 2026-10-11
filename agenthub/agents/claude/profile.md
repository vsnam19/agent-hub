# claude

- Claude Code headless
- Last fallback for implement
- Review restricted to Read/Grep/Glob; no shell (the diff is precomputed into diff.patch)
- 2026-10-11: with user settings the worker answered in the owner's language and ended on a background-task notification instead of the handoff. Now isolated with `--setting-sources project,local` plus a worker system prompt.
