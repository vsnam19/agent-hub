import ast
import subprocess
from pathlib import Path

from agenthub.guards import _git_argv, _git_env
from agenthub.plan import Plan
from agenthub.planrun import merge_order
from agenthub.planstate import State


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


def _git_identity(worktree: Path) -> list[str]:
    identity = []
    for key, fallback in (("user.name", "agent-hub"), ("user.email", "agent-hub@localhost")):
        res = subprocess.run(
            _git_argv("config", "--get", key),
            cwd=worktree,
            capture_output=True,
            text=True,
            env=_git_env(),
        )
        if res.returncode != 0:
            identity += ["-c", f"{key}={fallback}"]
    return identity


def cascade(plan: Plan, state: State, task_name: str) -> dict[str, str]:
    """Merge the fixed branch of task_name into each done dependent in dependency order."""
    results: dict[str, str] = {}
    fixed_task_state = state.task(task_name)
    fixed_branch = fixed_task_state.branch or f"orch/{plan.slug}/{task_name}"

    task_map = {t.name: t for t in plan.tasks}

    def get_transitive_deps(name: str) -> set[str]:
        deps: set[str] = set()
        stack = list(task_map[name].depends_on) if name in task_map else []
        while stack:
            dep = stack.pop()
            if dep not in deps:
                deps.add(dep)
                if dep in task_map:
                    stack.extend(task_map[dep].depends_on)
        return deps

    ordered = merge_order(plan)
    dependents = [
        t
        for t in ordered
        if t.name != task_name and task_name in get_transitive_deps(t.name) and state.task(t.name).status == "done"
    ]

    for dep in dependents:
        dep_state = state.task(dep.name)
        if not dep_state.worktree:
            continue
        worktree = Path(dep_state.worktree)
        if not worktree.is_dir():
            continue

        merge_cmd = _git_argv(
            *_git_identity(worktree),
            "-c",
            "commit.gpgsign=false",
            "merge",
            "--no-edit",
            fixed_branch,
        )
        res = subprocess.run(merge_cmd, cwd=worktree, capture_output=True, text=True, env=_git_env())
        if res.returncode == 0:
            results[dep.name] = "merged"
            continue

        merge_head_proc = subprocess.run(
            _git_argv("rev-parse", "-q", "--verify", "MERGE_HEAD"),
            cwd=worktree,
            capture_output=True,
            text=True,
            env=_git_env(),
        )
        if merge_head_proc.returncode != 0:
            err_lines = (res.stderr or "").strip().splitlines()
            first_line = err_lines[0].strip() if err_lines else "merge failed"
            results[dep.name] = f"error: {first_line}"
            continue

        diff_cmd = _git_argv("diff", "--name-only", "--diff-filter=U")
        conflicted = subprocess.run(
            diff_cmd, cwd=worktree, capture_output=True, text=True, env=_git_env()
        ).stdout.splitlines()
        conflicted = [f.strip() for f in conflicted if f.strip()]

        for f in conflicted:
            if f.endswith(".py"):
                ok, _ = astmerge_file(f, cwd=worktree)
                if ok:
                    add_cmd = _git_argv("add", f)
                    subprocess.run(add_cmd, cwd=worktree, capture_output=True, text=True, check=True, env=_git_env())

        remaining = subprocess.run(
            diff_cmd, cwd=worktree, capture_output=True, text=True, env=_git_env()
        ).stdout.splitlines()
        remaining = [f.strip() for f in remaining if f.strip()]

        has_merge_head = (
            subprocess.run(
                _git_argv("rev-parse", "-q", "--verify", "MERGE_HEAD"),
                cwd=worktree,
                capture_output=True,
                text=True,
                env=_git_env(),
            ).returncode
            == 0
        )

        if remaining or not has_merge_head:
            if has_merge_head:
                subprocess.run(
                    _git_argv("merge", "--abort"), cwd=worktree, capture_output=True, text=True, env=_git_env()
                )
            results[dep.name] = f"conflict: {', '.join(sorted(remaining))}" if remaining else "conflict: commit failed"
        else:
            commit_cmd = _git_argv(
                *_git_identity(worktree),
                "-c",
                "commit.gpgsign=false",
                "commit",
                "--no-edit",
            )
            commit_res = subprocess.run(commit_cmd, cwd=worktree, capture_output=True, text=True, env=_git_env())
            if commit_res.returncode == 0:
                results[dep.name] = "merged"
            else:
                subprocess.run(
                    _git_argv("merge", "--abort"), cwd=worktree, capture_output=True, text=True, env=_git_env()
                )
                results[dep.name] = "conflict: commit failed"

    return results
