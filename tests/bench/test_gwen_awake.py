import json
import os
import stat
import time

from bench.gwen.awake import KeepAwake

FAKE_SSH = """#!/usr/bin/env python3
import json, os, sys, time
log = os.path.join(os.environ["FAKE_LOG_DIR"], f"ssh-{os.getpid()}.json")
with open(log, "w") as f:
    json.dump({"argv": sys.argv, "pid": os.getpid()}, f)
time.sleep(60)
"""


def _fake_ssh(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    ssh = bindir / "ssh"
    ssh.write_text(FAKE_SSH)
    ssh.chmod(ssh.stat().st_mode | stat.S_IEXEC)
    logdir = tmp_path / "log"
    logdir.mkdir()
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_LOG_DIR", str(logdir))
    return logdir


def _wait_for_log(logdir):
    deadline = time.time() + 5
    while time.time() < deadline:
        logs = list(logdir.glob("ssh-*.json"))
        if logs:
            time.sleep(0.1)
            return json.loads(logs[0].read_text())
        time.sleep(0.05)
    raise AssertionError("fake ssh was never started")


def test_holds_caffeinate_with_a_tty_and_stops_it(tmp_path, monkeypatch):
    logdir = _fake_ssh(tmp_path, monkeypatch)
    with KeepAwake("me@mac") as awake:
        call = _wait_for_log(logdir)
        assert awake.proc.poll() is None
    assert call["argv"][1] == "-tt"
    assert call["argv"][-2:] == ["me@mac", "caffeinate -i"]
    assert awake.proc.poll() is not None


def test_stops_even_when_the_batch_raises(tmp_path, monkeypatch):
    logdir = _fake_ssh(tmp_path, monkeypatch)
    awake = KeepAwake("me@mac")
    try:
        with awake:
            _wait_for_log(logdir)
            raise RuntimeError("batch failed")
    except RuntimeError:
        pass
    assert awake.proc.poll() is not None


def test_disabled_without_target():
    with KeepAwake(None) as awake:
        assert awake.proc is None
