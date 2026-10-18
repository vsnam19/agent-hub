#!/usr/bin/env python3
"""3-way merge of one Python module by top-level definition, for an in-progress `git merge` (MERGE_HEAD set).

Usage: astmerge.py <path>. Keys are a def/class name, an assignment target, or the statement text. A key changed on
one side only takes that side; changed identically on both sides is fine; changed differently on both sides aborts
with the key names (the director resolves those). Order follows ours, with theirs-only keys placed after their
predecessor in theirs. `from dataclasses import ...` lines are united.
"""

import ast
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agenthub.guards import _git_argv, _git_env  # noqa: E402


def _show(ref: str, path: str, cwd: Path | None = None) -> str:
    r = subprocess.run(
        _git_argv("show", f"{ref}:{path}"),
        cwd=cwd,
        capture_output=True,
        text=True,
        env=_git_env(),
    )
    return r.stdout if r.returncode == 0 else ""


def _chunks(src: str) -> list[tuple[str, str]]:
    if not src:
        return []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    lines = src.splitlines(keepends=True)
    out = []
    for i, n in enumerate(tree.body):
        start = n.decorator_list[0].lineno if getattr(n, "decorator_list", None) else n.lineno
        while start > 1 and lines[start - 2].lstrip().startswith("#"):
            start -= 1
        text = "".join(lines[start - 1 : n.end_lineno]).rstrip("\n")
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            key = n.name
        elif isinstance(n, (ast.Assign, ast.AnnAssign)):
            target = n.targets[0] if isinstance(n, ast.Assign) else n.target
            key = getattr(target, "id", text)
        elif isinstance(n, ast.ImportFrom) and n.module == "dataclasses":
            key = "import:dataclasses"
        elif isinstance(n, ast.Expr) and i == 0:
            key = "docstring"
        else:
            key = text
        out.append((key, text))
    return out


def _dataclasses_import(*texts: str | None) -> str:
    names: set[str] = set()
    for text in texts:
        if text:
            names |= {s.strip() for s in text.split("import", 1)[1].split(",")}
    return "from dataclasses import " + ", ".join(sorted(names))


def _is_unnamed(key: str, text: str) -> bool:
    if key in ("import:dataclasses", "docstring"):
        return False
    if key.startswith(("import ", "from ")):
        return False
    if key != text:
        return False
    return True


def _unattached_comments(src: str) -> list[str]:
    if not src:
        return []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    lines = src.splitlines(keepends=True)
    covered: set[int] = set()
    for n in tree.body:
        start = n.decorator_list[0].lineno if getattr(n, "decorator_list", None) else n.lineno
        while start > 1 and lines[start - 2].lstrip().startswith("#"):
            start -= 1
        for lineno in range(start, n.end_lineno + 1):
            covered.add(lineno)
    unattached = []
    for i, line in enumerate(lines, start=1):
        if i not in covered and line.lstrip().startswith("#"):
            unattached.append(line.strip())
    return unattached


def _duplicate_keys(keys: list[str]) -> list[str]:
    seen = set()
    dups = set()
    for k in keys:
        if k in seen:
            dups.add(k)
        seen.add(k)
    return sorted(dups)


def astmerge_file(path: str, cwd: Path | None = None, print_summary: bool = False) -> tuple[bool, list[str]]:
    """3-way merge of one Python module by top-level definition during an in-progress git merge."""
    if cwd is None:
        cwd = Path(".")

    try:
        path_p = Path(path)
        if path_p.is_absolute():
            path_str = str(path_p.resolve().relative_to(cwd.resolve()))
        else:
            path_str = str(path_p)
    except ValueError:
        path_str = Path(path).name

    base_proc = subprocess.run(
        _git_argv("merge-base", "HEAD", "MERGE_HEAD"),
        cwd=cwd,
        capture_output=True,
        text=True,
        env=_git_env(),
    )
    if base_proc.returncode != 0:
        return False, [path_str]
    base_ref = base_proc.stdout.strip()

    base_src = _show(base_ref, path_str, cwd=cwd)
    ours_src = _show("HEAD", path_str, cwd=cwd)
    theirs_src = _show("MERGE_HEAD", path_str, cwd=cwd)

    base_list = _chunks(base_src)
    ours_list = _chunks(ours_src)
    theirs_list = _chunks(theirs_src)

    if not ours_list and ours_src.strip():
        return False, [path_str]
    if not theirs_list and theirs_src.strip():
        return False, [path_str]

    ours_keys = [k for k, _ in ours_list]
    theirs_keys = [k for k, _ in theirs_list]
    base_keys = [k for k, _ in base_list]

    dups = sorted(set(_duplicate_keys(ours_keys) + _duplicate_keys(theirs_keys) + _duplicate_keys(base_keys)))
    if dups:
        return False, dups

    if _unattached_comments(ours_src) != _unattached_comments(theirs_src):
        return False, [path_str]

    ours, theirs = dict(ours_list), dict(theirs_list)
    base = dict(base_list)

    ours_unnamed = {k: v for k, v in ours_list if _is_unnamed(k, v)}
    theirs_unnamed = {k: v for k, v in theirs_list if _is_unnamed(k, v)}
    base_unnamed = {k: v for k, v in base_list if _is_unnamed(k, v)}

    base_unnamed_removed_or_changed = {
        k: v for k, v in base_unnamed.items() if (k not in ours or ours[k] != v) and (k not in theirs or theirs[k] != v)
    }

    ours_only = set(ours.keys()) - set(base.keys()) - set(theirs.keys())
    theirs_only = set(theirs.keys()) - set(base.keys()) - set(ours.keys())

    has_unnamed_ours_only = any(k in ours_unnamed for k in ours_only)
    has_unnamed_theirs_only = any(k in theirs_unnamed for k in theirs_only)

    if (has_unnamed_ours_only or has_unnamed_theirs_only) and base_unnamed_removed_or_changed:
        return False, [path_str]
    if has_unnamed_ours_only and has_unnamed_theirs_only:
        return False, [path_str]

    oorder = [k for k, _ in ours_list]
    torder = [k for k, _ in theirs_list]

    conflicts: list[str] = []
    result: dict[str, str] = {}
    for k in dict.fromkeys(oorder + torder):
        if k == "import:dataclasses":
            result[k] = _dataclasses_import(ours.get(k), theirs.get(k))
            continue
        b, o, t = base.get(k), ours.get(k), theirs.get(k)
        if o == t:
            v = o
        elif o == b:
            v = t
        elif t == b:
            v = o
        else:
            conflicts.append(k)
            continue
        if v is not None:
            result[k] = v

    if conflicts:
        return False, conflicts

    order = [k for k in oorder if k in result]
    for i, k in enumerate(torder):
        if k in result and k not in order:
            prev = next((p for p in reversed(torder[:i]) if p in order), None)
            order.insert(order.index(prev) + 1 if prev else 0, k)

    def is_import(k: str) -> bool:
        return k.startswith(("import ", "from ")) or k == "import:dataclasses"

    def is_def(k: str) -> bool:
        return result[k].lstrip().startswith(("def ", "class ", "@", "async def ", "#"))

    out: list[str] = []
    for i, k in enumerate(order):
        if i:
            p = order[i - 1]
            plain = not (is_def(k) or is_def(p) or is_import(p) or is_import(k))
            out.append("" if (is_import(k) and is_import(p)) or plain else "\n\n")
        out.append(result[k] + "\n")

    target = cwd / path_str
    target.write_text("".join(out), encoding="utf-8")
    if print_summary:
        print("merged", path_str, len(order), "top-level items")
    return True, []


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
