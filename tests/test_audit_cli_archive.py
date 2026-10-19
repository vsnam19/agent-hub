from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from agenthub.audit.archive import default_month
from agenthub.audit.writer import append, audit_root
from agenthub.cli import _build_parser, main


@pytest.fixture
def audit_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir(parents=True, exist_ok=True)

    (config_dir / "audit.toml").write_text(f'vault = "{vault_dir}"\n', encoding="utf-8")

    monkeypatch.setenv("AGENT_HUB_CONFIG", str(config_dir))
    monkeypatch.setenv("AGENT_HUB_STATE", str(state_dir))

    return config_dir, state_dir, vault_dir


def _emit_synthetic_month(state_dir: Path, month: str) -> None:
    append(
        {
            "ts": f"{month}-01T10:00:00.000Z",
            "kind": "command",
            "target": "c1",
        },
        state_dir=state_dir,
    )
    append(
        {
            "ts": f"{month}-02T10:00:00.000Z",
            "kind": "command",
            "target": "c2",
        },
        state_dir=state_dir,
    )


def test_archive_has_no_vault_option(audit_env: tuple[Path, Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    # 1. Running with --vault must fail with usage error (exit code 2)
    rc = main(["audit", "archive", "--vault", "/some/vault"])
    assert rc == 2
    out, err = capsys.readouterr()
    assert "unrecognized arguments" in err or "--vault" in err

    # 2. Inspecting the parser directly to ensure no vault option is configured
    parser = _build_parser()
    subparsers_actions = [
        action for action in parser._actions if isinstance(action, pytest.importorskip("argparse")._SubParsersAction)
    ]
    assert len(subparsers_actions) > 0
    audit_parser = subparsers_actions[0].choices["audit"]
    audit_sub_actions = [
        action
        for action in audit_parser._actions
        if isinstance(action, pytest.importorskip("argparse")._SubParsersAction)
    ]
    assert len(audit_sub_actions) > 0
    archive_parser = audit_sub_actions[0].choices["archive"]
    option_strings = [opt for action in archive_parser._actions for opt in action.option_strings]
    assert "--vault" not in option_strings


def test_archive_defaults_to_previous_month(
    audit_env: tuple[Path, Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    _, state_dir, vault_dir = audit_env
    prev = default_month()
    _emit_synthetic_month(state_dir, prev)

    rc = main(["audit", "archive"])
    assert rc == 0

    out, err = capsys.readouterr()
    assert err == ""
    assert f"archived {prev}" in out

    vault_audit = vault_dir / "agent-hub-audit"
    assert (vault_audit / f"{prev}.tar.xz").is_file()
    assert (vault_audit / f"{prev}.json").is_file()

    root = audit_root(state_dir)
    assert not (root / prev).exists()
    assert (root / "manifests" / f"{prev}.json").is_file()


def test_archive_current_month_refused_exit_one(
    audit_env: tuple[Path, Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    _, state_dir, vault_dir = audit_env
    now = datetime.now(timezone.utc)
    current_month = f"{now.year:04d}-{now.month:02d}"
    _emit_synthetic_month(state_dir, current_month)

    rc = main(["audit", "archive", "--month", current_month])
    assert rc == 1

    out, err = capsys.readouterr()
    assert f"refusing month '{current_month}'" in err or "current" in err.lower()

    # Local data must be untouched
    root = audit_root(state_dir)
    assert (root / current_month).is_dir()
    assert not (vault_dir / "agent-hub-audit" / f"{current_month}.tar.xz").exists()


def test_archive_success_exit_zero_and_message(
    audit_env: tuple[Path, Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    _, state_dir, vault_dir = audit_env
    past_month = "2026-08"
    _emit_synthetic_month(state_dir, past_month)

    rc = main(["audit", "archive", "--month", past_month])
    assert rc == 0

    out, err = capsys.readouterr()
    assert err == ""
    assert f"archived {past_month}" in out

    vault_audit = vault_dir / "agent-hub-audit"
    assert (vault_audit / f"{past_month}.tar.xz").is_file()
    assert (vault_audit / f"{past_month}.json").is_file()

    root = audit_root(state_dir)
    assert not (root / past_month).exists()
    assert (root / "manifests" / f"{past_month}.json").is_file()


def test_archive_noop_exit_zero(audit_env: tuple[Path, Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    _, state_dir, _ = audit_env
    past_month = "2026-08"
    _emit_synthetic_month(state_dir, past_month)

    rc1 = main(["audit", "archive", "--month", past_month])
    assert rc1 == 0
    capsys.readouterr()

    rc2 = main(["audit", "archive", "--month", past_month])
    assert rc2 == 0

    out, err = capsys.readouterr()
    assert err == ""
    assert f"month {past_month} already archived" in out or f"already archived {past_month}" in out


def test_archive_error_exit_one_and_local_kept(
    audit_env: tuple[Path, Path, Path], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    config_dir, state_dir, _ = audit_env
    # Point audit.toml to a non-existent vault path
    missing_vault = tmp_path / "does_not_exist_vault"
    (config_dir / "audit.toml").write_text(f'vault = "{missing_vault}"\n', encoding="utf-8")

    past_month = "2026-07"
    _emit_synthetic_month(state_dir, past_month)

    rc = main(["audit", "archive", "--month", past_month])
    assert rc == 1

    out, err = capsys.readouterr()
    assert err != ""
    assert "does not exist" in err or "error" in err.lower()

    # Local state must be kept intact
    root = audit_root(state_dir)
    assert (root / past_month).is_dir()
    day_files = list((root / past_month).glob("*.jsonl"))
    assert len(day_files) == 2
