import json
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agenthub import handoff, orchestrate
from agenthub.brief import BriefError
from agenthub.config import ConfigError, Pool, paths
from agenthub.jobs import list_jobs
from agenthub.orchestrate import EXIT, RunArgs, run_job
from agenthub.pool import Cooldowns, Link, parse_link
from tests.conftest import fake_calls

BRIEF = "## Input\n\nfoo\n\n## Expected output\n\nbar\n"
FAKES = Path(__file__).parent / "fakes"


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
    jev=False,
    allow=None,
    skills=None,
):
    return RunArgs(
        role=role,
        brief=brief,
        workdir=repo,
        links=[parse_link(x) if isinstance(x, str) else x for x in links],
        timeout=timeout,
        base=base,
        gates=gates or [],
        jev=jev,
        allow=allow or [],
        skills=skills or [],
    )


def go(repo, links, **kw):
    return run_job(args_for(repo, links, **kw), paths(), make_pool(), now)


def scenario(monkeypatch, name, value):
    monkeypatch.setenv(f"FAKE_{name.upper()}_SCENARIO", value)


def job_dir(result):
    return paths().jobs_dir / result["id"]


def test_implement_ok_records_everything(env_sandbox, repo):
    # the fake agy does not write; use the write scenario so the tree has changes to report
    os.environ["FAKE_AGY_SCENARIO"] = "write"
    result = go(repo, ["agy:m1"])
    assert result["status"] == "ok"
    assert result["exit_code"] == 0
    assert (result["agent"], result["model"]) == ("agy", "m1")
    assert result["changed_files"] == ["agent_wrote.txt"]
    assert "untracked" in result["diffstat"]
    assert result["workdir"] == str(repo)
    assert result["base"] is None
    assert len(result["attempts"]) == 1
    att = result["attempts"][0]
    assert att["n"] == 1 and att["outcome"] == "ok" and att["rc"] == 0 and att["duration_s"] >= 0
    jd = job_dir(result)
    assert (jd / "brief.md").read_text().startswith(BRIEF)
    from agenthub.procinfo import start_time

    assert json.loads((jd / "pid").read_text()) == {"pid": os.getpid(), "start_time": start_time(os.getpid())}
    req = json.loads((jd / "request.json").read_text())
    assert req["role"] == "implement" and req["links"] == ["agy:m1"] and req["timeout_s"] == 30
    assert json.loads((jd / "result.json").read_text()) == result
    reply = Path(result["reply_path"])
    assert reply.read_text().startswith("done:")
    assert reply.parent.name == "1-agy-m1"
    attempt_meta = json.loads((reply.parent / "attempt.json").read_text())
    assert attempt_meta["n"] == 1 and attempt_meta["agent"] == "agy" and attempt_meta["pgid"] > 0
    assert len(fake_calls(env_sandbox.fakelog, "agy")) == 1


def test_implement_refused_on_default_branch(env_sandbox, repo):
    import subprocess

    subprocess.run(["git", "checkout", "master"], cwd=repo, check=True, capture_output=True)
    result = go(repo, ["agy:m1"])
    assert result["status"] == "refused"
    assert result["exit_code"] == EXIT["refused"] == 3
    assert result["attempts"] == []
    assert fake_calls(env_sandbox.fakelog, "agy") == []
    assert (job_dir(result) / "result.json").is_file()
    assert (job_dir(result) / "brief.md").read_text().startswith(BRIEF)


def test_refused_run_is_refused_despite_broken_workers_toml(env_sandbox, repo, tmp_path):
    import subprocess

    missing = tmp_path / "no-such-plugin"
    (env_sandbox.config / "workers.toml").write_text(f'default = ["ghost"]\n[plugins]\nghost = "{missing}"\n')
    subprocess.run(["git", "checkout", "master"], cwd=repo, check=True, capture_output=True)
    result = go(repo, ["agy:m1"])
    assert result["status"] == "refused"
    assert result["exit_code"] == EXIT["refused"] == 3
    assert result["error"]
    assert result["attempts"] == []
    assert fake_calls(env_sandbox.fakelog, "agy") == []


def test_fallback_on_quota_sets_cooldown(env_sandbox, repo, monkeypatch):
    scenario(monkeypatch, "agy", "quota,ok")
    result = go(repo, ["agy:m1", "agy:m2"])
    assert result["status"] == "ok"
    assert [a["outcome"] for a in result["attempts"]] == ["quota", "ok"]
    assert result["model"] == "m2"
    until = Cooldowns(paths().cooldowns_file).until(Link("agy", "m1"), now())
    assert until is not None
    assert timedelta(minutes=59) < until - now() <= timedelta(minutes=60)


