import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agenthub import gateguard, process
from agenthub.cli import main
from agenthub.config import paths
from agenthub.jobs import list_jobs


def git_head(repo):
    res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True)
    return res.stdout.strip()


def set_repo_gates(repo, gates):
    (repo / ".agent-hub").mkdir(exist_ok=True)
    (repo / ".agent-hub" / "config.toml").write_text(f"gates = {json.dumps(gates)}\n")
    subprocess.run(["git", "add", ".agent-hub"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "repo gates"], cwd=repo, check=True)


def test_gate_cmd_refuses_dirty_tree(env_sandbox, repo, capsys):
    (repo / "dirty.txt").write_text("x\n")

    rc = main(["gate", "-d", str(repo), "--gate", "true"])

    assert rc == 2
    assert "commit first; gate runs on HEAD" in capsys.readouterr().err
    assert list_jobs(paths(), 10) == []


def test_gate_cmd_records_head_and_sha(env_sandbox, repo, capsys):
    set_repo_gates(repo, ["test -f README.md"])
    head = git_head(repo)

    rc = main(["gate", "-d", str(repo), "--gate", "test -f README.md", "--json"])

    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["status"] == "ok"
    assert out["kind"] == "gate"
    assert out["role"] == "gate"
    assert out["agent"] == "agent-hub"
    assert out["model"] == "gate"
    assert out["workdir"] == str(repo.resolve())
    assert out["head"] == head
    assert [g["cmd"] for g in out["gate"]] == ["test -f README.md"]
    assert [g["rc"] for g in out["gate"]] == [0]
    assert [g["sha"] for g in out["gate"]] == [head]
    [recorded] = list_jobs(paths(), 10)
    assert recorded["id"] == out["id"]


def test_gate_has_no_base_option(env_sandbox, repo, capsys):
    set_repo_gates(repo, ["true"])

    rc = main(["gate", "-d", str(repo), "--base", "HEAD"])

    assert rc == 2
    assert "unrecognized arguments" in capsys.readouterr().err
    assert list_jobs(paths(), 10) == []


def test_gate_cmd_failure_exit_9(env_sandbox, repo, capsys):
    set_repo_gates(repo, ["echo nope; exit 4"])

    rc = main(["gate", "-d", str(repo), "--gate", "true", "--json"])

    out = json.loads(capsys.readouterr().out)
    assert rc == 9
    assert out["status"] == "gate-failed"
    assert out["exit_code"] == 9
    assert [g["rc"] for g in out["gate"]] == [4]
    assert out["gate"][0]["sha"] == out["head"]


def test_gate_cmd_no_gates_exit_2(env_sandbox, repo, capsys):
    rc = main(["gate", "-d", str(repo)])

    assert rc == 2
    assert "no repo gates: set gates in .agent-hub/config.toml" in capsys.readouterr().err
    assert list_jobs(paths(), 10) == []


def test_gate_requires_repo_gates(env_sandbox, repo, capsys):
    rc = main(["gate", "-d", str(repo), "--gate", "true"])

    assert rc == 2
    assert "no repo gates: set gates in .agent-hub/config.toml" in capsys.readouterr().err
    assert list_jobs(paths(), 10) == []


def test_cli_gate_true_cannot_replace_repo_gates(env_sandbox, repo, capsys):
    set_repo_gates(repo, ["test -f missing.txt"])

    rc = main(["gate", "-d", str(repo), "--gate", "true", "--json"])

    out = json.loads(capsys.readouterr().out)
    assert rc == 9
    assert out["status"] == "gate-failed"
    assert [g["cmd"] for g in out["gate"]] == ["test -f missing.txt"]
    assert out["gate"][0]["rc"] != 0


def test_gate_job_records_gate_touched_and_ready_reports_it(env_sandbox, repo, capsys):
    (repo / "test_a.py").write_text("def test_x():\n    assert 1\n")
    subprocess.run(["git", "add", "test_a.py"], cwd=repo, check=True)
    set_repo_gates(repo, ["true"])
    subprocess.run(["git", "branch", "-f", "master"], cwd=repo, check=True)
    (repo / "test_a.py").write_text("def test_x():\n    assert 2\n")
    subprocess.run(["git", "commit", "-q", "-am", "weaken"], cwd=repo, check=True)
    head = git_head(repo)

    rc = main(["gate", "-d", str(repo), "--json"])

    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["head"] == head
    assert out["gate_touched"] == [{"file": "test_a.py", "kind": "assert-removed", "lines": [2]}]

    rc = main(["ready", "-d", str(repo), "--json"])

    ready_out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert "gate touched: 1 findings not judged by the reviewer" in ready_out["problems"]


