import io
import json
import os
import re
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
    assert f"wait with agent-hub wait -d {repo} before ending the turn" in res["reason"]
    assert job.id in res["reason"]

    # Verify counter file was created and is 1
    counter_file = p.state_dir / "hooks" / "stop-sess-stop"
    assert counter_file.is_file()
    assert counter_file.read_text().splitlines()[0].strip() == "1"

    # Also test that a live plan runner blocks stop
    # First finish the job so only the plan runner is alive
    job.write_json("result.json", {"id": job.id, "role": "implement", "status": "ok", "workdir": str(repo)})
    plan_dir = p.state_dir / "plans" / "plan-runner-test"
    plan_dir.mkdir(parents=True, exist_ok=True)
    plan_file = plan_dir / "plan.json"
    plan_file.write_text(json.dumps({"slug": "plan-runner-test", "repo": str(repo)}))
    (plan_dir / "state.json").write_text(json.dumps({"runner": {"pid": pid, "start_time": st}, "tasks": {}}))
    res_plan = stop({"cwd": str(repo), "session_id": "sess-plan", "hook_event_name": "Stop"}, p)
    assert res_plan is not None
    assert res_plan.get("decision") == "block"
    assert "plan runner plan-runner-test" in res_plan["reason"]
    assert f"wait with agent-hub plan wait {plan_file} before ending the turn" in res_plan["reason"]


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


def test_session_start_skips_merged_plans(tmp_path):
    repo = make_repo(tmp_path / "repo", default="master", branch=None)
    # create orch/slug/task-1 branch from master
    subprocess.run(["git", "-C", str(repo), "checkout", "-b", "orch/slug/task-1"], check=True)
    (repo / "file1.txt").write_text("content 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "file1.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "task 1"], check=True)
    # merge into master
    subprocess.run(["git", "-C", str(repo), "checkout", "master"], check=True)
    subprocess.run(["git", "-C", str(repo), "merge", "orch/slug/task-1"], check=True)

    p = paths()
    plan_dir = p.state_dir / "plans" / "plan-merged"
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "plan.json").write_text(
        json.dumps(
            {
                "slug": "plan-merged",
                "repo": str(repo),
                "base": "master",
                "tasks": [{"name": "task-1"}],
            }
        )
    )
    (plan_dir / "state.json").write_text(
        json.dumps(
            {
                "slug": "plan-merged",
                "tasks": {"task-1": {"status": "done", "branch": "orch/slug/task-1"}},
            }
        )
    )

    payload = {"cwd": str(repo), "session_id": "sess-merged", "hook_event_name": "SessionStart"}
    res = session_start(payload, p)
    # Merged plan is not reported; when nothing else, returns None
    assert res is None

    # Even if branch was deleted, still not reported
    subprocess.run(["git", "-C", str(repo), "branch", "-D", "orch/slug/task-1"], check=True)
    res_deleted = session_start(payload, p)
    assert res_deleted is None

    # When another active plan exists, only the active plan is listed, no plan-merged line
    plan_dir2 = p.state_dir / "plans" / "plan-active"
    plan_dir2.mkdir(parents=True, exist_ok=True)
    (plan_dir2 / "plan.json").write_text(
        json.dumps(
            {
                "slug": "plan-active",
                "repo": str(repo),
                "base": "master",
                "tasks": [{"name": "task-pending"}],
            }
        )
    )
    res_active = session_start(payload, p)
    assert res_active is not None
    ctx = res_active["hookSpecificOutput"]["additionalContext"]
    assert "plan-active" in ctx
    assert "plan-merged" not in ctx