def test_quota_reset_time_used_for_cooldown(env_sandbox, repo, monkeypatch):
    scenario(monkeypatch, "codex", "quota-reset")
    result = go(repo, ["codex:m1"])
    assert result["status"] == "exhausted"
    until = Cooldowns(paths().cooldowns_file).until(Link("codex", "m1"), now())
    assert until is not None
    # resets_in_seconds = 3600, quota_cooldown default is 60m: use a shorter default to tell them apart
    pool = Pool(roles={}, quota_cooldown=timedelta(minutes=5), timeouts={"implement": timedelta(minutes=5)})
    Cooldowns(paths().cooldowns_file).clear(Link("codex", "m1"))
    scenario(monkeypatch, "agy", "quota-reset")
    run_job(args_for(repo, ["agy:m1"]), paths(), pool, now)
    until = Cooldowns(paths().cooldowns_file).until(Link("agy", "m1"), now())
    assert until is not None
    assert until - now() > timedelta(hours=100)  # "Resets in 117h47m44s", not the 5 minute default


def test_fallback_on_auth_no_cooldown(env_sandbox, repo, monkeypatch):
    scenario(monkeypatch, "agy", "auth,ok")
    result = go(repo, ["agy:m1", "agy:m2"])
    assert result["status"] == "ok"
    assert [a["outcome"] for a in result["attempts"]] == ["auth", "ok"]
    assert Cooldowns(paths().cooldowns_file).all(now()) == {}


def test_fallback_on_model(env_sandbox, repo, monkeypatch):
    scenario(monkeypatch, "agy", "model,ok")
    result = go(repo, ["agy:m1", "agy:m2"])
    assert result["status"] == "ok"
    assert [a["outcome"] for a in result["attempts"]] == ["model-unavailable", "ok"]
    assert Cooldowns(paths().cooldowns_file).all(now()) == {}


def test_review_falls_through_on_permission_denied(env_sandbox, repo, monkeypatch):
    scenario(monkeypatch, "agy", "denied,ok")
    result = go(repo, ["agy:m1", "agy:m2"], role="review", brief="")
    assert result["status"] == "ok"
    assert result["model"] == "m2"
    first, second = result["attempts"]
    assert first["outcome"] == "failed" and first["fallthrough"] == "permission auto-denied"
    assert "fallthrough" not in second and second["outcome"] == "ok"
    assert len(fake_calls(env_sandbox.fakelog, "agy")) == 2


def test_implement_does_not_fall_through_on_permission_denied(env_sandbox, repo, monkeypatch):
    scenario(monkeypatch, "agy", "denied,ok")
    result = go(repo, ["agy:m1", "agy:m2"])
    assert result["status"] == "failed"
    assert result["exit_code"] == 1
    assert len(result["attempts"]) == 1
    assert result["attempts"][0]["outcome"] == "failed"
    assert "fallthrough" not in result["attempts"][0]
    assert len(fake_calls(env_sandbox.fakelog, "agy")) == 1


def test_review_permission_denied_on_last_link_is_failed(env_sandbox, repo, monkeypatch):
    scenario(monkeypatch, "agy", "denied")
    result = go(repo, ["agy:m1", "agy:m2"], role="review", brief="")
    assert result["status"] == "failed"
    assert [a["outcome"] for a in result["attempts"]] == ["failed", "failed"]
    assert result["attempts"][0]["fallthrough"] == "permission auto-denied"
    assert "fallthrough" in result["attempts"][1]


def test_skip_not_installed(env_sandbox, repo, monkeypatch, tmp_path):
    bindir = tmp_path / "onlyclaude"
    bindir.mkdir()
    (bindir / "claude").symlink_to(FAKES / "claude")
    git = Path(shutil.which("git")).parent
    py = Path(shutil.which("python3")).parent
    monkeypatch.setenv("PATH", f"{bindir}:{git}:{py}:/usr/bin:/bin")
    result = go(repo, ["agy:m1", "claude:sonnet"])
    assert result["status"] == "ok"
    first, second = result["attempts"]
    assert first["outcome"] == "skipped" and first["reason"] == "not-installed" and first["n"] == 1
    assert second["outcome"] == "ok" and second["n"] == 2
    assert fake_calls(env_sandbox.fakelog, "agy") == []


def test_skip_cooldown(env_sandbox, repo):
    until = now() + timedelta(hours=1)
    Cooldowns(paths().cooldowns_file).set(Link("agy", "m1"), until)
    result = go(repo, ["agy:m1", "codex:m2"])
    assert result["status"] == "ok"
    first = result["attempts"][0]
    assert first["outcome"] == "skipped"
    assert first["reason"].startswith("cooldown until ")
    assert fake_calls(env_sandbox.fakelog, "agy") == []
    assert len(fake_calls(env_sandbox.fakelog, "codex")) == 1


