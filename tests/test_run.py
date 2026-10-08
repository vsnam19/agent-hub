import json
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agenthub import orchestrate
from agenthub.config import ConfigError, Pool, paths
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


def args_for(repo, links, role="implement", brief=BRIEF, timeout=timedelta(seconds=30), base=None, gates=None):
    return RunArgs(
        role=role,
        brief=brief,
        workdir=repo,
        links=[parse_link(x) if isinstance(x, str) else x for x in links],
        timeout=timeout,
        base=base,
        gates=gates or [],
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
