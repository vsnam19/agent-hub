import fnmatch
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from agenthub.config import load_pool, paths
from agenthub.guards import _git_argv
from agenthub.plan import Plan, Task
from agenthub.planstate import AttemptState, State, TaskState

PLAN_POLL_S = float(os.environ.get("AGENT_HUB_PLAN_POLL_S", "2.0"))


def _kind(task: Task) -> str:
    """Keep programmatically constructed legacy tasks working in tests and integrations."""
    return task.kind or task.role


def _implementer(vstate: TaskState) -> str | None:
    """The agent:model that produced the verified task's final successful attempt, from its job record."""
    ok = [a for a in vstate.attempts if a.status == "ok" and a.job]
    if not ok:
        return None
    result_path = paths().jobs_dir / ok[-1].job / "result.json"
    if not result_path.is_file():
        return None
    record = json.loads(result_path.read_text())
    if not record.get("agent") or not record.get("model"):
        return None
    return f"{record['agent']}:{record['model']}"


def _reviewers_besides(role: str, implementer: str) -> list[str]:
    return [link for link in load_pool().roles[role].chain if link != implementer]


def _autocommit(worktree: str, slug: str, task_name: str, job_result: dict) -> None:
    """Commit work a gated task's agent left uncommitted, so dependent tasks start from it. No-op on a clean tree."""
    status = subprocess.run(
        _git_argv("status", "--porcelain"), cwd=worktree, capture_output=True, text=True, check=True
    ).stdout
    if not status.strip():
        return
    subprocess.run(_git_argv("add", "-A"), cwd=worktree, capture_output=True, text=True, check=True)
    # Fall back to a placeholder identity per key only when the repository configures none.
    identity = []
    for key, fallback in (("user.name", "agent-hub"), ("user.email", "agent-hub@localhost")):
        if subprocess.run(_git_argv("config", "--get", key), cwd=worktree, capture_output=True).returncode != 0:
            identity += ["-c", f"{key}={fallback}"]
    agent_label = f"{job_result.get('agent', '?')}:{job_result.get('model', '?')}"
    message = f"orch: {slug}/{task_name} — work left uncommitted by {agent_label} (gate passed)"
    subprocess.run(
        _git_argv(*identity, "-c", "commit.gpgsign=false", "commit", "-m", message),
        cwd=worktree,
        capture_output=True,
        text=True,
        check=True,
    )


def compose_brief(plan: Plan, task: Task, state: State, attempt_note: str) -> str:
    parts = []
    parts.append("## Goal (whole plan)\n\n" + plan.goal.strip())

    if task.brief and task.brief.is_file():
        parts.append(task.brief.read_text().strip())

    for dep_name in task.depends_on:
        dep_state = state.task(dep_name)
        if dep_state.status == "done" and dep_state.handoff:
            handoff_path = plan.path.parent / dep_state.handoff
            if handoff_path.is_file():
                parts.append(f"## Upstream handoff: {dep_name}\n\n" + handoff_path.read_text().strip())

    if attempt_note:
        parts.append(attempt_note.strip())

    return "\n\n".join(parts)


