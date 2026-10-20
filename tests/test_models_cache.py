import os
import stat
from datetime import datetime, timedelta, timezone

from agenthub import models

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


class FakeAdapter:
    def __init__(self, name, models_list=None, error=None, installed=True):
        self.name = name
        self._models = models_list
        self.models_error = error
        self._installed = installed
        self.calls = 0

    def installed(self):
        return self._installed

    def list_models(self):
        self.calls += 1
        return self._models


def _fresh_entry(models_list, at=NOW):
    return {"fetched_at": at.isoformat(), "models": models_list, "error": None}


def test_model_known_true_when_in_fresh_catalog():
    cache = {"gwen": _fresh_entry(["m1", "m2"])}
    assert models.model_known(cache, "gwen", "m1", NOW) is True


def test_model_known_false_when_fresh_catalog_lacks_it():
    cache = {"gwen": _fresh_entry(["m1", "m2"])}
    assert models.model_known(cache, "gwen", "m3", NOW) is False


def test_model_known_none_when_stale_or_error_or_missing():
    stale = {"gwen": _fresh_entry(["m1"], at=NOW - timedelta(hours=25))}
    errored = {"gwen": {"fetched_at": NOW.isoformat(), "models": [], "error": "not installed"}}
    assert models.model_known(stale, "gwen", "m1", NOW) is None
    assert models.model_known(errored, "gwen", "m1", NOW) is None
    assert models.model_known({}, "gwen", "m1", NOW) is None


def test_claude_prefixed_id_is_always_known():
    assert models.model_known({}, "claude", "claude-anything-9", NOW) is True
    cache = {"claude": _fresh_entry(["haiku"])}
    assert models.model_known(cache, "claude", "claude-anything-9", NOW) is True


def test_refresh_writes_mode_600_and_returns_cache(tmp_path):
    path = tmp_path / "state" / "models.json"
    adapters = {"gwen": FakeAdapter("gwen", ["m1", "m2"])}

    cache = models.refresh(adapters, path, NOW)

    assert cache == {"gwen": {"fetched_at": NOW.isoformat(), "models": ["m1", "m2"], "error": None}}
    assert models.read(path) == cache
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_refresh_skips_fresh_entries_unless_forced(tmp_path):
    path = tmp_path / "models.json"
    adapter = FakeAdapter("gwen", ["m1"])
    adapters = {"gwen": adapter}

    models.refresh(adapters, path, NOW)
    assert adapter.calls == 1

    models.refresh(adapters, path, NOW + timedelta(hours=1))
    assert adapter.calls == 1

    models.refresh(adapters, path, NOW + timedelta(hours=1), force=True)
    assert adapter.calls == 2
