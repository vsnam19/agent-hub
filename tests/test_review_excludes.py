import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agenthub import guards
from agenthub.config import Pool, paths
from agenthub.jobs import list_jobs, new_job
from agenthub.orchestrate import RunArgs, implementers, readiness, run_job
from agenthub.plan import Plan, Task
from agenthub.planrun import _reviewers_besides, review_task
from agenthub.planstate import AttemptState, TaskState
from agenthub.pool import parse_link

BRIEF = "## Input\n\nfoo\n\n## Expected output\n\nbar\n"
_UNSET = object()


def now():
    return datetime.now(timezone.utc)


def make_pool(**kw):
    return Pool(
        roles={},
        quota_cooldown=kw.get("quota_cooldown", timedelta(minutes=60)),
        timeouts={"implement": timedelta(minutes=5), "review": timedelta(minutes=5)},
    )


def args_for(
    repo,
    links,
    role="implement",
    brief=BRIEF,
    timeout=timedelta(seconds=30),
    base=None,
    gates=None,
    exclude=None,
):
    return RunArgs(
        role=role,
        kind=role,
        brief=brief,
        workdir=repo,
        links=[parse_link(x) if isinstance(x, str) else x for x in links],
        timeout=timeout,
        base=base,
        gates=gates or [],
        exclude=exclude or [],
    )


def _record_implement_job(p, workdir, agent, model, status="ok", ts_offset=30, head=_UNSET, gate=None):
    t = now() - timedelta(seconds=ts_offset)
    job = new_job(p, "implement", t)
    if head is _UNSET:
        try:
            head = guards.head(guards.inspect(Path(workdir)))
        except Exception:
            head = None
    record = {
        "id": job.id,
        "role": "implement",
        "kind": "implement",
        "status": status,
        "workdir": str(Path(workdir).resolve()),
        "agent": agent,
        "model": model,
        "started": t.isoformat(),
        "ended": t.isoformat(),
        "attempts": [{"n": 1, "agent": agent, "model": model, "outcome": "ok", "rc": 0}],
        "changed_files": [],
        "diffstat": "",
        "reply_path": None,
        "head": head,
        "verdict": None,
    }
    if gate is not None:
        record["gate"] = gate
    job.write_json("result.json", record)
    return job


def _record_review_job(p, workdir, agent, model, verdict="approve", status="ok", ts_offset=10, head=_UNSET):
    t = now() - timedelta(seconds=ts_offset)
    job = new_job(p, "review", t)
    if head is _UNSET:
        try:
            head = guards.head(guards.inspect(Path(workdir)))
        except Exception:
            head = None
    record = {
        "id": job.id,
        "role": "review",
        "kind": "review",
        "status": status,
        "workdir": str(Path(workdir).resolve()),
        "agent": agent,
        "model": model,
        "started": t.isoformat(),
        "ended": t.isoformat(),
        "attempts": [{"n": 1, "agent": agent, "model": model, "outcome": "ok", "rc": 0}],
        "changed_files": [],
        "diffstat": "",
        "reply_path": None,
        "head": head,
        "verdict": verdict,
    }
    job.write_json("result.json", record)
    return job


def _record_gate_job(p, workdir, head, status="ok", ts_offset=20, gates=None):
    t = now() - timedelta(seconds=ts_offset)
    job = new_job(p, "gate", t)
    gates = gates or ["true"]
    record = {
        "id": job.id,
        "role": "gate",
        "kind": "gate",
        "status": status,
        "workdir": str(Path(workdir).resolve()),
        "agent": "agent-hub",
        "model": "gate",
        "head": head,
        "repo_gates": gates,
        "gate": [{"cmd": g, "rc": 0, "sha": head} for g in gates],
        "started": t.isoformat(),
        "ended": t.isoformat(),
    }
    job.write_json("result.json", record)
    return job


