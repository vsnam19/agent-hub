import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenthub.cli import AUDIT_SPEC, ScheduleSpec, main

SERVICE = "agent-hub-audit-archive.service"
TIMER = "agent-hub-audit-archive.timer"
DEFAULT_AT = "Sun *-*-01..07 21:17:00"

SKILLS_SERVICE = "agent-hub-skills-weekly.service"
SKILLS_TIMER = "agent-hub-skills-weekly.timer"


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
        'if [ -n "$FAKE_SYSTEMCTL_FAIL" ]; then echo "Failed to connect to bus" >&2; exit 1; fi\n'
        'if [ -n "$FAKE_SYSTEMCTL_FAIL_ON" ]; then\n'
        '  case "$*" in *"$FAKE_SYSTEMCTL_FAIL_ON"*) echo "Failed to connect to bus" >&2; exit 1 ;; esac\n'
        "fi\n"
        'case "$*" in *is-active*) [ -z "$FAKE_TIMER_INACTIVE" ] || exit 3 ;; esac',
    )
    # like systemd-analyze: a value after "--" is a calendar spec even when it starts with "-";
    # without "--" the value "--version" would print the version and succeed
    script(
        bin_dir / "systemd-analyze",
        'case "$*" in\n'
        '  calendar\\ --\\ -*|*bogus*) echo "Failed to parse calendar specification" >&2; exit 1 ;;\n'
        "  calendar\\ --\\ *) echo normalized; exit 0 ;;\n"
        '  *) echo "systemd 255"; exit 0 ;;\n'
        "esac",
    )
    config = tmp_path / "config"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.delenv("FAKE_SYSTEMCTL_FAIL", raising=False)
    monkeypatch.delenv("FAKE_SYSTEMCTL_FAIL_ON", raising=False)
    monkeypatch.delenv("FAKE_TIMER_INACTIVE", raising=False)
    return SimpleNamespace(bin=bin_dir, unit_dir=config / "systemd" / "user", log=log)


def calls(sched) -> list[str]:
    return sched.log.read_text().splitlines() if sched.log.exists() else []


def test_dry_run_prints_units_and_writes_nothing(sched, capsys):
    rc = main(["audit", "schedule"])

    out = capsys.readouterr().out
    assert rc == 0
    assert f"ExecStart={sched.bin / 'agent-hub'} audit archive" in out
    assert f"OnCalendar={DEFAULT_AT}" in out
    assert "Persistent=true" in out
    assert f"systemctl --user enable --now {TIMER}" in out
    assert "dry run: nothing written; add --apply to schedule" in out
    assert not sched.unit_dir.exists()
    assert calls(sched) == []


def test_default_calendar_is_first_sunday_21_17(sched, capsys):
    assert AUDIT_SPEC.default_calendar == DEFAULT_AT
    assert AUDIT_SPEC.default_at == DEFAULT_AT

    rc = main(["audit", "schedule"])
    out = capsys.readouterr().out
    assert rc == 0
    assert f"OnCalendar={DEFAULT_AT}" in out


def test_apply_writes_only_audit_units(sched, capsys):
    rc = main(["audit", "schedule", "--apply"])

    assert rc == 0
    service = (sched.unit_dir / SERVICE).read_text(encoding="utf-8")
    timer = (sched.unit_dir / TIMER).read_text(encoding="utf-8")
    assert "Type=oneshot" in service
    assert f"ExecStart={sched.bin / 'agent-hub'} audit archive" in service
    assert f"Environment=PATH={os.environ['PATH']}" in service
    assert "TimeoutStartSec=1h" in service
    assert "Nice=10" in service
    assert f"OnCalendar={DEFAULT_AT}" in timer
    assert "Persistent=true" in timer
    assert "WantedBy=timers.target" in timer
    assert not (sched.unit_dir / SKILLS_SERVICE).exists()
    assert not (sched.unit_dir / SKILLS_TIMER).exists()
    assert calls(sched) == ["--user daemon-reload", f"--user enable --now {TIMER}"]


def test_remove_deletes_only_audit_units_and_keeps_skills_units(sched, capsys):
    assert main(["skills", "schedule", "--apply"]) == 0
    assert (sched.unit_dir / SKILLS_SERVICE).exists()
    assert (sched.unit_dir / SKILLS_TIMER).exists()

    assert main(["audit", "schedule", "--apply"]) == 0
    assert (sched.unit_dir / SERVICE).exists()
    assert (sched.unit_dir / TIMER).exists()

    log_before = len(calls(sched))
    capsys.readouterr()

    rc = main(["audit", "schedule", "--remove"])

    assert rc == 0
    out = capsys.readouterr().out
    assert f"removed: {sched.unit_dir / TIMER}" in out
    assert not (sched.unit_dir / SERVICE).exists()
    assert not (sched.unit_dir / TIMER).exists()
    assert (sched.unit_dir / SKILLS_SERVICE).exists()
    assert (sched.unit_dir / SKILLS_TIMER).exists()
    assert calls(sched)[log_before:] == [f"--user disable --now {TIMER}", "--user daemon-reload"]


def test_at_is_validated_before_writing(sched, capsys):
    rc = main(["audit", "schedule", "--apply", "--at", "bogus"])

    assert rc == 2
    assert "bogus" in capsys.readouterr().err
    assert not sched.unit_dir.exists()
    assert calls(sched) == []

    rc2 = main(["audit", "schedule", "--apply", "--at=--version"])
    assert rc2 == 2
    assert "--version" in capsys.readouterr().err
    assert not sched.unit_dir.exists()
    assert calls(sched) == []


def test_apply_failure_rolls_back(sched, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_SYSTEMCTL_FAIL_ON", "enable --now")
    log_before = len(calls(sched))

    rc = main(["audit", "schedule", "--apply"])

    assert rc == 1
    assert "Failed to connect to bus" in capsys.readouterr().err
    assert calls(sched)[log_before:] == [
        "--user daemon-reload",
        f"--user enable --now {TIMER}",
        f"--user disable --now {TIMER}",
        "--user daemon-reload",
    ]
    assert not (sched.unit_dir / SERVICE).exists()
    assert not (sched.unit_dir / TIMER).exists()


def test_apply_twice_is_idempotent(sched, capsys):
    main(["audit", "schedule", "--apply"])
    before = (sched.unit_dir / TIMER).read_text(encoding="utf-8")
    log_before = calls(sched)
    capsys.readouterr()

    rc = main(["audit", "schedule", "--apply"])

    assert rc == 0
    assert "already scheduled" in capsys.readouterr().out
    assert (sched.unit_dir / TIMER).read_text(encoding="utf-8") == before
    assert calls(sched)[len(log_before) :] == [f"--user is-enabled {TIMER}", f"--user is-active {TIMER}"]


def test_remove_when_not_scheduled_exit_0(sched, capsys):
    rc = main(["audit", "schedule", "--remove"])

    assert rc == 0
    assert "nothing scheduled" in capsys.readouterr().out
    assert not sched.unit_dir.exists()
    assert calls(sched) == []


def test_audit_spec_fields():
    assert isinstance(AUDIT_SPEC, ScheduleSpec)
    assert AUDIT_SPEC.unit == "agent-hub-audit-archive"
    assert AUDIT_SPEC.subcommand == "audit archive"
    assert AUDIT_SPEC.default_calendar == "Sun *-*-01..07 21:17:00"
    assert "monthly" in AUDIT_SPEC.service_description.lower()
    assert "monthly" in AUDIT_SPEC.timer_description.lower()
