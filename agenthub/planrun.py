import fnmatch
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from agenthub import guards
from agenthub.config import ConfigError, load_pool, paths
from agenthub.guards import _git_argv
from agenthub.jobs import list_jobs
from agenthub.orchestrate import RunArgs, _passed_gate, implementers, readiness, run_gate_job, run_job
from agenthub.plan import Plan, Task
from agenthub.planstate import AttemptState, State, TaskState
from agenthub.pool import parse_link

PLAN_POLL_S = float(os.environ.get("AGENT_HUB_PLAN_POLL_S", "2.0"))
TRAIL_HEADER = "time\ttask\tphase\tdecision\treason\tevidence\tresult"
TERMINAL = ("done", "failed", "blocked", "scope-violation")


def _trail(
    plan: Plan, task: str, phase: str, decision: str, reason: str = "", evidence: str = "", result: str = ""
) -> None:
    """Append one row to the plan's decision trail (decisions.tsv). Only the runner writes it; the header goes first."""
    trail_file = plan.path.parent / "decisions.tsv"
    cells = [datetime.now(timezone.utc).astimezone().isoformat(), task, phase, decision, reason, evidence, result]
    fresh = not trail_file.is_file()
    with open(trail_file, "a") as f:
        if fresh:
            f.write(TRAIL_HEADER + "\n")
        f.write("\t".join(str(c).replace("\t", " ").replace("\r", " ").replace("\n", " ") for c in cells) + "\n")


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


def _reviewers_besides(role: str, implementer: str | set[str]) -> list[str]:
    # Compare agent:model only: a link's effort does not make it a different reviewer.
    excluded = {implementer} if isinstance(implementer, str) else set(implementer)
    return [link for link in load_pool().roles[role].chain if parse_link(link).quota_key not in excluded]


def _autocommit(worktree: str, slug: str, task_name: str, job_result: dict) -> str | None:
    """Commit work a gated task's agent left uncommitted, so dependent tasks start from it.

    Returns the new commit's sha, or None on a clean tree (nothing committed).
    """
    status = subprocess.run(
        _git_argv("status", "--porcelain"), cwd=worktree, capture_output=True, text=True, check=True
    ).stdout
    if not status.strip():
        return None
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
    return subprocess.run(
        _git_argv("rev-parse", "HEAD"), cwd=worktree, capture_output=True, text=True, check=True
    ).stdout.strip()


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
                        tstate.status = "cancelled"
                        tstate.reason = "cancelled"
                        _trail(plan, name, "final", "task cancelled", "cancelled", "", "cancelled")
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
                        if dep_status in ("failed", "blocked", "scope-violation", "skipped", "cancelled"):
                            tstate.status = "skipped"
                            tstate.reason = f"dependency {dep} {dep_status}"
                            _trail(plan, task.name, "final", "task skipped", tstate.reason, "", "skipped")
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
                        _trail(plan, task.name, "final", "task blocked", tstate.reason, "", "blocked")
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
                        _trail(plan, task.name, "final", "task blocked", tstate.reason, "", "blocked")
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
                if task.agent and (tstate.role or task.role) == task.role:
                    argv.extend(["--agent", task.agent])
                if task.timeout:
                    argv.extend(["--timeout", task.timeout])
                for g in task.gate:
                    argv.extend(["--gate", g])
                for s in task.skills:
                    argv.extend(["--skill", s])
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
                if attempt_num == 1:
                    _trail(
                        plan,
                        task.name,
                        "start",
                        "task started",
                        f"role {tstate.role or task.role}",
                        tstate.start_ref,
                        "running",
                    )
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
            if cancelled:
                continue  # the top of the loop marks running tasks cancelled; a child ending now is not an outcome

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
                    _trail(plan, name, "attempt", f"attempt {len(tstate.attempts)} ended", job_status, attempt.job)
                    role_before = tstate.role

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
                                _trail(plan, name, "scope", "scope violation", ", ".join(violations), attempt.job)
                            else:
                                sha = None
                                try:
                                    # A task resumed without a recorded worktree has no tree to commit.
                                    if tstate.worktree:
                                        sha = _autocommit(tstate.worktree, plan.slug, name, job_result)
                                except subprocess.CalledProcessError as exc:
                                    tstate.status = "failed"
                                    tstate.reason = f"autocommit failed: {(exc.stderr or '').strip()}"
                                else:
                                    if sha:
                                        _trail(plan, name, "commit", "autocommit", "agent left work uncommitted", sha)
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

                    if tstate.status == "pending":
                        if tstate.role != role_before:
                            _trail(
                                plan,
                                name,
                                "escalate",
                                f"escalated {tstate.escalated_from} -> {tstate.role}",
                                job_status,
                                f"reset to {tstate.start_ref}",
                                "pending",
                            )
                        else:
                            _trail(
                                plan,
                                name,
                                "retry",
                                "retry",
                                job_status,
                                f"{len(tstate.attempts)} attempts so far",
                                "pending",
                            )
                    elif tstate.status in TERMINAL:
                        _trail(
                            plan, name, "final", f"task {tstate.status}", tstate.reason, tstate.branch, tstate.status
                        )

                state.save()

    finally:
        signal.signal(signal.SIGTERM, orig_sigterm)
        signal.signal(signal.SIGINT, orig_sigint)


