import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenthub.cli import _systemd_word, main

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
    assert calls(sched)[len(log_before) :] == [f"--user is-enabled {TIMER}", f"--user is-active {TIMER}"]


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


def test_environment_not_dollar_escaped(sched, capsys, monkeypatch):
    dollar_bin = sched.bin.parent / "tools$x"
    dollar_bin.mkdir()
    script(dollar_bin / "agent-hub", "exit 0")
    monkeypatch.setenv("PATH", f"{dollar_bin}:{os.environ['PATH']}")

    rc = main(["skills", "schedule", "--apply"])

    assert rc == 0
    service = (sched.unit_dir / SERVICE).read_text(encoding="utf-8")
    assert f"Environment=PATH={os.environ['PATH']}\n" in service
    assert "$$" in service.split("Environment=")[0]  # ExecStart keeps its escape


def test_at_option_like_rejected(sched, capsys):
    rc = main(["skills", "schedule", "--apply", "--at=--version"])

    assert rc == 2
    assert "--version" in capsys.readouterr().err
    assert not sched.unit_dir.exists()
    assert calls(sched) == []


def test_stopped_timer_restarted(sched, capsys, monkeypatch):
    assert main(["skills", "schedule", "--apply"]) == 0
    capsys.readouterr()
    monkeypatch.setenv("FAKE_TIMER_INACTIVE", "1")
    log_before = len(calls(sched))

    rc = main(["skills", "schedule", "--apply"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "scheduled:" in out
    assert "already scheduled" not in out
    assert calls(sched)[log_before:] == [
        f"--user is-enabled {TIMER}",
        f"--user is-active {TIMER}",
        "--user daemon-reload",
        f"--user enable --now {TIMER}",
    ]


def test_rollback_disables_and_reloads(sched, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_SYSTEMCTL_FAIL_ON", "enable --now")
    log_before = len(calls(sched))

    rc = main(["skills", "schedule", "--apply"])

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


def test_failed_reschedule_restores_working_timer(sched, capsys, monkeypatch):
    assert main(["skills", "schedule", "--apply"]) == 0
    capsys.readouterr()
    before_service = (sched.unit_dir / SERVICE).read_text(encoding="utf-8")
    before_timer = (sched.unit_dir / TIMER).read_text(encoding="utf-8")

    monkeypatch.setenv("FAKE_SYSTEMCTL_FAIL_ON", "enable --now")
    log_before = len(calls(sched))

    rc = main(["skills", "schedule", "--apply", "--at", "Mon *-*-* 09:00"])

    assert rc == 1
    assert "Failed to connect to bus" in capsys.readouterr().err
    assert (sched.unit_dir / SERVICE).read_text(encoding="utf-8") == before_service
    assert (sched.unit_dir / TIMER).read_text(encoding="utf-8") == before_timer
    assert calls(sched)[log_before:] == [
        f"--user is-enabled {TIMER}",
        f"--user is-active {TIMER}",
        "--user daemon-reload",
        f"--user enable --now {TIMER}",
        f"--user disable --now {TIMER}",
        "--user daemon-reload",
        f"--user enable {TIMER}",
        f"--user restart {TIMER}",
    ]


def test_restore_error_keeps_original_error(sched, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_SYSTEMCTL_FAIL_ON", "enable --now")

    orig_unlink = Path.unlink

    def fake_unlink(self, *args, **kwargs):
        if self.name == TIMER:
            raise OSError("permission denied during restore")
        return orig_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fake_unlink)

    rc = main(["skills", "schedule", "--apply"])

    assert rc == 1
    err = capsys.readouterr().err
    lines = [line for line in err.splitlines() if line.strip()]
    assert lines[0] == "agent-hub: Failed to connect to bus"
    assert any("permission denied during restore" in line for line in lines[1:])
    assert err.index("Failed to connect to bus") < err.index("permission denied during restore")


def test_single_quote_value_quoted(sched, capsys, monkeypatch):
    assert _systemd_word("o'neil") == '"o\'neil"'
    assert _systemd_word("PATH=/home/o'neil/bin") == '"PATH=/home/o\'neil/bin"'
    assert _systemd_word("/home/o'neil/bin/agent-hub", exec_line=True) == '"/home/o\'neil/bin/agent-hub"'

    quote_bin = sched.bin.parent / "tools'x"
    quote_bin.mkdir()
    script(quote_bin / "agent-hub", "exit 0")
    monkeypatch.setenv("PATH", f"{quote_bin}:{os.environ['PATH']}")

    rc = main(["skills", "schedule", "--apply"])

    assert rc == 0
    service = (sched.unit_dir / SERVICE).read_text(encoding="utf-8")
    assert f'ExecStart="{quote_bin / "agent-hub"}" skills weekly' in service
    assert f'Environment="PATH={os.environ["PATH"]}"\n' in service


def test_remove_unlink_error_still_reloads(sched, capsys, monkeypatch):
    assert main(["skills", "schedule", "--apply"]) == 0
    capsys.readouterr()
    log_before = len(calls(sched))

    orig_unlink = Path.unlink

    def fake_unlink(self, *args, **kwargs):
        if self.name == SERVICE:
            raise OSError("permission denied during unlink")
        return orig_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fake_unlink)
    monkeypatch.setenv("FAKE_SYSTEMCTL_FAIL_ON", "disable")

    rc = main(["skills", "schedule", "--remove"])

    assert rc == 1
    err = capsys.readouterr().err
    assert f"disable {TIMER}" in err
    assert "permission denied during unlink" in err
    assert calls(sched)[log_before:] == [
        f"--user disable --now {TIMER}",
        "--user daemon-reload",
    ]
    assert not (sched.unit_dir / TIMER).exists()
    assert (sched.unit_dir / SERVICE).exists()

    monkeypatch.delenv("FAKE_SYSTEMCTL_FAIL_ON")
    log_before = len(calls(sched))
    capsys.readouterr()

    rc2 = main(["skills", "schedule", "--remove"])

    assert rc2 == 1
    err2 = capsys.readouterr().err
    assert "permission denied during unlink" in err2
    assert calls(sched)[log_before:] == [
        f"--user disable --now {TIMER}",
        "--user daemon-reload",
    ]
