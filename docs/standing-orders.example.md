# Standing orders

Copy this file to `~/.config/agent-hub/standing-orders.md` (every repository) or to
`<repo>/.agent-hub/standing-orders.md` (one repository). agent-hub appends the non-empty files to every implement
and review brief, under `## Standing orders`, so the rules are repeated on every spawn.

1. Run the repository's full lint and the existing tests of every file you touch before you hand off.
2. Never write a test without assertions.
3. Never write into the repository root from tests.
4. Never edit gate configuration (lint, test or type-check settings) to make a check pass.
5. Do not call real external services from tests.