def _fix_brief_part(reason: str) -> str:
    """Return the text before the first line that starts with '## Previous attempt' (stripped),
    or '' if reason starts with it.
    """
    if not reason:
        return ""
    lines = reason.splitlines(keepends=True)
    prefix: list[str] = []
    for line in lines:
        if line.startswith("## Previous attempt"):
            break
        prefix.append(line)
    return "".join(prefix).strip()


def apply_retry(tstate: TaskState, task: Task, job_status: str, job_result: dict):
    # retries logic (3.4)
    attempts_so_far = len(tstate.attempts) - tstate.attempts_base

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
        brief = _fix_brief_part(tstate.reason) if tstate.fix_rounds > 0 else ""
        tstate.reason = f"{brief}\n\n{note}" if brief else note
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
    reset_ref = tstate.fix_start if tstate.fix_rounds > 0 and tstate.fix_start else tstate.start_ref
    subprocess.run(_git_argv("reset", "--hard", reset_ref), cwd=cwd, check=True, capture_output=True)
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
    note = f"## Previous attempt failed\n{failure}"
    brief = _fix_brief_part(tstate.reason) if tstate.fix_rounds > 0 else ""
    tstate.reason = f"{brief}\n\n{note}" if brief else note


def rejected_context(plan_dir: Path, task_name: str) -> str:
    """Read fixes/<task>-<round>-rejected.md in numeric round order and format for the review brief."""
    fixes_dir = Path(plan_dir) / "fixes"
    if not fixes_dir.is_dir():
        return ""

    prefix = f"{task_name}-"
    suffix = "-rejected.md"
    rounds: list[tuple[int, Path]] = []
    for p in fixes_dir.iterdir():
        if not p.is_file():
            continue
        name = p.name
        if name.startswith(prefix) and name.endswith(suffix):
            round_str = name[len(prefix) : -len(suffix)]
            if round_str.isdigit():
                rounds.append((int(round_str), p))

    if not rounds:
        return ""

    rounds.sort(key=lambda x: x[0])
    lines: list[str] = []
    for _, path in rounds:
        content = path.read_text(encoding="utf-8")
        for line in content.splitlines():
            if line.strip():
                lines.append(line.rstrip())

    if not lines:
        return ""

    heading = "## Already answered (rejected findings; repeat one only with new evidence)"
    return f"{heading}\n" + "\n".join(lines) + "\n"