def test_review_skips_all_implementers(env_sandbox, repo):
    p = paths()
    # Two implement jobs by different models on one work tree:
    # Model A: agy:m1 (ok)
    _record_implement_job(p, repo, "agy", "m1", status="ok", ts_offset=40)
    # Model B: codex:m2 (gate-failed)
    _record_implement_job(p, repo, "codex", "m2", status="gate-failed", ts_offset=20)

    # 1. Review chain contains [agy:m1, codex:m2, claude:m3] -> skips both, runs on claude:m3
    review_args = args_for(repo, ["agy:m1", "codex:m2", "claude:m3"], role="review", base="master")
    result = run_job(review_args, p, make_pool(), now)

    assert result["status"] == "ok"
    assert (result["agent"], result["model"]) == ("claude", "m3")
    assert [(a["agent"], a["model"]) for a in result["attempts"]] == [("claude", "m3")]

    req_json = json.loads((p.jobs_dir / result["id"] / "request.json").read_text())
    assert req_json["links"] == ["claude:m3"]
    assert "agy:m1" in req_json["exclude"]
    assert "codex:m2" in req_json["exclude"]

    # 2. When only the implementers are in the chain, the job ends exhausted with a reason naming them
    exhausted_args = args_for(repo, ["agy:m1", "codex:m2"], role="review", base="master")
    exhausted_res = run_job(exhausted_args, p, make_pool(), now)
    assert exhausted_res["status"] == "exhausted"
    assert exhausted_res["exit_code"] == 4
    assert "agy:m1" in exhausted_res.get("reason", "")
    assert "codex:m2" in exhausted_res.get("reason", "")


def test_reviewers_besides_with_set(tmp_path):
    config_dir = paths().config_dir
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "pool.toml").write_text('[roles.review]\nkind = "review"\nchain = ["codex:m2", "claude:m3"]\n')
    # When passed a set of implementers, excludes all of them
    assert _reviewers_besides("review", {"codex:m2"}) == ["claude:m3"]
    assert _reviewers_besides("review", {"codex:m2", "claude:m3"}) == []
    # When passed a single string, works as before
    assert _reviewers_besides("review", "codex:m2") == ["claude:m3"]
    assert _reviewers_besides("review", "other:model") == ["codex:m2", "claude:m3"]


