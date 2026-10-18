import json
import os
import re
import signal
import subprocess
import threading
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO

from agenthub.config import Paths, paths
from agenthub.jobs import list_jobs
from agenthub.procinfo import is_running, live_runner, read_pid_file, start_time

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


def _get_orch_branches(repo: Path) -> set[str]:
    try:
        res = subprocess.run(
            ["git", "-C", str(repo), "for-each-ref", "refs/heads/orch/"],
            capture_output=True,
            text=True,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        if res.returncode != 0:
            return set()
        branches = set()
        for line in res.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            ref = parts[-1]
            if ref.startswith("refs/heads/"):
                ref = ref[len("refs/heads/") :]
            branches.add(ref)
        return branches
    except OSError:
        return set()


def _get_merged_branches(repo: Path, base: str) -> set[str]:
    try:
        res = subprocess.run(
            ["git", "-C", str(repo), "branch", "--merged", base],
            capture_output=True,
            text=True,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        if res.returncode != 0:
            return set()
        merged = set()
        for line in res.stdout.splitlines():
            name = line.strip().lstrip("*+ ")
            if name:
                if name.startswith("refs/heads/"):
                    name = name[len("refs/heads/") :]
                merged.add(name)
        return merged
    except OSError:
        return set()


def _is_unmerged_done(
    t_branch: str,
    plan_repo: Path,
    base: str,
    orch_cache: dict[Path, set[str]],
    merged_cache: dict[tuple[Path, str], set[str]],
) -> bool:
    if not t_branch or not isinstance(t_branch, str):
        return False
    if t_branch.startswith("refs/heads/"):
        t_branch = t_branch[len("refs/heads/") :]
    if plan_repo not in orch_cache:
        orch_cache[plan_repo] = _get_orch_branches(plan_repo)
    if t_branch not in orch_cache[plan_repo]:
        return False
    key = (plan_repo, base)
    if key not in merged_cache:
        merged_cache[key] = _get_merged_branches(plan_repo, base)
    return t_branch not in merged_cache[key]


def _get_alive_running_jobs(p: Paths, worktrees: set[Path]) -> list[dict]:
    alive_jobs = []
    for r in list_jobs(p, ALL_JOBS):
        if not isinstance(r, dict):
            continue
        job_id = r.get("id")
        workdir_str = r.get("workdir")
        status = r.get("status")
        if not isinstance(job_id, str) or not isinstance(workdir_str, str) or not isinstance(status, str):
            continue
        if status != "running":
            continue
        if not workdir_str:
            continue
        try:
            workdir = Path(workdir_str).resolve()
        except Exception:
            continue
        if not any(workdir == wt or workdir.is_relative_to(wt) for wt in worktrees):
            continue
        pid_file = p.jobs_dir / job_id / "pid"
        pid, recorded = read_pid_file(pid_file)
        if pid is not None and recorded is not None and is_running(pid) and start_time(pid) == recorded:
            alive_jobs.append(r)
    return alive_jobs


def _get_plans_info(p: Paths, repo: Path, worktrees: set[Path]) -> list[dict]:
    plans_dir = p.state_dir / "plans"
    if not plans_dir.is_dir():
        return []

    orch_cache: dict[Path, set[str]] = {}
    merged_cache: dict[tuple[Path, str], set[str]] = {}

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
        if not plan_repo_str or not isinstance(plan_repo_str, str):
            continue
        try:
            plan_repo = Path(plan_repo_str).resolve()
        except Exception:
            continue
        if plan_repo != repo and plan_repo not in worktrees:
            continue

        slug = plan_data.get("slug") or plan_dir.name
        runner_pid = live_runner(plan_dir, exclude_self=False)
        base = plan_data.get("base") or "master"

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
        has_unmerged = False

        if isinstance(plan_tasks, list) and plan_tasks:
            for t in plan_tasks:
                t_name = t.get("name") if isinstance(t, dict) else str(t)
                ts = state_tasks.get(t_name, {}) if isinstance(state_tasks, dict) else {}
                if isinstance(ts, dict) and "status" in ts:
                    status = ts["status"]
                elif isinstance(t, dict) and "status" in t:
                    status = t["status"]
                else:
                    status = "pending"
                if not isinstance(status, str):
                    continue
                counts[status] += 1
                if status == "done":
                    t_branch = ts.get("branch", "") if isinstance(ts, dict) else ""
                    if not isinstance(t_branch, str):
                        t_branch = ""
                    if _is_unmerged_done(t_branch, plan_repo, base, orch_cache, merged_cache):
                        has_unmerged = True
                else:
                    has_unmerged = True
        elif isinstance(state_tasks, dict) and state_tasks:
            for _t_name, ts in state_tasks.items():
                if not isinstance(ts, dict):
                    continue
                status = ts.get("status", "pending")
                if not isinstance(status, str):
                    continue
                counts[status] += 1
                if status == "done":
                    t_branch = ts.get("branch", "")
                    if not isinstance(t_branch, str):
                        t_branch = ""
                    if _is_unmerged_done(t_branch, plan_repo, base, orch_cache, merged_cache):
                        has_unmerged = True
                else:
                    has_unmerged = True

        plans_info.append(
            {
                "slug": slug,
                "dir": plan_dir,
                "live_runner": runner_pid,
                "counts": dict(counts),
                "has_unmerged": has_unmerged,
            }
        )
    return plans_info


def _get_live_plan_runners(p: Paths, repo: Path, worktrees: set[Path]) -> list[dict]:
    plans_dir = p.state_dir / "plans"
    if not plans_dir.is_dir():
        return []

    live_runners = []
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
        if not plan_repo_str or not isinstance(plan_repo_str, str):
            continue
        try:
            plan_repo = Path(plan_repo_str).resolve()
        except Exception:
            continue
        if plan_repo != repo and plan_repo not in worktrees:
            continue

        runner_pid = live_runner(plan_dir, exclude_self=False)
        if runner_pid is not None:
            slug = plan_data.get("slug") or plan_dir.name
            live_runners.append(
                {
                    "slug": slug,
                    "dir": plan_dir,
                }
            )
    return live_runners


def session_start(payload: dict, p: Paths) -> dict | None:
    raw_cwd = payload.get("cwd")
    cwd = str(raw_cwd) if raw_cwd is not None else os.getcwd()
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
    raw_cwd = payload.get("cwd")
    cwd = str(raw_cwd) if raw_cwd is not None else os.getcwd()
    repo, worktrees = _repo_and_worktrees(cwd)

    alive_jobs = _get_alive_running_jobs(p, worktrees)
    live_runners = _get_live_plan_runners(p, repo, worktrees)

    raw_session_id = payload.get("session_id")
    if raw_session_id is None or raw_session_id == "":
        session_id_str = "default"
    else:
        session_id_str = str(raw_session_id)
    session_id = re.sub(r"[^A-Za-z0-9_-]", "_", session_id_str)
    counter_file = p.state_dir / "hooks" / f"stop-{session_id}"

    if not alive_jobs and not live_runners:
        if counter_file.exists():
            try:
                counter_file.unlink(missing_ok=True)
            except OSError:
                pass
        return None

    running_desc: list[str] = []
    wait_cmds: list[str] = []
    for job in alive_jobs:
        running_desc.append(f"job {job['id']} ({job.get('role', 'unknown')})")
        workdir = job.get("workdir", "")
        wait_cmds.append(f"agent-hub wait -d {workdir}")
    for pi in live_runners:
        running_desc.append(f"plan runner {pi['slug']}")
        plan_file = pi["dir"] / "plan.json"
        wait_cmds.append(f"agent-hub plan wait {plan_file}")

    what_is_running = ", ".join(running_desc)
    wait_str = " / ".join(wait_cmds)

    counter_file.parent.mkdir(parents=True, exist_ok=True)
    saved_count = 0
    saved_desc = None
    if counter_file.is_file():
        try:
            content = counter_file.read_text()
            if content.strip().startswith("{"):
                data = json.loads(content)
                saved_count = int(data.get("count", 0))
                saved_desc = data.get("description", "")
            else:
                lines_cf = content.splitlines()
                if lines_cf:
                    saved_count = int(lines_cf[0].strip())
                    saved_desc = lines_cf[1].strip() if len(lines_cf) > 1 else ""
        except (ValueError, OSError, json.JSONDecodeError):
            saved_count = 0
            saved_desc = None

    if saved_desc != what_is_running:
        count = 0
    else:
        count = saved_count

    count += 1
    try:
        counter_file.write_text(f"{count}\n{what_is_running}\n")
    except OSError:
        pass

    if count > 20:
        return {"systemMessage": f"agent-hub: stop guard gave up after 20 blocks; {what_is_running}"}

    return {
        "decision": "block",
        "reason": (f"{what_is_running} — wait with {wait_str} before ending the turn"),
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
