#!/usr/bin/env bash
# Skill test: one fresh headless Claude Code session on a temp repo, asked to get slugify() implemented by the
# agent pool. Spends real quota (Claude and whichever agent runs the job); run by hand only, never from tests.
#
# usage: [PROMPT=...] [SKILL_TEST_OUT=dir] scripts/skill-test/run.sh <label> <with-skill:0|1>
#   PROMPT defaults to prompt-a.txt (names agent-hub); prompt-b.txt does not (tests skill discovery).
# Output: <out>/<label>/{transcript.jsonl,gitlog.txt,gitstatus.txt,stderr.txt}
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
repo_root="$(cd "$here/../.." && pwd)"
label="$1"
with_skill="$2"
out="${SKILL_TEST_OUT:-$(mktemp -d)}"
prompt="${PROMPT:-$(cat "$here/prompt-a.txt")}"

d="$out/$label"
rm -rf "$d"
mkdir -p "$d"
repo="$d/repo"
git init -q -b main "$repo"
cd "$repo"
git config user.name skill-test
git config user.email skill-test@example.invalid
git config commit.gpgsign false
cat >textutil.py <<'PY'
"""Small text helpers."""


def title_case(text: str) -> str:
    return " ".join(w.capitalize() for w in text.split())
PY
cat >test_textutil.py <<'PY'
from textutil import title_case


def test_title_case():
    assert title_case("hello world") == "Hello World"
PY
git add -A
git commit -qm "init"
if [ "$with_skill" = 1 ]; then
    mkdir -p .claude/skills
    ln -s "$repo_root/skills/orchestrate" .claude/skills/orchestrate
    printf '.claude/\n' >.git/info/exclude
fi

env -u CLAUDECODE PATH="$repo_root/.venv/bin:$PATH" \
    timeout 1500 claude -p "$prompt" --model sonnet --permission-mode bypassPermissions \
    --output-format stream-json --verbose >"$d/transcript.jsonl" 2>"$d/stderr.txt" || echo "claude exit $?" >"$d/exit.txt"
git -C "$repo" log --oneline --all >"$d/gitlog.txt"
git -C "$repo" status --short >"$d/gitstatus.txt"
echo "finished $label in $d"
