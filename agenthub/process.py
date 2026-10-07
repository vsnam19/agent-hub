import os
import signal
import subprocess
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Callable

from agenthub.agents import Invocation

KILL_GRACE_S = 10.0


@dataclass(frozen=True)
class Ran:
    rc: int
    timed_out: bool
    duration_s: float
    pgid: int


def run(
    inv: Invocation,
    cwd: Path,
    env: dict[str, str],
    attempt_dir: Path,
    timeout: timedelta,
    on_start: Callable[[int], None] = lambda pgid: None,
) -> Ran:
    start_time = time.monotonic()

    stdout_fd = os.open(attempt_dir / "stdout", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    stderr_fd = os.open(attempt_dir / "stderr", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)

    if inv.stdin_path:
        stdin_fd = os.open(inv.stdin_path, os.O_RDONLY)
    else:
        stdin_fd = os.open(os.devnull, os.O_RDONLY)

    try:
        proc = subprocess.Popen(
            inv.argv,
            cwd=cwd,
            env=env,
            stdin=stdin_fd,
            stdout=stdout_fd,
            stderr=stderr_fd,
            start_new_session=True,
        )
        pgid = proc.pid
        on_start(pgid)

        timed_out = False
        try:
            rc = proc.wait(timeout=timeout.total_seconds())
        except subprocess.TimeoutExpired:
            timed_out = True
            terminate(pgid)
            proc.wait()
            rc = -signal.SIGTERM

        # Terminate any remaining processes in the group
        if not timed_out:
            try:
                os.killpg(pgid, signal.SIGTERM)
            except ProcessLookupError:
                pass

        duration_s = time.monotonic() - start_time
        return Ran(rc=rc, timed_out=timed_out, duration_s=duration_s, pgid=pgid)

    finally:
        os.close(stdout_fd)
        os.close(stderr_fd)
        os.close(stdin_fd)


def terminate(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return

    time.sleep(KILL_GRACE_S)

    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
