import json
from datetime import datetime, timedelta, timezone

from agenthub.cli import main
from agenthub.config import paths
from tests.conftest import fake_calls

BRIEF = "## Input\n\nfoo\n\n## Expected output\n\nbar\n"


def write_brief(tmp_path, text=BRIEF):
    f = tmp_path / "brief.md"
    f.write_text(text)
    return f


def write_pool(env_sandbox, text):
    (env_sandbox.config / "pool.toml").write_text(text)


def write_models(entry_agy):
    path = paths().models_file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"agy": entry_agy}))


def fresh_agy(now=None):
    now = now or datetime.now(timezone.utc)
    return {"fetched_at": now.isoformat(), "models": ["other"], "error": None}


def run_chain(env_sandbox, repo, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_AGY_SCENARIO", "write")
    write_pool(env_sandbox, '[roles.implement]\nchain = ["agy:m1", "claude:haiku"]\n')
    brief = write_brief(tmp_path)
    rc = main(["run", "--role", "implement", "--brief", str(brief), "-d", str(repo)])
    out = capsys.readouterr().out
    job_id = out.splitlines()[3].split(": ", 1)[1]
    result = json.loads((paths().jobs_dir / job_id / "result.json").read_text())
    return rc, result


def test_unknown_model_link_is_skipped_and_next_link_runs(env_sandbox, repo, tmp_path, monkeypatch, capsys):
    write_models(fresh_agy())
    rc, result = run_chain(env_sandbox, repo, tmp_path, monkeypatch, capsys)
    assert rc == 0
    assert fake_calls(env_sandbox.fakelog, "agy") == []
    assert len(fake_calls(env_sandbox.fakelog, "claude")) == 1
    assert [a["agent"] for a in result["attempts"]] == ["agy", "claude"]
    assert result["attempts"][1]["outcome"] == "ok"


def test_unknown_model_attempt_records_reason(env_sandbox, repo, tmp_path, monkeypatch, capsys):
    write_models(fresh_agy())
    _, result = run_chain(env_sandbox, repo, tmp_path, monkeypatch, capsys)
    skipped = result["attempts"][0]
    assert skipped["agent"] == "agy" and skipped["model"] == "m1"
    assert skipped["outcome"] == "skipped"
    assert skipped["reason"] == "unknown-model"


def test_stale_catalog_does_not_skip(env_sandbox, repo, tmp_path, monkeypatch, capsys):
    old = datetime.now(timezone.utc) - timedelta(hours=25)
    write_models(fresh_agy(old))
    _, result = run_chain(env_sandbox, repo, tmp_path, monkeypatch, capsys)
    assert len(fake_calls(env_sandbox.fakelog, "agy")) == 1
    assert result["attempts"][0].get("reason") != "unknown-model"


def test_missing_catalog_does_not_skip(env_sandbox, repo, tmp_path, monkeypatch, capsys):
    assert not paths().models_file.exists()
    _, result = run_chain(env_sandbox, repo, tmp_path, monkeypatch, capsys)
    assert len(fake_calls(env_sandbox.fakelog, "agy")) == 1
    assert result["attempts"][0].get("reason") != "unknown-model"


def test_catalog_error_entry_does_not_skip(env_sandbox, repo, tmp_path, monkeypatch, capsys):
    write_models({"fetched_at": datetime.now(timezone.utc).isoformat(), "models": [], "error": "boom"})
    _, result = run_chain(env_sandbox, repo, tmp_path, monkeypatch, capsys)
    assert len(fake_calls(env_sandbox.fakelog, "agy")) == 1
    assert result["attempts"][0].get("reason") != "unknown-model"
