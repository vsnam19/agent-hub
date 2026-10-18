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

    base = dict(_chunks(_show(base_ref, path_str, cwd=cwd)))
    ours_list = _chunks(_show("HEAD", path_str, cwd=cwd))
    theirs_list = _chunks(_show("MERGE_HEAD", path_str, cwd=cwd))

    if not ours_list and _show("HEAD", path_str, cwd=cwd).strip():
        return False, [path_str]
    if not theirs_list and _show("MERGE_HEAD", path_str, cwd=cwd).strip():
        return False, [path_str]

    ours, theirs = dict(ours_list), dict(theirs_list)
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

        if remaining:
            subprocess.run(_git_argv("merge", "--abort"), cwd=worktree, capture_output=True, text=True, env=_git_env())
            results[dep.name] = f"conflict: {', '.join(sorted(remaining))}"
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
