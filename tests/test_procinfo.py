import json
import os
import subprocess
import time

from agenthub.procinfo import is_running, pid_file_dead, start_time


def test_start_time_of_self_is_int():
    value = start_time(os.getpid())
    assert isinstance(value, int)
    assert value > 0


def test_start_time_of_missing_pid_is_none():
    assert start_time(999999999) is None


def test_start_time_parses_comm_with_spaces_and_parens(tmp_path):
    d = tmp_path / "4242"
    d.mkdir()
    fields_after = ["S"] + ["0"] * 18 + ["123456"] + ["0"] * 5  # state=field3 ... starttime=field22
    (d / "stat").write_text("4242 (we ird) name) " + " ".join(fields_after) + "\n")
    assert start_time(4242, proc_root=tmp_path) == 123456


def test_is_running_false_for_zombie():
    proc = subprocess.Popen(["true"])
    try:
        deadline = time.monotonic() + 5
        state = ""
        while time.monotonic() < deadline:
            text = open(f"/proc/{proc.pid}/stat").read()
            state = text[text.rfind(")") + 1 :].split()[0]
            if state == "Z":
                break
            time.sleep(0.02)
        assert state == "Z"
        assert is_running(proc.pid) is False
    finally:
        proc.wait()


def test_is_running_true_for_self():
    assert is_running(os.getpid()) is True


def test_is_running_false_for_missing(tmp_path):
    assert is_running(4242, proc_root=tmp_path) is False


def test_pid_file_dead_for_reaped_process(tmp_path):
    proc = subprocess.Popen(["true"])
    proc.wait()
    pid_file = tmp_path / "pid"
    pid_file.write_text(json.dumps({"pid": proc.pid, "start_time": 123456}))
    assert pid_file_dead(pid_file) is True


def test_pid_file_dead_false_for_self(tmp_path):
    pid = os.getpid()
    st = start_time(pid)
    assert isinstance(st, int)
    pid_file = tmp_path / "pid"
    pid_file.write_text(json.dumps({"pid": pid, "start_time": st}))
    assert pid_file_dead(pid_file) is False


def test_pid_file_dead_for_reused_pid(tmp_path):
    pid = os.getpid()
    st = start_time(pid)
    assert isinstance(st, int)
    pid_file = tmp_path / "pid"
    pid_file.write_text(json.dumps({"pid": pid, "start_time": st + 9999}))
    assert pid_file_dead(pid_file) is True


def test_pid_file_dead_false_for_missing_or_legacy(tmp_path):
    missing = tmp_path / "nonexistent"
    assert pid_file_dead(missing) is False

    legacy = tmp_path / "legacy"
    legacy.write_text("123")
    assert pid_file_dead(legacy) is False
