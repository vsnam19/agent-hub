"""The weekly skill run (D-091): mine, draft, test, review, release, report; commands go through `runner`."""

import fcntl
import importlib.resources
import json
import math
import re
import shutil
import string
import subprocess
import sys
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

from agenthub import skillmine

PLUGINS = ("learned-worker", "learned-director")
LEARNED_DIRS = tuple(f"plugins/{p}/" for p in PLUGINS)
MANIFESTS = (".claude-plugin/plugin.json", ".codex-plugin/plugin.json", "plugin.json")
MIN_FINDINGS = 3
FIX_ROUNDS = 2
PASS_SCORE = 0.8
GIT_TIMEOUT = 60
EVAL_TIMEOUT = 45 * 60
JOB_TIMEOUT = 2 * 60 * 60  # outer safety net; agent-hub enforces its own per-role timeout
GATE_TIMEOUT = 30 * 60
READY_TIMEOUT = 120
INSTALL_TIMEOUT = 10 * 60
CLAUDE_TIMEOUT = 120
DRAFT_ROLE = "implement"  # drafts and fix rounds run on haiku (D-093)
EXIT_STOPPED = 1
EXIT_LOCKED = 3
_VERSION = re.compile(r'("version"\s*:\s*")(\d+)\.(\d+)\.(\d+)(")')


class Proc(NamedTuple):
    rc: int
    stdout: str
    stderr: str


Runner = Callable[[list[str], Path | None, int], Proc]


def run_command(argv: list[str], cwd: Path | None, timeout: int) -> Proc:
    try:
        done = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return Proc(124, "", f"timed out after {timeout}s: {argv[0]}")
    except OSError as e:
        return Proc(127, "", str(e))
    return Proc(done.returncode, done.stdout, done.stderr)


@dataclass
class Config:
    state_dir: Path
    skills_repo: Path
    transcripts: Path
    max_cost_usd: float = 3.0
    notify: bool = field(default_factory=lambda: shutil.which("notify-send") is not None)


@dataclass
class Step:
    name: str
    status: str  # ok | stopped | would | skipped | failed | warning
    detail: str = ""


@dataclass
class Report:
    date: str
    window: str = ""
    steps: list[Step] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    previous_metrics: dict = field(default_factory=dict)
    candidates: list[str] = field(default_factory=list)
    drafted: list[str] = field(default_factory=list)
    evals: dict[str, str] = field(default_factory=dict)
    reviews: list[str] = field(default_factory=list)
    released: list[str] = field(default_factory=list)
    proposals: list[str] = field(default_factory=list)
    outcome: str = ""
    stop_reason: str = ""
    exit_code: int = 0

    def one_line(self) -> str:
        if self.stop_reason:
            return f"skills weekly {self.date}: stopped — {self.stop_reason}"
        return f"skills weekly {self.date}: {self.outcome}"

    def render(self) -> str:
        lines = [f"# Skills weekly {self.date}", "", f"Window: {self.window or '(unknown)'}", ""]
        outcome = f"stopped — {self.stop_reason}" if self.stop_reason else self.outcome or "(dry run)"
        lines.append(f"Outcome: {outcome}")
        lines += ["", "## Steps"]
        lines += [f"- {s.name}: {s.status}" + (f" — {s.detail}" if s.detail else "") for s in self.steps]
        lines += ["", "## Metrics (this window vs previous week)"]
        keys = [k for k in self.metrics if k != "findings_per_category"]
        lines += [f"- {k}: {self.metrics[k]} (previous: {self.previous_metrics.get(k, 'n/a')})" for k in keys]
        top = self.metrics.get("findings_per_category") or {}
        lines += [f"- finding category: {n}× {c}" for c, n in list(top.items())[:5]]
        for title, items in (
            ("Candidates", self.candidates),
            ("Drafted skills", self.drafted),
            ("Eval scores vs baseline", [f"{k}: {v}" for k, v in self.evals.items()]),
            ("Review verdicts", self.reviews),
            ("Released", self.released),
            ("MCP proposals", self.proposals),
        ):
            lines += ["", f"## {title}"] + ([f"- {i}" for i in items] or ["- (none)"])
        lines += ["", "## Stop reason", self.stop_reason or "(none)"]
        return "\n".join(lines) + "\n"