def test_no_fallback_on_fail(env_sandbox, repo, monkeypatch):
    scenario(monkeypatch, "agy", "fail,ok")
    result = go(repo, ["agy:m1", "agy:m2"])
    assert result["status"] == "failed"
    assert result["exit_code"] == 1
    assert len(result["attempts"]) == 1
    assert len(fake_calls(env_sandbox.fakelog, "agy")) == 1


def test_timeout_status_and_no_fallback(env_sandbox, repo, monkeypatch):
    from agenthub import process

    monkeypatch.setattr(process, "KILL_GRACE_S", 0.5)
    scenario(monkeypatch, "codex", "slow,ok")
    result = go(repo, ["codex:m1", "codex:m2"], timeout=timedelta(seconds=1))
    assert result["status"] == "timeout"
    assert result["exit_code"] == 6
    assert len(result["attempts"]) == 1
    assert result["attempts"][0]["outcome"] == "timeout"


def test_empty_status(env_sandbox, repo, monkeypatch):
    scenario(monkeypatch, "claude", "empty")
    result = go(repo, ["claude:sonnet"])
    assert result["status"] == "empty"
    assert result["exit_code"] == 7


def test_write_then_quota_is_interrupted(env_sandbox, repo, monkeypatch):
    scenario(monkeypatch, "agy", "write-quota,ok")
    result = go(repo, ["agy:m1", "agy:m2"])
    assert result["status"] == "interrupted"
    assert result["exit_code"] == 8
    assert len(result["attempts"]) == 1
    assert result["changed_files"] == ["agent_wrote.txt"]
    assert Cooldowns(paths().cooldowns_file).until(Link("agy", "m1"), now()) is not None
    assert len(fake_calls(env_sandbox.fakelog, "agy")) == 1


def test_exhausted_when_all_quota(env_sandbox, repo, monkeypatch):
    scenario(monkeypatch, "agy", "quota")
    scenario(monkeypatch, "codex", "quota")
    result = go(repo, ["agy:m1", "codex:m2"])
    assert result["status"] == "exhausted"
    assert result["exit_code"] == 4
    assert [a["outcome"] for a in result["attempts"]] == ["quota", "quota"]


def test_review_ok_default_base(env_sandbox, repo):
    result = go(repo, ["codex:m1"], role="review", brief="")
    assert result["status"] == "ok"
    assert result["base"] == "master"
    assert result["changed_files"] == []
    assert result["diffstat"] == ""


def test_review_modified_tree(env_sandbox, repo, monkeypatch):
    scenario(monkeypatch, "codex", "write")
    result = go(repo, ["codex:m1", "agy:m2"], role="review", brief="")
    assert result["status"] == "modified-tree"
    assert result["exit_code"] == 5
    assert len(result["attempts"]) == 1
    assert fake_calls(env_sandbox.fakelog, "agy") == []


def test_review_brief_includes_preamble(env_sandbox, repo):
    result = go(repo, ["codex:m1"], role="review", brief="Focus on locking.", base="master")
    assert result["status"] == "ok"
    sent = fake_calls(env_sandbox.fakelog, "codex")[0]["stdin"]
    assert str(job_dir(result) / "diff.patch") in sent
    assert "git diff" not in sent
    assert "Focus on locking." in sent
    assert (job_dir(result) / "brief.md").read_text() == sent


def test_review_brief_lists_touched_findings(env_sandbox, repo):
    import subprocess

    from agenthub.jobs import new_job

    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    new_job(paths(), "gate", now()).write_json(
        "result.json",
        {
            "kind": "gate",
            "status": "ok",
            "workdir": str(repo),
            "head": head,
            "gate": [{"cmd": "true", "rc": 0, "sha": head}],
            "repo_gates": ["true"],
            "gate_touched": [{"file": "test_a.py", "kind": "assert-removed", "lines": [2]}],
        },
    )
    result = go(repo, ["codex:m1"], role="review", brief="", base="master")
    sent = (job_dir(result) / "brief.md").read_text()
    assert "## Gate-touched findings to judge" in sent
    assert "- test_a.py assert-removed lines 2" in sent
    assert "`## Gate-touched` section" in sent
    assert sent.index("## Gate-touched findings to judge") < sent.index("## Findings")


def test_review_brief_without_touched_findings_has_no_section(env_sandbox, repo):
    result = go(repo, ["codex:m1"], role="review", brief="", base="master")
    assert "Gate-touched" not in (job_dir(result) / "brief.md").read_text()


