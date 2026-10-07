import os
import signal
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest

from agenthub.agents import Invocation
from agenthub.process import run, terminate


def test_stdout_stderr_go_to_files(tmp_path: Path):
    attempt_dir = tmp_path / "attempt"
    attempt_dir.mkdir()
    script = tmp_path / "script.py"
    script.write_text("import sys; sys.stdout.write('out'); sys.stderr.write('err')")

    inv = Invocation(argv=[sys.executable, str(script)], stdin_path=None)
    ran = run(inv, cwd=tmp_path, env={}, attempt_dir=attempt_dir, timeout=timedelta(seconds=5))

    assert ran.rc == 0
    assert not ran.timed_out
    assert (attempt_dir / "stdout").read_text() == "out"
    assert (attempt_dir / "stderr").read_text() == "err"


def test_stdin_from_file(tmp_path: Path):
    attempt_dir = tmp_path / "attempt"
    attempt_dir.mkdir()
    stdin_file = tmp_path / "in.txt"
    stdin_file.write_text("input data")
    script = tmp_path / "script.py"
    script.write_text("import sys; sys.stdout.write(sys.stdin.read())")

    inv = Invocation(argv=[sys.executable, str(script)], stdin_path=stdin_file)
    ran = run(inv, cwd=tmp_path, env={}, attempt_dir=attempt_dir, timeout=timedelta(seconds=5))

    assert ran.rc == 0
    assert (attempt_dir / "stdout").read_text() == "input data"


def test_stdin_devnull_when_none(tmp_path: Path):
    attempt_dir = tmp_path / "attempt"
    attempt_dir.mkdir()
    script = tmp_path / "script.py"
    script.write_text("import sys; sys.stdout.write(sys.stdin.read())")

    inv = Invocation(argv=[sys.executable, str(script)], stdin_path=None)
    ran = run(inv, cwd=tmp_path, env={}, attempt_dir=attempt_dir, timeout=timedelta(seconds=5))

    assert ran.rc == 0
    assert (attempt_dir / "stdout").read_text() == ""


def test_nonzero_rc(tmp_path: Path):
    attempt_dir = tmp_path / "attempt"
    attempt_dir.mkdir()
    script = tmp_path / "script.py"
    script.write_text("import sys; sys.exit(42)")

    inv = Invocation(argv=[sys.executable, str(script)], stdin_path=None)
    ran = run(inv, cwd=tmp_path, env={}, attempt_dir=attempt_dir, timeout=timedelta(seconds=5))

    assert ran.rc == 42


def test_env_is_exact(tmp_path: Path):
    attempt_dir = tmp_path / "attempt"
    attempt_dir.mkdir()
    script = tmp_path / "script.py"
    script.write_text("import os, sys, json; json.dump(dict(os.environ), sys.stdout)")

    inv = Invocation(argv=[sys.executable, str(script)], stdin_path=None)
    env = {"MY_SPECIAL_VAR": "123"}
    run(inv, cwd=tmp_path, env=env, attempt_dir=attempt_dir, timeout=timedelta(seconds=5))

    import json

    out_env = json.loads((attempt_dir / "stdout").read_text())
    assert out_env.get("MY_SPECIAL_VAR") == "123"
    assert "OMITTED_VAR" not in out_env


