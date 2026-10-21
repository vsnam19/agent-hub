import json
import os
import shutil
import signal
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.gitrepo import make_repo


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_HUB_STATE", str(tmp_path / "hub_state"))
    monkeypatch.setenv("AGENT_HUB_CONFIG", str(tmp_path / "hub_config"))


@pytest.fixture
def env_sandbox(tmp_path, monkeypatch):
    home = tmp_path / "home"
    config = tmp_path / "config"
    state = tmp_path / "state"
    fakelog = tmp_path / "fakelog"

    home.mkdir()
    config.mkdir()
    state.mkdir()
    fakelog.mkdir()

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("AGENT_HUB_CONFIG", str(config))
    monkeypatch.setenv("AGENT_HUB_STATE", str(state))
    monkeypatch.setenv("FAKE_LOG_DIR", str(fakelog))

    fakes_dir = Path(__file__).parent / "fakes"
    git_dir = Path(shutil.which("git")).parent
    py_dir = Path(shutil.which("python3")).parent

    new_path = f"{fakes_dir}:{git_dir}:{py_dir}:/usr/bin:/bin"
    monkeypatch.setenv("PATH", new_path)

    ns = SimpleNamespace(
        home=home,
        config=config,
        state=state,
        fakelog=fakelog,
    )
    return ns


@pytest.fixture(autouse=True)
def check_fakes_sandbox(request):
    if "env_sandbox" in request.fixturenames:
        request.getfixturevalue("env_sandbox")
        fakes_dir = Path(__file__).parent / "fakes"
        for agent in ["agy", "codex", "claude"]:
            which_path = shutil.which(agent)
            if which_path is not None:
                assert Path(which_path).parent == fakes_dir, f"Real {agent} found on PATH instead of fake: {which_path}"


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path / "repo")


def fake_calls(fakelog: Path, name: str) -> list[dict]:
    calls = []
    # read sequentially
    i = 1
    while True:
        log_file = fakelog / f"{name}.{i}.json"
        if not log_file.exists():
            break
        calls.append(json.loads(log_file.read_text()))
        i += 1
    return calls


@pytest.fixture(autouse=True)
def _restore_environ():
    """Undo any direct os.environ edits a test makes, so nothing leaks into later tests."""
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


_AGENT_NAMES = ("agy", "claude", "codex", "opencode")
_FAKES_DIR = Path(__file__).parent / "fakes"
_STUB_DIR_CACHE: dict[str, Path] = {}


def _make_stub_dir(base: Path) -> Path:
    stub_dir = base / "agent_stubs"
    stub_dir.mkdir(parents=True, exist_ok=True)
    for name in _AGENT_NAMES:
        exe = stub_dir / name
        exe.write_text(
            "#!/bin/sh\n"
            'if [ -n "$AGENTHUB_REAL_AGENT_LOG" ]; then echo "$0 $*" >> "$AGENTHUB_REAL_AGENT_LOG"; fi\n'
            "exit 97\n"
        )
        exe.chmod(0o755)
    return stub_dir


def _has_real_agent(directory: str) -> bool:
    if not directory:
        return False
    for name in _AGENT_NAMES:
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return True
    return False


def _guarded_path(path: str, stub_dir: Path) -> str:
    """Replace PATH entries holding a real agent binary with the stub dir (added at most once)."""
    out: list[str] = []
    stub_added = False
    stub = str(stub_dir)
    for entry in path.split(os.pathsep):
        if entry == stub:
            if not stub_added:
                out.append(entry)
                stub_added = True
            continue
        try:
            is_fakes = bool(entry) and Path(entry).resolve() == _FAKES_DIR.resolve()
        except OSError:
            is_fakes = False
        if not is_fakes and _has_real_agent(entry):
            if not stub_added:
                out.append(stub)
                stub_added = True
            continue
        out.append(entry)
    return os.pathsep.join(out)


_STDLIB_HELPERS = ("multiprocessing.resource_tracker", "multiprocessing.forkserver")


def _leaked_pids(token: str) -> list[int]:
    """Pids (current uid, not this process) whose environ carries AGENTHUB_TEST_TOKEN=<token>."""
    proc = Path("/proc")
    if not proc.is_dir():
        return []
    needle = f"AGENTHUB_TEST_TOKEN={token}".encode()
    uid = os.getuid()
    me = os.getpid()
    found = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == me:
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
            environ = (entry / "environ").read_bytes()
        except OSError:
            continue
        if needle not in environ.split(b"\0"):
            continue
        # The stdlib multiprocessing helper is a session-wide singleton that inherits the env of
        # whichever test first used multiprocessing; it is not a leak of that test.
        if any(h in _cmdline(pid) for h in _STDLIB_HELPERS):
            continue
        found.append(pid)
    return found


def _kill_pid(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, PermissionError):
        return
    if pgid == pid and pgid != os.getpgrp():
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _cmdline(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return "<unavailable>"
    return raw.replace(b"\0", b" ").decode(errors="replace").strip()


@pytest.fixture(autouse=True)
def _guard_agents_and_children(tmp_path_factory, tmp_path, monkeypatch):
    if "stub" not in _STUB_DIR_CACHE:
        _STUB_DIR_CACHE["stub"] = _make_stub_dir(tmp_path_factory.mktemp("agentguard", numbered=False))
    stub_dir = _STUB_DIR_CACHE["stub"]
    log = tmp_path / "real_agent.log"
    token = uuid.uuid4().hex
    monkeypatch.setenv("AGENTHUB_REAL_AGENT_LOG", str(log))
    monkeypatch.setenv("AGENTHUB_TEST_TOKEN", token)
    monkeypatch.setenv("PATH", _guarded_path(os.environ.get("PATH", ""), stub_dir))
    yield
    problems = []
    if Path("/proc").is_dir():
        deadline = time.monotonic() + 3
        leaked = _leaked_pids(token)
        while leaked and time.monotonic() < deadline:
            time.sleep(0.1)
            leaked = _leaked_pids(token)
        if leaked:
            descr = [(pid, _cmdline(pid)) for pid in leaked]
            for pid in leaked:
                _kill_pid(pid)
            problems.append("test leaked running processes: " + "; ".join(f"pid {pid}: {cmd}" for pid, cmd in descr))
    if log.exists() and log.read_text().strip():
        problems.append(f"test ran a real agent: {log.read_text().strip()}")
    if problems:
        pytest.fail("\n".join(problems))
