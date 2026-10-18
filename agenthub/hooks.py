import json
import os
import signal
import subprocess
import threading
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO

from agenthub.config import Paths, paths
from agenthub.jobs import list_jobs
from agenthub.procinfo import is_running, read_pid_file, start_time

ALL_JOBS = 100000


@contextmanager
def _timeout_budget(seconds: int = 5):
    can_alarm = hasattr(signal, "alarm") and threading.current_thread() is threading.main_thread()
    if not can_alarm:
        yield
        return

    def _on_timeout(signum, frame):
        raise TimeoutError(f"budget of {seconds}s exceeded")

    old_handler = signal.signal(signal.SIGALRM, _on_timeout)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)


def _repo_and_worktrees(cwd: str | Path) -> tuple[Path, set[Path]]:
    """Return (repo, worktrees).

    The repo of cwd is `git rev-parse --show-toplevel` (or cwd itself when not a git repo).
    worktrees contains the repo and any worktrees from `git worktree list --porcelain`.
    """
    cwd_path = Path(cwd).resolve()
    try:
        res = subprocess.run(
            ["git", "-C", str(cwd_path), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        if res.returncode == 0 and res.stdout.strip():
            repo = Path(res.stdout.strip()).resolve()
        else:
            return cwd_path, {cwd_path}
    except OSError:
        return cwd_path, {cwd_path}

    worktrees = {repo}
    try:
        wt_res = subprocess.run(
            ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        if wt_res.returncode == 0:
            for line in wt_res.stdout.splitlines():
                if line.startswith("worktree "):
                    wt_dir = line[len("worktree ") :].strip()
                    if wt_dir:
                        worktrees.add(Path(wt_dir).resolve())
    except OSError:
        pass

    return repo, worktrees


def _live_runner(plan_dir: Path) -> int | None:
    """Return the recorded runner pid when it is alive and verified."""
    state_file = plan_dir / "state.json"
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
    if isinstance(pid, int) and recorded is not None and is_running(pid) and start_time(pid) == recorded:
        return pid
    return None


def _get_alive_running_jobs(p: Paths, worktrees: set[Path]) -> list[dict]:
    alive_jobs = []
    for r in list_jobs(p, ALL_JOBS):
        if r.get("status") != "running":
            continue
        workdir_str = r.get("workdir")
        if not workdir_str:
            continue
        workdir = Path(workdir_str).resolve()
        if not any(workdir == wt or workdir.is_relative_to(wt) for wt in worktrees):
            continue
        pid_file = p.jobs_dir / r["id"] / "pid"
        pid, recorded = read_pid_file(pid_file)
        if pid is not None and recorded is not None and is_running(pid) and start_time(pid) == recorded:
            alive_jobs.append(r)
    return alive_jobs


def _get_plans_info(p: Paths, repo: Path, worktrees: set[Path]) -> list[dict]:
    plans_dir = p.state_dir / "plans"
    if not plans_dir.is_dir():
        return []

    plans_info = []
    for plan_dir in sorted(plans_dir.iterdir()):
        if not plan_dir.is_dir():
            continue
        plan_file = plan_dir / "plan.json"
        if not plan_file.is_file():
            continue
        try:
            plan_data = json.loads(plan_file.read_text())
            if not isinstance(plan_data, dict):
                continue
        except (OSError, ValueError):
            continue

        plan_repo_str = plan_data.get("repo")
        if not plan_repo_str:
            continue
        plan_repo = Path(plan_repo_str).resolve()
        if plan_repo != repo and plan_repo not in worktrees:
            continue

        slug = plan_data.get("slug") or plan_dir.name
        live_runner = _live_runner(plan_dir)

        state_file = plan_dir / "state.json"
        state_tasks = {}
        if state_file.is_file():
            try:
                state_data = json.loads(state_file.read_text())
                if isinstance(state_data, dict):
                    state_tasks = state_data.get("tasks", {})
            except (OSError, ValueError):
                pass

        plan_tasks = plan_data.get("tasks", [])
        counts: Counter[str] = Counter()
        has_tasks = False

        if isinstance(plan_tasks, list) and plan_tasks:
            has_tasks = True
            for t in plan_tasks:
                t_name = t.get("name") if isinstance(t, dict) else str(t)
                ts = state_tasks.get(t_name, {}) if isinstance(state_tasks, dict) else {}
                status = ts.get("status", "pending") if isinstance(ts, dict) else "pending"
                counts[status] += 1
        elif isinstance(state_tasks, dict) and state_tasks:
            has_tasks = True
            for _t_name, ts in state_tasks.items():
                status = ts.get("status", "pending") if isinstance(ts, dict) else "pending"
                counts[status] += 1

        has_unmerged = has_tasks and any(st != "merged" for st in counts.keys())

        plans_info.append(
            {
                "slug": slug,
                "dir": plan_dir,
                "live_runner": live_runner,
                "counts": dict(counts),
                "has_unmerged": has_unmerged,
            }
        )
    return plans_info


def session_start(payload: dict, p: Paths) -> dict | None:
    cwd = payload.get("cwd") or os.getcwd()
    repo, worktrees = _repo_and_worktrees(cwd)

    lines: list[str] = []

    # 1. Running jobs whose process is alive (id, role, work tree)
    alive_jobs = _get_alive_running_jobs(p, worktrees)
    for job in alive_jobs:
        lines.append(f"running job {job['id']} ({job.get('role', 'unknown')}) in {job.get('workdir', '')}")

    # 2. Plans with a live runner or tasks not yet merged (slug and counts by status)
    plans_info = _get_plans_info(p, repo, worktrees)
    for pi in plans_info:
        if pi["live_runner"] is not None or pi["has_unmerged"]:
            counts_str = ", ".join(f"{cnt} {st}" for st, cnt in sorted(pi["counts"].items()))
            if counts_str:
                lines.append(f"plan {pi['slug']}: {counts_str}")
            else:
                lines.append(f"plan {pi['slug']}")

    # 3. Autopilot line
    autopilot_found = False
    next_phase = repo / "docs" / "next-phase.md"
    if next_phase.is_file():
        try:
            content = next_phase.read_text(encoding="utf-8")
            if content.startswith("# AUTOPILOT"):
                autopilot_found = True
        except OSError:
            pass

    if not autopilot_found:
        for wt in worktrees:
            candidate = wt / "docs" / "next-phase.md"
            if candidate.is_file():
                try:
                    if candidate.read_text(encoding="utf-8").startswith("# AUTOPILOT"):
                        autopilot_found = True
                        break
                except OSError:
                    pass

    if autopilot_found:
        lines.append("owner's resume protocol: docs/next-phase.md AUTOPILOT")

    if not lines:
        return None

    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "\n".join(lines),
        }
    }


def stop(payload: dict, p: Paths) -> dict | None:
    cwd = payload.get("cwd") or os.getcwd()
    repo, worktrees = _repo_and_worktrees(cwd)

    alive_jobs = _get_alive_running_jobs(p, worktrees)
    plans_info = _get_plans_info(p, repo, worktrees)
    live_runners = [pi for pi in plans_info if pi["live_runner"] is not None]

    session_id = payload.get("session_id") or "default"
    counter_file = p.state_dir / "hooks" / f"stop-{session_id}"

    if not alive_jobs and not live_runners:
        if counter_file.exists():
            try:
                counter_file.unlink(missing_ok=True)
            except OSError:
                pass
        return None

    running_desc: list[str] = []
    for job in alive_jobs:
        running_desc.append(f"job {job['id']} ({job.get('role', 'unknown')})")
    for pi in live_runners:
        running_desc.append(f"plan runner {pi['slug']}")

    what_is_running = ", ".join(running_desc)

    counter_file.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    if counter_file.is_file():
        try:
            count = int(counter_file.read_text().strip())
        except (ValueError, OSError):
            count = 0
    count += 1
    try:
        counter_file.write_text(str(count))
    except OSError:
        pass

    if count > 20:
        return {"systemMessage": f"agent-hub: stop guard gave up after 20 blocks; {what_is_running}"}

    return {
        "decision": "block",
        "reason": (
            f"{what_is_running} — wait with agent-hub wait -d <wt> / agent-hub plan wait <plan> before ending the turn"
        ),
    }


def main(event: str, stdin: TextIO, stdout: TextIO, p: Paths | None = None) -> int:
    try:
        with _timeout_budget(5):
            if p is None:
                p = paths()
            raw_input = stdin.read()
            payload = json.loads(raw_input)
            if not isinstance(payload, dict):
                return 0

            if event == "session-start":
                result = session_start(payload, p)
            elif event == "stop":
                result = stop(payload, p)
            else:
                result = None

            if result is not None:
                stdout.write(json.dumps(result) + "\n")
                stdout.flush()
    except Exception:
        return 0
    return 0
