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
