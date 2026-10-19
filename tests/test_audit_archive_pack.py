import fcntl
import hashlib
import json
import tarfile
from datetime import date
from pathlib import Path

import pytest

from agenthub.audit import archive as archive_mod
from agenthub.audit.archive import (
    ArchiveError,
    BuiltArchive,
    archive_month,
    build_archive,
    check_month,
    default_month,
    load_vault,
)
from agenthub.audit.writer import append, audit_root, canonical_json


def test_load_vault_from_audit_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    audit_file = config_dir / "audit.toml"
    vault_target = tmp_path / "my_vault"
    audit_file.write_text(f'vault = "{vault_target}"\n', encoding="utf-8")

    # Explicit config_dir
    loaded = load_vault(config_dir=config_dir)
    assert loaded == vault_target

    # Honoring AGENT_HUB_CONFIG when config_dir is None
    monkeypatch.setenv("AGENT_HUB_CONFIG", str(config_dir))
    loaded_env = load_vault()
    assert loaded_env == vault_target

    # Expanding user path
    home = Path.home()
    audit_file.write_text('vault = "~/test_vault"\n', encoding="utf-8")
    loaded_home = load_vault(config_dir=config_dir)
    assert loaded_home == home / "test_vault"


def test_missing_config_or_key_is_error(tmp_path: Path) -> None:
    config_dir = tmp_path / "empty_config"
    config_dir.mkdir(parents=True, exist_ok=True)

    # Missing audit.toml
    with pytest.raises(ArchiveError, match="missing audit config"):
        load_vault(config_dir=config_dir)

    audit_file = config_dir / "audit.toml"

    # Invalid TOML
    audit_file.write_text("not a valid toml = = =", encoding="utf-8")
    with pytest.raises(ArchiveError, match="failed to parse|invalid audit config"):
        load_vault(config_dir=config_dir)

    # Missing vault key
    audit_file.write_text('other = "value"\n', encoding="utf-8")
    with pytest.raises(ArchiveError, match="missing 'vault' key"):
        load_vault(config_dir=config_dir)

    # Non-string vault: integer
    audit_file.write_text("vault = 123\n", encoding="utf-8")
    with pytest.raises(ArchiveError, match="must be a string"):
        load_vault(config_dir=config_dir)

    # Non-string vault: boolean
    audit_file.write_text("vault = true\n", encoding="utf-8")
    with pytest.raises(ArchiveError, match="must be a string"):
        load_vault(config_dir=config_dir)

    # Empty string
    audit_file.write_text('vault = "   "\n', encoding="utf-8")
    with pytest.raises(ArchiveError, match="cannot be empty"):
        load_vault(config_dir=config_dir)


def test_current_month_refused_and_default_is_previous() -> None:
    today = date(2026, 10, 19)

    # default_month returns previous calendar month
    assert default_month(today) == "2026-09"

    # Boundary across year: January -> December previous year
    assert default_month(date(2026, 1, 1)) == "2025-12"

    # Default with no arguments returns a valid YYYY-MM string
    cur_default = default_month()
    assert len(cur_default) == 7
    assert cur_default[4] == "-"

    # check_month allows past months
    assert check_month("2026-09", today=today) == "2026-09"
    assert check_month("2025-12", today=today) == "2025-12"

    # check_month refuses current month
    with pytest.raises(ArchiveError, match="refusing month '2026-10'"):
        check_month("2026-10", today=today)

    # check_month refuses future months
    with pytest.raises(ArchiveError, match="refusing month '2026-11'"):
        check_month("2026-11", today=today)
    with pytest.raises(ArchiveError, match="refusing month '2027-01'"):
        check_month("2027-01", today=today)

    # Invalid formats
    with pytest.raises(ArchiveError, match="invalid month format"):
        check_month("2026-1", today=today)
    with pytest.raises(ArchiveError, match="invalid month format"):
        check_month("2026-13", today=today)
    with pytest.raises(ArchiveError, match="invalid month format"):
        check_month("2026-00", today=today)
    with pytest.raises(ArchiveError, match="invalid month format"):
        check_month("2026/09", today=today)
    with pytest.raises(ArchiveError, match="invalid month format"):
        check_month("not-a-month", today=today)


