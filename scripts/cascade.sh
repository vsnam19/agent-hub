#!/bin/bash
# cascade.sh <slug> <task> <dep-task>... : merge fixed dependency branches orch/<slug>/<dep> into the work tree of
# orch/<slug>/<task> (.worktrees/<slug>-<task>). Python conflicts are resolved per top-level definition by
# astmerge.py; any other conflict, or a definition changed on both sides, aborts the merge (exit 2) for the director.
here=$(cd "$(dirname "$0")" && pwd)
slug=$1; t=$2; shift 2
cd "$(git -C "$here" rev-parse --show-toplevel)/.worktrees/$slug-$t" || exit 1
[ -z "$(git status --porcelain)" ] || { echo "$t dirty"; exit 1; }
for dep in "$@"; do
  if git merge-base --is-ancestor "orch/$slug/$dep" HEAD; then echo "$t: $dep already in"; continue; fi
  if ! git -c commit.gpgsign=false merge --no-ff --no-commit "orch/$slug/$dep" >/dev/null 2>&1; then
    for f in $(git diff --name-only --diff-filter=U); do
      case $f in
        *.py) python3 "$here/astmerge.py" "$f" || { echo "$t: astmerge failed on $f"; git merge --abort; exit 2; } ;;
        *) echo "$t: conflict in $f"; git merge --abort; exit 2 ;;
      esac
      uv run ruff check --fix --select I -q "$f"; uv run ruff format -q "$f"; git add "$f"
    done
  fi
  uv run ruff check . -q || { echo "$t: lint failed after $dep"; exit 3; }
  git -c commit.gpgsign=false commit -q -m "Merge orch/$slug/$dep (review fixes) into $t" && echo "$t: merged $dep"
done
