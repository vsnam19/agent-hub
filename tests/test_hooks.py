import io
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

from agenthub.config import paths
from agenthub.hooks import main, session_start, stop
from agenthub.jobs import new_job
from agenthub.procinfo import start_time
from tests.gitrepo import make_repo


def test_session_start_lists_running_jobs(tmp_path):
    repo = make_repo(tmp_path / "repo")
    p = paths()

    # Create a running job with live process
    job = new_job(p, "implement", datetime.now(timezone.utc))
    job.write_json(
        "result.json",
        {"id": job.id, "role": "implement", "status": "running", "workdir": str(repo)},
    )
    pid = os.getpid()
    st = start_time(pid)
    (job.dir / "pid").write_text(json.dumps({"pid": pid, "start_time": st}))

    # Create a plan with tasks not yet merged
    plan_dir = p.state_dir / "plans" / "plan-demo"
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "plan.json").write_text(
        json.dumps(
            {
                "slug": "plan-demo",
                "repo": str(repo),
                "tasks": [{"name": "task-a"}],
            }
        )
    )
    (plan_dir / "state.json").write_text(
        json.dumps(
            {
                "slug": "plan-demo",
                "tasks": {"task-a": {"status": "pending"}},
            }
        )
    )

    payload = {"cwd": str(repo), "session_id": "sess-1", "hook_event_name": "SessionStart"}
    res = session_start(payload, p)
    assert res is not None
    assert "hookSpecificOutput" in res
    assert res["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    ctx = res["hookSpecificOutput"]["additionalContext"]
    assert job.id in ctx
    assert "implement" in ctx
    assert str(repo) in ctx
    assert "plan-demo" in ctx
    assert "pending" in ctx


def test_session_start_mentions_autopilot(tmp_path):
    repo = make_repo(tmp_path / "repo")
    p = paths()
    docs = repo / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "next-phase.md").write_text("# AUTOPILOT — next steps\nSome plan here\n")

    payload = {"cwd": str(repo), "session_id": "sess-1", "hook_event_name": "SessionStart"}
    res = session_start(payload, p)
    assert res is not None
    ctx = res["hookSpecificOutput"]["additionalContext"]
    assert "owner's resume protocol: docs/next-phase.md AUTOPILOT" in ctx


def test_session_start_silent_when_idle(tmp_path):
    repo = make_repo(tmp_path / "repo")
    p = paths()
    payload = {"cwd": str(repo), "session_id": "sess-1", "hook_event_name": "SessionStart"}
    assert session_start(payload, p) is None

    # Via main: should print nothing and return 0
    stdin = io.StringIO(json.dumps(payload))
    stdout = io.StringIO()
    rc = main("session-start", stdin, stdout, p=p)
    assert rc == 0
    assert stdout.getvalue() == ""


def test_stop_blocks_while_job_alive(tmp_path):
    repo = make_repo(tmp_path / "repo")
    p = paths()
    job = new_job(p, "implement", datetime.now(timezone.utc))
    job.write_json(
        "result.json",
        {"id": job.id, "role": "implement", "status": "running", "workdir": str(repo)},
    )
    pid = os.getpid()
    st = start_time(pid)
    (job.dir / "pid").write_text(json.dumps({"pid": pid, "start_time": st}))

    payload = {"cwd": str(repo), "session_id": "sess-stop", "hook_event_name": "Stop", "stop_hook_active": True}
    res = stop(payload, p)
    assert res is not None
    assert res.get("decision") == "block"
    assert "reason" in res
    assert "wait with agent-hub wait -d <wt> / agent-hub plan wait <plan> before ending the turn" in res["reason"]
    assert job.id in res["reason"]

    # Verify counter file was created and is 1
    counter_file = p.state_dir / "hooks" / "stop-sess-stop"
    assert counter_file.is_file()
    assert counter_file.read_text().strip() == "1"

    # Also test that a live plan runner blocks stop
    # First finish the job so only the plan runner is alive
    job.write_json("result.json", {"id": job.id, "role": "implement", "status": "ok", "workdir": str(repo)})
    plan_dir = p.state_dir / "plans" / "plan-runner-test"
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "plan.json").write_text(json.dumps({"slug": "plan-runner-test", "repo": str(repo)}))
    (plan_dir / "state.json").write_text(json.dumps({"runner": {"pid": pid, "start_time": st}, "tasks": {}}))
    res_plan = stop({"cwd": str(repo), "session_id": "sess-plan", "hook_event_name": "Stop"}, p)
    assert res_plan is not None
    assert res_plan.get("decision") == "block"
    assert "plan runner plan-runner-test" in res_plan["reason"]


