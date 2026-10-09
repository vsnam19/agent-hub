# gwen — a local-model coding agent on the MacBook Air

Status: approved (autonomous mode). Decisions D-039..D-048. Builds on the v1 spec and v0.3 (handoffs, gates, plans).

## 0. Purpose

**(user)** gwen is an **experiment** (D-039): measure how far a small local model gets as a coding agent driven by
the orchestrator, using the same brief → job → gate → verify loop as agy/codex/claude. Success means:

1. a reproducible measurement — which harness × model passes which tasks, how fast, with how much memory — that can be
   re-run whenever a new small model appears;
2. `agent-hub run --agent gwen:<model>` works end to end with the winning harness, so the orchestrator can hand gwen
   real tasks and verify them like any other agent.

Not goals: replacing the cloud agents, putting gwen in the default role chains, serving several users, privacy
guarantees for code (the prompt carries code to the Mac over Tailscale; nothing else leaves the LAN/tailnet).

## 1. Facts about the host (checked over SSH on 2026-10-09)

| Item | Value |
|---|---|
| Machine | MacBook Air, Apple M2 (8 cores), 16 GB unified memory, fanless |
| OS | macOS 27.0.1, `iogpu.wired_limit_mb` = 0 (default ≈ 70–75 % of RAM for the GPU) |
| Disk | 35 GB free of 228 GB |
| Network | Tailscale `sammys-macbook-air` = `100.86.30.90`; SSH as `sammy` with the zbook's key |
| Power | on AC, sleep prevented; on battery or lid closed it sleeps |
| Existing | Ollama.app 0.40.2 (127.0.0.1 only, no local models); `uv` in `~/.local/bin`; system Python 3.9 |
| Already downloaded | nothing usable — `Qwen3-14B-MLX-4bit` (4 MB) and `Qwen3-8B-MLX-4bit` (388 MB) are partial downloads |
| Daily use | Chrome, Spotify etc. hold ~10 GB; free pages ≈ 70 MB before any model is loaded |

## 2. Model choice (D-040)

The request named Qwen3-14B 4-bit ("14 GB"). In MLX 4-bit it is ≈ 8.3 GB of weights, and every layer keeps a full KV
cache (≈ 160 KB/token at fp16), so only ≈ 16k tokens of context fit beside it — less than a coding agent's system
prompt plus a few files. The bake-off measures three models:

| Model (Rapid-MLX alias) | Why | Peak RSS (Rapid-MLX table) |
|---|---|---|
| `qwen3.5-4b-4bit` | Rapid-MLX's pick for 16 GB; fastest; most room for daily apps | ≈ 6.0 GB |
| `qwen3.5-9b-4bit` | expected best quality that fits; Qwen3.5 hybrid attention (3 Gated DeltaNet : 1 full attention layer) keeps the KV cache ≈ 4× smaller, so 32k+ context is realistic | ≈ 8.7 GB |
| `Qwen/Qwen3-14B-MLX-4bit` (Rapid-MLX 0.15.7 has no alias) | the user's original choice, as the reference point | ≈ 9–11 GB |

Total download ≈ 17 GB, within the 35 GB free. KV cache stays bf16: Rapid-MLX 0.15.7 refuses KV quantisation for
Qwen3.5's hybrid (ArraysCache) layers, and their cache is small anyway. Expected
generation on an M2 (100 GB/s): ≈ 20–25 tok/s for 4B, ≈ 12–15 for 9B, ≈ 8–10 for 14B; prefill a few hundred tok/s,
which is why the prefix cache matters (the system prompt is re-sent every turn).

Rejected: Qwen3.6/3.8 (smallest is 27B), gpt-oss-20b (≈ 12 GB, no headroom), any 8-bit quant.

## 2a. Measured on 2026-10-09 (qwen3.5-4b-4bit, Linux → Mac over Tailscale)

Tool call parsed correctly (hermes parser); first request 11 s including model load; generation ≈ 26 tok/s; prefill
9.4k tokens in 52 s (≈ 180 tok/s) — a cold 10k-token agent prompt costs ~50 s per turn unless the prefix cache hits;
the Wi-Fi LAN address (`192.168.0.109:8642`) refuses connections.

## 3. Serving on the Mac (D-041, D-042)

