import json
import subprocess

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
    assert "gate touched: 1 findings — inspect before merging" in ready_out["problems"]


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
    assert "gate touched: 1 findings — inspect before merging" in ready_out["problems"]


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