def test_gate_cmd_repo_gates_run_before_cli_gates(env_sandbox, repo, capsys):
    (repo / ".agent-hub").mkdir()
    (repo / ".agent-hub" / "config.toml").write_text('gates = ["test -f README.md"]\n')
    subprocess.run(["git", "add", ".agent-hub"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "repo gates"], cwd=repo, check=True)

    rc = main(["gate", "-d", str(repo), "--gate", "true", "--gate", "test -f README.md", "--json"])

    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert [g["cmd"] for g in out["gate"]] == ["test -f README.md", "true"]


def test_gate_job_uses_repo_gates_from_merge_base(env_sandbox, repo, capsys):
    set_repo_gates(repo, ["test -f README.md"])
    subprocess.run(["git", "branch", "-f", "master"], cwd=repo, check=True)
    base = git_head(repo)
    (repo / "feature.txt").write_text("x\n")
    subprocess.run(["git", "add", "feature.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "feature"], cwd=repo, check=True)

    rc = main(["gate", "-d", str(repo), "--gate", "true", "--json"])

    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["repo_gates"] == ["test -f README.md"]
    assert out["repo_gates_source"] == base
    assert [g["cmd"] for g in out["gate"]] == ["test -f README.md", "true"]
    assert out["gate_touched"] == []


def test_weakened_config_in_head_is_ignored_and_flagged(env_sandbox, repo, capsys):
    set_repo_gates(repo, ["test -f README.md"])
    subprocess.run(["git", "branch", "-f", "master"], cwd=repo, check=True)
    base = git_head(repo)
    set_repo_gates(repo, ["true"])

    rc = main(["gate", "-d", str(repo), "--json"])

    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["repo_gates_source"] == base
    assert [g["cmd"] for g in out["gate"]] == ["test -f README.md"]
    assert {"file": ".agent-hub/config.toml", "kind": "gate-config-changed", "lines": [1]} in out["gate_touched"]

    rc = main(["ready", "-d", str(repo), "--json"])

    ready_out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert "gate touched: 1 findings not judged by the reviewer" in ready_out["problems"]


def test_gate_job_falls_back_to_worktree_config_when_base_has_none(env_sandbox, repo, capsys):
    set_repo_gates(repo, ["test -f README.md"])

    rc = main(["gate", "-d", str(repo), "--json"])

    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["repo_gates"] == ["test -f README.md"]
    assert out["repo_gates_source"] == "worktree"
    assert [g["cmd"] for g in out["gate"]] == ["test -f README.md"]
    assert {"file": ".agent-hub/config.toml", "kind": "gate-config-changed", "lines": [1]} in out["gate_touched"]


def test_gate_runs_base_gates_when_head_removed_config(env_sandbox, repo, capsys):
    set_repo_gates(repo, ["test -f README.md"])
    subprocess.run(["git", "branch", "-f", "master"], cwd=repo, check=True)
    base = git_head(repo)
    subprocess.run(["git", "rm", "-q", "-r", ".agent-hub"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "drop gates"], cwd=repo, check=True)

    rc = main(["gate", "-d", str(repo), "--json"])

    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["status"] == "ok"
    assert out["repo_gates_source"] == base
    assert out["repo_gates"] == ["test -f README.md"]
    assert [g["cmd"] for g in out["gate"]] == ["test -f README.md"]
    assert [g["rc"] for g in out["gate"]] == [0]
    assert list_jobs(paths(), 10)[0]["id"] == out["id"]


def test_gate_refuses_only_when_no_gates_anywhere(env_sandbox, repo, capsys):
    subprocess.run(["git", "branch", "-f", "master"], cwd=repo, check=True)
    (repo / "feature.txt").write_text("x\n")
    subprocess.run(["git", "add", "feature.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "feature"], cwd=repo, check=True)

    rc = main(["gate", "-d", str(repo), "--gate", "true"])

    assert rc == 2
    assert "no repo gates: set gates in .agent-hub/config.toml" in capsys.readouterr().err
    assert list_jobs(paths(), 10) == []

    set_repo_gates(repo, ["test -f feature.txt"])

    rc = main(["gate", "-d", str(repo), "--json"])

    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["repo_gates_source"] == "worktree"
    assert [g["cmd"] for g in out["gate"]] == ["test -f feature.txt"]


def test_gate_job_sigterm_marks_cancelled(env_sandbox, repo, tmp_path):
    marker = tmp_path / "started"
    set_repo_gates(repo, [f"touch {marker} && sleep 30"])

    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])

    proc = subprocess.Popen(
        [sys.executable, "-m", "agenthub", "gate", "-d", str(repo)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )

    stdout, stderr = "", ""
    try:
        deadline = time.time() + 10.0
        result_path = None
        while time.time() < deadline:
            if proc.poll() is not None:
                out_bytes, err_bytes = proc.communicate()
                stdout = out_bytes.decode(errors="replace")
                stderr = err_bytes.decode(errors="replace")
                break
            gate_jobs = [j for j in list_jobs(paths(), 10) if j.get("kind") == "gate" and j.get("status") == "running"]
            if gate_jobs:
                result_path = paths().jobs_dir / gate_jobs[0]["id"] / "result.json"
                if marker.is_file():
                    break
            time.sleep(0.02)

        assert result_path is not None, f"gate job did not enter running status; proc exit code: {proc.poll()}"
        assert marker.is_file(), f"gate command marker did not appear; proc exit code: {proc.poll()}"

        proc.send_signal(signal.SIGTERM)
        try:
            out_bytes, err_bytes = proc.communicate(timeout=10.0)
            stdout = out_bytes.decode(errors="replace")
            stderr = err_bytes.decode(errors="replace")
        except subprocess.TimeoutExpired as exc:
            stdout = (exc.stdout or b"").decode(errors="replace")
            stderr = (exc.stderr or b"").decode(errors="replace")
            raise
    finally:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()

    assert proc.returncode == 130, f"proc returncode={proc.returncode}\nstdout:\n{stdout}\nstderr:\n{stderr}"
    result_data = json.loads(result_path.read_text())
    assert result_data["status"] == "cancelled"
    assert result_data["exit_code"] == 130
    assert [j["status"] for j in list_jobs(paths(), 100) if j["kind"] == "gate"] == ["cancelled"]


def test_gate_job_exception_marks_failed(env_sandbox, repo, monkeypatch):
    set_repo_gates(repo, ["true"])

    def crash(*args, **kwargs):
        raise OSError("boom")

    monkeypatch.setattr(process, "run", crash)

    with pytest.raises(OSError, match="boom"):
        main(["gate", "-d", str(repo)])

    assert [j["status"] for j in list_jobs(paths(), 100) if j["kind"] == "gate"] == ["failed"]


def test_cancelled_gate_job_skips_gateguard(env_sandbox, repo, capsys, monkeypatch):
    set_repo_gates(repo, ["true"])
    calls = []

    def fake_inspect(repo_, since):
        calls.append(since)
        return [{"file": "test_a.py", "kind": "assert-removed", "lines": [1]}]

    def sigterm_during_gate(inv, cwd, env, attempt_dir, timeout, on_start):
        (attempt_dir / "stdout").write_text("")
        (attempt_dir / "stderr").write_text("")
        os.kill(os.getpid(), signal.SIGTERM)
        return process.Ran(rc=-15, timed_out=False, duration_s=0.0, pgid=0)

    monkeypatch.setattr(process, "run", sigterm_during_gate)
    monkeypatch.setattr(gateguard, "inspect_diff", fake_inspect)

    rc = main(["gate", "-d", str(repo), "--json"])

    out = json.loads(capsys.readouterr().out)
    assert rc == 130
    assert out["status"] == "cancelled"
    assert calls == []
    assert out["gate_touched"] == []


def test_review_cancel_after_last_repo_gate_is_not_a_pass(env_sandbox, repo, capsys, monkeypatch):
    set_repo_gates(repo, ["true"])
    head = git_head(repo)
    calls = []

    def sigterm_after_repo_gate(inv, cwd, env, attempt_dir, timeout, on_start):
        calls.append(inv.argv)
        (attempt_dir / "stdout").write_text("")
        (attempt_dir / "stderr").write_text("")
        os.kill(os.getpid(), signal.SIGTERM)
        return process.Ran(rc=0, timed_out=False, duration_s=0.0, pgid=0)

    monkeypatch.setattr(process, "run", sigterm_after_repo_gate)

    rc = main(["gate", "-d", str(repo), "--gate", "echo extra", "--json"])

    out = json.loads(capsys.readouterr().out)
    assert rc == 130
    assert out["status"] == "cancelled"
    assert [g["cmd"] for g in out["gate"]] == ["true"]
    assert len(calls) == 1

    rc = main(["ready", "-d", str(repo), "--json"])

    ready_out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert f"no passing gate on {head}" in ready_out["problems"]


def test_running_gate_job_has_pid(env_sandbox, repo, tmp_path, capsys, monkeypatch):
    import agenthub.cli as cli
    from agenthub.jobs import get_job
    from agenthub.procinfo import start_time

    seen_during_gate = tmp_path / "seen_pid.json"
    gate_cmd = (
        f'{sys.executable} -c "'
        f"import json, pathlib; "
        f"from agenthub.config import paths; "
        f"from agenthub.jobs import list_jobs, get_job; "
        f"jobs = list_jobs(paths(), 10); "
        f"assert len(jobs) >= 1; "
        f"j = get_job(paths(), jobs[0]['id']); "
        f"assert (j.dir / 'pid').is_file(); "
        f"pathlib.Path({str(seen_during_gate)!r}).write_text((j.dir / 'pid').read_text()); "
        f'"'
    )
    set_repo_gates(repo, [gate_cmd])
    announce_file = tmp_path / "announced.txt"
    seen_at_announce = []
    orig_atomic_write = cli._atomic_write

    def checking_atomic_write(path, content):
        if path == announce_file:
            job_id = content.strip()
            job = get_job(paths(), job_id)
            pid_file = job.dir / "pid"
            assert pid_file.is_file(), f"pid file must exist when job {job_id} is announced"
            seen_at_announce.append(json.loads(pid_file.read_text()))
        orig_atomic_write(path, content)

    monkeypatch.setattr(cli, "_atomic_write", checking_atomic_write)
    rc = main(["gate", "-d", str(repo), "--announce", str(announce_file), "--json"])

    assert rc == 0
    expected = {"pid": os.getpid(), "start_time": start_time(os.getpid())}
    assert len(seen_at_announce) == 1
    assert seen_at_announce[0] == expected

    assert seen_during_gate.is_file()
    assert json.loads(seen_during_gate.read_text()) == expected

    out = json.loads(capsys.readouterr().out)
    job = get_job(paths(), out["id"])
    assert (job.dir / "pid").is_file()
    assert json.loads((job.dir / "pid").read_text()) == expected


def test_cancel_running_gate_job_stops_it(env_sandbox, repo, tmp_path, capsys):
    import threading
    import time
    from datetime import datetime, timezone

    from agenthub.jobs import get_job
    from agenthub.orchestrate import _finish

    gate2_marker = tmp_path / "gate2_ran.txt"
    gate1_started = tmp_path / "gate1_started.txt"

    gate1_cmd = (
        f'{sys.executable} -c "'
        f"import pathlib, time; "
        f"pathlib.Path({str(gate1_started)!r}).write_text('started'); "
        f"time.sleep(30); "
        f'"'
    )
    set_repo_gates(repo, [gate1_cmd, f"touch {gate2_marker}"])

    announce_file = tmp_path / "announce.txt"
    cancel_output = []

    def cancel_when_started():
        deadline = time.time() + 10
        while time.time() < deadline and not gate1_started.exists():
            time.sleep(0.02)
        assert gate1_started.exists()

        job_id = announce_file.read_text().strip()
        rc = main(["cancel", job_id, "-d", str(repo)])
        assert rc == 0
        captured = capsys.readouterr()
        cancel_output.append(captured.out)

    thread = threading.Thread(target=cancel_when_started)
    thread.start()

    start_t = time.time()
    rc = main(["gate", "-d", str(repo), "--announce", str(announce_file), "--json"])
    duration = time.time() - start_t
    thread.join(timeout=5)

    assert rc == 130
    assert duration < 10, f"gate took {duration}s, sleep 30 was not stopped"
    assert not gate2_marker.exists(), "gate 2 should not have run"

    assert len(cancel_output) == 1
    assert f"sent SIGTERM to {os.getpid()}" in cancel_output[0]

    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "cancelled"
    assert out["exit_code"] == 130

    job = get_job(paths(), out["id"])
    rec = job.read_json("result.json")
    assert rec["status"] == "cancelled"
    assert rec["exit_code"] == 130

    # _finish does not overwrite a cancelled record
    def now_fn():
        return datetime.now(timezone.utc)

    finished_rec = _finish(job, {"id": job.id, "status": "running"}, "ok", now_fn, datetime.now(timezone.utc))
    assert finished_rec["status"] == "cancelled"
    assert job.read_json("result.json")["status"] == "cancelled"
