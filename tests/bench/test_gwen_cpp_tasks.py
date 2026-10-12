import fnmatch
import shutil
import subprocess

import pytest

from bench.gwen.run import _expand_gate
from bench.gwen.tasks import install_hidden, load_suites, load_tasks, prepare

TASK_IDS = ["c1", "c2", "c3", "c4", "c5", "c6"]
BUILD_AND_TEST = "cmake -S . -B build -G Ninja && cmake --build build && ctest --test-dir build --output-on-failure"


def _tasks():
    return {tid: t for tid, t in load_tasks().items() if t.suite == "cpp"}


def _gate(task, repo, build_dir):
    """Run the cpp suite's own gate steps, as run.py does; returns {step: (rc, output)}."""
    for tool in ("cmake", "ninja"):
        assert shutil.which(tool), f"{tool} is required by the cpp suite"
    build_dir.mkdir()
    steps = {}
    for name, argv in load_suites()["cpp"].gate.items():
        res = subprocess.run(_expand_gate(argv, task, build_dir), cwd=repo, capture_output=True, text=True)
        steps[name] = (res.returncode, res.stdout + res.stderr)
    return steps


def _failures(steps):
    return {name: out[-2000:] for name, (rc, out) in steps.items() if rc != 0}


def _apply(repo, patch):
    subprocess.run(["git", "apply", str(patch)], cwd=repo, check=True, capture_output=True)


def test_cpp_suite_has_six_tasks():
    assert sorted(_tasks()) == TASK_IDS
    assert list(load_suites()["cpp"].gate) == ["configure", "build", "visible", "hidden"]


def test_cpp_fixture_builds_and_passes(tmp_path):
    task = _tasks()["c2"]  # c2 has an empty setup patch: the plain fixture
    repo = tmp_path / "fx"
    prepare(task, repo)
    steps = _gate(task, repo, tmp_path / "build")
    assert {n: rc for n, (rc, _) in steps.items() if n != "hidden"} == {"configure": 0, "build": 0, "visible": 0}
    assert steps["hidden"][0] != 0  # no hidden tests installed: --no-tests=error


@pytest.mark.parametrize("tid", TASK_IDS)
def test_cpp_hidden_tests_fail_before_solution(tmp_path, tid):
    task = _tasks()[tid]
    repo = tmp_path / tid
    prepare(task, repo)
    install_hidden(task, repo)
    steps = _gate(task, repo, tmp_path / "build")
    assert steps["hidden"][0] != 0, steps["hidden"][1][-2000:]


@pytest.mark.parametrize("tid", TASK_IDS)
def test_cpp_solution_passes_every_gate_step(tmp_path, tid):
    task = _tasks()[tid]
    repo = tmp_path / tid
    prepare(task, repo)
    _apply(repo, task.dir / "solution.patch")
    install_hidden(task, repo)
    assert _failures(_gate(task, repo, tmp_path / "build")) == {}


@pytest.mark.parametrize("tid", TASK_IDS)
def test_cpp_solution_stays_in_scope(tmp_path, tid):
    task = _tasks()[tid]
    repo = tmp_path / tid
    prepare(task, repo)
    _apply(repo, task.dir / "solution.patch")
    out = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout
    changed = [line[3:] for line in out.splitlines()]
    assert changed
    assert all(any(fnmatch.fnmatch(p, g) for g in task.paths_allowed) for p in changed), changed
    # Every allowed path is really used by the solution: paths_allowed lists exactly what it changes.
    assert all(any(fnmatch.fnmatch(p, g) for p in changed) for g in task.paths_allowed), task.paths_allowed


@pytest.mark.parametrize("tid", TASK_IDS)
def test_cpp_brief_sections_and_gate(tid):
    task = _tasks()[tid]
    brief = task.brief
    for section in ("## Goal", "## Input", "## Expected output", "## Gate"):
        assert section in brief
    assert BUILD_AND_TEST in brief
    assert "hidden" not in brief.lower()
    assert task.visible_tests == []
