import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from bench.gwen.run import main, run_one
from bench.gwen.tasks import load_tasks
from tests.bench.fakeserver import FakeServer


@pytest.fixture
def fake_upstream():
    srv = FakeServer()
    srv.start()
    yield srv
    srv.stop()


@pytest.fixture
def harnesses_file(tmp_path):
    h = tmp_path / "harnesses.toml"
    h.write_text("""
[fake]
argv = ["fake-harness"]
env = { OPENAI_BASE_URL = "{base_url}/v1", OPENAI_API_KEY = "{api_key}" }
""")
    return h


@pytest.fixture
def env_with_fakes(monkeypatch):
    cwd = Path(__file__).parent.parent.parent.absolute()
    fake_bin = cwd / "tests" / "bench" / "fakes"
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")
    return monkeypatch


def test_solution_mode_passes(tmp_path, fake_upstream, harnesses_file, env_with_fakes):
    env_with_fakes.setenv("FAKE_HARNESS_MODE", "solve")
    tasks = load_tasks()
    t1 = tasks["t1"]
    env_with_fakes.setenv("FAKE_SOLUTION_PATCH", str(t1.dir / "solution.patch"))

    res = run_one(
        "fake",
        "model1",
        t1,
        base_url=fake_upstream.url,
        api_key="secret",
        out_root=tmp_path,
        timeout=60.0,
        ssh_target=None,
        harnesses_file=harnesses_file,
    )

    assert res["pass"] is True
    assert res["gate"]["hidden"] == 0
    assert not res["scope_violation"]
    assert res["usage"]["prompt_tokens"] > 0


def test_noop_mode_fails_hidden(tmp_path, fake_upstream, harnesses_file, env_with_fakes):
    env_with_fakes.setenv("FAKE_HARNESS_MODE", "noop")
    tasks = load_tasks()
    t1 = tasks["t1"]

    res = run_one(
        "fake",
        "model1",
        t1,
        base_url=fake_upstream.url,
        api_key="secret",
        out_root=tmp_path,
        timeout=60.0,
        ssh_target=None,
        harnesses_file=harnesses_file,
    )

    assert res["pass"] is False
    assert res["gate"]["visible"] != 0


def test_scope_violation(tmp_path, fake_upstream, harnesses_file, env_with_fakes):
    env_with_fakes.setenv("FAKE_HARNESS_MODE", "outside")
    tasks = load_tasks()
    t1 = tasks["t1"]
    env_with_fakes.setenv("FAKE_SOLUTION_PATCH", str(t1.dir / "solution.patch"))

    res = run_one(
        "fake",
        "model1",
        t1,
        base_url=fake_upstream.url,
        api_key="secret",
        out_root=tmp_path,
        timeout=60.0,
        ssh_target=None,
        harnesses_file=harnesses_file,
    )

    assert res["pass"] is False
    assert "README.md" in res["scope_violation"]
    assert "data/items.csv" in res["scope_violation"]


def test_committed_changes_are_scoped_and_caches_ignored(tmp_path, fake_upstream, harnesses_file, env_with_fakes):
    env_with_fakes.setenv("FAKE_HARNESS_MODE", "commit_outside")
    t1 = load_tasks()["t1"]
    env_with_fakes.setenv("FAKE_SOLUTION_PATCH", str(t1.dir / "solution.patch"))

    res = run_one(
        "fake",
        "org/model1",
        t1,
        base_url=fake_upstream.url,
        api_key="secret",
        out_root=tmp_path,
        timeout=60.0,
        ssh_target=None,
        harnesses_file=harnesses_file,
    )

    assert res["scope_violation"] == ["README.md"]
    assert res["pass"] is False
    assert res["gate"]["hidden"] == 0
    assert "/" not in Path(res["run_dir"]).name


def test_hidden_tests_cannot_be_preempted(tmp_path, fake_upstream, harnesses_file, env_with_fakes):
    env_with_fakes.setenv("FAKE_HARNESS_MODE", "prehide")
    tasks = load_tasks()
    t1 = tasks["t1"]

    res = run_one(
        "fake",
        "model1",
        t1,
        base_url=fake_upstream.url,
        api_key="secret",
        out_root=tmp_path,
        timeout=60.0,
        ssh_target=None,
        harnesses_file=harnesses_file,
    )

    assert res["gate"]["hidden"] != 0


def test_timeout_kills_group(tmp_path, fake_upstream, harnesses_file, env_with_fakes):
    env_with_fakes.setenv("FAKE_HARNESS_MODE", "hang")
    tasks = load_tasks()
    t1 = tasks["t1"]

    res = run_one(
        "fake",
        "model1",
        t1,
        base_url=fake_upstream.url,
        api_key="secret",
        out_root=tmp_path,
        timeout=1.0,
        ssh_target=None,
        harnesses_file=harnesses_file,
    )

    assert res["timed_out"] is True
    assert "hidden" in res["gate"]


