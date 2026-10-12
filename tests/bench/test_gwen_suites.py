import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from bench.gwen.run import main, run_one
from bench.gwen.tasks import FIXTURE_DIR, TASKS_DIR, load_suites, load_tasks
from tests.bench.fakeserver import FakeServer

FAKE_BIN = Path(__file__).parent / "fakes"
PY_TASK_IDS = ["t1", "t2", "t3", "t4", "t5", "t6"]
UNREACHABLE = "http://127.0.0.1:9"
# gate tables for two suites, alpha and beta; their [alpha]/[beta] headers come from _suites()
ALPHA_BETA_GATES = '\n[alpha.gate]\nok = ["true"]\n\n[beta.gate]\nok = ["true"]\n'


@pytest.fixture
def fake_upstream():
    srv = FakeServer()
    srv.start()
    yield srv
    srv.stop()


@pytest.fixture
def fake_env(monkeypatch):
    monkeypatch.setenv("PATH", f"{FAKE_BIN}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_HARNESS_MODE", "noop")
    return monkeypatch


def _task(tasks_dir: Path, tid: str, visible: list[str] | None = None) -> None:
    d = tasks_dir / tid
    (d / "hidden").mkdir(parents=True)
    (d / "hidden" / "test_hidden.py").write_text("def test_hidden():\n    pass\n")
    (d / "brief.md").write_text(f"Brief for {tid}.\n")
    vis = ", ".join(f'"{v}"' for v in (visible if visible is not None else ["tests/test_a.py"]))
    (d / "task.toml").write_text(f'title = "{tid}"\npaths_allowed = ["README.md"]\nvisible_tests = [{vis}]\n')


def _suites(root: Path, layout: dict[str, list[str]], tables: str = "") -> Path:
    """A fixture and tasks per suite under root, plus a suites.toml with each suite's table followed by `tables`."""
    heads = []
    for name, tids in layout.items():
        (root / name / "fixture").mkdir(parents=True)
        (root / name / "fixture" / "README.md").write_text("fixture\n")
        for tid in tids:
            _task(root / name / "tasks", tid)
        heads.append(f'[{name}]\nfixture = "{name}/fixture"\ntasks = "{name}/tasks"\n')
    path = root / "suites.toml"
    path.write_text("\n".join(heads) + tables)
    return path


def _harnesses(root: Path) -> Path:
    path = root / "harnesses.toml"
    path.write_text('[fake]\nargv = ["fake-harness"]\nenv = { BENCH_LINT = "{lint_cmd}", BENCH_TEST = "{test_cmd}" }\n')
    return path


def _run(tmp_path: Path, suites_file: Path, tid: str, harnesses_file: Path) -> dict:
    suites = load_suites(suites_file)
    return run_one(
        "fake",
        "model1",
        load_tasks(suites)[tid],
        base_url=UNREACHABLE,
        api_key="secret",
        out_root=tmp_path / "runs",
        timeout=60.0,
        ssh_target=None,
        harnesses_file=harnesses_file,
        suites=suites,
    )


def test_python_suite_loads_t1_to_t6():
    suites = load_suites()
    python = suites["python"]
    tasks = load_tasks({"python": python})
    assert [t.id for t in tasks.values()] == PY_TASK_IDS
    assert {t.suite for t in tasks.values()} == {"python"}
    assert python.fixture == FIXTURE_DIR
    assert python.tasks_dir == TASKS_DIR
    assert list(python.gate) == ["ruff", "visible", "hidden"]


def test_tasks_discovered_across_suites(tmp_path):
    suites_file = _suites(tmp_path, {"alpha": ["t1", "t2"], "beta": ["c1"]})
    (tmp_path / "alpha" / "tasks" / "scratch").mkdir()  # no task.toml: not a task
    tasks = load_tasks(load_suites(suites_file))
    assert {tid: t.suite for tid, t in tasks.items()} == {"t1": "alpha", "t2": "alpha", "c1": "beta"}
    assert "scratch" not in tasks


def test_duplicate_task_id_is_error(tmp_path):
    suites_file = _suites(tmp_path, {"alpha": ["t1"], "beta": ["t1"]})
    with pytest.raises(ValueError, match="duplicate task id 't1'"):
        load_tasks(load_suites(suites_file))


def test_natural_task_order(tmp_path):
    suites_file = _suites(tmp_path, {"alpha": ["t10", "t2", "t1", "t3"]})
    assert list(load_tasks(load_suites(suites_file))) == ["t1", "t2", "t3", "t10"]


def test_gate_steps_follow_suite_order_and_all_run(tmp_path, fake_env):
    tables = (
        "\n[alpha.gate]\n"
        'zeta = ["false"]\n'
        'alpha = ["true"]\n'
        'marker = ["{python}", "-c", "import pathlib, sys; pathlib.Path(sys.argv[1]).write_text(\'ran\')", '
        '"{build_dir}/marker.txt"]\n'
    )
    suites_file = _suites(tmp_path, {"alpha": ["t1"]}, tables)
    res = _run(tmp_path, suites_file, "t1", _harnesses(tmp_path))

    assert list(res["gate"]) == ["zeta", "alpha", "marker"]
    assert res["gate"] == {"zeta": 1, "alpha": 0, "marker": 0}
    assert (Path(res["run_dir"]) / "gate-build" / "marker.txt").read_text() == "ran"
    assert res["pass"] is False


