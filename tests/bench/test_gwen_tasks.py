import subprocess
import sys

import pytest

from bench.gwen.tasks import FIXTURE_DIR, install_hidden, load_tasks, prepare

TASK_IDS = ["t1", "t2", "t3", "t4", "t5", "t6"]


def _pytest(repo, *paths):
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *paths],
        cwd=repo,
        capture_output=True,
        text=True,
    )


def _ruff(repo):
    return subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--no-cache", "."], cwd=repo, capture_output=True, text=True
    )


def _apply(repo, patch):
    subprocess.run(["git", "apply", str(patch)], cwd=repo, check=True, capture_output=True)


def test_load_tasks_has_six():
    assert sorted(t for t, task in load_tasks().items() if task.suite == "python") == TASK_IDS


def test_fixture_tests_and_lint_pass(tmp_path):
    repo = tmp_path / "fx"
    prepare(load_tasks()["t2"], repo)  # t2 has an empty setup patch
    assert _pytest(repo).returncode == 0
    assert _ruff(repo).returncode == 0


@pytest.mark.parametrize("tid", TASK_IDS)
def test_hidden_tests_fail_before_solution(tmp_path, tid):
    task = load_tasks()[tid]
    repo = tmp_path / tid
    prepare(task, repo)
    install_hidden(task, repo)
    assert _pytest(repo, "tests/hidden").returncode != 0


@pytest.mark.parametrize("tid", TASK_IDS)
def test_solution_passes_everything(tmp_path, tid):
    task = load_tasks()[tid]
    repo = tmp_path / tid
    prepare(task, repo)
    _apply(repo, task.dir / "solution.patch")
    install_hidden(task, repo)
    assert _pytest(repo, *task.visible_tests).returncode == 0
    assert _pytest(repo, "tests/hidden").returncode == 0
    assert _ruff(repo).returncode == 0


@pytest.mark.parametrize("tid", TASK_IDS)
def test_solution_stays_in_scope(tmp_path, tid):
    import fnmatch

    task = load_tasks()[tid]
    repo = tmp_path / tid
    prepare(task, repo)
    _apply(repo, task.dir / "solution.patch")
    out = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout
    changed = [line[3:] for line in out.splitlines()]
    assert changed
    assert all(any(fnmatch.fnmatch(p, g) for g in task.paths_allowed) for p in changed), changed


@pytest.mark.parametrize("tid", TASK_IDS)
def test_brief_has_required_sections_and_hides_hidden_tests(tid):
    brief = load_tasks()[tid].brief
    assert "## Goal" in brief and "## Input" in brief and "## Expected output" in brief
    assert "## Gate" in brief and "ruff check ." in brief and "python -m pytest -q" in brief
    assert "hidden" not in brief.lower()


def test_prepare_does_not_touch_fixture(tmp_path):
    before = sorted(p.relative_to(FIXTURE_DIR) for p in FIXTURE_DIR.rglob("*"))
    prepare(load_tasks()["t1"], tmp_path / "x")
    assert sorted(p.relative_to(FIXTURE_DIR) for p in FIXTURE_DIR.rglob("*")) == before
    assert not (FIXTURE_DIR / ".git").exists()
