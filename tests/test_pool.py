from datetime import datetime, timedelta, timezone
from multiprocessing import Pool as ProcessPool
from pathlib import Path

import pytest

from agenthub.config import ConfigError
from agenthub.pool import Cooldowns, parse_link


def test_parse_link_ok():
    link = parse_link("agy:model-name")
    assert link.agent == "agy"
    assert link.model == "model-name"
    assert str(link) == "agy:model-name"


@pytest.mark.parametrize("text", ["agy", ":m", "a:"])
def test_parse_link_invalid(text):
    with pytest.raises(ConfigError):
        parse_link(text)


def test_parse_link_model_with_colon_kept():
    link = parse_link("codex:gpt:5")
    assert link.agent == "codex"
    assert link.model == "gpt:5"
    assert str(link) == "codex:gpt:5"


def test_cooldown_roundtrip(tmp_path):
    c = Cooldowns(tmp_path / "cooldowns.json")
    link = parse_link("agy:m1")
    now = datetime(2026, 10, 8, tzinfo=timezone.utc)
    until = now + timedelta(hours=1)
    c.set(link, until)
    assert c.until(link, now) == until

    # Reload from file
    c2 = Cooldowns(tmp_path / "cooldowns.json")
    assert c2.until(link, now) == until


def test_cooldown_expired_reads_none(tmp_path):
    c = Cooldowns(tmp_path / "cooldowns.json")
    link = parse_link("agy:m1")
    now = datetime(2026, 10, 8, tzinfo=timezone.utc)
    c.set(link, now - timedelta(hours=1))
    assert c.until(link, now) is None


def test_cooldown_clear(tmp_path):
    c = Cooldowns(tmp_path / "cooldowns.json")
    link = parse_link("agy:m1")
    now = datetime(2026, 10, 8, tzinfo=timezone.utc)
    until = now + timedelta(hours=1)
    c.set(link, until)
    assert c.clear(link) is True
    assert c.until(link, now) is None
    assert c.clear(link) is False


def test_cooldown_corrupt_file_reads_empty(tmp_path):
    p = tmp_path / "cooldowns.json"
    p.write_text("{invalid")
    c = Cooldowns(p)
    now = datetime(2026, 10, 8, tzinfo=timezone.utc)
    assert c.until(parse_link("agy:m1"), now) is None


def test_cooldown_naive_datetime_rejected(tmp_path):
    c = Cooldowns(tmp_path / "cooldowns.json")
    link = parse_link("agy:m1")
    with pytest.raises(ValueError):
        c.set(link, datetime(2026, 10, 8))
    with pytest.raises(ValueError):
        c.until(link, datetime(2026, 10, 8))


def _worker(args):
    path, i = args
    c = Cooldowns(Path(path))
    link = parse_link(f"agent{i}:m")
    c.set(link, datetime(2026, 10, 8, tzinfo=timezone.utc) + timedelta(hours=i + 1))


def test_cooldown_concurrent_writes(tmp_path):
    path = tmp_path / "cooldowns.json"
    args = [(str(path), i) for i in range(8)]
    with ProcessPool(8) as pool:
        pool.map(_worker, args)

    c = Cooldowns(path)
    now = datetime(2026, 10, 8, tzinfo=timezone.utc)
    all_cd = c.all(now)
    assert len(all_cd) == 8
    for i in range(8):
        link_str = f"agent{i}:m"
        assert link_str in all_cd
        assert all_cd[link_str] == datetime(2026, 10, 8, tzinfo=timezone.utc) + timedelta(hours=i + 1)
