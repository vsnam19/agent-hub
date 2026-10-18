import json
import os
import subprocess
import sys
from pathlib import Path

import agenthub.planrun
from agenthub.cli import main
from agenthub.jobs import new_job
from agenthub.planrun import rejected_context


def make_repo(path: Path, default: str = "master", branch: str | None = "feat/x") -> Path:
    path.mkdir(parents=True, exist_ok=True)

    def run_git(*args):
        subprocess.run(
            ["git", *args],
            cwd=path,
            check=True,
            capture_output=True,
            text=True,
            env={"GIT_TERMINAL_PROMPT": "0"},
        )

    run_git("init", "-b", default)
    run_git("config", "user.name", "Test User")
    run_git("config", "user.email", "test@example.com")
    run_git("config", "commit.gpgsign", "false")

    (path / "README.md").write_text("Hello\n")
    run_git("add", "README.md")
    run_git("commit", "-m", "Initial commit")

    if branch is not None:
        run_git("checkout", "-b", branch)

    return path


def make_plan_file(tmp_path: Path, repo_dir: Path, content: dict | None = None) -> Path:
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(exist_ok=True)
    plan_file = plan_dir / "plan.json"
    if content is None:
        content = {
            "goal": "do it",
            "repo": str(repo_dir),
            "base": "master",
            "slug": "plan-1",
            "tasks": [
                {"name": "t1", "role": "implement", "brief": "b1.md", "gate": ["test -f agent_wrote.txt"]},
                {"name": "t2", "role": "implement", "brief": "b2.md", "depends_on": ["t1"]},
            ],
        }
    plan_file.write_text(json.dumps(content))
    (plan_dir / "b1.md").write_text("## Input\nstuff\n## Expected output\nstuff\n")
    (plan_dir / "b2.md").write_text("## Input\nstuff\n## Expected output\nstuff\n")
    return plan_file


def run_cli_plan(env_sandbox, plan_file: Path, cmd: str, extra_args=None, extra_env=None):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    env["AGENT_HUB_PLAN_POLL_S"] = "0.1"
    if extra_env:
        env.update(extra_env)
    argv = [sys.executable, "-m", "agenthub", "plan", cmd, str(plan_file)]
    if extra_args:
        argv.extend(extra_args)
    return subprocess.run(argv, env=env, capture_output=True, text=True)


def make_gated_plan_run(env_sandbox, tmp_path: Path):
    repo = tmp_path / "repo"
    make_repo(repo, branch=None)
    (repo / ".agent-hub").mkdir()
    (repo / ".agent-hub" / "config.toml").write_text('gates = ["test -f README.md"]\n')
    (repo / ".gitignore").write_text(".worktrees/\n")
    subprocess.run(["git", "add", ".agent-hub", ".gitignore"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "repo gates"], cwd=repo, check=True)
    plan_file = make_plan_file(tmp_path, repo)
    env = {"FAKE_CLAUDE_SCENARIO": "handoff", "FAKE_AGY_SCENARIO": "handoff", "FAKE_CODEX_SCENARIO": "handoff"}
    run_cli_plan(env_sandbox, plan_file, "run", extra_env=env)
    state = json.loads((plan_file.parent / "state.json").read_text())
    assert state["tasks"]["t1"]["status"] == "done"
    assert state["tasks"]["t2"]["status"] == "done"
    return plan_file, state


def fake_review(monkeypatch, verdict: str):
    calls = []

    def run(args, p, pool, now):
        calls.append(args)
        job = new_job(p, "review", now())
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=args.workdir, capture_output=True, text=True, check=True
        ).stdout.strip()
        record = {
            "id": job.id,
            "role": "review",
            "kind": "review",
            "status": "ok",
            "verdict": verdict,
            "head": head,
            "workdir": str(args.workdir),
            "agent": "reviewer-agent",
            "model": "reviewer-model",
            "error": "",
        }
        job.write_json("result.json", record)
        return record

    monkeypatch.setattr(agenthub.planrun, "run_job", run)
    return calls


def test_rejected_context_empty_without_files(tmp_path: Path):
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()

    # Case 1: no fixes directory exists
    assert rejected_context(plan_dir, "t1") == ""

    fixes_dir = plan_dir / "fixes"
    fixes_dir.mkdir()

    # Case 2: fixes directory is empty
    assert rejected_context(plan_dir, "t1") == ""

    # Case 3: fixes directory has rejected files for other tasks only
    (fixes_dir / "other-1-rejected.md").write_text("- F1 (medium): finding for other\n")
    assert rejected_context(plan_dir, "t1") == ""

    # Case 4: fixes directory has fix brief for t1 (not rejected)
    (fixes_dir / "t1-1.md").write_text("## Goal\nFix t1\n")
    assert rejected_context(plan_dir, "t1") == ""

    # Case 5: non-numeric round rejected file
    (fixes_dir / "t1-round1-rejected.md").write_text("- F1 (low): bad name\n")
    assert rejected_context(plan_dir, "t1") == ""

    # Case 6: matching file is empty
    (fixes_dir / "t1-1-rejected.md").write_text("   \n")
    assert rejected_context(plan_dir, "t1") == ""


def test_rejected_context_in_round_order(tmp_path: Path):
    plan_dir = tmp_path / "plan"
    fixes_dir = plan_dir / "fixes"
    fixes_dir.mkdir(parents=True)

    # Write files out of numeric order: round 10, then round 2, then round 1
    (fixes_dir / "t1-10-rejected.md").write_text("- F10 (low): tenth round finding\n")
    (fixes_dir / "t1-2-rejected.md").write_text("- F2 (medium): second round finding\n")
    (fixes_dir / "t1-1-rejected.md").write_text(
        "- F1 (high): first round finding line 1\n- F1b (minor): first round finding line 2\n"
    )

    result = rejected_context(plan_dir, "t1")
    expected = (
        "## Already answered (rejected findings; repeat one only with new evidence)\n"
        "- F1 (high): first round finding line 1\n"
        "- F1b (minor): first round finding line 2\n"
        "- F2 (medium): second round finding\n"
        "- F10 (low): tenth round finding\n"
    )
    assert result == expected


def test_review_task_passes_context_as_brief(env_sandbox, tmp_path: Path, monkeypatch, capsys):
    plan_file, state = make_gated_plan_run(env_sandbox, tmp_path)
    fixes_dir = plan_file.parent / "fixes"
    fixes_dir.mkdir(exist_ok=True)
    (fixes_dir / "t1-1-rejected.md").write_text("- F1 (medium): bad stuff\n")

    calls = fake_review(monkeypatch, "approve")
    rc = main(["plan", "review", str(plan_file)])

    assert rc == 0
    assert len(calls) == 2

    expected_t1 = (
        "## Already answered (rejected findings; repeat one only with new evidence)\n- F1 (medium): bad stuff\n"
    )
    assert calls[0].brief == expected_t1
    assert calls[1].brief == ""