class Stop(Exception):
    def __init__(self, reason: str, code: int = EXIT_STOPPED):
        super().__init__(reason)
        self.reason = reason
        self.code = code


class Done(Exception):
    """A normal early finish: the window is consumed, nothing to release."""


def window_start(state_dir: Path, now: datetime) -> datetime:
    since = now - timedelta(days=28)
    try:
        last = datetime.fromisoformat((state_dir / "skills" / "last-run").read_text(errors="replace").strip())
    except (OSError, ValueError):
        return since
    return min(since, last if last.tzinfo else last.replace(tzinfo=timezone.utc))


def weekly(cfg: Config, now: datetime, runner: Runner, dry_run: bool) -> Report:
    date = now.strftime("%Y-%m-%d")
    report = Report(date=date)
    if dry_run:
        _Run(cfg, now, runner, report).plan()
        return report
    skills_dir = cfg.state_dir / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    with open(skills_dir / "weekly.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            report.stop_reason = "another weekly run holds the lock"
            report.exit_code = EXIT_LOCKED
            return report
        run = _Run(cfg, now, runner, report)
        finished = False
        crash = ""
        try:
            run.pipeline()
            finished = True
            report.outcome = report.outcome or "released"
        except Done:
            finished = True
        except Stop as stop:
            report.stop_reason = stop.reason
            report.exit_code = stop.code
            report.steps.append(Step("stop", "stopped", stop.reason))
        except Exception as error:  # an unexpected failure still writes the report and the notification
            report.outcome = f"crashed: {type(error).__name__}: {error}"
            report.exit_code = EXIT_STOPPED
            report.steps.append(Step("crash", "failed", "see crash.txt"))
            crash = traceback.format_exc()
        run.out.mkdir(parents=True, exist_ok=True)
        (run.out / "report.md").write_text(report.render(), encoding="utf-8")
        if crash:
            (run.out / "crash.txt").write_text(crash, encoding="utf-8")
        if finished:
            (skills_dir / "last-run").write_text(now.isoformat() + "\n", encoding="utf-8")
        run.notify(report.one_line())
    return report


class _Run:
    def __init__(self, cfg: Config, now: datetime, runner: Runner, report: Report):
        self.cfg, self.now, self.runner, self.report = cfg, now, runner, report
        self.date = report.date
        self.out = cfg.state_dir / "skills" / self.date
        self.repo = cfg.skills_repo
        self.branch = f"learned/{self.date}"
        self.wt = self.repo / ".worktrees" / f"learned-{self.date}"
        self.spent = 0.0
        self.bumped: set[str] = set()
        self._default_branch: str | None = None

    @property
    def default_branch(self) -> str:
        """The skills repo's default branch, resolved once and cached for the run.

        Mirrors the logic in ``guards.inspect``: prefer ``refs/remotes/origin/HEAD``,
        then probe ``main``, then ``master``.
        """
        if self._default_branch is not None:
            return self._default_branch
        origin = self.git("symbolic-ref", "refs/remotes/origin/HEAD", check=False)
        if origin.rc == 0 and origin.stdout.strip():
            self._default_branch = origin.stdout.strip().replace("refs/remotes/origin/", "")
            return self._default_branch
        if self.git("rev-parse", "--verify", "main", check=False).rc == 0:
            self._default_branch = "main"
        elif self.git("rev-parse", "--verify", "master", check=False).rc == 0:
            self._default_branch = "master"
        else:
            raise Stop("cannot determine the skills repo's default branch")
        return self._default_branch

    # -- helpers -----------------------------------------------------------------------------------------------

    def step(self, name: str, detail: str = "") -> None:
        self.report.steps.append(Step(name, "ok", detail))

    def git(self, *args: str, cwd: Path | None = None, check: bool = True) -> Proc:
        proc = self.runner(["git", *args], cwd or self.repo, GIT_TIMEOUT)
        if check and proc.rc != 0:
            raise Stop(f"git {args[0]} failed: {(proc.stderr or proc.stdout).strip()[:300]}")
        return proc

    def hub(self, *args: str, timeout: int, cwd: Path | None = None) -> Proc:
        return self.runner([sys.executable, "-m", "agenthub", *args], cwd, timeout)

    def notify(self, text: str) -> None:
        if self.cfg.notify:
            self.runner(["notify-send", "agent-hub skills weekly", text], None, 10)

    def job(self, label: str, proc: Proc) -> dict:
        try:
            result = json.loads(proc.stdout)
        except ValueError:
            raise Stop(f"{label}: no result from agent-hub (rc {proc.rc}): {proc.stderr.strip()[:200]}") from None
        if not isinstance(result, dict):
            raise Stop(f"{label}: unexpected result from agent-hub")
        return result

    def reply(self, result: dict) -> str:
        path = result.get("reply_path")
        try:
            return Path(path).read_text(encoding="utf-8", errors="replace") if path else ""
        except OSError:
            return ""

    # -- dry run -----------------------------------------------------------------------------------------------

    def plan(self) -> None:
        since = window_start(self.cfg.state_dir, self.now)
        extract = skillmine.mine(self.cfg.state_dir, self.cfg.transcripts, since, self.now)
        self.report.window = f"{since.date()} .. {self.date}"
        add = self.report.steps.append
        add(Step("lock", "would", f"{self.cfg.state_dir / 'skills' / 'weekly.lock'}"))
        found = f"{len(extract.findings)} findings, {extract.corrections_total} corrections since {since.date()}"
        add(Step("mine", "would", f"into {self.out}: {found}"))
        if len(extract.findings) < MIN_FINDINGS and not extract.corrections_total:
            add(Step("stop", "would", "nothing to learn; advance last-run"))
            return
        add(Step("worktree", "would", f"{self.wt} on new branch {self.branch} from {self.default_branch}"))
        add(Step("draft", "would", f"write the drafting brief; agent-hub run --role {DRAFT_ROLE}"))
        add(Step("version", "would", "bump the patch version of each changed plugin in 3 manifests; commit"))
        add(Step("eval", "would", f"claude plugin eval per skill, ablation with-without, max ${self.cfg.max_cost_usd}"))
        add(Step("review", "would", f"gate, review, up to {FIX_ROUNDS} fix rounds, then ready"))
        add(
            Step(
                "merge",
                "would",
                f"git merge --no-ff into {self.default_branch} (clean main checkout), hub install --apply",
            )
        )
        add(Step("report", "would", f"write {self.out / 'report.md'}; advance last-run; notify-send"))

    # -- the run -----------------------------------------------------------------------------------------------

    def pipeline(self) -> None:
        self.step("lock", "acquired")
        self.mine()
        self.worktree()
        self.draft()
        skills = self.commit_draft()
        self.evaluate(skills)
        self.review(skills)
        self.release()

    def mine(self) -> skillmine.Extract:
        since = window_start(self.cfg.state_dir, self.now)
        self.report.window = f"{since.date()} .. {self.date}"
        extract = skillmine.mine(self.cfg.state_dir, self.cfg.transcripts, since, self.now)
        skillmine.write(extract, self.out)
        self.report.metrics = skillmine.metrics(extract)
        self.report.previous_metrics = self.previous_metrics()
        self.report.candidates = [
            f"{g['count']}× {g['severity']} in {g['module']} ({g['branches']} branches)"
            for g in extract.groups
            if g["count"] >= 2
        ][:5]
        self.step("mine", f"{len(extract.findings)} findings, {extract.corrections_total} corrections")
        if len(extract.findings) < MIN_FINDINGS and not extract.corrections_total:
            self.report.outcome = "nothing to learn"
            self.step("stop", "nothing to learn")
            raise Done
        return extract

    def previous_metrics(self) -> dict:
        names = sorted(
            p.name for p in (self.cfg.state_dir / "skills").glob("*/metrics.json") if p.parent.name < self.date
        )
        for name in reversed(names):
            try:
                data = json.loads((self.cfg.state_dir / "skills" / name / "metrics.json").read_text(errors="replace"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict):
                return data
        return {}

    def worktree(self) -> None:
        if self.git("rev-parse", "--verify", "--quiet", f"refs/heads/{self.branch}", check=False).rc == 0:
            raise Stop(f"branch {self.branch} already exists")
        if self.wt.exists():
            raise Stop(f"worktree {self.wt} already exists")
        self.git("worktree", "add", "-b", self.branch, str(self.wt), self.default_branch)
        self.exclude_extract()
        learned = self.wt / ".agent-hub" / "learned"
        learned.mkdir(parents=True, exist_ok=True)
        for name in ("extract.json", "extract.md"):
            shutil.copy(self.out / name, learned / name)
        self.step("worktree", f"{self.wt} on {self.branch}")

    def exclude_extract(self) -> None:
        """Keep the extract copy out of commits whatever the skills repo's .gitignore says."""
        found = Path(self.git("rev-parse", "--git-path", "info/exclude", cwd=self.wt).stdout.strip())
        path = found if found.is_absolute() else self.wt / found
        path.parent.mkdir(parents=True, exist_ok=True)
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        for pattern in (".agent-hub/learned/", ".worktrees/"):
            if pattern not in text.splitlines():
                if text and not text.endswith("\n"):
                    text += "\n"
                text += pattern + "\n"
        path.write_text(text, encoding="utf-8")

    def existing_skills(self, plugin: str) -> str:
        found = sorted(p.name for p in (self.wt / "plugins" / plugin / "skills").glob("*") if p.is_dir())
        return ", ".join(found) or "(none)"

    def brief(self) -> str:
        template = importlib.resources.files("agenthub").joinpath("templates/learned-brief.md").read_text("utf-8")
        return string.Template(template).substitute(
            date=self.date,
            window=self.report.window,
            existing_worker=self.existing_skills("learned-worker"),
            existing_director=self.existing_skills("learned-director"),
            existing_base=self.existing_skills("worker"),
        )

    def draft(self) -> None:
        self.out.mkdir(parents=True, exist_ok=True)
        brief = self.out / "brief.md"
        brief.write_text(self.brief(), encoding="utf-8")
        proc = self.hub(
            "run", "--role", DRAFT_ROLE, "--brief", str(brief), "-d", str(self.wt), "--json",
            timeout=JOB_TIMEOUT,
        )  # fmt: skip
        result = self.job("draft", proc)
        if result.get("status") != "ok":
            raise Stop(f"draft job {result.get('status')}: {result.get('error') or ''}".strip())
        self.learned_only()
        self.report.proposals = [
            line.strip("-* ").strip() for line in self.reply(result).splitlines() if "MCP proposal:" in line
        ]
        changed = self.changed(self.default_branch)
        if not [p for p in changed if _plugin_of(p) in PLUGINS]:
            self.git("worktree", "remove", "--force", str(self.wt))
            self.git("branch", "-D", self.branch)
            self.report.outcome = "no skill drafted"
            self.step("draft", "no skill drafted; worktree and branch removed")
            raise Done
        self.step("draft", f"{len(changed)} files changed")

    def changed(self, ref: str) -> list[str]:
        diff = self.git("diff", "--name-only", ref, cwd=self.wt).stdout.split("\n")
        other = self.git("ls-files", "--others", "--exclude-standard", cwd=self.wt).stdout.split("\n")
        return sorted({p for p in diff + other if p.strip()})

    def learned_only(self) -> None:
        """Stop when a job changed any path outside the learned plugins, committed or not."""
        for path in self.changed(self.default_branch):
            if not path.startswith(LEARNED_DIRS):
                raise Stop(f"job changed {path} outside the learned plugins")

    def skills_of(self, paths: list[str]) -> list[tuple[str, str]]:
        found: set[tuple[str, str]] = set()
        for path in paths:
            parts = path.split("/")
            plugin = _plugin_of(path)
            if plugin not in PLUGINS or len(parts) < 4:
                continue
            if parts[2] == "skills":
                found.add((plugin, parts[3]))
            elif parts[2] == "evals":
                owner = self.owner(plugin, parts[3])
                if owner:
                    found.add((plugin, owner))
        return sorted(found)

    def owner(self, plugin: str, case: str) -> str | None:
        """The longest skill name that the eval case directory name starts with, so review-loop-x is not review's."""
        names = [p.name for p in (self.wt / "plugins" / plugin / "skills").glob("*") if p.is_dir()]
        owners = [n for n in names if case.startswith(n + "-")]
        return max(owners, key=len) if owners else None

    def eval_cases(self, plugin: str, skill: str) -> list[str]:
        evals = self.wt / "plugins" / plugin / "evals"
        cases = sorted(p.name for p in evals.glob("*") if p.is_dir() and self.owner(plugin, p.name) == skill)
        if not cases:
            raise Stop(f"no eval cases for {skill}")
        return cases

    def commit(self, message: str) -> None:
        self.git("add", "-A", "--", *(f"plugins/{p}" for p in PLUGINS), cwd=self.wt)
        # A fix job may have committed its own changes already; there is nothing to commit then.
        if self.git("diff", "--cached", "--quiet", cwd=self.wt, check=False).rc:
            self.git("commit", "-m", message, cwd=self.wt)
        left = self.git("status", "--porcelain", "-uall", cwd=self.wt).stdout.strip()
        if left:
            raise Stop(f"files outside the learned plugins changed: {left.splitlines()[0][3:]}")

    def commit_draft(self) -> list[tuple[str, str]]:
        changed = self.changed(self.default_branch)
        skills = self.skills_of(changed)
        if not skills:
            raise Stop("the draft changed no skill (only eval cases or other files)")
        self.bump_touched(changed)
        names = ", ".join(s for _, s in skills)
        self.commit(f"learned: {self.date} — {names}")
        self.report.drafted = [f"{plugin}/{skill}" for plugin, skill in skills]
        self.step("version", "; ".join(self.report.released))
        return skills

    def bump_touched(self, changed: list[str]) -> list[str]:
        """Bump each changed plugin not yet bumped in this run; return the new release entries."""
        new = []
        for plugin in sorted({p for p in map(_plugin_of, changed) if p in PLUGINS} - self.bumped):
            version = self.bump(plugin)
            self.bumped.add(plugin)
            self.report.released.append(f"{plugin} {version}")
            new.append(f"{plugin} {version}")
        return new

    def bump(self, plugin: str) -> str:
        texts = {}
        for manifest in MANIFESTS:
            file = self.wt / "plugins" / plugin / manifest
            try:
                texts[file] = file.read_text(encoding="utf-8")
            except OSError:
                raise Stop(f"{plugin}: manifest {manifest} is missing") from None
            if not _VERSION.search(texts[file]):
                raise Stop(f"{plugin}: manifest {manifest} has no x.y.z version")
        first = _VERSION.search(next(iter(texts.values())))
        version = f"{first.group(2)}.{first.group(3)}.{int(first.group(4)) + 1}"
        for file, text in texts.items():
            file.write_text(
                _VERSION.sub(lambda m: f"{m.group(1)}{version}{m.group(5)}", text, count=1), encoding="utf-8"
            )
        return version

    def evaluate(self, skills: list[tuple[str, str]]) -> None:
        for plugin, skill in skills:
            remaining = self.cfg.max_cost_usd - self.spent
            if remaining < 0.01:  # less than a cent left cannot pay for an eval, and is passed as 0.00
                raise Stop(f"eval budget ${self.cfg.max_cost_usd} spent before {skill}")
            target = self.out / f"eval-{skill}.json"
            cases = [arg for case in self.eval_cases(plugin, skill) for arg in ("--case", case)]
            proc = self.runner(
                [
                    "claude", "plugin", "eval", str(self.wt / "plugins" / plugin), *cases,
                    "--ablation", "with-without", "--max-cost-usd", f"{remaining:.2f}", "--json", str(target),
                    "--no-publish", "--trust-plugin",
                ],
                self.wt,
                EVAL_TIMEOUT,
            )  # fmt: skip
            if proc.rc != 0:
                raise Stop(f"eval of {skill} failed (rc {proc.rc}): {(proc.stderr or proc.stdout).strip()[:200]}")
            try:
                data = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self.spent += remaining  # no cost is readable, so the whole remaining budget counts as spent
                self.report.evals[skill] = "cost unknown"
                raise Stop(f"eval of {skill} wrote no readable result") from None
            try:
                with_mean, without_mean = _arm_means(skill, data, target)
            except Stop:
                self.spent += _cost_or(data, remaining)
                if _cost(data) is None:
                    self.report.evals[skill] = "cost unknown"
                raise
            cost = _cost(data)
            self.spent += _cost_or(data, remaining)
            unknown = " (cost unknown)" if cost is None else ""
            self.report.evals[skill] = f"with {with_mean:.2f} vs without {without_mean:.2f}{unknown}"
            if with_mean < PASS_SCORE or with_mean <= without_mean:
                raise Stop(
                    f"eval of {skill} below baseline: with {with_mean:.2f}, without {without_mean:.2f} "
                    f"(needs ≥ {PASS_SCORE} and above baseline)"
                )
        self.step("eval", "; ".join(f"{k}: {v}" for k, v in self.report.evals.items()))

    def review(self, skills: list[tuple[str, str]]) -> None:
        for round_no in range(FIX_ROUNDS + 1):
            gate = self.hub("gate", "-d", str(self.wt), timeout=GATE_TIMEOUT)
            if gate.rc != 0:
                raise Stop(f"gate failed: {(gate.stdout or gate.stderr).strip()[-300:]}")
            proc = self.hub(
                "run",
                "--role",
                "review",
                "-d",
                str(self.wt),
                "--base",
                self.default_branch,
                "--json",
                timeout=JOB_TIMEOUT,
            )
            result = self.job("review", proc)
            verdict = result.get("verdict")
            if result.get("status") != "ok" or verdict not in ("approve", "request-changes"):
                raise Stop(f"review job {result.get('status')}, verdict {verdict}")
            self.report.reviews.append(f"round {round_no}: {verdict}")
            if verdict == "approve":
                break
            if round_no == FIX_ROUNDS:
                raise Stop(f"review still request-changes after {FIX_ROUNDS} fix rounds")
            skills = self.fix(round_no + 1, self.reply(result)) or skills
        self.step("review", ", ".join(self.report.reviews))
        proc = self.hub("ready", "-d", str(self.wt), timeout=READY_TIMEOUT)
        if proc.rc != 0:
            raise Stop(f"ready failed: {(proc.stdout or proc.stderr).strip()[:300]}")
        self.step("ready", "passed")

    def fix(self, round_no: int, reply: str) -> list[tuple[str, str]]:
        findings = _section(reply, "findings") or reply.strip()
        brief = self.out / f"fix-{round_no}.md"
        brief.write_text(
            f"# Brief: round {round_no} fixes — learned {self.date}\n\n## Goal\n\n"
            "Fix every review finding below in the learned skills and their eval cases.\n\n"
            f"## Findings (verbatim)\n\n{findings}\n\n## Constraints\n\n"
            "- Change only files under `plugins/learned-worker` and `plugins/learned-director`.\n"
            "- Do not commit and do not change versions.\n",
            encoding="utf-8",
        )
        before = self.git("rev-parse", "HEAD", cwd=self.wt).stdout.strip()
        proc = self.hub(
            "run", "--role", DRAFT_ROLE, "--brief", str(brief), "-d", str(self.wt), "--json",
            timeout=JOB_TIMEOUT,
        )  # fmt: skip
        result = self.job(f"fix round {round_no}", proc)
        if result.get("status") != "ok":
            raise Stop(f"fix round {round_no} job {result.get('status')}")
        self.learned_only()
        changed = self.changed(before)
        if not changed:
            raise Stop(f"fix round {round_no} changed nothing")
        released = self.bump_touched(self.changed(self.default_branch))
        if released:
            self.step("version", "; ".join(released))
        self.commit(f"learned: {self.date} — review round {round_no} fixes")
        skills = self.skills_of(changed)
        self.evaluate(skills)
        return skills

    def release(self) -> None:
        head = self.git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        if head != self.default_branch:
            raise Stop(f"main checkout is on {head}, not {self.default_branch}")
        if self.git("status", "--porcelain").stdout.strip():
            raise Stop("main checkout is not clean")
        director = any(item.startswith("learned-director ") for item in self.report.released)
        marketplace = _marketplace_name(self.repo) if director else ""
        merged = self.git("merge", "--no-ff", "-m", f"Merge {self.branch}", self.branch, check=False)
        if merged.rc != 0:
            self.git("merge", "--abort", check=False)
            raise Stop(f"merge failed: {(merged.stderr or merged.stdout).strip()[:300]}")
        self.step("merge", f"{self.branch} into {self.default_branch}")
        # The merge is already on master, so an install failure must not stop the run before cleanup and last-run.
        failure = self.install(marketplace)
        self.cleanup()
        if failure:
            self.report.outcome = f"released, install failed: {failure}"
            self.report.exit_code = EXIT_STOPPED
        else:
            self.report.outcome = "released " + ", ".join(self.report.released)

    def cleanup(self) -> None:
        """Remove the worktree and branch after the merge; a failure is a warning, never a stop."""
        for args in (("worktree", "remove", "--force", str(self.wt)), ("branch", "-d", self.branch)):
            proc = self.git(*args, check=False)
            if proc.rc != 0:
                reason = (proc.stderr or proc.stdout).strip()[:200]
                self.report.steps.append(Step("cleanup", "warning", f"git {args[0]} {args[1]}: {reason}"))

    def install(self, marketplace: str) -> str:
        """Run the post-merge installs; return the failure reason, or "" when all of them succeed."""
        installed = self.hub("hub", "install", "--apply", timeout=INSTALL_TIMEOUT)
        if installed.rc != 0:
            reason = (installed.stdout or installed.stderr).strip()[-300:]
            self.report.steps.append(Step("install", "failed", "hub install --apply"))
            return f"hub install --apply: {reason}"
        self.step("install", "hub install --apply")
        if marketplace:
            return self.update_director_plugin(marketplace)
        return ""

    def update_director_plugin(self, marketplace: str) -> str:
        commands = (
            ("plugin", "marketplace", "update", marketplace),
            ("plugin", "update", f"learned-director@{marketplace}"),
        )
        for args in commands:
            proc = self.runner(["claude", *args], None, CLAUDE_TIMEOUT)
            if proc.rc != 0:
                reason = (proc.stderr or proc.stdout).strip()[:300]
                self.report.steps.append(Step("claude", "failed", " ".join(args)))
                return f"claude plugin update: {reason}"
        self.step("claude", f"claude plugin update learned-director@{marketplace}")
        return ""


def _marketplace_name(repo: Path) -> str:
    """The Claude marketplace name from the skills repo's .claude-plugin/marketplace.json; a missing name stops."""
    try:
        name = json.loads((repo / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")).get("name")
    except (OSError, ValueError, AttributeError):
        name = None
    if not isinstance(name, str) or not name:
        raise Stop("learned-director released but .claude-plugin/marketplace.json has no name")
    return name


def _cost(data: object) -> float | None:
    """The top-level costUsd of an eval result when numeric, finite and non-negative, else None."""
    cost = data.get("costUsd") if isinstance(data, dict) else None
    if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not math.isfinite(cost) or cost < 0:
        return None
    return float(cost)


def _cost_or(data: object, fallback: float) -> float:
    """The top-level costUsd of an eval result, or the fallback when it has none."""
    cost = _cost(data)
    return fallback if cost is None else cost


def _plugin_of(path: str) -> str:
    parts = path.split("/")
    return parts[1] if len(parts) > 1 and parts[0] == "plugins" else ""


def _section(text: str, title: str) -> str:
    lines: list[str] = []
    inside = False
    for line in text.splitlines():
        if line.startswith("## "):
            inside = line[3:].strip().lower() == title
        elif inside:
            lines.append(line)
    return "\n".join(lines).strip()


def _arm_means(skill: str, data: object, source: Path) -> tuple[float, float]:
    """Mean score of the plugin arm ('with') and the baseline arm ('without') over all runs of all cases."""
    scores: dict[str, list[float]] = {"with": [], "without": []}
    cases = data.get("cases") if isinstance(data, dict) else None
    if not isinstance(cases, list) or not cases:
        raise Stop(f"eval of {skill} ran no cases")
    for case in cases:
        arms = case.get("arms") if isinstance(case, dict) else None
        if not isinstance(arms, dict) or not set(scores) <= set(arms):
            raise Stop(f"eval of {skill}: unknown arm shape {sorted(arms) if isinstance(arms, dict) else arms!r}")
        for arm in scores:
            runs = arms[arm]
            if not isinstance(runs, list):
                raise Stop(f"eval of {skill}: malformed score in {source}")
            scores[arm] += [_score(skill, run, source) for run in runs]
    if not scores["with"] or not scores["without"]:
        raise Stop(f"eval of {skill}: an arm has no scored runs")
    return tuple(sum(v) / len(v) for v in scores.values())  # type: ignore[return-value]


def _score(skill: str, run: object, source: Path) -> float:
    score = run.get("score") if isinstance(run, dict) else None
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
        raise Stop(f"eval of {skill}: malformed score in {source}")
    return float(score)
