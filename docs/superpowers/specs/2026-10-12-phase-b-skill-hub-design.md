# Phase B — one skill set for every worker

Status: **done** in agent-hub v0.9.0, 2026-10-12 (tasks 1–6 merged through `agent-hub ready`; decisions D-083..D-089). Open: a live `codex exec` run after 2026-11-07 and a gwen run when the Mac host is up (`docs/follow-ups.md`).

## Goal

agy, codex, claude and gwen (opencode) workers get the same worker skills — including `telaf` and `aspice` — through
each tool's official mechanism, so domain knowledge reaches every worker (D-019, D-077).

## Spike: what each host really does (task 1)

Method: one canary plugin (`skills/spike-canary/SKILL.md` whose body holds a secret word) built in a scratch
marketplace, installed into an **isolated** home per tool (`CODEX_HOME=…`, `HOME=…` for agy, `--plugin-dir` for
Claude, `OPENCODE_CONFIG` + env for opencode), then asked "what is the canary word?" headless. The real tool
configurations were not changed (checked afterwards: codex has only `openai-curated`, agy only `superpowers`).
Versions: codex-cli 0.152.1, agy 1.3.3, opencode 1.18.35, Claude Code 2.1.296.

### Claude Code headless worker (`claude -p`)

- The worker runs with `--setting-sources project,local` (D-065). **No user-scope plugin reaches it**: asked for its
  skills, a worker listed only the built-ins — no `superpowers`, no `agent-hub`, no canary. So today no Claude worker
  has TDD/debugging/telaf skills, whatever is installed for the director.
- `--plugin-dir <path>` (repeatable; a folder of plugins loads each child) loads a plugin for that session only and
  works together with `--setting-sources project,local`: the worker listed `canary:spike-canary` and answered the
  canary word (Haiku). Skill names are namespaced `<plugin>:<skill>`.
- Caveat for tests: `--allowedTools` is variadic and swallows a trailing prompt argument; the adapter passes the brief
  on stdin, so it is not affected.

### Codex (`codex exec`)

- Plugin: `.codex-plugin/plugin.json` with `"skills": "./skills/"` (plus `name`, `version`, `description`, `author`,
  `license`, `interface{displayName, shortDescription, developerName, category, capabilities}`), the same `skills/`
  tree as Claude.
- Marketplace: `<root>/.agents/plugins/marketplace.json` — `{"name", "interface": {"displayName"}, "plugins": [{"name",
  "source": {"source": "local", "path": "./plugins/<p>"}, "policy": {"installation": "AVAILABLE", "authentication":
  "ON_INSTALL"}, "category"}]}`.
- `codex plugin marketplace add <path>` writes `[marketplaces.<m>] source_type = "local"` to `config.toml`;
  `codex plugin add <p>@<m>` copies a **snapshot** to `$CODEX_HOME/plugins/cache/<m>/<p>/<version>/` and writes
  `[plugins."<p>@<m>"] enabled = true`. Running `add` again refreshes the snapshot even at the same version.
  `codex plugin list -m <m> [--json]` shows `installed, enabled` and the version; `codex plugin remove` undoes it.
- A `local` source must lie **inside** the marketplace root: an absolute path, `../x` and a git `file://` source were
  silently dropped (`plugin … was not found in marketplace`); a symlink inside the root and `"path": "./"` (plugin at
  the marketplace root) both work.
- Headless loading: `codex debug prompt-input "<prompt>"` renders the model-visible prompt without calling a model; it
  contains `spike-canary: <description> (file: …/canary/0.0.1/skills/spike-canary/SKILL.md)`. A live `codex exec` run
  was blocked by the monthly quota (back 2026-11-07) — repeat it then.
- Codex also reads `~/.agents/skills/` and the repository's `.agents/skills/`. This machine already has hand-copied,
  old `~/.codex/skills/telaf-legato` and `telaf-api` that would duplicate the new `telaf`.

### agy (`agy -p`)

- Plugin: a directory with **`plugin.json` at its root** (`name`, `description`, `version`) and `skills/`;
  `agents/`, `commands/`, `mcpServers`, `hooks` are picked up if present. `.claude-plugin/plugin.json` alone fails
  `agy plugin validate` (`missing plugin.json`). `telaf`, `aspice`, `token-audit` validate once a root `plugin.json`
  exists.
- `agy plugin install <path>` copies a snapshot to `~/.gemini/config/plugins/<name>/` and records it in
  `~/.gemini/config/import_manifest.json`; installing again refreshes it. `agy plugin list` prints JSON;
  `uninstall/enable/disable` exist; `plugin@marketplace` targets exist but were not needed.
- Headless loading: `agy --model gemini-3.8-flash-low --mode plan -p "…"` listed `spike-canary` and answered the canary
  word. agy also reads the repository's `.agents/skills/`.
- agy imports a plugin's `agents/` too: the agent-hub plugin would bring its five Claude role agents (`model: haiku`)
  into agy, where they mean nothing.
- A `HOME=<dir>` override isolates agy for tests and its login still worked.

### opencode (gwen, `opencode run`)

- No plugin store for skills (`opencode plugin` installs npm JS modules). Skills are discovered from: built-in,
  `~/.claude/skills/**` **recursively**, `~/.agents/skills/`, the repository's `.claude/skills/`, `.agents/skills/`,
  `.opencode/skills/`, and the config key `skills.paths`. Claude Code plugins are not read.
