import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenthub.cli import main

SERVICE = "agent-hub-skills-weekly.service"
TIMER = "agent-hub-skills-weekly.timer"
DEFAULT_AT = "Sun *-*-* 20:17"


def script(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def sched(tmp_path, monkeypatch):
    """Fake systemctl, systemd-analyze and agent-hub first on PATH; every unit lands under tmp_path."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "systemctl.log"
    script(bin_dir / "agent-hub", "exit 0")
    script(
        bin_dir / "systemctl",
        'echo "$@" >> "$FAKE_LOG"\n'
        'if [ -n "$FAKE_SYSTEMCTL_FAIL" ]; then echo "Failed to connect to bus" >&2; exit 1; fi',
    )
    script(
        bin_dir / "systemd-analyze",
        'case "$*" in\n  *bogus*) echo "Failed to parse calendar specification" >&2; exit 1 ;;\nesac\necho normalized',
    )
    config = tmp_path / "config"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.delenv("FAKE_SYSTEMCTL_FAIL", raising=False)
    return SimpleNamespace(bin=bin_dir, unit_dir=config / "systemd" / "user", log=log)


def calls(sched) -> list[str]:
    return sched.log.read_text().splitlines() if sched.log.exists() else []


def test_dry_run_prints_units_and_writes_nothing(sched, capsys):
    rc = main(["skills", "schedule"])

    out = capsys.readouterr().out
    assert rc == 0
    assert f"ExecStart={sched.bin / 'agent-hub'} skills weekly" in out
    assert f"OnCalendar={DEFAULT_AT}" in out
    assert "Persistent=true" in out
    assert "systemctl --user enable --now agent-hub-skills-weekly.timer" in out
    assert not sched.unit_dir.exists()
    assert calls(sched) == []


def test_apply_writes_units_and_enables_timer(sched, capsys):
    rc = main(["skills", "schedule", "--apply"])

    assert rc == 0
    service = (sched.unit_dir / SERVICE).read_text(encoding="utf-8")
    timer = (sched.unit_dir / TIMER).read_text(encoding="utf-8")
    assert "Type=oneshot" in service
    assert f"ExecStart={sched.bin / 'agent-hub'} skills weekly" in service
    assert f"Environment=PATH={os.environ['PATH']}" in service
    assert "TimeoutStartSec=6h" in service
    assert "Nice=10" in service
    assert f"OnCalendar={DEFAULT_AT}" in timer
    assert "Persistent=true" in timer
    assert "WantedBy=timers.target" in timer
    assert calls(sched) == ["--user daemon-reload", f"--user enable --now {TIMER}"]


def test_apply_twice_is_idempotent(sched, capsys):
    main(["skills", "schedule", "--apply"])
    before = (sched.unit_dir / TIMER).read_text(encoding="utf-8")
    log_before = calls(sched)
    capsys.readouterr()

    rc = main(["skills", "schedule", "--apply"])

    assert rc == 0
    assert "already scheduled" in capsys.readouterr().out
    assert (sched.unit_dir / TIMER).read_text(encoding="utf-8") == before
    assert calls(sched)[len(log_before) :] == [f"--user is-enabled {TIMER}"]


def test_invalid_calendar_exit_2(sched, capsys):
    rc = main(["skills", "schedule", "--apply", "--at", "bogus"])

    assert rc == 2
    assert "bogus" in capsys.readouterr().err
    assert not sched.unit_dir.exists()
    assert calls(sched) == []


def test_remove_disables_and_deletes(sched, capsys):
    main(["skills", "schedule", "--apply"])
    assert (sched.unit_dir / SERVICE).exists()
    assert (sched.unit_dir / TIMER).exists()
    log_before = len(calls(sched))

    rc = main(["skills", "schedule", "--remove"])

    assert rc == 0
    assert not (sched.unit_dir / SERVICE).exists()
    assert not (sched.unit_dir / TIMER).exists()
    assert calls(sched)[log_before:] == [f"--user disable --now {TIMER}", "--user daemon-reload"]


def test_systemctl_failure_exit_1(sched, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_SYSTEMCTL_FAIL", "1")

    rc = main(["skills", "schedule", "--apply"])

    assert rc == 1
    assert "Failed to connect to bus" in capsys.readouterr().err


def test_apply_after_failed_enable_retries(sched, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_SYSTEMCTL_FAIL", "1")
    assert main(["skills", "schedule", "--apply"]) == 1
    capsys.readouterr()
    monkeypatch.delenv("FAKE_SYSTEMCTL_FAIL")
    log_before = len(calls(sched))

    rc = main(["skills", "schedule", "--apply"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "scheduled:" in out
    assert "already scheduled" not in out
    assert calls(sched)[log_before:] == ["--user daemon-reload", f"--user enable --now {TIMER}"]
    assert (sched.unit_dir / TIMER).exists()


def test_failed_enable_removes_new_units(sched, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_SYSTEMCTL_FAIL", "1")

    rc = main(["skills", "schedule", "--apply"])

    assert rc == 1
    assert "Failed to connect to bus" in capsys.readouterr().err
    assert not (sched.unit_dir / SERVICE).exists()
    assert not (sched.unit_dir / TIMER).exists()


def test_remove_when_not_scheduled_exit_0(sched, capsys):
    rc = main(["skills", "schedule", "--remove"])

    assert rc == 0
    assert "nothing scheduled" in capsys.readouterr().out
    assert not sched.unit_dir.exists()
    assert calls(sched) == []


def test_remove_failed_disable_still_deletes_units_exit_1(sched, capsys, monkeypatch):
    assert main(["skills", "schedule", "--apply"]) == 0
    capsys.readouterr()
    monkeypatch.setenv("FAKE_SYSTEMCTL_FAIL", "1")

    rc = main(["skills", "schedule", "--remove"])

    assert rc == 1
    err = capsys.readouterr().err
    assert f"disable {TIMER}" in err
    assert "Failed to connect to bus" in err
    assert not (sched.unit_dir / SERVICE).exists()
    assert not (sched.unit_dir / TIMER).exists()


def test_dollar_in_exec_path_escaped(sched, capsys, monkeypatch):
    dollar_bin = sched.bin.parent / "tools$x"
    dollar_bin.mkdir()
    script(dollar_bin / "agent-hub", "exit 0")
    monkeypatch.setenv("PATH", f"{dollar_bin}:{os.environ['PATH']}")

    rc = main(["skills", "schedule", "--apply"])

    assert rc == 0
    service = (sched.unit_dir / SERVICE).read_text(encoding="utf-8")
    escaped = str(dollar_bin / "agent-hub").replace("$", "$$")
    assert f'ExecStart="{escaped}" skills weekly' in service