def run_plan(
    plan: Plan, state: State, spawn: Callable[[list[str], Path], subprocess.Popen], max_parallel: int | None = None
) -> int:
    if max_parallel is None:
        max_parallel = plan.max_parallel

    running_children: dict[str, subprocess.Popen] = {}

    cancelled = False

    def handle_sigterm(signum, frame):
        nonlocal cancelled
        cancelled = True
        for p in running_children.values():
            p.terminate()

    orig_sigterm = signal.signal(signal.SIGTERM, handle_sigterm)
    orig_sigint = signal.signal(signal.SIGINT, handle_sigterm)

    try:
        while True:
            if cancelled:
                for name, tstate in state.tasks.items():
                    if tstate.status == "running":
                        tstate.status = "failed"
                        tstate.reason = "cancelled"
                        if name in running_children:
                            try:
                                running_children[name].wait(timeout=1)
                            except subprocess.TimeoutExpired:
                                running_children[name].kill()
                state.save()
                return 130

            changed = False

            # 1. Mark skipped
            for task in plan.tasks:
                tstate = state.task(task.name)
                if tstate.status == "pending":
                    for dep in task.depends_on:
                        dep_status = state.task(dep).status
                        if dep_status in ("failed", "blocked", "scope-violation", "skipped"):
                            tstate.status = "skipped"
                            tstate.reason = f"dependency {dep} {dep_status}"
                            changed = True
                            break

            # 2. Start ready tasks
            pending_tasks = [t for t in plan.tasks if state.task(t.name).status == "pending"]
            running_tasks = [t for t in plan.tasks if state.task(t.name).status == "running"]

            ready_tasks = []
            for t in pending_tasks:
                all_done = True
                for dep in t.depends_on:
                    if state.task(dep).status != "done":
                        all_done = False
                        break
                if all_done:
                    ready_tasks.append(t)

            for task in ready_tasks:
                if len(running_tasks) >= max_parallel:
                    break

                tstate = state.task(task.name)
                # 3. Start a task
                attempt_num = len(tstate.attempts) + 1
                attempt_note = tstate.reason

                exclude = None
                if _kind(task) == "implement":
                    worktree_path = plan.repo.parent / f"{plan.repo.name}/.worktrees/{plan.slug}-{task.name}"
                    worktree_abs = worktree_path.resolve()
                    branch = f"orch/{plan.slug}/{task.name}"

                    if not worktree_abs.exists():
                        # Determine start_ref
                        implement_deps = [
                            d
                            for d in task.depends_on
                            if _kind(next(t for t in plan.tasks if t.name == d)) == "implement"
                        ]

                        start_ref = plan.base
                        if len(implement_deps) == 1:
                            start_ref = state.task(implement_deps[0]).branch

                        cmd_argv = _git_argv("worktree", "add", "-b", branch, str(worktree_abs), start_ref)
                        subprocess.run(cmd_argv, cwd=plan.repo, check=True)
                        tstate.worktree = str(worktree_abs)
                        tstate.branch = branch
                        tstate.start_ref = start_ref

                        if len(implement_deps) > 1:
                            for dep in implement_deps:
                                dep_branch = state.task(dep).branch
                                res = subprocess.run(
                                    _git_argv(
                                        "-c",
                                        "core.hooksPath=/dev/null",
                                        "-c",
                                        "commit.gpgsign=false",
                                        "merge",
                                        "--no-ff",
                                        "--no-edit",
                                        dep_branch,
                                    ),
                                    cwd=worktree_abs,
                                    capture_output=True,
                                )
                                if res.returncode != 0:
                                    subprocess.run(_git_argv("merge", "--abort"), cwd=worktree_abs)
                                    tstate.status = "blocked"
                                    tstate.reason = f"merge conflict with {dep_branch}"
                                    changed = True
                                    break
                    else:
                        tstate.worktree = str(worktree_abs)
                        tstate.branch = branch
                        # reuse existing

                    if tstate.status == "blocked":
                        continue

                elif _kind(task) == "review":
                    verifies = task.verifies
                    vstate = state.task(verifies)
                    tstate.worktree = vstate.worktree
                    tstate.branch = vstate.branch
                    tstate.start_ref = vstate.start_ref
                    exclude = _implementer(vstate)
                    if exclude and not _reviewers_besides(task.role, exclude):
                        tstate.status = "blocked"
                        tstate.reason = f"no reviewer different from {exclude}"
                        changed = True
                        continue

                brief_text = compose_brief(plan, task, state, attempt_note)

                briefs_dir = plan.path.parent / "briefs-sent"
                briefs_dir.mkdir(exist_ok=True)
                brief_file = briefs_dir / f"{task.name}-{attempt_num}.md"
                brief_file.write_text(brief_text)

                logs_dir = plan.path.parent / "logs"
                logs_dir.mkdir(exist_ok=True)
                log_file = logs_dir / f"{task.name}-{attempt_num}.json"

                argv = [
                    sys.executable,
                    "-m",
                    "agenthub",
                    "run",
                    "--role",
                    tstate.role or task.role,
                    "--brief",
                    str(brief_file),
                    "-d",
                    tstate.worktree,
                    "--json",
                ]
                if task.agent:
                    argv.extend(["--agent", task.agent])
                if task.timeout:
                    argv.extend(["--timeout", task.timeout])
                for g in task.gate:
                    argv.extend(["--gate", g])
                if _kind(task) == "review":
                    argv.extend(["--base", tstate.start_ref])
                if exclude:
                    argv.extend(["--exclude", exclude])
                if plan.jev:
                    argv.append("--jev")

                popen = spawn(argv, log_file)
                running_children[task.name] = popen

                tstate.status = "running"
                tstate.attempts.append(AttemptState(job="", status="", exit_code=-1))
                running_tasks.append(task)
                changed = True

            if changed:
                state.save()

            # 6. Stop when nothing is pending or running
            if not any(state.task(t.name).status in ("pending", "running") for t in plan.tasks):
                if all(state.task(t.name).status == "done" for t in plan.tasks):
                    return 0
                return 1

            # 4. Poll
            time.sleep(PLAN_POLL_S)

            newly_finished = []
            for name, popen in list(running_children.items()):
                if popen.poll() is not None:
                    newly_finished.append(name)

            for task in plan.tasks:
                tstate = state.task(task.name)
                if tstate.status == "running" and task.name not in running_children and task.name not in newly_finished:
                    if tstate.attempts and tstate.attempts[-1].status != "":
                        newly_finished.append(task.name)

            if newly_finished:
                for name in newly_finished:
                    popen = running_children.pop(name, None)
                    task = next(t for t in plan.tasks if t.name == name)
                    tstate = state.task(name)
                    attempt = tstate.attempts[-1]

                    log_file = plan.path.parent / "logs" / f"{name}-{len(tstate.attempts)}.json"
                    job_result = {}
                    if log_file.is_file():
                        try:
                            job_result = json.loads(log_file.read_text())
                        except Exception:
                            pass
                    else:
                        from agenthub.config import paths

                        p = paths()
                        if attempt.job:
                            job_result_path = p.jobs_dir / attempt.job / "result.json"
                            if job_result_path.is_file():
                                try:
                                    job_result = json.loads(job_result_path.read_text())
                                except Exception:
                                    pass

                    attempt.job = job_result.get("id", "")
                    job_status = job_result.get("status", "failed")
                    attempt.status = job_status
                    attempt.exit_code = job_result.get("exit_code", popen.returncode if popen else -1)
                    attempt.gate_touched = job_result.get("gate_touched", [])

                    reply_text = ""
                    reply_path = job_result.get("reply_path")
                    if reply_path and Path(reply_path).is_file():
                        reply_text = Path(reply_path).read_text()

                    handoffs_dir = plan.path.parent / "handoffs"
                    handoffs_dir.mkdir(exist_ok=True)
                    handoff_file = handoffs_dir / f"{name}.md"

                    attempt_len = len(tstate.attempts)
                    header = f"<!-- task {name} attempt {attempt_len} job {attempt.job} status {job_status} -->\n"
                    handoff_file.write_text(header + reply_text)
                    tstate.handoff = f"handoffs/{name}.md"

                    # 5. Outcome
                    if _kind(task) == "implement":
                        if job_status == "ok":
                            paths_allowed = task.paths_allowed
                            changed_files = job_result.get("changed_files", [])
                            violations = []
                            if paths_allowed:
                                for f in changed_files:
                                    if not any(
                                        fnmatch.fnmatch(f, p) or fnmatch.fnmatch(f, p.replace("**", "*"))
                                        for p in paths_allowed
                                    ):
                                        violations.append(f)
                            if violations:
                                tstate.status = "scope-violation"
                                tstate.reason = "files outside scope: " + ", ".join(violations)
                            else:
                                try:
                                    # A task resumed without a recorded worktree has no tree to commit.
                                    if tstate.worktree:
                                        _autocommit(tstate.worktree, plan.slug, name, job_result)
                                except subprocess.CalledProcessError as exc:
                                    tstate.status = "failed"
                                    tstate.reason = f"autocommit failed: {(exc.stderr or '').strip()}"
                                else:
                                    tstate.status = "done"
                                    tstate.reason = ""
                        else:
                            apply_retry(tstate, task, job_status, job_result)
                            if tstate.status == "failed" and job_status in ESCALATING:
                                escalate(tstate, task, job_result, job_status)
                    else:  # review
                        if job_status == "ok":
                            tstate.status = "done"
                            tstate.reason = ""
                        elif job_status == "modified-tree":
                            tstate.status = "failed"
                        else:
                            apply_retry(tstate, task, job_status, job_result)

                state.save()

    finally:
        signal.signal(signal.SIGTERM, orig_sigterm)
        signal.signal(signal.SIGINT, orig_sigint)