def test_session_start_lists_unmerged_done_plan(tmp_path):
    repo = make_repo(tmp_path / "repo", default="master", branch=None)
    # create orch/slug/task-2 branch with a commit not merged into master
    subprocess.run(["git", "-C", str(repo), "checkout", "-b", "orch/slug/task-2"], check=True)
    (repo / "file2.txt").write_text("content 2\n")
    subprocess.run(["git", "-C", str(repo), "add", "file2.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "task 2"], check=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "master"], check=True)

    p = paths()
    plan_dir = p.state_dir / "plans" / "plan-unmerged"
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "plan.json").write_text(
        json.dumps(
            {
                "slug": "plan-unmerged",
                "repo": str(repo),
                "base": "master",
                "tasks": [{"name": "task-2"}],
            }
        )
    )
    (plan_dir / "state.json").write_text(
        json.dumps(
            {
                "slug": "plan-unmerged",
                "tasks": {"task-2": {"status": "done", "branch": "orch/slug/task-2"}},
            }
        )
    )

    payload = {"cwd": str(repo), "session_id": "sess-unmerged", "hook_event_name": "SessionStart"}
    res = session_start(payload, p)
    assert res is not None
    ctx = res["hookSpecificOutput"]["additionalContext"]
    assert "plan-unmerged" in ctx
    assert "1 done" in ctx


def test_stop_reason_names_real_paths(tmp_path):
    repo = make_repo(tmp_path / "repo")
    p = paths()

    # Create running job
    job = new_job(p, "implement", datetime.now(timezone.utc))
    job.write_json(
        "result.json",
        {"id": job.id, "role": "implement", "status": "running", "workdir": str(repo)},
    )
    pid = os.getpid()
    st = start_time(pid)
    (job.dir / "pid").write_text(json.dumps({"pid": pid, "start_time": st}))

    # Create live plan runner
    plan_dir = p.state_dir / "plans" / "plan-real-paths"
    plan_dir.mkdir(parents=True, exist_ok=True)
    plan_file = plan_dir / "plan.json"
    plan_file.write_text(json.dumps({"slug": "plan-real-paths", "repo": str(repo)}))
    (plan_dir / "state.json").write_text(json.dumps({"runner": {"pid": pid, "start_time": st}, "tasks": {}}))

    payload = {"cwd": str(repo), "session_id": "sess-paths", "hook_event_name": "Stop"}
    res = stop(payload, p)
    assert res is not None
    assert res.get("decision") == "block"
    reason = res["reason"]
    assert "<wt>" not in reason
    assert "<plan>" not in reason
    assert f"agent-hub wait -d {repo}" in reason
    assert f"agent-hub plan wait {plan_file}" in reason
    assert f"wait with agent-hub wait -d {repo} / agent-hub plan wait {plan_file} before ending the turn" in reason


def test_stop_counter_file_name_is_sanitised(tmp_path):
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

    unsafe_session_id = "session/with:invalid@chars!#$123"
    payload = {"cwd": str(repo), "session_id": unsafe_session_id, "hook_event_name": "Stop"}
    res = stop(payload, p)
    assert res is not None
    assert res.get("decision") == "block"

    expected_file_name = "stop-" + re.sub(r"[^A-Za-z0-9_-]", "_", unsafe_session_id)
    counter_file = p.state_dir / "hooks" / expected_file_name
    assert counter_file.is_file()


def test_stop_counter_resets_when_running_set_changes(tmp_path):
    repo = make_repo(tmp_path / "repo")
    p = paths()

    job1 = new_job(p, "implement", datetime.now(timezone.utc))
    job1.write_json(
        "result.json",
        {"id": job1.id, "role": "implement", "status": "running", "workdir": str(repo)},
    )
    pid = os.getpid()
    st = start_time(pid)
    (job1.dir / "pid").write_text(json.dumps({"pid": pid, "start_time": st}))

    payload = {"cwd": str(repo), "session_id": "sess-reset", "hook_event_name": "Stop"}
    # Call 20 times -> blocks
    for _ in range(20):
        res = stop(payload, p)
        assert res.get("decision") == "block"

    # 21st call -> gave up
    res21 = stop(payload, p)
    assert "decision" not in res21
    assert "systemMessage" in res21

    # Now a second job starts running
    job2 = new_job(p, "review", datetime.now(timezone.utc))
    job2.write_json(
        "result.json",
        {"id": job2.id, "role": "review", "status": "running", "workdir": str(repo)},
    )
    (job2.dir / "pid").write_text(json.dumps({"pid": pid, "start_time": st}))

    # Next call with changed running set -> counter resets and it blocks again!
    res_new = stop(payload, p)
    assert res_new is not None
    assert res_new.get("decision") == "block"
    assert job1.id in res_new["reason"]
    assert job2.id in res_new["reason"]
    counter_file = p.state_dir / "hooks" / "stop-sess-reset"
    assert counter_file.read_text().splitlines()[0].strip() == "1"


def test_autopilot_only_from_cwd_repo(tmp_path):
    repo = make_repo(tmp_path / "repo")
    wt = tmp_path / "wt-other"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", str(wt), "-b", "other-wt"], check=True)

    # Put AUTOPILOT in other worktree only
    wt_docs = wt / "docs"
    wt_docs.mkdir(parents=True, exist_ok=True)
    (wt_docs / "next-phase.md").write_text("# AUTOPILOT — not in repo of cwd\n")

    p = paths()
    # Call session_start from repo (which doesn't have AUTOPILOT in docs/next-phase.md)
    payload = {"cwd": str(repo), "session_id": "sess-auto", "hook_event_name": "SessionStart"}
    res = session_start(payload, p)
    # Should not pick up autopilot from the other worktree
    assert res is None

    # Now put AUTOPILOT in the repo of cwd
    repo_docs = repo / "docs"
    repo_docs.mkdir(parents=True, exist_ok=True)
    (repo_docs / "next-phase.md").write_text("# AUTOPILOT — in cwd repo\n")

    res2 = session_start(payload, p)
    assert res2 is not None
    ctx = res2["hookSpecificOutput"]["additionalContext"]
    assert "owner's resume protocol: docs/next-phase.md AUTOPILOT" in ctx