def test_plan_review_skips_fix_author(env_sandbox, repo, tmp_path, monkeypatch):
    # Configure review chain: [codex:m2, claude:m3] (i.e. [B, C])
    config_dir = paths().config_dir
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "pool.toml").write_text('[roles.review]\nkind = "review"\nchain = ["codex:m2", "claude:m3"]\n')

    p = paths()
    hub_cfg = repo / ".agent-hub"
    hub_cfg.mkdir(exist_ok=True)
    (hub_cfg / "config.toml").write_text('gates = ["true"]\n')
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "add repo gates"], cwd=repo, check=True)

    start_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()

    (repo / "change.txt").write_text("fixed code\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "fix change"], cwd=repo, check=True)

    # Task implemented by Model A (agy:m1)
    job_a = _record_implement_job(p, repo, "agy", "m1", status="ok", ts_offset=40)
    # Fix round by Model B (codex:m2)
    _record_implement_job(p, repo, "codex", "m2", status="ok", ts_offset=20)

    task = Task(name="t1", role="implement", gate=["true"])
    # Task state records first implementer as job_a
    tstate = TaskState(
        status="done",
        role="implement",
        worktree=str(repo),
        start_ref=start_commit,
        attempts=[AttemptState(job=job_a.id, status="ok", exit_code=0)],
        fix_rounds=1,
    )
    plan = Plan(goal="goal", repo=repo, base="master", slug="test-plan", tasks=[task], path=tmp_path / "plan.json")

    # Case 1: When review chain only contains the fix author [B] (codex:m2),
    # review_task reports no reviewer different from implementers without running review
    (config_dir / "pool.toml").write_text('[roles.review]\nkind = "review"\nchain = ["codex:m2"]\n')
    res_no_reviewer = review_task(plan, task, tstate, now)
    assert res_no_reviewer["gate"] == "ok"
    assert res_no_reviewer["review"] == "failed"
    assert "agy:m1" in res_no_reviewer["reason"]
    assert "codex:m2" in res_no_reviewer["reason"]

    # Case 2: Review chain is [B, C] -> review skips B and runs on C
    (config_dir / "pool.toml").write_text('[roles.review]\nkind = "review"\nchain = ["codex:m2", "claude:m3"]\n')
    monkeypatch.setattr("agenthub.handoff.parse_verdict", lambda text: "approve")
    res = review_task(plan, task, tstate, now)
    assert res["gate"] == "ok"
    assert res["review"] == "approve"

    review_job_record = json.loads((p.jobs_dir / res["job"] / "result.json").read_text())
    assert (review_job_record["agent"], review_job_record["model"]) == ("claude", "m3")

    req_json = json.loads((p.jobs_dir / res["job"] / "request.json").read_text())
    assert "agy:m1" in req_json["exclude"]
    assert "codex:m2" in req_json["exclude"]


def test_readiness_and_review_agree(env_sandbox, repo, tmp_path):
    p = paths()

    # Commit c1 on feat/x
    (repo / "c1.txt").write_text("c1\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, env=guards._git_env())
    subprocess.run(["git", "commit", "-m", "commit c1"], cwd=repo, check=True, env=guards._git_env())
    c1 = guards.head(guards.inspect(repo))

    # Commit c2 on feat/x
    (repo / "c2.txt").write_text("c2\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, env=guards._git_env())
    subprocess.run(["git", "commit", "-m", "commit c2"], cwd=repo, check=True, env=guards._git_env())
    c2 = guards.head(guards.inspect(repo))

    other_worktree = tmp_path / "other"
    other_worktree.mkdir()

    # Record implement jobs:
    # 1. agy:m1 on repo (ok)
    _record_implement_job(p, repo, "agy", "m1", status="ok", ts_offset=50, head=c1)
    # 2. codex:m2 on repo (gate-failed)
    _record_implement_job(p, repo, "codex", "m2", status="gate-failed", ts_offset=40, head=c2)
    # 3. claude:m3 on other_worktree (ok) -> different worktree, not an implementer of repo
    _record_implement_job(p, other_worktree, "claude", "m3", status="ok", ts_offset=30, head="other_head")
    # 4. gemini:m4 on repo (failed status -> should not count)
    t = now() - timedelta(seconds=20)
    j_fail = new_job(p, "implement", t)
    j_fail.write_json(
        "result.json",
        {
            "id": j_fail.id,
            "role": "implement",
            "kind": "implement",
            "status": "failed",
            "workdir": str(repo.resolve()),
            "agent": "gemini",
            "model": "m4",
            "head": c2,
            "started": t.isoformat(),
            "ended": t.isoformat(),
        },
    )

    jobs = list_jobs(p, 100000)
    expected_implementers = {"agy:m1", "codex:m2"}
    assert implementers(jobs, repo) == expected_implementers

    # run_job for review with candidate models:
    review_args = args_for(repo, ["agy:m1", "codex:m2", "claude:m3"], role="review", base="master")
    result = run_job(review_args, p, make_pool(), now)
    assert result["status"] == "ok"
    assert (result["agent"], result["model"]) == ("claude", "m3")

    req_json = json.loads((p.jobs_dir / result["id"] / "request.json").read_text())
    excluded_by_review = set(req_json["exclude"])
    assert excluded_by_review == expected_implementers

    # Gate on c2
    _record_gate_job(p, repo, c2)

    # An approving review by non-implementer claude:m3 is accepted by readiness
    _record_review_job(p, repo, "claude", "m3", verdict="approve", status="ok", ts_offset=10, head=c2)
    jobs_after_review = list_jobs(p, 100000)
    assert readiness(jobs_after_review, repo, c2) == []

    # An approving review by implementer agy:m1 is rejected as "reviewer is the implementer"
    _record_review_job(p, repo, "agy", "m1", verdict="approve", status="ok", ts_offset=5, head=c2)
    # Filter out claude:m3 review so only implementer approval is considered
    jobs_with_only_impl_review = [j for j in list_jobs(p, 100000) if j.get("agent") != "claude"]
    problems = readiness(jobs_with_only_impl_review, repo, c2)
    assert "reviewer is the implementer" in problems


def test_implementer_survives_history_rewrite(env_sandbox, repo):
    p = paths()
    # Initial commit / setup on feat/x
    (repo / "feature.txt").write_text("initial work\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, env=guards._git_env())
    subprocess.run(["git", "commit", "-m", "implement commit H1"], cwd=repo, check=True, env=guards._git_env())
    h1 = guards.head(guards.inspect(repo))

    # Implement job recorded at head H1
    _record_implement_job(
        p,
        repo,
        "agy",
        "m1",
        status="ok",
        ts_offset=40,
        head=h1,
        gate=[{"cmd": "true", "rc": 0, "sha": h1}],
    )

    # Branch amended so H1 is no longer an ancestor
    (repo / "feature.txt").write_text("amended work\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, env=guards._git_env())
    subprocess.run(["git", "commit", "--amend", "-m", "amended commit H2"], cwd=repo, check=True, env=guards._git_env())
    h2 = guards.head(guards.inspect(repo))
    assert h2 != h1

    # Verify H1 is no longer an ancestor of H2
    ancestor_check = subprocess.run(
        guards._git_argv("merge-base", "--is-ancestor", h1, h2),
        cwd=repo,
        capture_output=True,
        env=guards._git_env(),
    )
    assert ancestor_check.returncode != 0

    # Under fail-closed rule, agy:m1 is still an implementer
    jobs = list_jobs(p, 100000)
    assert implementers(jobs, repo) == {"agy:m1"}

    # A review excludes that model and selects claude:m3
    review_args = args_for(repo, ["agy:m1", "claude:m3"], role="review", base="master")
    result = run_job(review_args, p, make_pool(), now)
    assert result["status"] == "ok"
    assert (result["agent"], result["model"]) == ("claude", "m3")
    req_json = json.loads((p.jobs_dir / result["id"] / "request.json").read_text())
    assert "agy:m1" in req_json["exclude"]
    assert req_json["links"] == ["claude:m3"]

    # Passing gate on H2
    _record_gate_job(p, repo, h2)

    # Approving review by agy:m1 on H2 is refused by readiness
    _record_review_job(p, repo, "agy", "m1", verdict="approve", status="ok", ts_offset=10, head=h2)
    jobs_after_review = list_jobs(p, 100000)
    problems = readiness(jobs_after_review, repo, h2)
    assert "reviewer is the implementer" in problems


def test_plan_review_refusal_error_wins_over_reason(env_sandbox, repo, tmp_path, monkeypatch):
    hub_cfg = repo / ".agent-hub"
    hub_cfg.mkdir(exist_ok=True)
    (hub_cfg / "config.toml").write_text('gates = ["true"]\n')
    subprocess.run(["git", "add", "."], cwd=repo, check=True, env=guards._git_env())
    subprocess.run(["git", "commit", "-m", "add repo gates"], cwd=repo, check=True, env=guards._git_env())

    start_commit = guards.head(guards.inspect(repo))
    (repo / "change.txt").write_text("fixed code\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, env=guards._git_env())
    subprocess.run(["git", "commit", "-m", "fix change"], cwd=repo, check=True, env=guards._git_env())

    task = Task(name="t1", role="implement", gate=["true"])
    tstate = TaskState(
        status="done",
        role="implement",
        worktree=str(repo),
        start_ref=start_commit,
        attempts=[],
        fix_rounds=0,
    )
    plan = Plan(goal="goal", repo=repo, base="master", slug="test-plan", tasks=[task], path=tmp_path / "plan.json")

    monkeypatch.setattr(
        "agenthub.planrun.run_job",
        lambda *args, **kw: {
            "id": "job-refused",
            "status": "refused",
            "error": "refusal: cannot diff",
            "reason": "all links excluded: agy:m1",
        },
    )
    res = review_task(plan, task, tstate, now)
    assert res["reason"] == "refusal: cannot diff"
