import subprocess

import pytest

from agenthub.guards import GuardError, _git_env, changes, check_implement, fingerprint, head, inspect, review_diff
from tests.gitrepo import make_repo


def test_inspect_not_a_repo(tmp_path):
    d = tmp_path / "not_repo"
    d.mkdir()
    with pytest.raises(GuardError, match="Not in a git work tree"):
        inspect(d)


def test_inspect_missing_dir(tmp_path):
    with pytest.raises(GuardError, match="Not a directory"):
        inspect(tmp_path / "missing")


def test_default_branch_master(tmp_path):
    repo_path = make_repo(tmp_path / "repo", default="master", branch=None)
    r = inspect(repo_path)
    assert r.default_branch == "master"


def test_default_branch_main_preferred_when_both_exist(tmp_path):
    repo_path = make_repo(tmp_path / "repo", default="main", branch=None)
    subprocess.run(["git", "branch", "master"], cwd=repo_path, check=True)
    r = inspect(repo_path)
    assert r.default_branch == "main"


def test_check_implement_refuses_default_branch(tmp_path):
    repo_path = make_repo(tmp_path / "repo", default="main", branch=None)
    r = inspect(repo_path)
    with pytest.raises(GuardError, match="Cannot implement on default branch main"):
        check_implement(r)


def test_check_implement_refuses_detached(tmp_path):
    repo_path = make_repo(tmp_path / "repo", default="main", branch=None)
    subprocess.run(["git", "checkout", "--detach", "HEAD"], cwd=repo_path, check=True)
    r = inspect(repo_path)
    with pytest.raises(GuardError, match="Cannot implement on detached HEAD"):
        check_implement(r)


def test_check_implement_ok_on_feature(tmp_path):
    repo_path = make_repo(tmp_path / "repo", default="main", branch="feat/x")
    r = inspect(repo_path)
    assert check_implement(r) is None


def test_guard_subdirectory_and_spaces(tmp_path):
    repo_path = make_repo(tmp_path / "my repo", default="main", branch=None)
    src = repo_path / "src"
    src.mkdir()
    r = inspect(src)
    assert r.root == repo_path.resolve()


def test_fingerprint_changes_on_edit_untracked_and_commit(tmp_path):
    repo_path = make_repo(tmp_path / "repo", default="main", branch=None)
    r = inspect(repo_path)
    fp1 = fingerprint(r)

    # edit
    (repo_path / "README.md").write_text("Hello 2\n")
    fp2 = fingerprint(r)
    assert fp2 != fp1

    # untracked
    (repo_path / "untracked.txt").write_text("hi")
    fp3 = fingerprint(r)
    assert fp3 != fp2

    # commit
    subprocess.run(["git", "add", "README.md", "untracked.txt"], cwd=repo_path, check=True)
    subprocess.run(["git", "commit", "-m", "update"], cwd=repo_path, check=True)
    fp4 = fingerprint(r)
    assert fp4 != fp3

    # no change -> equal
    fp5 = fingerprint(r)
    assert fp5 == fp4


def test_changes_reports_committed_modified_untracked(tmp_path):
    repo_path = make_repo(tmp_path / "repo", default="main", branch=None)
    base = head(inspect(repo_path))

    (repo_path / "README.md").write_text("Changed\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo_path, check=True)
    subprocess.run(["git", "commit", "-m", "commit1"], cwd=repo_path, check=True)

    (repo_path / "README.md").write_text("Modified again\n")
    (repo_path / "untracked.txt").write_text("hi")

    r = inspect(repo_path)
    changed_paths, diffstat = changes(r, base)

    assert "README.md" in changed_paths
    assert "untracked.txt" in changed_paths
    assert "1 untracked" in diffstat


def test_changes_empty(tmp_path):
    repo_path = make_repo(tmp_path / "repo", default="main", branch=None)
    r = inspect(repo_path)
    base = head(r)

    changed_paths, diffstat = changes(r, base)
    assert changed_paths == []
    assert diffstat == ""


def test_common_dir_of_linked_worktree(tmp_path):
    main_repo = make_repo(tmp_path / "main_repo", default="main", branch=None)
    wt_repo = tmp_path / "wt"
    subprocess.run(["git", "worktree", "add", str(wt_repo), "HEAD"], cwd=main_repo, check=True)

    r = inspect(wt_repo)
    assert r.common_dir == main_repo.resolve() / ".git"


def test_git_env_keeps_environment(monkeypatch):
    monkeypatch.setenv("HOME", "/fake/home")
    monkeypatch.setenv("PATH", "/fake/path")
    env = _git_env()
    assert env["HOME"] == "/fake/home"
    assert env["PATH"] == "/fake/path"
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_review_diff_contains_branch_change(tmp_path):
    repo_path = make_repo(tmp_path / "repo")
    (repo_path / "new.txt").write_text("added line\n")
    subprocess.run(["git", "add", "new.txt"], cwd=repo_path, check=True, env=_git_env())
    subprocess.run(["git", "commit", "-m", "add"], cwd=repo_path, check=True, capture_output=True, env=_git_env())
    out = review_diff(inspect(repo_path), "master")
    assert "new.txt" in out
    assert "+added line" in out


def test_review_diff_unknown_base_raises(tmp_path):
    repo_path = make_repo(tmp_path / "repo")
    with pytest.raises(GuardError, match="no-such-base"):
        review_diff(inspect(repo_path), "no-such-base")
