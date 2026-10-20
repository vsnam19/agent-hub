import json
from datetime import datetime, timedelta, timezone

from agenthub import models
from agenthub.cli import main
from agenthub.config import paths

POOL = '[roles.check]\nkind = "review"\nchain = ["agy:m1", "agy:m2", "codex:c1"]\n'


def write_pool(env_sandbox, text=POOL):
    (env_sandbox.config / "pool.toml").write_text(text)


def write_catalog(path, fetched_at):
    # agy has a fresh catalog that lists m1 only; codex has no entry at all.
    cache = {"agy": {"fetched_at": fetched_at.isoformat(), "models": ["m1"], "error": None}}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache))


def stub_refresh(monkeypatch):
    calls = []

    def fake_refresh(adapters, path, now, force=False):
        calls.append({"path": path, "force": force})
        return models.read(path)

    monkeypatch.setattr(models, "refresh", fake_refresh)
    return calls


def check_entries(capsys):
    data = json.loads(capsys.readouterr().out)
    return {e["link"]: e for e in data["roles"]["check"]["chain"]}


def test_pool_json_model_known_true_false_null(env_sandbox, monkeypatch, capsys):
    write_pool(env_sandbox)
    write_catalog(paths().models_file, datetime.now(timezone.utc))
    stub_refresh(monkeypatch)

    assert main(["pool", "--json"]) == 0
    entries = check_entries(capsys)
    assert entries["agy:m1"]["model_known"] is True
    assert entries["agy:m2"]["model_known"] is False
    assert entries["codex:c1"]["model_known"] is None


def test_pool_text_prints_unknown_model(env_sandbox, monkeypatch, capsys):
    write_pool(env_sandbox)
    write_catalog(paths().models_file, datetime.now(timezone.utc))
    stub_refresh(monkeypatch)

    assert main(["pool"]) == 0
    lines = capsys.readouterr().out.splitlines()
    m1 = next(x for x in lines if x.strip().startswith("agy:m1"))
    m2 = next(x for x in lines if x.strip().startswith("agy:m2"))
    c1 = next(x for x in lines if x.strip().startswith("codex:c1"))
    assert "unknown-model" not in m1
    assert "unknown-model" in m2
    assert "unknown-model" not in c1


def test_pool_stale_catalog_prints_nothing(env_sandbox, monkeypatch, capsys):
    write_pool(env_sandbox)
    write_catalog(paths().models_file, datetime.now(timezone.utc) - timedelta(hours=25))
    stub_refresh(monkeypatch)

    assert main(["pool", "--json"]) == 0
    entries = check_entries(capsys)
    assert entries["agy:m2"]["model_known"] is None

    assert main(["pool"]) == 0
    assert "unknown-model" not in capsys.readouterr().out


def test_pool_lazy_refresh_runs_without_flag(env_sandbox, monkeypatch, capsys):
    write_pool(env_sandbox)
    calls = stub_refresh(monkeypatch)

    assert main(["pool"]) == 0
    assert len(calls) == 1
    assert calls[0]["path"] == paths().models_file
    assert calls[0]["force"] is False


def test_pool_refresh_models_flag_forces(env_sandbox, monkeypatch, capsys):
    write_pool(env_sandbox)
    calls = stub_refresh(monkeypatch)

    assert main(["pool", "--refresh-models"]) == 0
    assert len(calls) == 1
    assert calls[0]["force"] is True


def test_pool_clear_does_not_refresh(env_sandbox, monkeypatch, capsys):
    write_pool(env_sandbox)
    calls = stub_refresh(monkeypatch)

    assert main(["pool", "--clear", "agy:m1"]) == 0
    assert calls == []
    assert "no cooldown for agy:m1" in capsys.readouterr().out
