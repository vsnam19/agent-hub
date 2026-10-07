#!/usr/bin/env bash
# Real-agent smoke test. Run by hand only (`just smoke`); it spends quota and is never run by tests.
set -euo pipefail

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

repo="$tmp/repo"
git init -q -b main "$repo"
git -C "$repo" config user.email smoke@example.invalid
git -C "$repo" config user.name smoke
git -C "$repo" config commit.gpgsign false
git -C "$repo" config tag.gpgsign false
printf '# smoke\n' >"$repo/README.md"
git -C "$repo" add README.md
git -C "$repo" commit -q -m "chore: init"
base_sha="$(git -C "$repo" rev-parse HEAD)"
git -C "$repo" checkout -q -b feat/smoke

cat >"$tmp/brief.md" <<'BRIEF'
## Goal

Add an `add(a, b)` function and a test for it.

## Input

The repository has only a README.md. There is no existing code.

## Expected output

- `calc.py` defines `add(a, b)` returning the sum.
- `test_calc.py` has a test `test_add` that asserts `add(2, 3) == 5`.
- Running `python3 -m pytest -q` passes.

## Constraints

No dependencies besides pytest. Every test needs a real assertion.

## Gate

Run `python3 -m pytest -q` and make it pass before finishing.
BRIEF

first_model() {
    local agent="$1"
    agent-hub pool --json | python3 -c '
import json, sys
agent = sys.argv[1]
for entry in json.load(sys.stdin)["roles"]["implement"]:
    name, _, model = entry["link"].partition(":")
    if name == agent:
        print(model)
        break
' "$agent"
}

for agent in agy codex claude; do
    if ! command -v "$agent" >/dev/null 2>&1; then
        echo "$agent not installed, skipped"
        continue
    fi
    upper="$(printf '%s' "$agent" | tr '[:lower:]' '[:upper:]')"
    var="SMOKE_${upper}_MODEL"
    model="${!var:-$(first_model "$agent")}"
    if [ -z "$model" ]; then
        echo "$agent has no link in the implement chain, skipped"
        continue
    fi

    git -C "$repo" reset -q --hard "$base_sha"
    git -C "$repo" clean -fdq

    impl="$(agent-hub run --role implement --agent "$agent:$model" --brief "$tmp/brief.md" -d "$repo" --json |
        python3 -c 'import json, sys; print(json.load(sys.stdin)["status"])' || true)"

    git -C "$repo" add -A
    git -C "$repo" commit -q -m "feat: smoke change" --allow-empty

    rev="$(agent-hub run --role review --agent "$agent:$model" -d "$repo" --base main --json |
        python3 -c 'import json, sys; print(json.load(sys.stdin)["status"])' || true)"

    echo "$agent implement=${impl:-unknown} review=${rev:-unknown}"
done
