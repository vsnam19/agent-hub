import os
import signal
import subprocess
import sys

import pytest

from tests import conftest
from tests.conftest import _guarded_path, _leaked_pids


def _stub(tmp_path):
    d = tmp_path / "stub"
    d.mkdir()
    return d


def test_guard_stub_replaces_real_agent_dir(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    exe = real / "codex"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    stub = _stub(tmp_path)
    result = _guarded_path(f"{real}:/usr/bin:/bin", stub).split(os.pathsep)
    assert str(real) not in result
    assert result == [str(stub), "/usr/bin", "/bin"]


def test_guarded_path_keeps_fakes_dir(tmp_path):
    stub = _stub(tmp_path)
    fakes = conftest._FAKES_DIR
    assert (fakes / "agy").exists()
    result = _guarded_path(f"{fakes}:/usr/bin", stub).split(os.pathsep)
    assert result == [str(fakes), "/usr/bin"]


def test_guarded_path_adds_stub_once(tmp_path):
    stub = _stub(tmp_path)
    dirs = []
    for n in ("a", "b"):
        d = tmp_path / n
        d.mkdir()
        for name in ("agy", "claude"):
            (d / name).write_text("#!/bin/sh\n")
            (d / name).chmod(0o755)
        dirs.append(str(d))
    result = _guarded_path(os.pathsep.join([dirs[0], "/usr/bin", dirs[1], str(stub)]), stub).split(os.pathsep)
    assert result.count(str(stub)) == 1
    assert result == [str(stub), "/usr/bin"]


@pytest.mark.skipif(not os.path.isdir("/proc"), reason="needs /proc")
def test_find_leaked_pids_finds_child(monkeypatch):
    token = "tokentest" + os.urandom(4).hex()
    env = {**os.environ, "AGENTHUB_TEST_TOKEN": token}
    proc = subprocess.Popen(["sleep", "30"], env=env)
    try:
        assert proc.pid in _leaked_pids(token)
    finally:
        proc.kill()
        proc.wait()
    assert proc.pid not in _leaked_pids(token)


def test_kill_never_targets_own_group(monkeypatch):
    kills = []
    killpgs = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: kills.append((pid, sig)))
    monkeypatch.setattr(os, "killpg", lambda pg, sig: killpgs.append((pg, sig)))
    monkeypatch.setattr(os, "getpgid", lambda pid: os.getpgrp())
    conftest._kill_pid(424242)
    assert kills == [(424242, signal.SIGKILL)]
    assert killpgs == []
    # a pid leading its own, different group does get its group signalled
    monkeypatch.setattr(os, "getpgid", lambda pid: pid)
    conftest._kill_pid(424243)
    assert killpgs == [(424243, signal.SIGKILL)]


def test_stub_exits_97_and_logs(tmp_path):
    stub = conftest._STUB_DIR_CACHE["stub"]
    log = tmp_path / "log"
    r = subprocess.run([str(stub / "claude"), "hi", "there"], env={"AGENTHUB_REAL_AGENT_LOG": str(log)}, check=False)
    assert r.returncode == 97
    assert "claude hi there" in log.read_text()
    log.unlink()  # consumed deliberately: the guard fails tests that leave a log
    assert sys.platform  # keep import used