def apply_retry(tstate: TaskState, task: Task, job_status: str, job_result: dict):
    # retries logic (3.4)
    attempts_so_far = len(tstate.attempts)

    can_retry = False
    note = ""

    if job_status == "gate-failed":
        can_retry = True
        gate_res = job_result.get("gate", [])
        failing_cmd = ""
        tail = ""
        for g in gate_res:
            if g.get("rc") != 0:
                failing_cmd = g.get("cmd", "")
                tail = g.get("tail", "")
                break
        note = f"## Previous attempt failed the gate\n{failing_cmd}\n{tail}"
    elif job_status == "timeout":
        can_retry = True
        note = "## Previous attempt timed out — work in smaller steps and commit as you go"
    elif job_status in ("failed", "empty", "interrupted"):
        # yes (once, even if max_attempts is larger)
        if attempts_so_far == 1:
            can_retry = True
        else:
            can_retry = False
        note = f"## Previous attempt ended with {job_status}\n"
        if tstate.handoff and Path(tstate.handoff).is_file():
            note += Path(tstate.handoff).read_text()
    elif job_status in ("exhausted", "refused", "modified-tree", "cancelled"):
        can_retry = False

    if tstate.escalated_from:
        can_retry = False  # an escalated role gets one run

    if can_retry and attempts_so_far < task.max_attempts:
        tstate.status = "pending"
        tstate.reason = note
    else:
        if job_status == "exhausted":
            tstate.status = "blocked"
        else:
            tstate.status = "failed"


ESCALATING = ("gate-failed", "failed", "timeout", "empty", "interrupted")


def escalate(tstate: TaskState, task: Task, job_result: dict, job_status: str) -> None:
    """Move a failed implement task one tier up: wipe its work tree and requeue it at the role's `escalate` role."""
    current = tstate.role or task.role
    target = load_pool().roles[current].escalate
    if not target:
        return
    visited = tstate.visited_roles or [current]
    if target in visited:
        return  # cycle guard

    cwd = Path(tstate.worktree)
    subprocess.run(_git_argv("reset", "--hard", tstate.start_ref), cwd=cwd, check=True, capture_output=True)
    subprocess.run(_git_argv("clean", "-fdx"), cwd=cwd, check=True, capture_output=True)

    failure = ""
    for g in job_result.get("gate", []):
        if g.get("rc") != 0:
            failure = f"{g.get('cmd', '')}\n{g.get('tail', '')}"
            break
    if not failure:
        failure = job_result.get("reason") or job_status

    tstate.visited_roles = [*visited, target]
    tstate.escalated_from = current
    tstate.role = target
    tstate.status = "pending"
    tstate.reason = f"## Previous attempt failed\n{failure}"
