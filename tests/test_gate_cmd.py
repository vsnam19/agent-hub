import json
import subprocess

from agenthub.cli import main
from agenthub.config import paths
from agenthub.jobs import list_jobs


def git_head(repo):
    res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True)
    return res.stdout.strip()


def test_gate_cmd_refuses_dirty_tree(env_sandbox, repo, capsys):
    (repo / "dirty.txt").write_text("x\n")

    rc = main(["gate", "-d", str(repo), "--gate", "true"])

    assert rc == 2
    assert "commit first; gate runs on HEAD" in capsys.readouterr().err
    assert list_jobs(paths(), 10) == []


def test_gate_cmd_records_head_and_sha(env_sandbox, repo, capsys):
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


def test_gate_cmd_failure_exit_9(env_sandbox, repo, capsys):
    rc = main(["gate", "-d", str(repo), "--gate", "echo nope; exit 4", "--gate", "true", "--json"])

    out = json.loads(capsys.readouterr().out)
    assert rc == 9
    assert out["status"] == "gate-failed"
    assert out["exit_code"] == 9
    assert [g["rc"] for g in out["gate"]] == [4]
    assert out["gate"][0]["sha"] == out["head"]


def test_gate_cmd_no_gates_exit_2(env_sandbox, repo, capsys):
    rc = main(["gate", "-d", str(repo)])

    assert rc == 2
    assert "no gates: pass --gate or set .agent-hub/config.toml" in capsys.readouterr().err
    assert list_jobs(paths(), 10) == []


def test_gate_cmd_repo_gates_run_before_cli_gates(env_sandbox, repo, capsys):
    (repo / ".agent-hub").mkdir()
    (repo / ".agent-hub" / "config.toml").write_text('gates = ["test -f README.md"]\n')
    subprocess.run(["git", "add", ".agent-hub"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "repo gates"], cwd=repo, check=True)

    rc = main(["gate", "-d", str(repo), "--gate", "true", "--gate", "test -f README.md", "--json"])

    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert [g["cmd"] for g in out["gate"]] == ["test -f README.md", "true"]