def test_stop_ignores_dead_job(tmp_path):
    repo = make_repo(tmp_path / "repo")
    p = paths()
    job = new_job(p, "implement", datetime.now(timezone.utc))
    job.write_json(
        "result.json",
        {"id": job.id, "role": "implement", "status": "running", "workdir": str(repo)},
    )
    (job.dir / "pid").write_text(json.dumps({"pid": 999999999, "start_time": 12345}))

    payload = {"cwd": str(repo), "session_id": "sess-dead", "hook_event_name": "Stop"}
    res = stop(payload, p)
    assert res is None


def test_stop_allows_when_idle(tmp_path):
    repo = make_repo(tmp_path / "repo")
    p = paths()
    counter_file = p.state_dir / "hooks" / "stop-sess-idle"
    counter_file.parent.mkdir(parents=True, exist_ok=True)
    counter_file.write_text("10")

    payload = {"cwd": str(repo), "session_id": "sess-idle", "hook_event_name": "Stop"}
    res = stop(payload, p)
    assert res is None
    # Counter file must be deleted when nothing runs
    assert not counter_file.exists()


def test_stop_block_limit(tmp_path):
    repo = make_repo(tmp_path / "repo")
    p = paths()
    job = new_job(p, "implement", datetime.now(timezone.utc))
    job.write_json(
        "result.json",
        {"id": job.id, "role": "implement", "status": "running", "workdir": str(repo)},
    )
    pid = os.getpid()
    st = start_time(pid)
    (job.dir / "pid").write_text(json.dumps({"pid": pid, "start_time": st}))

    payload = {"cwd": str(repo), "session_id": "sess-limit", "hook_event_name": "Stop"}
    for _ in range(20):
        res = stop(payload, p)
        assert res is not None
        assert res.get("decision") == "block"

    # 21st call: must give up without blocking
    res21 = stop(payload, p)
    assert res21 is not None
    assert "decision" not in res21
    assert "systemMessage" in res21
    assert "agent-hub: stop guard gave up after 20 blocks; " in res21["systemMessage"]
    assert job.id in res21["systemMessage"]


def test_hook_never_raises_on_bad_stdin():
    stdout = io.StringIO()
    rc = main("stop", io.StringIO("not-valid-json{"), stdout)
    assert rc == 0
    assert stdout.getvalue() == ""

    stdout = io.StringIO()
    rc = main("session-start", io.StringIO(""), stdout)
    assert rc == 0
    assert stdout.getvalue() == ""

    stdout = io.StringIO()
    rc = main("session-start", io.StringIO("[]"), stdout)
    assert rc == 0
    assert stdout.getvalue() == ""

    # Also test via subprocess: python -m agenthub hook stop with garbage stdin
    proc = subprocess.run(
        [sys.executable, "-m", "agenthub", "hook", "stop"],
        input="invalid json text\n",
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert proc.stdout == ""

    proc_start = subprocess.run(
        [sys.executable, "-m", "agenthub", "hook", "session-start"],
        input="invalid json text\n",
        capture_output=True,
        text=True,
    )
    assert proc_start.returncode == 0
    assert proc_start.stdout == ""
