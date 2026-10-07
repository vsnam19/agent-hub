import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


class GuardError(Exception):
    pass


@dataclass(frozen=True)
class Repo:
    root: Path
    common_dir: Path
    branch: str | None
    default_branch: str


def _git_env() -> dict[str, str]:
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0"}


def _git_argv(*args: str) -> list[str]:
    """Every git argv is built here: repo-configured executables (fsmonitor, hooks) are disabled."""
    return ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", *args]


def _run_git(workdir: Path, *args: str) -> str:
    argv = _git_argv(*args)
    res = subprocess.run(argv, cwd=workdir, capture_output=True, text=True, env=_git_env())
    if res.returncode != 0:
        raise subprocess.CalledProcessError(res.returncode, argv, output=res.stdout, stderr=res.stderr)
    return res.stdout.strip()


def inspect(workdir: Path) -> Repo:
    if not workdir.is_dir():
        raise GuardError("Not a directory")

    try:
        root_str = _run_git(workdir, "rev-parse", "--show-toplevel")
        common_dir_str = _run_git(workdir, "rev-parse", "--git-common-dir")
    except subprocess.CalledProcessError:
        raise GuardError("Not in a git work tree") from None

    root = Path(root_str).resolve()
    # git-common-dir can be relative or absolute
    common_dir_p = Path(common_dir_str)
    if not common_dir_p.is_absolute():
        common_dir = (workdir / common_dir_p).resolve()
    else:
        common_dir = common_dir_p.resolve()

    try:
        branch = _run_git(workdir, "symbolic-ref", "--short", "HEAD")
    except subprocess.CalledProcessError:
        branch = None

    try:
        origin_head = _run_git(workdir, "symbolic-ref", "refs/remotes/origin/HEAD")
        default_branch = origin_head.replace("refs/remotes/origin/", "")
    except subprocess.CalledProcessError:
        try:
            _run_git(workdir, "rev-parse", "--verify", "main")
            default_branch = "main"
        except subprocess.CalledProcessError:
            try:
                _run_git(workdir, "rev-parse", "--verify", "master")
                default_branch = "master"
            except subprocess.CalledProcessError:
                raise GuardError("No default branch found") from None

    return Repo(root=root, common_dir=common_dir, branch=branch, default_branch=default_branch)


def check_implement(repo: Repo) -> None:
    if repo.branch is None:
        raise GuardError("Cannot implement on detached HEAD")
    if repo.branch == repo.default_branch:
        raise GuardError(f"Cannot implement on default branch {repo.default_branch}")


def review_diff(repo: Repo, base: str) -> str:
    verify = subprocess.run(
        _git_argv("rev-parse", "--verify", "--quiet", "--end-of-options", f"{base}^{{commit}}"),
        cwd=repo.root,
        capture_output=True,
        text=True,
        env=_git_env(),
    )
    if verify.returncode != 0:
        raise GuardError(f"Cannot diff against base {base!r}: not a valid commit")
    res = subprocess.run(
        _git_argv("diff", "--no-ext-diff", "--no-textconv", "--end-of-options", f"{base}...HEAD", "--"),
        cwd=repo.root,
        capture_output=True,
        text=True,
        env=_git_env(),
    )
    if res.returncode != 0:
        raise GuardError(f"Cannot diff against base {base!r}: {res.stderr.strip()}")
    return res.stdout


def fingerprint(repo: Repo) -> str:
    h = hashlib.sha256()
    h.update(head(repo).encode())

    res = subprocess.run(
        _git_argv("status", "--porcelain=v1", "-z", "--untracked-files=all"),
        cwd=repo.root,
        capture_output=True,
        check=True,
        env=_git_env(),
    )
    h.update(res.stdout)

    paths_to_hash = []
    i = 0
    parts = res.stdout.split(b"\0")
    while i < len(parts) - 1:
        part = parts[i]
        if not part:
            break
        xy = part[:2]
        path_bytes = part[3:]
        paths_to_hash.append(path_bytes)

        if xy[0:1] in (b"R", b"C") or xy[1:2] in (b"R", b"C"):
            i += 2
        else:
            i += 1

    for pb in paths_to_hash:
        p = repo.root / os.fsdecode(pb)
        if p.is_file():
            h.update(p.read_bytes())

    return h.hexdigest()


def head(repo: Repo) -> str:
    return _run_git(repo.root, "rev-parse", "HEAD")


def changes(repo: Repo, since: str) -> tuple[list[str], str]:
    changed_paths = set()

    res_diff = subprocess.run(
        _git_argv("diff", "--no-ext-diff", "--no-textconv", "--name-only", since),
        cwd=repo.root,
        capture_output=True,
        text=True,
        check=True,
        env=_git_env(),
    )
    if res_diff.stdout.strip():
        changed_paths.update(res_diff.stdout.strip().splitlines())

    res_untracked = subprocess.run(
        _git_argv("ls-files", "--others", "--exclude-standard"),
        cwd=repo.root,
        capture_output=True,
        text=True,
        check=True,
        env=_git_env(),
    )
    untracked_files = []
    if res_untracked.stdout.strip():
        untracked_files = res_untracked.stdout.strip().splitlines()
        changed_paths.update(untracked_files)

    sorted_paths = sorted(list(changed_paths))

    if not sorted_paths:
        return [], ""

    res_stat = subprocess.run(
        _git_argv("diff", "--no-ext-diff", "--no-textconv", "--stat", since),
        cwd=repo.root,
        capture_output=True,
        text=True,
        check=True,
        env=_git_env(),
    )
    lines = res_stat.stdout.strip().splitlines()
    diffstat = lines[-1].strip() if lines else ""

    if untracked_files:
        n = len(untracked_files)
        note = f"{n} untracked"
        if diffstat:
            diffstat += f" plus {note}"
        else:
            diffstat = note

    return sorted_paths, diffstat