def test_timeout_kills_group(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("agenthub.process.KILL_GRACE_S", 0.1)
    attempt_dir = tmp_path / "attempt"
    attempt_dir.mkdir()

    child_pid_file = tmp_path / "child.pid"
    script = tmp_path / "script.py"
    script.write_text(f"""
import os, sys, time
pid = os.fork()
if pid == 0:
    with open({str(child_pid_file)!r}, "w") as f:
        f.write(str(os.getpid()))
    time.sleep(10)
    sys.exit(0)
time.sleep(10)
""")

    inv = Invocation(argv=[sys.executable, str(script)], stdin_path=None)

    start = time.time()
    ran = run(inv, cwd=tmp_path, env={}, attempt_dir=attempt_dir, timeout=timedelta(seconds=1))
    duration = time.time() - start

    assert ran.timed_out
    assert ran.rc == -signal.SIGTERM
    assert duration < 3.0  # Should be quick

    # Wait for child pid file to be written, if it wasn't already (though it should be before script sleeps)
    for _ in range(50):
        if child_pid_file.exists():
            break
        time.sleep(0.1)

    assert child_pid_file.exists()
    child_pid = int(child_pid_file.read_text())

    # Check if grandchild is dead
    for _ in range(50):
        try:
            os.kill(child_pid, 0)
            time.sleep(0.1)
        except ProcessLookupError:
            break
    else:
        pytest.fail("Child process was not killed")


def test_grandchild_holding_stdout_does_not_block(tmp_path: Path):
    attempt_dir = tmp_path / "attempt"
    attempt_dir.mkdir()

    script = tmp_path / "script.py"
    script.write_text("""
import os, sys, time
pid = os.fork()
if pid == 0:
    # Grandchild inherits stdout/stderr
    time.sleep(30)
    sys.exit(0)
# Leader exits immediately
sys.exit(0)
""")

    inv = Invocation(argv=[sys.executable, str(script)], stdin_path=None)

    start = time.time()
    ran = run(inv, cwd=tmp_path, env={}, attempt_dir=attempt_dir, timeout=timedelta(seconds=5))
    duration = time.time() - start

    assert ran.rc == 0
    assert not ran.timed_out
    assert duration < 15.0  # Should not wait 30s for grandchild


def test_on_start_receives_pgid(tmp_path: Path):
    attempt_dir = tmp_path / "attempt"
    attempt_dir.mkdir()
    script = tmp_path / "script.py"
    script.write_text("import sys; sys.exit(0)")

    inv = Invocation(argv=[sys.executable, str(script)], stdin_path=None)

    received_pgid = None

    def on_start(pgid: int):
        nonlocal received_pgid
        received_pgid = pgid

    run(inv, cwd=tmp_path, env={}, attempt_dir=attempt_dir, timeout=timedelta(seconds=5), on_start=on_start)

    assert received_pgid is not None
    assert received_pgid > 0


def test_terminate_missing_group_is_quiet():
    # A process group that definitely doesn't exist (e.g. 999999)
    assert terminate(999999) is None  # Should not raise exception


def test_terminate_returns_quickly_when_group_exits(tmp_path: Path):
    import threading

    script = tmp_path / "script.py"
    script.write_text("import time; time.sleep(10)")
    proc = subprocess.Popen([sys.executable, str(script)], start_new_session=True)
    pgid = proc.pid
    threading.Thread(target=proc.wait).start()
    time.sleep(0.1)

    start = time.time()
    terminate(pgid)
    duration = time.time() - start

    assert duration < 2.0
    proc.wait()
    with pytest.raises(ProcessLookupError):
        os.killpg(pgid, 0)


def test_terminate_escalates_to_sigkill(tmp_path: Path, monkeypatch):
    import threading

    monkeypatch.setattr("agenthub.process.KILL_GRACE_S", 0.5)
    script = tmp_path / "script.py"
    script.write_text("import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(10)")
    proc = subprocess.Popen([sys.executable, str(script)], start_new_session=True)
    pgid = proc.pid
    threading.Thread(target=proc.wait).start()
    time.sleep(0.1)

    start = time.time()
    terminate(pgid)
    duration = time.time() - start

    assert duration < 2.0
    proc.wait()
    with pytest.raises(ProcessLookupError):
        os.killpg(pgid, 0)


def test_timeout_rc_is_real_returncode(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("agenthub.process.KILL_GRACE_S", 0.5)
    attempt_dir = tmp_path / "attempt"
    attempt_dir.mkdir()
    script = tmp_path / "script.py"
    script.write_text("import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(10)")

    inv = Invocation(argv=[sys.executable, str(script)], stdin_path=None)
    ran = run(inv, cwd=tmp_path, env={}, attempt_dir=attempt_dir, timeout=timedelta(seconds=0.5))

    assert ran.timed_out
    assert ran.rc == -signal.SIGKILL