def test_isolated_home_and_secret_env(tmp_path, fake_upstream, harnesses_file, env_with_fakes):
    env_with_fakes.setenv("FAKE_HARNESS_MODE", "env_check")
    env_with_fakes.setenv("GH_TOKEN", "should_be_hidden")
    tasks = load_tasks()
    t1 = tasks["t1"]

    res = run_one(
        "fake",
        "model1",
        t1,
        base_url=fake_upstream.url,
        api_key="secret",
        out_root=tmp_path,
        timeout=60.0,
        ssh_target=None,
        harnesses_file=harnesses_file,
    )

    run_dir = Path(res["run_dir"])
    stdout = (run_dir / "stdout").read_text()

    assert f"HOME={run_dir}/home" in stdout
    assert "GH_TOKEN=" not in stdout
    assert "OPENAI_API_KEY=secret" in stdout


def test_api_key_redacted(tmp_path, fake_upstream, harnesses_file, env_with_fakes):
    env_with_fakes.setenv("FAKE_HARNESS_MODE", "solve")
    tasks = load_tasks()
    t1 = tasks["t1"]

    res = run_one(
        "fake",
        "model1",
        t1,
        base_url=fake_upstream.url,
        api_key="super_secret_key",
        out_root=tmp_path,
        timeout=60.0,
        ssh_target=None,
        harnesses_file=harnesses_file,
    )

    run_dir = Path(res["run_dir"])
    for p in run_dir.rglob("*"):
        if p.is_file():
            assert "super_secret_key" not in p.read_text(errors="ignore")

    assert "super_secret_key" not in json.dumps(res)


def test_main_cross_product_appends_jsonl(tmp_path, fake_upstream, harnesses_file, env_with_fakes):
    env_with_fakes.setenv("FAKE_HARNESS_MODE", "solve")
    results = tmp_path / "results.jsonl"

    with patch("bench.gwen.run.get_harnesses_file", return_value=harnesses_file):
        rc = main(
            [
                "--harness",
                "fake",
                "--task",
                "t1",
                "--task",
                "t2",
                "--repeat",
                "2",
                "--out-root",
                str(tmp_path / "runs"),
                "--results",
                str(results),
                "--base-url",
                fake_upstream.url,
                "--no-mem",
                "--api-key-file",
                "/dev/null",
                "--model",
                "model1",
            ]
        )

    assert rc == 0
    lines = results.read_text().splitlines()
    assert len(lines) == 4


def _main_args(tmp_path, base_url, model="model1"):
    return [
        "--harness", "fake", "--task", "t1", "--task", "t2",
        "--out-root", str(tmp_path / "runs"), "--results", str(tmp_path / "results.jsonl"),
        "--base-url", base_url, "--no-mem", "--api-key-file", "/dev/null", "--model", model,
    ]  # fmt: skip


def test_preflight_unreachable_stops_with_exit_3(tmp_path, harnesses_file, env_with_fakes):
    env_with_fakes.setenv("FAKE_HARNESS_MODE", "solve")
    with patch("bench.gwen.run.get_harnesses_file", return_value=harnesses_file):
        rc = main(_main_args(tmp_path, "http://127.0.0.1:9"))
    assert rc == 3
    assert not (tmp_path / "results.jsonl").exists() or (tmp_path / "results.jsonl").read_text() == ""
    assert not (tmp_path / "runs").exists()


def test_preflight_wrong_model_stops_with_exit_3(tmp_path, fake_upstream, harnesses_file, env_with_fakes, capsys):
    env_with_fakes.setenv("FAKE_HARNESS_MODE", "solve")
    with patch("bench.gwen.run.get_harnesses_file", return_value=harnesses_file):
        rc = main(_main_args(tmp_path, fake_upstream.url, model="other-model"))
    assert rc == 3
    assert "does not serve other-model" in capsys.readouterr().err


def test_server_gone_mid_batch_marks_infra_error_and_stops(tmp_path, fake_upstream, harnesses_file, env_with_fakes):
    env_with_fakes.setenv("FAKE_HARNESS_MODE", "solve")
    t1 = load_tasks()["t1"]
    env_with_fakes.setenv("FAKE_SOLUTION_PATCH", str(t1.dir / "solution.patch"))

    def run_then_server_dies(*args, **kwargs):
        kwargs["base_url"] = "http://127.0.0.1:9"  # preflight passed, then the upstream vanished
        return run_one(*args, **kwargs)

    with (
        patch("bench.gwen.run.get_harnesses_file", return_value=harnesses_file),
        patch("bench.gwen.run.run_one", side_effect=run_then_server_dies),
    ):
        rc = main(_main_args(tmp_path, fake_upstream.url))

    assert rc == 3
    records = [json.loads(line) for line in (tmp_path / "results.jsonl").read_text().splitlines()]
    assert len(records) == 1  # stopped after the first task instead of running t2
    assert records[0]["infra_error"] is True
    assert records[0]["pass"] is False
    assert records[0]["usage"]["upstream_failures"] >= 1


def test_unknown_harness_exit_2(tmp_path, harnesses_file):
    with patch("bench.gwen.run.get_harnesses_file", return_value=harnesses_file):
        rc = main(["--harness", "unknown_harness", "--task", "t1", "--model", "mod1"])
        assert rc == 2