**Server: Rapid-MLX** (`uv tool install rapid-mlx`), not `mlx_lm.server` and not Ollama. Rapid-MLX serves
`/v1/chat/completions`, `/v1/responses` and Anthropic `/v1/messages` from MLX, with 27 tool-call parsers, a radix prefix
cache, KV quantisation and `--api-key`, and is tested per release against Claude Code, Codex, Aider, OpenCode and Qwen
Code. That lets every candidate harness, including the existing `codex` and `claude` CLIs, talk to the same server.
`mlx_lm.server` offers only chat completions, does not expose `--kv-bits`, and had cross-request KV contamination
(ml-explore/mlx-lm#965). Ollama is MLX-capable on the Mac too, but the user asked for MLX directly and Rapid-MLX's
tool-call parsing is its stated focus.

**Service** (D-042): a launchd user agent `~/Library/LaunchAgents/dev.vsn.gwen.plist` runs
the wrapper `~/.local/bin/gwen-serve` → `rapid-mlx serve <model> --host 100.86.30.90 --port 8642
--lazy-load --idle-unload-seconds 1800` (unloads after 30 idle
minutes, giving the memory back to daily apps), `KeepAlive`
true, logs to `~/Library/Logs/gwen/`. The model to serve is the first line of `~/.config/gwen/model`; switching models
= write the file, `launchctl kickstart -k gui/$UID/dev.vsn.gwen`. One model loaded at a time.

`scripts/gwen-host.sh` in agent-hub (run from Linux) does install / switch-model / status / stop over SSH, so the
setup is reproducible and never needs a human at the Mac after the SSH key is in place.

## 4. Network and security (D-043)

- Bind to the Tailscale address only, never `0.0.0.0` or the Wi-Fi address: devices on the Wi-Fi LAN cannot reach the
  port. Traffic is WireGuard-encrypted end to end. If Tailscale is not up yet, the bind fails and launchd retries.
- API key: 32 random bytes, stored in `~/.config/gwen/api-key` (mode 600) on both machines; the server reads it
  from `RAPID_MLX_API_KEY` (set by `gwen-serve`, never on argv); agent-hub passes it to the harness via
  `env_passthrough` (it would otherwise be dropped by D-014). Not in fnox: a new fnox secret must be declared in
  vsndevenv's seed config, too heavy for a key that only guards a tailnet inference endpoint.
- The Mac only does inference. It never receives a work tree and never runs tools; the harness and every tool call run
  on Linux under agent-hub's existing guards. A prompt-injected model can therefore do no more on the Mac than return
  text.
- Latency: Tailscale over the same LAN adds a few ms per request, negligible next to generation; responses stream.

## 5. Availability (D-044)

The Mac is a personal laptop: it sleeps on battery or lid close, and daily apps compete for memory.

- The `gwen` adapter does a preflight `GET /v1/models` (3 s timeout, with the API key) before starting the harness.
  Unreachable, 401, or the requested model not being the loaded one → `Outcome.MODEL_UNAVAILABLE` with a clear detail
  (`gwen host unreachable`, `gwen serves qwen3.5-4b-4bit, not …`). By D-008 the job falls through to the next link or
  stops; no cooldown is set, so it works again as soon as the Mac is awake.
- The adapter never switches the served model itself (that would evict another user's work and take minutes); the
  orchestrator does it with `scripts/gwen-host.sh switch <model>`.
- No wake-on-LAN, no `caffeinate` from Linux: keeping the laptop awake is the owner's call.
- Memory pressure is measured in the bake-off (§7), not prevented; the report tells which model is usable with daily
  apps open.

## 6. Where code runs and how it is verified (D-045)

Exactly like the other agents: the work tree is a git worktree on Linux, the harness runs on Linux in it, gates run on
Linux via `--gate`, the gate guard (D-038) applies, and the orchestrator verifies the diff and runs the result on real
data. Nothing about verification is gwen-specific. gwen's `profile.md` records the habits the bake-off finds.

## 7. Bake-off (D-046)

`bench/gwen/` in agent-hub — a small runner, a task suite, and results.

### 7.1 Harness candidates (all run on Linux, installed with `uv tool` / `npm -g`, outside vsndevenv until one wins)

| Harness | Talks | Why it is a candidate |
|---|---|---|
| Qwen Code (`qwen -p … --yolo`) | chat completions | Gemini-CLI fork tuned for Qwen tool formats |
| OpenCode (`opencode run`) | chat completions | popular, scriptable, provider config in JSON |
| Aider (`aider --message-file … --yes-always`) | chat completions, edit formats instead of tool calls | known to work best with weak models; tiny prompt |
| mini-swe-agent (`mini -t … -y`) | chat completions via litellm, bash only | ~100-line loop, smallest prompt; stands in for "write our own" |
| Codex CLI (`codex exec` with a `gwen` provider, `wire_api = "responses"`) | Responses | already an agent-hub adapter |
| Claude Code (`claude -p` with `ANTHROPIC_BASE_URL`, isolated `CLAUDE_CONFIG_DIR`) | Anthropic messages | already an adapter; large prompt, likely context-bound — worth knowing |

Each harness gets an isolated config/home directory under the run directory, never the user's real config.

### 7.2 Task suite

A fixture Python package (`bench/gwen/fixture/`, ≈ 300 lines, pytest + ruff, no network) and six tasks, each a brief
in agent-hub's brief format plus **hidden tests** the agent never sees:

| ID | Size | Task |
|---|---|---|
| t1 | 1 file | fix an off-by-one in a pagination helper (visible failing test) |
| t2 | 1 file + test | add a function from a precise spec and write its tests |
| t3 | 3 files | rename a function and its call sites, keep behaviour |
| t4 | 2 files + test | add a CLI flag end to end |
| t5 | debug | a bug reported only as a traceback in the brief |
| t6 | 4–5 files | a small feature needing a new module, wiring and tests |

Gate per run: `ruff check`, the visible tests, then the hidden tests copied in after the agent finishes. Pass =
all three green and no file outside the task's allowed paths changed.

### 7.3 Runner

`bench/gwen/run.py --harness <h> --model <m> --task <t> [--repeat n]` copies the fixture into a fresh temp git repo,
runs the harness command template with a 20-minute timeout, applies the gate, and appends one JSON line to
`bench/gwen/results/<date>.jsonl`: harness, model, task, pass, gate details, wall time, prompt/completion/cached tokens
(recorded by a small local proxy between the harness and the Mac, which reads `usage` from every
response — harness-agnostic), tool-call parse errors seen, and the Mac's memory
(`memory_pressure` and swap used, sampled over SSH every 10 s). `bench/gwen/report.py` turns the JSONL into a
markdown table. Stages, to keep machine time bounded:

- **A** — every harness × `qwen3.5-9b-4bit` × t1, t2, t3 (18 runs).
- **B** — the best two harnesses × three models × all six tasks, 2 repeats (72 runs, overnight).

Winner = highest pass rate on stage B; ties broken by wall time. The report goes to
`docs/gwen-bakeoff-<date>.md` with the decision recorded as a D-entry.

## 8. The `gwen` adapter (D-047)

Built only after stage B, for the winning harness — the bake-off runner proves the command line first.

- `agenthub/agents/gwen/{__init__.py,agent.toml,profile.md}`; `binary` = the winning harness's executable;
  `env_passthrough = ["GWEN_API_KEY"]`; base URL from `GWEN_BASE_URL`, default `http://100.86.30.90:8642`.
- Model names in the pool are Rapid-MLX aliases: `gwen:qwen3.5-9b-4bit`.
- `build()` = preflight (§5) + the harness command proven by the runner, with the harness's config pointed at a
  per-attempt directory; `classify()` maps connection errors → `MODEL_UNAVAILABLE`, context-length errors → `FAILED`
  with detail `context exceeded`, an empty reply → `EMPTY`.
- Review role: only if the winning harness has a real read-only mode; otherwise `review` is refused for gwen.
- Pool (D-048): gwen is **not** added to the default `implement`/`review` chains. It is used with `--agent gwen:<model>`
  or a user `pool.toml` override. Its default implement timeout is 90 min.

## 9. Delivery

1. Host setup over SSH (orchestrator, operational): install Rapid-MLX, download the three models, API key, launchd
   agent, smoke a chat completion and one tool call from Linux.
2. Plan task "bench" (agy via agent-hub): fixture, six tasks with hidden tests, runner, report, `gwen-host.sh`; tests
   with fake harnesses and a fake server, as D-016 requires.
3. Orchestrator runs stage A, then stage B; writes the report and the winning D-entry.
4. Plan task "adapter" (agy): the `gwen` adapter for the winner, with fake-server tests; `just smoke` gains an optional
   gwen case; README, overview.
5. Real end-to-end: one real `agent-hub run --agent gwen:<model>` on an agent-hub issue, verified as usual.

## 10. Risks

| Risk | Mitigation |
|---|---|
| Swap with daily apps open makes runs crawl or the Mac unusable | measured in the bake-off; the 4B model is the fallback; runs scheduled when the Mac is idle |
| Small model loops on tool calls or overflows context | 20 min timeout per run; context errors classified; Aider/mini-swe-agent keep prompts small |
| Rapid-MLX is a young project (≈ 3.9k stars) | the bench only needs OpenAI-compatible chat completions; `mlx_lm.server` is the drop-in fallback for the chat-completions harnesses |
| Fanless M2 throttles on long runs | wall time is part of the measurement; repeats spread over the night |
| The API key leaks into logs | agent-hub never logs env; the runner redacts it from saved commands |