def test_visible_tests_placeholder_expands(tmp_path, fake_env):
    script = "import json, pathlib, sys; pathlib.Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]))"
    tables = '\n[alpha.gate]\nargs = ["{python}", "-c", "' + script + '", "{build_dir}/args.json", "{visible_tests}"]\n'
    suites_file = _suites(tmp_path, {"alpha": ["t1", "t2"]}, tables)
    tasks_dir = tmp_path / "alpha" / "tasks"
    _task(tasks_dir, "t3", ["tests/test_a.py", "tests/test_b.py"])
    (tasks_dir / "t2" / "task.toml").write_text('title = "t2"\npaths_allowed = ["README.md"]\nvisible_tests = []\n')
    harnesses_file = _harnesses(tmp_path)

    two = _run(tmp_path, suites_file, "t3", harnesses_file)
    none = _run(tmp_path, suites_file, "t2", harnesses_file)

    assert two["gate"]["args"] == 0
    assert json.loads((Path(two["run_dir"]) / "gate-build" / "args.json").read_text()) == [
        "tests/test_a.py",
        "tests/test_b.py",
    ]
    assert none["gate"]["args"] == 0
    assert json.loads((Path(none["run_dir"]) / "gate-build" / "args.json").read_text()) == []


def test_build_dir_is_outside_repo_and_fresh(tmp_path, fake_env):
    tables = (
        "\n[alpha.gate]\n"
        'fresh = ["{python}", "-c", "import os, pathlib, sys; d = pathlib.Path(sys.argv[1]); '
        "assert os.listdir(d) == []; (d / 'stamp').write_text('x')\", \"{build_dir}\"]\n"
    )
    suites_file = _suites(tmp_path, {"alpha": ["t1"]}, tables)
    res = _run(tmp_path, suites_file, "t1", _harnesses(tmp_path))

    run_dir = Path(res["run_dir"])
    build = run_dir / "gate-build"
    assert res["gate"]["fresh"] == 0
    assert (build / "stamp").read_text() == "x"
    assert run_dir / "repo" not in build.parents
    assert not (run_dir / "repo" / "gate-build").exists()


def test_harness_lint_and_test_cmd_placeholders(tmp_path, fake_env):
    fake_env.setenv("FAKE_HARNESS_MODE", "env_check")
    tables = (
        ALPHA_BETA_GATES
        + '\n[alpha.harness]\nlint_cmd = "python: ruff check --fix"\ntest_cmd = "python -m pytest -q"\n'
    )
    suites_file = _suites(tmp_path, {"alpha": ["t1"], "beta": ["t2"]}, tables)
    harnesses_file = _harnesses(tmp_path)

    with_cmds = _run(tmp_path, suites_file, "t1", harnesses_file)
    without = _run(tmp_path, suites_file, "t2", harnesses_file)

    lines = (Path(with_cmds["run_dir"]) / "stdout").read_text().splitlines()
    assert "BENCH_LINT=python: ruff check --fix" in lines
    assert "BENCH_TEST=python -m pytest -q" in lines
    lines = (Path(without["run_dir"]) / "stdout").read_text().splitlines()
    assert "BENCH_LINT=" in lines
    assert "BENCH_TEST=" in lines


def test_suite_filter_and_unknown_suite_exit_2(tmp_path, fake_upstream, fake_env, capsys):
    suites_file = _suites(tmp_path, {"alpha": ["t1"], "beta": ["t2"]}, ALPHA_BETA_GATES)
    fake_env.setenv("GWEN_BENCH_SUITES", str(suites_file))
    results = tmp_path / "results.jsonl"
    base = [
        "--harness", "fake", "--model", "model1",
        "--out-root", str(tmp_path / "runs"), "--results", str(results),
        "--base-url", fake_upstream.url, "--no-mem", "--api-key-file", "/dev/null",
    ]  # fmt: skip

    with patch("bench.gwen.run.get_harnesses_file", return_value=_harnesses(tmp_path)):
        assert main([*base, "--suite", "nope"]) == 2
        assert "Unknown suite: nope" in capsys.readouterr().err

        assert main([*base, "--suite", "beta", "--task", "t1"]) == 2
        assert "Unknown task: t1" in capsys.readouterr().err

        assert main([*base, "--suite", "beta", "--task", "all"]) == 0

    records = [json.loads(line) for line in results.read_text().splitlines()]
    assert [(r["task"], r["suite"]) for r in records] == [("t2", "beta")]


def test_result_records_suite(tmp_path, fake_env):
    suites_file = _suites(tmp_path, {"alpha": ["t1"], "beta": ["t2"]}, ALPHA_BETA_GATES)
    res = _run(tmp_path, suites_file, "t2", _harnesses(tmp_path))

    assert res["suite"] == "beta"
    assert res["pass"] is True
