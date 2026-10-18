import json
import os
from pathlib import Path


def start_time(pid: int, *, proc_root: Path = Path("/proc")) -> int | None:
    """Process start time in clock ticks (field 22 of /proc/<pid>/stat), or None."""
    try:
        text = (proc_root / str(pid) / "stat").read_text()
    except OSError:
        return None
    # comm (field 2) may contain spaces and parentheses: parse after the last ')'.
    rest = text[text.rfind(")") + 1 :].split()
    try:
        return int(rest[19])  # field 3 is rest[0], so field 22 is rest[19]
    except (IndexError, ValueError):
        return None


def is_running(pid: int, *, proc_root: Path = Path("/proc")) -> bool:
    """False when the process is gone, a zombie (Z) or dead (X); True otherwise."""
    try:
        text = (proc_root / str(pid) / "stat").read_text()
    except OSError:
        return False
    rest = text[text.rfind(")") + 1 :].split()
    return not rest or rest[0] not in ("Z", "X")


def read_pid_file(path: Path) -> tuple[int | None, int | None]:
    """Return (pid, start_time) from the JSON pid file; a legacy plain int is unverifiable."""
    try:
        data = json.loads(Path(path).read_text())
        return int(data["pid"]), int(data["start_time"])
    except (OSError, ValueError, TypeError, KeyError):
        return None, None


def pid_file_dead(path: Path, *, proc_root: Path = Path("/proc")) -> bool:
    """True only when path holds JSON {"pid", "start_time"} and the process is gone.

    False for a missing, unreadable or legacy file (unknown -> keep waiting).
    """
    pid, recorded = read_pid_file(path)
    if pid is None or recorded is None:
        return False
    return not is_running(pid, proc_root=proc_root) or start_time(pid, proc_root=proc_root) != recorded


def live_runner(
    plan_dir: Path,
    *,
    exclude_self: bool = True,
    _is_running=None,
    _start_time=None,
) -> int | None:
    """Return the recorded runner pid when it is alive and verified (is_running and matching
    start_time, not this process if exclude_self=True), else None. Unreadable file -> None."""
    check_running = _is_running or is_running
    check_start_time = _start_time or start_time

    state_file = Path(plan_dir) / "state.json"
    try:
        data = json.loads(state_file.read_text())
        if not isinstance(data, dict):
            return None
    except (OSError, ValueError):
        return None
    runner = data.get("runner")
    if not isinstance(runner, dict):
        return None
    pid = runner.get("pid")
    recorded = runner.get("start_time")
    if (
        isinstance(pid, int)
        and recorded is not None
        and (not exclude_self or pid != os.getpid())
        and check_running(pid)
        and check_start_time(pid) == recorded
    ):
        return pid
    return None
