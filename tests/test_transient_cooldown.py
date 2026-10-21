import json
from datetime import timedelta

import pytest

from agenthub import process
from agenthub.config import ConfigError, Pool, load_pool, paths
from agenthub.orchestrate import run_job
from agenthub.pool import Cooldowns, Link
from tests.test_run import args_for, now


def _conf(monkeypatch, tmp_path, text=None):
    conf = tmp_path / "conf"
    conf.mkdir()
    if text is not None:
        (conf / "pool.toml").write_text(text)
    monkeypatch.setenv("AGENT_HUB_CONFIG", str(conf))
    return conf


def test_default_transient_cooldown_is_3m(monkeypatch, tmp_path):
    _conf(monkeypatch, tmp_path)
    assert load_pool(paths()).transient_cooldown == timedelta(minutes=3)


def test_user_transient_cooldown_overrides(monkeypatch, tmp_path):
    _conf(monkeypatch, tmp_path, '[defaults]\ntransient_cooldown = "10m"\n')
    assert load_pool(paths()).transient_cooldown == timedelta(minutes=10)


def test_bad_transient_cooldown_is_config_error(monkeypatch, tmp_path):
    conf = _conf(monkeypatch, tmp_path, '[defaults]\ntransient_cooldown = "soon"\n')
    with pytest.raises(ConfigError, match=str(conf / "pool.toml")):
        load_pool(paths())


def test_run_job_uses_pool_transient_cooldown(env_sandbox, repo, monkeypatch):
    orig_run = process.run

    def fake_run(inv, cwd, env, attempt_dir, timeout, on_start=lambda pgid: None):
        if attempt_dir.name == "1-claude-m1":
            (attempt_dir / "stdout").write_text(
                json.dumps({"is_error": True, "result": "API Error: Can't reach the API server"})
            )
            (attempt_dir / "stderr").write_text("")
            on_start(12345)
            return process.Ran(rc=1, timed_out=False, duration_s=0.1, pgid=12345)
        return orig_run(inv, cwd, env, attempt_dir, timeout, on_start=on_start)

    monkeypatch.setattr(process, "run", fake_run)

    pool = Pool(
        roles={},
        quota_cooldown=timedelta(minutes=60),
        timeouts={"implement": timedelta(minutes=5), "review": timedelta(minutes=5)},
        transient_cooldown=timedelta(minutes=7),
    )
    result = run_job(args_for(repo, ["claude:m1", "agy:m2"]), paths(), pool, now)
    assert result["status"] == "ok"
    assert [a["outcome"] for a in result["attempts"]] == ["transient", "ok"]

    until = Cooldowns(paths().cooldowns_file).until(Link("claude", "m1"), now())
    assert until is not None
    assert timedelta(minutes=6) < until - now() <= timedelta(minutes=7)
