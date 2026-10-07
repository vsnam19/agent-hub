import os

from agenthub.procinfo import start_time


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
