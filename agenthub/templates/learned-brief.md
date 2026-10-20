# Brief: learned skills — $date

## Goal

Turn what repeated in the last weeks of agent-hub work into at most 3 tested skills in this repository, or into
nothing: zero skills is a valid answer.

## Input

- `.agent-hub/learned/extract.md` (short) and `.agent-hub/learned/extract.json` (full): review findings grouped by
  severity and module, branches with several review rounds, owner corrections. Window: $window. It is already
  redacted; do not copy text from it that looks like a secret, and never add a secret to a skill.
- The `## Incidents` section of the same extract (and the `incidents` list in `extract.json`): counts per incident
  kind in the window, and the top 3 incidents by cost with their lessons.
- Two plugins you own: `plugins/learned-worker` (lessons for implementers: what reviewers keep finding, how to prove
  a change) and `plugins/learned-director` (repeated orchestration loops and owner corrections).
- Existing skills. Update one of these rather than add a near-duplicate:
  - learned-worker: $existing_worker
  - learned-director: $existing_director
  - worker (read-only, not yours): $existing_base

## Expected output

1. Pick at most 3 candidates. A candidate is a mistake or loop seen at least 3 times across at least 2 branches or
   sessions in the extract. Worker lessons go to `learned-worker`; orchestration lessons and owner corrections go to
   `learned-director`.
   When one incident kind has 3 or more incidents in the window, draft or update exactly one `learned-director` skill
   for that kind from its lessons. It counts toward the at most 3 skills and does not need the 3-times-across-2-branches
   rule. With fewer than 3 incidents of a kind, mention that kind in the Notes of your reply only.
2. For each candidate write or update `plugins/<plugin>/skills/<skill>/SKILL.md`:
   - YAML front matter with `name` and `description` (when to use it, one sentence);
   - at most 450 words;
   - a rationalizations table (`| Excuse | Answer |`);
   - a `Sources` line with the job ids from the extract it comes from;
   - no secrets, no hooks, no scripts.
3. For each skill add at least 2 eval cases under `plugins/<plugin>/evals/<skill>-<case>/`: one positive case where
   the skill should change the answer (the positive case must be one the no-plugin baseline fails — state why in the
   case's `prompt.md`), one off-topic negative case where it must not apply. Each case has a `prompt.md` and
   `graders/*.md` files with front matter `type: llm`. Follow the layout of the existing cases in this repository.
4. A candidate that needs a deterministic tool (a query, a computation) that text cannot carry is not a skill: do not
   write code for it. Add a line `MCP proposal: <name> — <what it would do and why text is not enough>` to the Notes
   of your reply.
5. Do not edit any file outside `plugins/learned-worker` and `plugins/learned-director`. Do not change versions,
   manifests or gate configuration; the pipeline bumps versions. Do not commit.

## Constraints

- Write in English. Keep each skill short and specific to this evidence; no general advice.
- Gate: this repository's gates (`.agent-hub/config.toml`) run on the committed result.
