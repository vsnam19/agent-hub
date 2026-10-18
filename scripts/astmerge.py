#!/usr/bin/env python3
"""3-way merge of one Python module by top-level definition, for an in-progress `git merge` (MERGE_HEAD set).

Usage: astmerge.py <path>. Keys are a def/class name, an assignment target, or the statement text. A key changed on
one side only takes that side; changed identically on both sides is fine; changed differently on both sides aborts
with the key names (the director resolves those). Order follows ours, with theirs-only keys placed after their
predecessor in theirs. `from dataclasses import ...` lines are united.
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agenthub.cascade import astmerge_file  # noqa: E402
from agenthub.guards import _git_argv, _git_env  # noqa: E402


def merge(path: str) -> None:
    res = subprocess.run(
        _git_argv("rev-parse", "-q", "--verify", "MERGE_HEAD"),
        cwd=Path("."),
        capture_output=True,
        text=True,
        env=_git_env(),
    )
    if res.returncode != 0:
        print("no merge in progress")
        sys.exit(2)

    ok, conflicts = astmerge_file(path, cwd=Path("."), print_summary=True)
    if not ok:
        sys.exit("both sides changed: " + ", ".join(conflicts))


if __name__ == "__main__":
    merge(sys.argv[1])
