# gwen — local Qwen3.5-9B on the owner's Mac, driven through opencode

Bench findings (D-069):

- Passes small single-file tasks (bug fix, function from a spec, rename, CLI flag) in about 3–6 minutes.
- Gives up when asked to debug from a traceback.
- Runs out of time on multi-file features.
- Context is 32k+; a longer prompt fails with "maximum context length" (reported as `context exceeded`).
- Slow: about 180 tok/s prefill on an M2 Air.
- The Mac loads one model at a time; the model in the chain must be the one currently served.
- The Mac may be asleep or off Tailscale. A preflight (`GET /v1/models`, 3 s) falls through to the next link
  without setting a cooldown.
- Does not review (the preflight refuses the `review` kind).
- The brief is sent as message text: opencode rejects reading files outside its working directory.
- Read-only is best-effort only.

Settings: `$AGENT_HUB_CONFIG/gwen.toml` with `base_url` and `api_key_file`.
