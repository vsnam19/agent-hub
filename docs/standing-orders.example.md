# Standing orders

Copy this file to `~/.config/agent-hub/standing-orders.md` (every repository) or to
`<repo>/.agent-hub/standing-orders.md` (one repository). agent-hub appends the non-empty files to every implement
and review brief, under `## Standing orders`, so the rules are repeated on every spawn.

Each rule ends with `— enforced by: <mechanism>`, naming the check that enforces it or `none`. agent-hub strips that
suffix before sending the rule, and `agent-hub orders` lists the rules with their mechanisms and exits 1 if a line has
no suffix.

1. Run the repository's full lint and the existing tests of every file you touch before you hand off. — enforced by: gate
2. Never write a test without assertions; never weaken or delete an existing assertion to make a test pass. — enforced by: gate-guard
3. Never write into the repository root or the current directory from tests; use temporary directories. — enforced by: none
4. Never edit gate configuration (lint, test or type-check settings, justfile, conftest.py) to make a check pass. — enforced by: gate-guard
5. Do not call real external services, real agents, or the network from tests. — enforced by: none
6. Write code, commit messages and your final handoff in English, and end with the handoff exactly in the requested format. — enforced by: handoff-parser
7. Change only the files the brief allows; if you need another file, stop and say so in the handoff. — enforced by: scope