def review_task(plan: Plan, task: Task, tstate: TaskState, now: Callable[[], datetime]) -> dict:
    """Gate the committed HEAD of a done task, then run the plan's review role on it, unless HEAD is already up to
    date (the work tree is clean, readiness passes, a passing gate job on HEAD covers every command of task.gate, and
    the newest ok review of HEAD is an independent approval). A failed gate skips the review. Returns
    {"gate", "review", "job", "reason"} (or up_to_date when HEAD is up to date), where gate is ok|failed and review is
    approve|request-changes|failed."""
    failed = {"gate": "failed", "review": "failed", "job": "-"}
    if not tstate.worktree:
        return {**failed, "reason": "no work tree recorded"}
    try:
        repo = guards.inspect(Path(tstate.worktree))
    except guards.GuardError as e:
        return {**failed, "reason": str(e)}

    if guards._run_git(repo.root, "status", "--porcelain"):
        return {**failed, "reason": "work tree is not clean; commit before review"}

    head = guards.head(repo)
    # The branch the task was created from: the plan base, or its dependency's branch.
    base = tstate.start_ref or plan.base
    jobs = list_jobs(paths(), 100000)
    impl = implementers(jobs, repo.root)
    mine = [j for j in jobs if j.get("workdir") and Path(j["workdir"]).resolve() == repo.root.resolve()]
    on_head = [j for j in mine if j.get("kind") == "review" and j.get("status") == "ok" and j.get("head") == head]

    def who(j: dict) -> str:
        return f"{j.get('agent')}:{j.get('model')}"

    task_gated = any(
        j.get("kind") == "gate"
        and _passed_gate(j, head)
        and set(task.gate) <= {g.get("cmd") for g in (j.get("gate") or [])}
        for j in mine
    )
    if (
        not readiness(jobs, repo.root, head)
        and task_gated
        and on_head
        and on_head[0]["verdict"] == "approve"
        and who(on_head[0]) not in impl
    ):
        return {"gate": "ok", "review": "approve", "job": on_head[0]["id"], "reason": "", "up_to_date": True}

    try:
        since = guards._run_git(repo.root, "merge-base", "HEAD", base)
    except subprocess.CalledProcessError:
        return {**failed, "reason": f"no merge base of HEAD and {base}"}

    try:
        gate = run_gate_job(paths(), repo, task.gate, base, since, now)
    except ConfigError as e:
        return {**failed, "reason": str(e)}
    if gate["status"] != "ok":
        res = {**failed, "gate": "failed", "job": gate["id"], "reason": f"gate {gate['status']}"}
        if gate["status"] == "cancelled":
            res["cancelled"] = True
        return res

    pool = load_pool()
    if impl and not _reviewers_besides("review", impl):
        names = ", ".join(sorted(impl))
        return {
            "gate": "ok",
            "review": "failed",
            "job": gate["id"],
            "reason": f"no reviewer different from {names}",
        }

    review = run_job(
        RunArgs(
            role="review",
            kind="review",
            brief=rejected_context(plan.path.parent, task.name),
            workdir=repo.root,
            links=[parse_link(x) for x in pool.roles["review"].chain],
            timeout=pool.timeouts["review"],
            base=base,
            jev=plan.jev,
            exclude=sorted(impl),
        ),
        paths(),
        pool,
        now,
    )
    verdict = review.get("verdict") if review["status"] == "ok" else None
    if verdict not in ("approve", "request-changes"):
        verdict = "failed"
    reason = review.get("error") or review.get("reason", "")
    res = {"gate": "ok", "review": verdict, "job": review["id"], "reason": reason}
    if review.get("status") == "cancelled":
        res["cancelled"] = True
    return res


def merge_order(plan: Plan) -> list[Task]:
    """The plan's tasks with every dependency before its dependents; ties keep the plan's order."""
    by_name = {t.name: t for t in plan.tasks}
    ordered: list[Task] = []
    seen: set[str] = set()

    def visit(name: str) -> None:
        if name in seen:
            return
        seen.add(name)
        for dep in by_name[name].depends_on:
            visit(dep)
        ordered.append(by_name[name])

    for t in plan.tasks:
        visit(t.name)
    return ordered


def merge_target_problem(repo_dir: Path, target: str) -> str | None:
    """Why the main checkout cannot take a merge into `target`, or None when it is clean and on `target`."""
    repo = guards.inspect(repo_dir)
    if repo.branch != target:
        return f"main checkout is on {repo.branch or 'a detached HEAD'}, not {target}"
    if guards._run_git(repo.root, "status", "--porcelain"):
        return "main checkout is not clean"
    return None


