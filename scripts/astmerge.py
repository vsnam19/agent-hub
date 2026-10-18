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


def _show(ref: str, path: str) -> str:
    r = subprocess.run(["git", "show", f"{ref}:{path}"], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def _chunks(src: str) -> list[tuple[str, str]]:
    if not src:
        return []
    lines = src.splitlines(keepends=True)
    out = []
    for i, n in enumerate(ast.parse(src).body):
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


def merge(path: str) -> None:
    base_ref = subprocess.run(
        ["git", "merge-base", "HEAD", "MERGE_HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    base = dict(_chunks(_show(base_ref, path)))
    ours_list = _chunks(_show("HEAD", path))
    theirs_list = _chunks(_show("MERGE_HEAD", path))
    ours, theirs = dict(ours_list), dict(theirs_list)
    oorder = [k for k, _ in ours_list]
    torder = [k for k, _ in theirs_list]

    conflicts, result = [], {}
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
        sys.exit("both sides changed: " + ", ".join(conflicts))

    order = [k for k in oorder if k in result]
    for i, k in enumerate(torder):
        if k in result and k not in order:
            prev = next((p for p in reversed(torder[:i]) if p in order), None)
            order.insert(order.index(prev) + 1 if prev else 0, k)

    def is_import(k: str) -> bool:
        return k.startswith(("import ", "from ")) or k == "import:dataclasses"

    def is_def(k: str) -> bool:
        return result[k].lstrip().startswith(("def ", "class ", "@", "async def ", "#"))

    out = []
    for i, k in enumerate(order):
        if i:
            p = order[i - 1]
            plain = not (is_def(k) or is_def(p) or is_import(p) or is_import(k))
            out.append("" if (is_import(k) and is_import(p)) or plain else "\n\n")
        out.append(result[k] + "\n")
    with open(path, "w") as f:
        f.write("".join(out))
    print("merged", path, len(order), "top-level items")


if __name__ == "__main__":
    merge(sys.argv[1])