def test_build_archive_contains_month_files_and_hashes(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"

    # Emit events across 3 days in 2026-09
    append({"ts": "2026-09-01T10:00:00.000Z", "kind": "command", "target": "c1"}, state_dir=state_dir)
    append({"ts": "2026-09-01T10:00:01.000Z", "kind": "command", "target": "c2"}, state_dir=state_dir)
    append({"ts": "2026-09-02T12:00:00.000Z", "kind": "command", "target": "c3"}, state_dir=state_dir)
    append({"ts": "2026-09-15T09:00:00.000Z", "kind": "command", "target": "c4"}, state_dir=state_dir)
    append({"ts": "2026-09-15T09:00:01.000Z", "kind": "command", "target": "c5"}, state_dir=state_dir)

    # Emit an event in 2026-10 to ensure it is not included in the 2026-09 archive
    append({"ts": "2026-10-01T08:00:00.000Z", "kind": "command", "target": "oct1"}, state_dir=state_dir)

    # Read the expected hashes directly from day files
    root = audit_root(state_dir)
    sep_dir = root / "2026-09"
    day1_file = sep_dir / "2026-09-01.jsonl"
    day3_file = sep_dir / "2026-09-15.jsonl"

    day1_first_line = json.loads(day1_file.read_text(encoding="utf-8").splitlines()[0])
    day3_last_line = json.loads(day3_file.read_text(encoding="utf-8").splitlines()[-1])

    expected_first_prev_hash = day1_first_line["prev_hash"]
    expected_first_hash = day1_first_line["hash"]
    expected_last_hash = day3_last_line["hash"]

    custom_tmp = tmp_path / "custom_tmp"
    archive = build_archive("2026-09", state_dir=state_dir, tmp_dir=custom_tmp)

    assert isinstance(archive, BuiltArchive)
    assert archive.path == custom_tmp / "2026-09.tar.xz"
    assert archive.path.is_file()
    assert oct(archive.path.stat().st_mode & 0o777) == "0o600"
    assert archive.files == ["2026-09-01.jsonl", "2026-09-02.jsonl", "2026-09-15.jsonl"]
    assert archive.count == 5
    assert archive.first_prev_hash == expected_first_prev_hash
    assert archive.first_hash == expected_first_hash
    assert archive.last_hash == expected_last_hash

    # Verify sha256 matches actual file
    hasher = hashlib.sha256()
    hasher.update(archive.path.read_bytes())
    assert archive.sha256 == hasher.hexdigest()

    # Inspect tarball contents
    with tarfile.open(archive.path, "r:xz") as tar:
        names = tar.getnames()
        assert names == [
            "2026-09/2026-09-01.jsonl",
            "2026-09/2026-09-02.jsonl",
            "2026-09/2026-09-15.jsonl",
        ]
        # Verify content of entries matches day files
        for day_name in archive.files:
            member = tar.getmember(f"2026-09/{day_name}")
            f = tar.extractfile(member)
            assert f is not None
            tar_content = f.read()
            original_content = (sep_dir / day_name).read_bytes()
            assert tar_content == original_content

    # Default tmp_dir uses <state>/audit/tmp
    archive_default = build_archive("2026-09", state_dir=state_dir)
    assert archive_default.path == root / "tmp" / "2026-09.tar.xz"
    assert archive_default.path.is_file()
    assert archive_default.sha256 == archive.sha256


def test_broken_chain_builds_nothing_and_keeps_files(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    append({"ts": "2026-09-01T10:00:00.000Z", "kind": "command", "target": "c1"}, state_dir=state_dir)
    append({"ts": "2026-09-01T10:00:01.000Z", "kind": "command", "target": "c2"}, state_dir=state_dir)

    root = audit_root(state_dir)
    day_file = root / "2026-09" / "2026-09-01.jsonl"
    lines = day_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2

    # Tamper with the event payload without updating hash
    ev = json.loads(lines[1])
    ev["target"] = "tampered"
    lines[1] = canonical_json(ev)
    tampered_bytes = ("\n".join(lines) + "\n").encode("utf-8")
    day_file.write_bytes(tampered_bytes)

    tmp_dir = tmp_path / "archive_tmp"
    with pytest.raises(ArchiveError, match="verification failed"):
        build_archive("2026-09", state_dir=state_dir, tmp_dir=tmp_dir)

    # Nothing built in tmp_dir
    assert not (tmp_dir / "2026-09.tar.xz").exists()

    # Local day file kept intact
    assert day_file.is_file()
    assert day_file.read_bytes() == tampered_bytes


def test_failure_removes_only_own_tmp_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_dir = tmp_path / "state"
    append({"ts": "2026-09-01T10:00:00.000Z", "kind": "command", "target": "c1"}, state_dir=state_dir)

    tmp_dir = tmp_path / "archive_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    unrelated_file = tmp_dir / "other.txt"
    unrelated_file.write_text("keep me safe", encoding="utf-8")

    # Force a failure during tar creation
    def fake_add(*args: object, **kwargs: object) -> None:
        raise OSError("simulated disk write failure")

    monkeypatch.setattr(tarfile.TarFile, "add", fake_add)

    with pytest.raises(OSError, match="simulated disk write failure"):
        build_archive("2026-09", state_dir=state_dir, tmp_dir=tmp_dir)

    # Unrelated file was not touched
    assert unrelated_file.is_file()
    assert unrelated_file.read_text(encoding="utf-8") == "keep me safe"

    # Tarball was cleaned up
    assert not (tmp_dir / "2026-09.tar.xz").exists()


def test_fix_1_archive_month_takes_chain_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_dir = tmp_path / "state"
    vault = tmp_path / "vault"
    vault.mkdir()
    append({"ts": "2026-09-01T10:00:00.000Z", "kind": "command", "target": "c1"}, state_dir=state_dir)
    root = audit_root(state_dir)
    monkeypatch.setattr(archive_mod, "_CHAIN_LOCK_WAIT_S", 0.05)

    with (root / "chain.lock").open("a+b") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            res = archive_month("2026-09", state_dir=state_dir, vault=vault, today=date(2026, 10, 19))
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)

    assert res.status == "error"
    assert "timed out" in res.message
    assert (root / "2026-09" / "2026-09-01.jsonl").is_file()

    # once the lock is free the archive goes through
    res_ok = archive_month("2026-09", state_dir=state_dir, vault=vault, today=date(2026, 10, 19))
    assert res_ok.status == "ok"
    assert not (root / "2026-09").exists()


def test_archive_packs_without_holding_chain_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_dir = tmp_path / "state"
    vault = tmp_path / "vault"
    vault.mkdir()
    append({"ts": "2026-09-01T10:00:00.000Z", "kind": "command", "target": "c1"}, state_dir=state_dir)
    root = audit_root(state_dir)
    real_pack = archive_mod._build_archive_locked
    appended: list[dict] = []

    def slow_pack(*args, **kwargs):  # type: ignore[no-untyped-def]
        appended.append(
            append({"ts": "2026-10-19T10:00:00.000Z", "kind": "command", "target": "during"}, state_dir=state_dir)
        )
        return real_pack(*args, **kwargs)

    monkeypatch.setattr(archive_mod, "_build_archive_locked", slow_pack)
    res = archive_month("2026-09", state_dir=state_dir, vault=vault, today=date(2026, 10, 19))

    assert res.status == "ok"
    assert len(appended) == 1
    assert not (root / "2026-09").exists()
    assert (root / "2026-10" / "2026-10-19.jsonl").is_file()