def test_review_writes_diff_patch(env_sandbox, repo):
    import subprocess

    (repo / "feat.txt").write_text("feature line\n")
    subprocess.run(["git", "add", "feat.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "feat"], cwd=repo, check=True, capture_output=True)
    result = go(repo, ["codex:m1"], role="review", brief="", base="master")
    assert result["status"] == "ok"
    patch = job_dir(result) / "diff.patch"
    assert patch.is_file()
    assert "+feature line" in patch.read_text()
    sent = fake_calls(env_sandbox.fakelog, "codex")[0]["stdin"]
    assert str(patch) in sent


def test_review_unknown_base_refused(env_sandbox, repo):
    result = go(repo, ["codex:m1"], role="review", brief="", base="no-such-base")
    assert result["status"] == "refused"
    assert result["exit_code"] == 3
    assert "no-such-base" in result["error"]
    assert fake_calls(env_sandbox.fakelog, "codex") == []


def test_secret_env_not_passed(env_sandbox, repo, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "super-secret-value")
    monkeypatch.setenv("CLAUDECODE", "1")
    result = go(repo, ["claude:sonnet"])
    assert result["status"] == "ok"
    env = fake_calls(env_sandbox.fakelog, "claude")[0]["env"]
    assert "GH_TOKEN" not in env and "CLAUDECODE" not in env
    request_text = (job_dir(result) / "request.json").read_text()
    assert "super-secret-value" not in request_text
    assert "GH_TOKEN" in json.loads(request_text)["removed_env"]
    assert "super-secret-value" not in (job_dir(result) / "result.json").read_text()


def test_unknown_agent_raises_config_error(env_sandbox, repo):
    with pytest.raises(ConfigError, match="nosuch"):
        go(repo, [Link("nosuch", "m")])
    assert not paths().jobs_dir.exists() or list(paths().jobs_dir.iterdir()) == []


def test_run_job_cancelled_flag_via_signal(env_sandbox, repo, monkeypatch):
    """A SIGTERM delivered to the CLI while an attempt runs ends the job as cancelled."""
    import signal
    import threading

    from agenthub import process

    monkeypatch.setattr(process, "KILL_GRACE_S", 0.5)
    scenario(monkeypatch, "agy", "slow")
    timer = threading.Timer(1.5, lambda: os.kill(os.getpid(), signal.SIGTERM))
    timer.start()
    try:
        result = go(repo, ["agy:m1", "agy:m2"], timeout=timedelta(seconds=30))
    finally:
        timer.cancel()
    assert result["status"] == "cancelled"
    assert result["exit_code"] == 130
    assert len(result["attempts"]) == 1
    assert orchestrate.EXIT["cancelled"] == 130


def test_implement_brief_has_handoff_footer(env_sandbox, repo):
    result = go(repo, ["codex:m1"])
    sent = fake_calls(env_sandbox.fakelog, "codex")[0]["stdin"]
    assert "## Status" in sent
    assert "## Status" in (job_dir(result) / "brief.md").read_text()


def test_review_brief_has_no_footer(env_sandbox, repo):
    result = go(repo, ["codex:m1"], role="review", brief="", base="master")
    sent = fake_calls(env_sandbox.fakelog, "codex")[0]["stdin"]
    assert "## Status" not in sent
    assert "## Status" not in (job_dir(result) / "brief.md").read_text()


PLAN_BRIEF = "## Goal\n\nsplit the work\n\n## Input\n\nthe spec\n"


def test_plan_job_has_no_diff_and_no_gates(env_sandbox, repo):
    result = go(repo, ["codex:m1"], role="plan", brief=PLAN_BRIEF, gates=["exit 1"])
    assert result["status"] == "ok"
    assert result["kind"] == "plan"
    assert result["base"] is None
    assert result["gate"] == []
    assert result["gate_touched"] == []
    assert result["handoff"] is None
    assert result["handoff_missing"] is False
    assert not (job_dir(result) / "diff.patch").exists()
    sent = fake_calls(env_sandbox.fakelog, "codex")[0]["stdin"]
    assert PLAN_BRIEF in sent
    assert "do not run commands" in sent
    assert "## Status" not in sent
    assert "## Status" not in (job_dir(result) / "brief.md").read_text()


def test_plan_job_that_modifies_tree_is_modified_tree(env_sandbox, repo, monkeypatch):
    scenario(monkeypatch, "codex", "write")
    result = go(repo, ["codex:m1", "agy:m2"], role="plan", brief=PLAN_BRIEF)
    assert result["status"] == "modified-tree"
    assert len(result["attempts"]) == 1
    assert fake_calls(env_sandbox.fakelog, "agy") == []


def test_plan_brief_needs_goal_and_input(env_sandbox, repo):
    with pytest.raises(BriefError, match="Brief missing headings: Goal"):
        go(repo, ["codex:m1"], role="plan", brief=BRIEF)
    assert fake_calls(env_sandbox.fakelog, "codex") == []


def test_standing_orders_user_and_repo_appended_in_order(env_sandbox, repo):
    (env_sandbox.config / "standing-orders.md").write_text("USER RULE\n")
    (repo / ".agent-hub").mkdir()
    (repo / ".agent-hub" / "standing-orders.md").write_text("PROJECT RULE\n")
    result = go(repo, ["codex:m1"])
    sent = fake_calls(env_sandbox.fakelog, "codex")[0]["stdin"]
    assert sent.index(BRIEF.strip()) < sent.index("## Standing orders") < sent.index("USER RULE")
    assert sent.index("USER RULE") < sent.index("PROJECT RULE") < sent.index("## Status")
    assert (job_dir(result) / "brief.md").read_text() == sent


def test_no_standing_orders_when_files_absent_or_empty(env_sandbox, repo):
    result = go(repo, ["codex:m1"])
    sent = fake_calls(env_sandbox.fakelog, "codex")[0]["stdin"]
    assert "## Standing orders" not in sent
    assert "## Standing orders" not in (job_dir(result) / "brief.md").read_text()

    (env_sandbox.config / "standing-orders.md").write_text("  \n\n")
    (repo / ".agent-hub").mkdir()
    (repo / ".agent-hub" / "standing-orders.md").write_text("")
    result = go(repo, ["codex:m1"])
    assert "## Standing orders" not in (job_dir(result) / "brief.md").read_text()


def test_unreadable_standing_orders_warns_and_continues(env_sandbox, repo, capsys):
    # Invalid UTF-8 cannot be read as text: the user file is skipped, the project file still applies.
    (env_sandbox.config / "standing-orders.md").write_bytes(b"\xff\xfe\x00 not text")
    (repo / ".agent-hub").mkdir()
    (repo / ".agent-hub" / "standing-orders.md").write_text("PROJECT RULE\n")
    result = go(repo, ["codex:m1"])
    assert result["status"] == "ok"
    sent = fake_calls(env_sandbox.fakelog, "codex")[0]["stdin"]
    assert "PROJECT RULE" in sent
    assert "## Status" in sent
    assert "skipping standing orders" in capsys.readouterr().err


def test_review_brief_gets_standing_orders_but_no_handoff_footer(env_sandbox, repo):
    (env_sandbox.config / "standing-orders.md").write_text("USER RULE\n")
    result = go(repo, ["codex:m1"], role="review", brief="Focus on locking.", base="master")
    assert result["status"] == "ok"
    sent = fake_calls(env_sandbox.fakelog, "codex")[0]["stdin"]
    assert sent.index("Focus on locking.") < sent.index("## Standing orders") < sent.index("USER RULE")
    assert "## Status" not in sent
    assert "## Status" not in (job_dir(result) / "brief.md").read_text()


def test_handoff_parsed_into_result(env_sandbox, repo, monkeypatch):
    scenario(monkeypatch, "agy", "handoff")
    result = go(repo, ["agy:m1"])
    assert result["handoff"] is not None
    assert result["handoff"]["status"] == "success"
    assert result["handoff"]["verification"] == "unit-test-verified"
    assert result["handoff"]["did"] == ["one", "two"]
    assert result["handoff_missing"] is False


def test_handoff_missing_flag(env_sandbox, repo, monkeypatch):
    scenario(monkeypatch, "agy", "write")
    result = go(repo, ["agy:m1"])
    assert result["handoff"] is None
    assert result["handoff_missing"] is True


def test_gate_passes(env_sandbox, repo, monkeypatch):
    scenario(monkeypatch, "agy", "write")
    result = go(repo, ["agy:m1"], gates=["test -f agent_wrote.txt"])
    assert result["status"] == "ok"
    assert len(result["gate"]) == 1
    assert result["gate"][0]["rc"] == 0
    assert result["gate"][0]["cmd"] == "test -f agent_wrote.txt"


def test_gate_fails_status_and_exit(env_sandbox, repo, monkeypatch):
    scenario(monkeypatch, "agy", "write")
    result = go(repo, ["agy:m1"], gates=["echo failed; exit 3"])
    assert result["status"] == "gate-failed"
    assert result["exit_code"] == 9
    assert len(result["gate"]) == 1
    assert result["gate"][0]["rc"] == 3
    assert "failed" in result["gate"][0]["tail"]
    assert Path(result["gate"][0]["log"]).is_file()


def test_gate_stops_at_first_failure(env_sandbox, repo, monkeypatch):
    scenario(monkeypatch, "agy", "write")
    result = go(repo, ["agy:m1"], gates=["exit 1", "exit 0"])
    assert result["status"] == "gate-failed"
    assert len(result["gate"]) == 1
    assert result["gate"][0]["rc"] == 1


def test_gate_not_run_when_job_failed(env_sandbox, repo, monkeypatch):
    scenario(monkeypatch, "agy", "fail")
    result = go(repo, ["agy:m1"], gates=["exit 1"])
    assert result["status"] == "failed"
    assert result["gate"] == []


def test_gate_env_filtered(env_sandbox, repo, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "super-secret")
    scenario(monkeypatch, "agy", "write")
    result = go(repo, ["agy:m1"], gates=["env"])
    assert result["status"] == "ok"
    assert "super-secret" not in result["gate"][0]["tail"]


def test_gate_ignored_for_review(env_sandbox, repo):
    result = go(repo, ["codex:m1"], role="review", brief="", base="master", gates=["exit 1"])
    assert result["status"] == "ok"
    assert result["gate"] == []


def test_gate_touched_recorded(env_sandbox, repo, monkeypatch):
    import subprocess

    subprocess.run(["git", "checkout", "-b", "feat/test"], cwd=repo, check=True)
    (repo / "test_foo.py").write_text("def test_a():\n    assert 1\n")
    subprocess.run(["git", "add", "."], cwd=repo)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo)

    os.environ["FAKE_AGY_SCENARIO"] = "remove-assert"
    result = go(repo, ["agy:m1"])
    assert result["status"] == "ok"
    assert result.get("gate_touched") == [{"file": "test_foo.py", "kind": "assert-removed", "lines": [2]}]


def test_jev_used_only_when_handoff_missing(env_sandbox, repo, monkeypatch, tmp_path):
    bin_path = tmp_path / "fake-jev"
    bin_path.write_text(
        '#!/bin/sh\necho \'{"results": [{"id": "reply", "label": "partial", "probability": 0.8, "decision": "auto"}], "model": "test"}\'\n'  # noqa: E501
    )
    bin_path.chmod(0o755)
    monkeypatch.setenv("AGENT_HUB_JEV_BIN", str(bin_path))
    monkeypatch.setenv("AGENT_HUB_JEV", "1")

    os.environ["FAKE_AGY_SCENARIO"] = "no-handoff"
    result = go(repo, ["agy:m1"], jev=True)
    assert result["status"] == "ok"
    assert result.get("handoff_missing") is True
    assert result.get("handoff_inferred") == {
        "label": "partial",
        "probability": 0.8,
        "decision": "auto",
        "model": "test",
    }


def test_jev_off_by_default(env_sandbox, repo, monkeypatch, tmp_path):
    bin_path = tmp_path / "fake-jev"
    bin_path.write_text("#!/bin/sh\nexit 1\n")
    bin_path.chmod(0o755)
    monkeypatch.setenv("AGENT_HUB_JEV_BIN", str(bin_path))

    os.environ["FAKE_AGY_SCENARIO"] = "no-handoff"
    result = go(repo, ["agy:m1"])
    assert result["status"] == "ok"
    assert result.get("handoff_missing") is True
    assert result.get("handoff_inferred") is None


def test_allow_scope_ok(env_sandbox, repo, monkeypatch):
    scenario(monkeypatch, "agy", "write")
    result = go(repo, ["agy:m1"], allow=["agent_*.txt"])
    assert result["status"] == "ok"
    assert result["exit_code"] == 0
    assert "scope_violation" not in result


def test_allow_scope_violation_exit_10(env_sandbox, repo, monkeypatch):
    scenario(monkeypatch, "agy", "write")
    result = go(repo, ["agy:m1"], allow=["src/**"], gates=["test -f agent_wrote.txt"])
    assert result["status"] == "scope-violation"
    assert result["exit_code"] == 10
    assert result["scope_violation"] == ["agent_wrote.txt"]
    assert result["gate"][0]["rc"] == 0


def test_allow_scope_deleted_file_counts(env_sandbox, repo, monkeypatch):
    scenario(monkeypatch, "agy", "delete")
    result = go(repo, ["agy:m1"], allow=["src/**"])
    assert result["status"] == "scope-violation"
    assert result["scope_violation"] == ["README.md"]


def test_no_allow_no_scope_check(env_sandbox, repo, monkeypatch):
    scenario(monkeypatch, "agy", "write")
    result = go(repo, ["agy:m1"])
    assert result["status"] == "ok"
    assert "scope_violation" not in result


def commit_repo_config(repo, gates):
    import subprocess

    cfg = repo / ".agent-hub" / "config.toml"
    cfg.parent.mkdir()
    cfg.write_text("gates = [" + ", ".join(f'"{g}"' for g in gates) + "]\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "repo gates"], cwd=repo, check=True)


def test_repo_default_gates_run(env_sandbox, repo, monkeypatch, tmp_path):
    marker = tmp_path / "repo_gate_ran"
    commit_repo_config(repo, [f"touch {marker}"])
    scenario(monkeypatch, "agy", "write")
    result = go(repo, ["agy:m1"])
    assert result["status"] == "ok"
    assert marker.is_file()
    assert [g["cmd"] for g in result["gate"]] == [f"touch {marker}"]


def test_repo_and_cli_gates_merged_once(env_sandbox, repo, monkeypatch, tmp_path):
    log = tmp_path / "gate_order.log"
    repo_cmds = [f"echo repo1 >> {log}", f"echo shared >> {log}"]
    cli_cmds = [f"echo shared >> {log}", f"echo cli1 >> {log}"]
    commit_repo_config(repo, repo_cmds)
    scenario(monkeypatch, "agy", "write")
    result = go(repo, ["agy:m1"], gates=cli_cmds)
    assert result["status"] == "ok"
    assert [g["cmd"] for g in result["gate"]] == [*repo_cmds, cli_cmds[1]]
    assert log.read_text().splitlines() == ["repo1", "shared", "cli1"]


def test_no_repo_config_no_extra_gates(env_sandbox, repo, monkeypatch):
    scenario(monkeypatch, "agy", "write")
    result = go(repo, ["agy:m1"], gates=["test -f agent_wrote.txt"])
    assert result["status"] == "ok"
    assert [g["cmd"] for g in result["gate"]] == ["test -f agent_wrote.txt"]


def test_run_passes_worker_plugins_to_adapter(env_sandbox, repo, tmp_path):
    telaf = tmp_path / "telaf"
    (telaf / "skills").mkdir(parents=True)
    (env_sandbox.config / "workers.toml").write_text(f'default = ["telaf"]\n[plugins]\ntelaf = "{telaf}"\n')
    result = go(repo, ["claude:sonnet"])
    assert result["status"] == "ok"
    argv = fake_calls(env_sandbox.fakelog, "claude")[0]["argv"]
    assert argv[argv.index("--plugin-dir") + 1] == str(telaf.resolve())


def test_review_ignores_branch_worker_plugins(env_sandbox, repo, tmp_path):
    import subprocess

    telaf = tmp_path / "telaf"
    (telaf / "skills").mkdir(parents=True)
    (env_sandbox.config / "workers.toml").write_text(f'default = ["telaf"]\n[plugins]\ntelaf = "{telaf}"\n')
    subprocess.run(["git", "checkout", "master"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "checkout", "-b", "feature"], cwd=repo, check=True, capture_output=True)
    (repo / ".agent-hub").mkdir()
    (repo / ".agent-hub" / "config.toml").write_text("worker_plugins = []\n")
    subprocess.run(["git", "add", ".agent-hub"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "opt out of plugins"], cwd=repo, check=True, capture_output=True)

    result = go(repo, ["claude:sonnet"], role="review", brief="", base="master")
    assert result["status"] == "ok"
    argv = fake_calls(env_sandbox.fakelog, "claude")[0]["argv"]
    assert argv[argv.index("--plugin-dir") + 1] == str(telaf.resolve())


def _skills_layout(env_sandbox, tmp_path):
    worker = tmp_path / "worker-plugin"
    (worker / "skills" / "debug-playbook").mkdir(parents=True)
    (worker / "skills" / "debug-playbook" / "SKILL.md").write_text("# debug\n")
    telaf = tmp_path / "telaf"
    (telaf / "skills" / "telaf").mkdir(parents=True)
    (telaf / "skills" / "telaf" / "SKILL.md").write_text("# telaf\n")
    (env_sandbox.config / "workers.toml").write_text(
        f'default = []\n[plugins]\nworker = "{worker}"\ntelaf = "{telaf}"\n'
    )


def test_skills_section_in_brief(env_sandbox, repo, tmp_path):
    _skills_layout(env_sandbox, tmp_path)
    skills = ["debug-playbook", "telaf"]
    line = "Load and follow these skills before you start (Claude lists them as <plugin>:<skill>): "
    expected = "debug-playbook (worker), telaf (telaf)"

    result = go(repo, ["agy:m1"], skills=skills)
    brief = (job_dir(result) / "brief.md").read_text()
    assert f"## Skills\n\n{line}{expected}\n" in brief
    assert brief.index("## Skills") < brief.index(handoff.FOOTER)

    result = go(repo, ["claude:sonnet"], role="review", brief="", base="master", skills=skills)
    brief = (job_dir(result) / "brief.md").read_text()
    assert f"## Skills\n\n{line}{expected}\n" in brief
    assert brief.index("## Skills") < brief.index(handoff.REVIEW_FOOTER)


def test_unknown_skill_is_config_error(env_sandbox, repo, tmp_path):
    _skills_layout(env_sandbox, tmp_path)
    with pytest.raises(ConfigError, match="'nope'") as err:
        go(repo, ["agy:m1"], skills=["debug-playbook", "nope"])
    assert "available skills are debug-playbook, telaf" in str(err.value)
    assert list_jobs(paths(), 100) == []


def test_duplicate_skills_listed_once(env_sandbox, repo, tmp_path):
    _skills_layout(env_sandbox, tmp_path)
    telaf = (tmp_path / "telaf").resolve()
    line = "Load and follow these skills before you start (Claude lists them as <plugin>:<skill>): "

    result = go(repo, ["agy:m1"], skills=["telaf", "debug-playbook", "telaf"])
    brief = (job_dir(result) / "brief.md").read_text()
    assert f"## Skills\n\n{line}telaf (telaf), debug-playbook (worker)\n" in brief
    assert brief.count("telaf (telaf)") == 1

    result = go(repo, ["claude:sonnet"], skills=["telaf", "telaf"])
    assert result["status"] == "ok"
    argv = fake_calls(env_sandbox.fakelog, "claude")[0]["argv"]
    assert [argv[i + 1] for i, a in enumerate(argv) if a == "--plugin-dir"] == [str(telaf)]


def test_no_skills_no_section(env_sandbox, repo):
    result = go(repo, ["agy:m1"])
    brief = (job_dir(result) / "brief.md").read_text()
    assert "## Skills" not in brief


def test_named_skill_plugin_is_added_for_per_run_hosts(env_sandbox, repo, tmp_path):
    _skills_layout(env_sandbox, tmp_path)
    telaf = (tmp_path / "telaf").resolve()
    worker = (tmp_path / "worker-plugin").resolve()

    result = go(repo, ["claude:sonnet"], skills=["telaf"])
    assert result["status"] == "ok"
    argv = fake_calls(env_sandbox.fakelog, "claude")[0]["argv"]
    assert [argv[i + 1] for i, a in enumerate(argv) if a == "--plugin-dir"] == [str(telaf)]

    # the plugin already in the default list is not listed twice
    (env_sandbox.config / "workers.toml").write_text(
        f'default = ["worker"]\n[plugins]\nworker = "{worker}"\ntelaf = "{telaf}"\n'
    )
    result = go(repo, ["claude:sonnet"], skills=["debug-playbook", "telaf"])
    assert result["status"] == "ok"
    argv = fake_calls(env_sandbox.fakelog, "claude")[1]["argv"]
    assert [argv[i + 1] for i, a in enumerate(argv) if a == "--plugin-dir"] == [str(worker), str(telaf)]


def test_non_claude_link_with_effort_is_refused(env_sandbox, repo):
    result = go(repo, ["agy:m1@low", "claude:sonnet"])
    assert result["status"] == "ok"
    first, second = result["attempts"]
    assert first["outcome"] == "config" and first["n"] == 1
    assert first["reason"] == "effort is only supported for claude links"
    assert second["outcome"] == "ok" and second["n"] == 2
    assert fake_calls(env_sandbox.fakelog, "agy") == []


def test_reviewer_with_effort_excluded_as_implementer(env_sandbox, repo):
    # the implementer ran claude:opus at its default effort; the same model at another effort is not independent
    args = args_for(repo, ["claude:opus@low", "claude:sonnet"], role="review")
    args.exclude = ["claude:opus"]
    result = run_job(args, paths(), make_pool(), now)
    assert result["status"] == "ok"
    assert [(a["agent"], a["model"]) for a in result["attempts"]] == [("claude", "sonnet")]
    request = json.loads((job_dir(result) / "request.json").read_text())
    assert request["links"] == ["claude:sonnet"]
    calls = fake_calls(env_sandbox.fakelog, "claude")
    assert len(calls) == 1 and "sonnet" in " ".join(calls[0]["argv"])


def test_effort_recorded_in_result(env_sandbox, repo):
    result = go(repo, ["claude:sonnet@low"])
    assert result["status"] == "ok"
    assert result["effort"] == "low"
    assert result["attempts"][0]["effort"] == "low"
    attempt_meta = json.loads((Path(result["reply_path"]).parent / "attempt.json").read_text())
    assert attempt_meta["effort"] == "low"
    assert json.loads((job_dir(result) / "result.json").read_text())["effort"] == "low"


def test_effort_cleared_after_fallback(env_sandbox, repo, monkeypatch):
    # the record names the effort of the link that ran; a fallback to a link without effort drops the old one
    scenario(monkeypatch, "claude", "quota,ok")
    result = go(repo, ["claude:opus@low", "claude:sonnet"])
    assert result["status"] == "ok"
    assert [(a["model"], a["outcome"]) for a in result["attempts"]] == [("opus", "quota"), ("sonnet", "ok")]
    assert result["model"] == "sonnet"
    assert "effort" not in result
    assert json.loads((job_dir(result) / "result.json").read_text()).get("effort") is None