def _merge_reviewer(jobs: list[dict], workdir: Path, head: str) -> str:
    """agent:model of the newest approving review of `head` by a reviewer other than the implementers; "?" if none."""

    def who(j: dict) -> str:
        return f"{j.get('agent')}:{j.get('model')}"

    mine = [j for j in jobs if j.get("workdir") and Path(j["workdir"]).resolve() == workdir.resolve()]
    impl = implementers(jobs, workdir)
    for j in mine:
        if (
            j.get("kind") == "review"
            and j.get("status") == "ok"
            and j.get("verdict") == "approve"
            and j.get("head") == head
            and who(j) not in impl
        ):
            return who(j)
    return "?"


def _remove_merged(plan: Plan, worktree: Path, branch: str) -> str | None:
    """Remove a merged branch's work tree (when it still exists) and the branch; the failure detail, or None."""
    argvs = [_git_argv("branch", "-d", branch)]
    if worktree.is_dir():
        argvs.insert(0, _git_argv("worktree", "remove", str(worktree)))
    for argv in argvs:
        res = subprocess.run(argv, cwd=plan.repo, capture_output=True, text=True)
        if res.returncode != 0:
            return (res.stderr or res.stdout).strip()
    return None


def merge_task(plan: Plan, task: Task, tstate: TaskState, target: str, jobs: list[dict]) -> dict:
    """Merge one done task's branch into `target` in the plan's main checkout if `agent-hub ready` accepts its HEAD,
    then remove its work tree and branch. Returns {"status", "problems", "reason"}, where status is merged,
    already-merged, not-ready or failed."""
    if not tstate.worktree or not tstate.branch:
        return {"status": "failed", "problems": [], "reason": "no work tree or branch recorded"}
    branch = tstate.branch
    worktree = Path(tstate.worktree)

    exists = subprocess.run(
        _git_argv("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"), cwd=plan.repo, capture_output=True
    )
    if exists.returncode != 0:
        return {"status": "already-merged", "problems": [], "reason": ""}
    ancestor = subprocess.run(
        _git_argv("merge-base", "--is-ancestor", branch, target), cwd=plan.repo, capture_output=True
    )
    if ancestor.returncode == 0:
        # Its commits are already in target (an earlier merge brought them): only the leftovers go
        cleanup = _remove_merged(plan, worktree, branch)
        if cleanup:
            return {"status": "failed", "problems": [], "reason": cleanup}
        return {"status": "already-merged", "problems": [], "reason": ""}

    if not worktree.is_dir():
        return {"status": "not-ready", "problems": [f"work tree missing: {worktree}"], "reason": ""}
    try:
        repo = guards.inspect(worktree)
    except guards.GuardError as e:
        return {"status": "not-ready", "problems": [str(e)], "reason": ""}

    head = guards.head(repo)
    problems = readiness(jobs, repo.root, head)
    if not problems and guards._run_git(repo.root, "status", "--porcelain"):
        problems = ["work tree is not clean; only the committed HEAD is merged"]
    if problems:
        return {"status": "not-ready", "problems": problems, "reason": ""}

    reviewer = _merge_reviewer(jobs, repo.root, head)
    message = f"Merge {branch} — merged through agent-hub ready (approve, {reviewer})"
    merge = subprocess.run(
        _git_argv("-c", "commit.gpgsign=false", "merge", "--no-ff", "-m", message, branch),
        cwd=plan.repo,
        capture_output=True,
        text=True,
    )
    if merge.returncode != 0:
        subprocess.run(_git_argv("merge", "--abort"), cwd=plan.repo, capture_output=True)
        detail = (merge.stderr or merge.stdout).strip()
        return {"status": "failed", "problems": [], "reason": f"merge of {branch} failed: {detail}"}

    cleanup = _remove_merged(plan, repo.root, branch)
    if cleanup:
        return {"status": "failed", "problems": [], "reason": f"merged {branch} but cleanup failed: {cleanup}"}

    sha = guards._run_git(plan.repo, "rev-parse", "--short", "HEAD")
    return {"status": "merged", "problems": [], "reason": "", "sha": sha}