- Today a gwen worker sees **40** skills: 17 from `~/.agents/skills` (Runpod), 15 claude.ai-synced skills under
  `~/.claude/skills/synced/`, and 7 **deleted** skills under `~/.claude/skills/.trash/` — all in the index sent to a
  9B model whose context is already the limit on large repositories (D-079).
- `OPENCODE_DISABLE_CLAUDE_CODE_SKILLS=1` drops the `.claude` sources; `OPENCODE_DISABLE_EXTERNAL_SKILLS=1` drops the
  `.claude` and `.agents` sources (user and repository), leaving built-in, `.opencode/skills/` and `skills.paths`.
- Headless loading: with `skills.paths` in `OPENCODE_CONFIG` and `OPENCODE_DISABLE_EXTERNAL_SKILLS=1`, `opencode run`
  called its `skill` tool on `spike-canary` and answered the word. Run with the free `opencode/ling-3.1-flash-free`
  model because the gwen host did not answer at 02:50 UTC; repeat on gwen when the Mac is up.

### Summary

| Host | Plugin manifest | How skills reach a headless worker | Update | Verified headless |
|---|---|---|---|---|
| claude worker | `.claude-plugin/plugin.json` (exists) | `--plugin-dir` per run (user plugins never load, D-065) | none — reads the source | yes (Haiku) |
| codex | `.codex-plugin/plugin.json` + `.agents/plugins/marketplace.json` | global install, snapshot in `$CODEX_HOME/plugins/cache` | `codex plugin add` again | prompt-input yes; live run after 2026-11-07 |
| agy | root `plugin.json` | global install, snapshot in `~/.gemini/config/plugins` | `agy plugin install` again | yes (Gemini Flash) |
| opencode | none | `skills.paths` in the per-run `OPENCODE_CONFIG` + `OPENCODE_DISABLE_EXTERNAL_SKILLS=1` | none — reads the source | yes (free model); gwen pending |

One `skills/` tree per plugin serves all four; only the manifests differ.

## Design consequences

1. **Two delivery styles (D-083).** codex and agy are *installed* hosts: their official plugin commands put a snapshot
   in their global store, so `agent-hub hub install|status` drives `codex plugin marketplace add/add/list` and
   `agy plugin install/list`. claude and gwen are *per-run* hosts: their adapters already build an isolated
   invocation, so they get skills by flag (`--plugin-dir`) or by the generated config (`skills.paths` + the disable
   flag) — nothing to install, nothing stale, and user-level noise stays out.
2. **Worker skills are not director skills (D-084).** `orchestrate` and `principle-*` tell an agent to delegate and
   never code; a worker that loads them could try to orchestrate. Workers get domain skills (`telaf`, `aspice`) and
   process skills (TDD, systematic debugging); `token-audit` stays Claude-only (it reads `/context`). The agent-hub
   plugin is therefore not installed into codex or agy, which also removes the codex "source outside the marketplace"
   problem and agy importing Claude role agents.
3. **The worker skill list lives in agent-hub's config (D-085):** a user-level list of worker plugin directories
   (default: the agent-plugins `telaf`, `aspice`, plus a process-skill source chosen in task 5), and a repository's
   `.agent-hub/config.toml` may add or restrict per repo (telaf only matters in TelAF repos). Per-run hosts honour the
   per-repo list exactly; installed hosts carry the union globally and rely on skill descriptions to stay idle (one
   index line each) — the same trade-off codex and agy make for every plugin.
4. **gwen's skill index is cleaned now, not later (D-086):** the gwen adapter sets `OPENCODE_DISABLE_EXTERNAL_SKILLS=1`
   so deleted, synced and Runpod skills stop reaching a 9B model; worker skills come back only through `skills.paths`.

## Tasks 2–6, revised (all merged; plus `claude-quota`, `review-fallback`, `agy-review-scope` found on the way, D-088/D-089)

| # | Task | Tier | Done means |
|---|---|---|---|
| 2 | Codex + agy manifests for `telaf` and `aspice` in agent-plugins: `.codex-plugin/plugin.json`, root `plugin.json`, `.agents/plugins/marketplace.json` | `implement` | `agy plugin validate` ok for both; `codex plugin add` into a temp `CODEX_HOME` installs both; tests check every manifest's name/version/description match `.claude-plugin/plugin.json` and point at the existing `skills/` |
| 3a | Per-run delivery: worker plugin list in agent-hub config; claude adapter adds `--plugin-dir` per entry; gwen adapter writes `skills.paths` and sets `OPENCODE_DISABLE_EXTERNAL_SKILLS=1` | `implement` | adapter unit tests on argv/env/config; a canary skill-test run on claude:haiku answers the word |
| 3b | `agent-hub hub install|status` for codex and agy with their official commands; dry-run by default, `--apply` to change; status reports installed version vs source version and flags stray copies (`~/.codex/skills/telaf-*`) without deleting them | `implement-hard` | tests with fake `codex`/`agy`; one real `--apply` into isolated homes; `status` on the real machine read-only |
| 4 | Brief footer names the skills relevant to the task (per-run hosts get exactly them) | `implement` | skill-test run per host shows the named skill loaded |
| 5 | Anti-rationalization table, debugging playbook; choose the process-skill source for workers (superpowers already ships codex/opencode/agy manifests; agent-skills after a license/content read) | `implement` | as in next-phase.md |
| 6 | agy review adapter fix (unchanged) | `implement` | a smoke review on agy returns a verdict |

Open: repeat the codex live run after 2026-11-07 and the gwen run when the Mac is up; check whether agy's
`plugin@marketplace` can install from the agent-plugins repo directly (would let `hub` use one marketplace for both).
