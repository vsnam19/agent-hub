import json
import os
import shutil
import subprocess

import pytest


def test_shellcheck_clean():
    if not shutil.which("shellcheck"):
        pytest.fail("shellcheck is not on PATH")
    subprocess.run(["shellcheck", "scripts/gwen-host.sh", "scripts/gwen-host/gwen-serve"], check=True)


@pytest.fixture(autouse=True)
def setup_fake_env(monkeypatch, tmp_path):
    log_dir = tmp_path / "fakes_log"
    log_dir.mkdir()
    monkeypatch.setenv("FAKE_LOG_DIR", str(log_dir))

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    recorder_path = os.path.abspath("tests/fakes/ssh-recorder")

    os.symlink(recorder_path, bin_dir / "ssh")
    os.symlink(recorder_path, bin_dir / "curl")

    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("GWEN_SSH", "fake-ssh-target")

    key_file = tmp_path / "api-key"
    key_file.write_text("fake-api-key-123")
    monkeypatch.setenv("GWEN_KEY_FILE", str(key_file))


def run_host_script(*args):
    return subprocess.run(["scripts/gwen-host.sh", *args], capture_output=True, text=True)


def get_fake_logs(tmp_path):
    log_dir = tmp_path / "fakes_log"
    logs = []
    for p in log_dir.glob("*.json"):
        with open(p) as f:
            logs.append(json.load(f))
    return logs


def test_switch_writes_model_and_kickstarts(tmp_path):
    res = run_host_script("switch", "my-model-name")
    assert res.returncode == 0
    logs = get_fake_logs(tmp_path)
    assert len(logs) == 1
    assert "fake-ssh-target" in logs[0]["argv"]
    cmd = "echo 'my-model-name' > ~/.config/gwen/model; launchctl kickstart -k gui/$(id -u)/dev.vsn.gwen"
    assert cmd in logs[0]["argv"]


def test_switch_rejects_bad_model(tmp_path):
    res = run_host_script("switch", "x; rm -rf ~")
    assert res.returncode == 2
    logs = get_fake_logs(tmp_path)
    assert len(logs) == 0


def test_logs_rejects_non_numeric(tmp_path):
    res = run_host_script("logs", "5; id")
    assert res.returncode == 2
    assert get_fake_logs(tmp_path) == []


def test_install_puts_local_bin_on_path(tmp_path):
    res = run_host_script("install")
    assert res.returncode == 0
    first = get_fake_logs(tmp_path)[0]["argv"][-1]
    assert first.index(".local/bin") < first.index("uv tool install")


def test_install_sends_key_on_stdin_not_argv(tmp_path):
    res = run_host_script("install")
    assert res.returncode == 0
    logs = get_fake_logs(tmp_path)

    key_in_stdin = False
    for log in logs:
        for arg in log["argv"]:
            assert "fake-api-key-123" not in arg
        if "fake-api-key-123" in log["stdin"]:
            key_in_stdin = True
    assert key_in_stdin


def test_status_curl_key_not_in_argv(tmp_path):
    res = run_host_script("status")
    assert res.returncode == 0
    logs = get_fake_logs(tmp_path)

    key_in_stdin = False
    curl_found = False
    for log in logs:
        if "curl" in log["argv"][0]:
            curl_found = True
            for arg in log["argv"]:
                assert "fake-api-key-123" not in arg
            if "fake-api-key-123" in log["stdin"]:
                key_in_stdin = True
    assert curl_found, "curl was not called"
    assert key_in_stdin


def test_unknown_command_usage_exit_2():
    res = run_host_script("unknown")
    assert res.returncode == 2
    assert "Usage:" in res.stderr
