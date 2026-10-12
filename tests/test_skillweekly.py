import fcntl
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agenthub import skillweekly
from agenthub.cli import main
from agenthub.skillweekly import Config, Proc
from tests.gitrepo import make_repo
from tests.test_skillmine import make_job, review_reply

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
DATE = "2026-10-12"
BRANCH = f"learned/{DATE}"
PLUGINS = ("learned-worker", "learned-director")
MANIFESTS = (".claude-plugin/plugin.json", ".codex-plugin/plugin.json", "plugin.json")


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)
    return done.stdout.strip()


def make_skills_repo(path: Path) -> Path:
    make_repo(path, branch=None)
    (path / ".gitignore").write_text(".worktrees/\n.agent-hub/*\n")
    for plugin in PLUGINS:
        for manifest in MANIFESTS:
            file = path / "plugins" / plugin / manifest
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(json.dumps({"name": plugin, "version": "0.1.0"}, indent=2) + "\n")
    write_marketplace(path, "agent-plugins")
    git(path, "add", "-A")
    git(path, "commit", "-m", "plugins")
    return path


def write_marketplace(path: Path, name: str | None) -> None:
    """Write the skills repo's .claude-plugin/marketplace.json with this name; None removes the file."""
    file = path / ".claude-plugin" / "marketplace.json"
    if name is None:
        file.unlink(missing_ok=True)
    else:
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(json.dumps({"name": name, "plugins": []}, indent=2) + "\n")


def make_state(state: Path, findings: int = 3) -> None:
    lines = [f"- high — agenthub/cli.py:{n} — Claim number {n} is wrong" for n in range(findings)]
    make_job(state, "20261010-100000-review-aaaa", reply=review_reply(*lines))


def eval_json(with_score: float, without_score: float, cost: float = 0.1) -> dict:
    arms = {"with": [{"score": with_score}], "without": [{"score": without_score}]}
    return {"costUsd": cost, "cases": [{"name": "case", "arms": arms}]}


class Fake:
    """Stands in for every external command; git runs for real in the temp repo."""

    def __init__(self, tmp: Path, **opts):
        self.tmp = tmp
        self.calls: list[list[str]] = []
        self.verdicts = list(opts.get("verdicts", ["approve"]))
        self.evals = list(opts.get("evals", [eval_json(1.0, 0.0)]))
        self.draft = opts.get("draft", True)
        self.director = opts.get("director", False)
        self.director_fix = opts.get("director_fix", False)
        self.plugin_rc = opts.get("plugin_rc", 0)
        self.install_rc = opts.get("install_rc", 0)
        self.ready_rc = opts.get("ready_rc", 0)
        self.gate_rc = opts.get("gate_rc", 0)
        self.crash = opts.get("crash", False)
        self.cleanup_rc = opts.get("cleanup_rc", 0)
        self.drafts = 0
        self.reply = "## Notes\n- MCP proposal: foo-query — a lookup text cannot carry\n"

    def __call__(self, argv, cwd, timeout):
        self.calls.append(list(argv))
        assert timeout > 0
        if argv[0] == "git" and self.cleanup_rc and argv[1:3] in (["worktree", "remove"], ["branch", "-d"]):
            return Proc(self.cleanup_rc, "", "cleanup refused\n")
        if argv[0] == "git":
            done = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
            return Proc(done.returncode, done.stdout, done.stderr)
        if argv[0] == "notify-send":
            return Proc(0, "", "")
        if argv[0] == "claude" and argv[2] in ("marketplace", "update"):
            return Proc(self.plugin_rc, "", "update refused\n" if self.plugin_rc else "")
        if argv[0] == "claude":
            return self.eval(argv)
        words = argv[3:]
        if words[0] == "run" and "implement" in words:
            return self.implement(words)
        if words[0] == "run":
            return self.review()
        if words[:2] == ["hub", "install"]:
            return Proc(self.install_rc, "", "install refused\n" if self.install_rc else "")
        if words[0] == "gate":
            if self.crash:
                raise RuntimeError("gate exploded")
            return Proc(self.gate_rc, "gate\n", "")
        if words[0] == "ready":
            return Proc(self.ready_rc, "not ready: x\n" if self.ready_rc else "ready\n", "")
        return Proc(0, "", "")

    def job(self, name: str, **fields) -> Proc:
        reply = self.tmp / f"{name}-{len(self.calls)}.md"
        reply.write_text(fields.pop("text", self.reply))
        return Proc(0, json.dumps({"id": name, "reply_path": str(reply), **fields}), "")

    def implement(self, words) -> Proc:
        self.drafts += 1
        workdir = Path(words[words.index("-d") + 1])
        if self.draft:
            skill = workdir / "plugins/learned-worker/skills/foo/SKILL.md"
            skill.parent.mkdir(parents=True, exist_ok=True)
            skill.write_text(f"---\nname: foo\n---\ndraft {self.drafts}\n")
            case = workdir / "plugins/learned-worker/evals/foo-pos/prompt.md"
            case.parent.mkdir(parents=True, exist_ok=True)
            case.write_text("prompt\n")
        if self.director or (self.director_fix and self.drafts > 1):
            skill = workdir / "plugins/learned-director/skills/bar/SKILL.md"
            skill.parent.mkdir(parents=True, exist_ok=True)
            skill.write_text("---\nname: bar\n---\ndirector draft\n")
        return self.job("draft", status="ok")

    def review(self) -> Proc:
        verdict = self.verdicts.pop(0) if len(self.verdicts) > 1 else self.verdicts[0]
        text = f"## Findings\n- high — plugins/x.md — Needs a table\n\n## Verdict\n{verdict}\n"
        return self.job("review", status="ok", verdict=verdict, text=text)

    def eval(self, argv) -> Proc:
        out = Path(argv[argv.index("--json") + 1])
        data = self.evals.pop(0) if len(self.evals) > 1 else self.evals[0]
        out.write_text(json.dumps(data))
        return Proc(0, "", "")

    def words(self, *needle: str) -> list[list[str]]:
        return [c for c in self.calls if all(n in c for n in needle)]


class Env:
    def __init__(self, tmp_path: Path, findings: int = 3, **opts):
        self.state = tmp_path / "state"
        self.repo = make_skills_repo(tmp_path / "skills")
        make_state(self.state, findings)
        (tmp_path / "fake").mkdir()
        self.fake = Fake(tmp_path / "fake", **opts)
        self.cfg = Config(
            state_dir=self.state,
            skills_repo=self.repo,
            transcripts=tmp_path / "claude",
            max_cost_usd=3.0,
            notify=False,
        )
        self.out = self.state / "skills" / DATE

    def run(self, dry_run: bool = False) -> skillweekly.Report:
        return skillweekly.weekly(self.cfg, NOW, self.fake, dry_run)

    @property
    def last_run(self) -> Path:
        return self.state / "skills" / "last-run"

    @property
    def worktree(self) -> Path:
        return self.repo / ".worktrees" / f"learned-{DATE}"

    def branches(self) -> list[str]:
        return git(self.repo, "branch", "--format=%(refname:short)").split()


def test_lock_held_exits_3(tmp_path):
    env = Env(tmp_path)
    (env.state / "skills").mkdir(parents=True, exist_ok=True)
    with open(env.state / "skills" / "weekly.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        report = env.run()
    assert report.exit_code == 3
    assert "another weekly run" in report.stop_reason
    assert env.fake.calls == []
    assert not env.last_run.exists()


def test_nothing_to_learn_advances_last_run(tmp_path):
    env = Env(tmp_path, findings=2)
    report = env.run()
    assert report.exit_code == 0
    assert "nothing to learn" in (env.out / "report.md").read_text()
    assert env.last_run.read_text().strip() == NOW.isoformat()
    assert env.fake.calls == []


def test_no_skill_drafted_cleans_up(tmp_path):
    env = Env(tmp_path, draft=False)
    report = env.run()
    assert report.exit_code == 0
    assert "no skill drafted" in (env.out / "report.md").read_text()
    assert not env.worktree.exists()
    assert env.branches() == ["master"]
    assert env.last_run.read_text().strip() == NOW.isoformat()


def test_version_bumped_in_all_three_manifests(tmp_path):
    env = Env(tmp_path, evals=[eval_json(0.0, 0.0)])
    env.run()
    for manifest in MANIFESTS:
        text = git(env.repo, "show", f"{BRANCH}:plugins/learned-worker/{manifest}")
        assert json.loads(text)["version"] == "0.1.1"
        untouched = git(env.repo, "show", f"{BRANCH}:plugins/learned-director/{manifest}")
        assert json.loads(untouched)["version"] == "0.1.0"
    assert git(env.repo, "log", "-1", "--format=%s", BRANCH) == f"learned: {DATE} — foo"


def test_fix_round_bumps_newly_touched_plugin(tmp_path):
    env = Env(tmp_path, verdicts=["request-changes", "approve"], director_fix=True)
    report = env.run()
    assert report.exit_code == 0, report.stop_reason
    assert report.released == ["learned-worker 0.1.1", "learned-director 0.1.1"]
    for plugin in PLUGINS:
        for manifest in MANIFESTS:
            text = git(env.repo, "show", f"master:plugins/{plugin}/{manifest}")
            assert json.loads(text)["version"] == "0.1.1"
    update = ["claude", "plugin", "update", "learned-director@agent-plugins"]
    assert update in env.fake.calls
    assert "claude plugin update learned-director" in (env.out / "report.md").read_text()


def test_eval_below_baseline_stops_and_keeps_branch(tmp_path):
    env = Env(tmp_path, evals=[eval_json(0.9, 0.95)])
    report = env.run()
    assert report.exit_code == 1
    assert "below baseline" in report.stop_reason
    assert BRANCH in env.branches()
    assert env.worktree.is_dir()
    assert not env.last_run.exists()
    assert "below baseline" in (env.out / "report.md").read_text()
    assert (env.out / "eval-foo.json").is_file()
    assert env.fake.words("review") == []


def test_malformed_eval_score_stops_with_report(tmp_path):
    arms = {"with": [{"score": None}], "without": [{"score": 0.5}]}
    env = Env(tmp_path, evals=[{"costUsd": 0.1, "cases": [{"name": "case", "arms": arms}]}])
    report = env.run()
    target = env.out / "eval-foo.json"
    assert report.exit_code == 1
    assert report.stop_reason == f"eval of foo: malformed score in {target}"
    assert BRANCH in env.branches()
    assert not env.last_run.exists()
    assert f"eval of foo: malformed score in {target}" in (env.out / "report.md").read_text()
    assert target.is_file()
    assert env.fake.words("review") == []


def test_review_fix_rounds_capped_at_two(tmp_path):
    env = Env(tmp_path, verdicts=["request-changes"])
    report = env.run()
    assert report.exit_code == 1
    assert env.fake.drafts == 3
    assert len(env.fake.words("--role", "review")) == 3
    assert env.fake.words("ready") == []
    assert "request-changes" in report.stop_reason
    fix_brief = (env.out / "fix-1.md").read_text()
    assert fix_brief.startswith("# Brief: round 1 fixes")
    assert "- high — plugins/x.md — Needs a table" in fix_brief
    assert BRANCH in env.branches()


def test_draft_and_fix_jobs_use_implement_role(tmp_path):
    env = Env(tmp_path, verdicts=["request-changes"])
    env.run()
    jobs = env.fake.words("--role", "implement")
    assert len(jobs) == 3  # one draft, then two fix rounds
    for call in jobs:
        assert call[call.index("--role") + 1] == "implement"
    assert env.fake.words("implement-hard") == []


def test_ready_failure_stops_before_merge(tmp_path):
    env = Env(tmp_path, ready_rc=1)
    before = git(env.repo, "rev-parse", "master")
    report = env.run()
    assert report.exit_code == 1
    assert "ready" in report.stop_reason
    assert git(env.repo, "rev-parse", "master") == before
    assert env.fake.words("merge") == []
    assert not env.last_run.exists()


def test_dirty_main_checkout_stops_before_merge(tmp_path):
    env = Env(tmp_path)
    (env.repo / "stray.txt").write_text("dirty\n")
    report = env.run()
    assert report.exit_code == 1
    assert "not clean" in report.stop_reason
    assert env.fake.words("merge") == []
    assert env.fake.words("install") == []
    assert BRANCH in env.branches()


def test_full_run_merges_installs_and_reports(tmp_path):
    env = Env(tmp_path)
    report = env.run()
    assert report.exit_code == 0, report.stop_reason
    assert git(env.repo, "show", "master:plugins/learned-worker/plugin.json").count("0.1.1") == 1
    assert len(git(env.repo, "log", "-1", "--format=%P", "master").split()) == 2
    assert env.fake.words("hub", "install", "--apply")
    assert not env.worktree.exists()
    assert env.branches() == ["master"]
    assert env.last_run.read_text().strip() == NOW.isoformat()
    text = (env.out / "report.md").read_text()
    assert "learned-worker 0.1.1" in text
    assert "approve" in text
    assert "foo: with 1.00 vs without 0.00" in text
    assert "MCP proposal: foo-query" in text
    assert (env.out / "extract.json").is_file()


def test_extract_ignored_without_repo_gitignore(tmp_path):
    env = Env(tmp_path)
    # The skills repo does not ignore .agent-hub/, so the extract copy must be excluded some other way.
    (env.repo / ".gitignore").write_text(".worktrees/\n")
    git(env.repo, "add", "-A")
    git(env.repo, "commit", "-m", "drop agent-hub ignore")
    report = env.run()
    assert report.exit_code == 0, report.stop_reason
    assert ".agent-hub/learned/" in (env.repo / ".git" / "info" / "exclude").read_text().splitlines()
    assert env.branches() == ["master"]


def test_release_updates_director_plugin_when_changed(tmp_path):
    env = Env(tmp_path, director=True)
    report = env.run()
    assert report.exit_code == 0, report.stop_reason
    install = env.fake.words("hub", "install", "--apply")[0]
    marketplace = ["claude", "plugin", "marketplace", "update", "agent-plugins"]
    update = ["claude", "plugin", "update", "learned-director@agent-plugins"]
    assert marketplace in env.fake.calls
    assert update in env.fake.calls
    assert env.fake.calls.index(marketplace) > env.fake.calls.index(install)
    assert env.fake.calls.index(update) > env.fake.calls.index(marketplace)
    assert "claude plugin update learned-director" in (env.out / "report.md").read_text()
    assert env.branches() == ["master"]


def test_release_skips_claude_update_when_only_worker_changed(tmp_path):
    env = Env(tmp_path)
    report = env.run()
    assert report.exit_code == 0, report.stop_reason
    assert env.fake.words("hub", "install", "--apply")
    assert env.fake.words("marketplace") == []
    assert env.fake.words("learned-director@agent-plugins") == []
    assert "claude plugin update" not in (env.out / "report.md").read_text()


def test_marketplace_name_read_from_repo(tmp_path):
    env = Env(tmp_path, director=True)
    write_marketplace(env.repo, "my-market")
    git(env.repo, "add", "-A")
    git(env.repo, "commit", "-m", "marketplace")
    report = env.run()
    assert report.exit_code == 0, report.stop_reason
    assert ["claude", "plugin", "marketplace", "update", "my-market"] in env.fake.calls
    assert ["claude", "plugin", "update", "learned-director@my-market"] in env.fake.calls
    assert env.fake.words("agent-plugins") == []
    assert "claude plugin update learned-director@my-market" in (env.out / "report.md").read_text()

    (tmp_path / "missing").mkdir()
    missing = Env(tmp_path / "missing", director=True)
    write_marketplace(missing.repo, None)
    git(missing.repo, "add", "-A")
    git(missing.repo, "commit", "-m", "no marketplace")
    before = git(missing.repo, "rev-parse", "master")
    report = missing.run()
    assert report.exit_code == 1
    assert report.stop_reason == "learned-director released but .claude-plugin/marketplace.json has no name"
    assert git(missing.repo, "rev-parse", "master") == before
    assert missing.fake.words("marketplace") == []
    assert missing.fake.words("hub", "install") == []


def test_failed_eval_cost_counted(tmp_path):
    arms = {"with": [{"score": None}], "without": [{"score": 0.5}]}
    cases = [{"name": "case", "arms": arms}]
    scenarios = (("with-cost", {"costUsd": 0.75, "cases": cases}, 0.75), ("no-cost", {"cases": cases}, 3.0))
    for name, data, expected in scenarios:
        (tmp_path / name).mkdir()
        env = Env(tmp_path / name, evals=[data])
        env.out.mkdir(parents=True)
        run = skillweekly._Run(env.cfg, NOW, env.fake, skillweekly.Report(date=DATE))
        with pytest.raises(skillweekly.Stop, match="malformed score"):
            run.evaluate([("learned-worker", "foo")])
        assert run.spent == expected
        assert (env.out / "eval-foo.json").is_file()


def test_eval_cost_missing_or_string_counts_budget(tmp_path):
    arms = {"with": [{"score": 1.0}], "without": [{"score": 0.0}]}
    cases = [{"name": "case", "arms": arms}]
    scenarios = (
        ("missing-cost", {"cases": cases}),
        ("string-cost", {"costUsd": "0.15", "cases": cases}),
        ("non-numeric-cost", {"costUsd": "n/a", "cases": cases}),
    )
    for name, data in scenarios:
        (tmp_path / name).mkdir()
        env = Env(tmp_path / name, evals=[data, eval_json(1.0, 0.0)])
        env.out.mkdir(parents=True)
        run = skillweekly._Run(env.cfg, NOW, env.fake, skillweekly.Report(date=DATE))
        with pytest.raises(skillweekly.Stop, match=r"eval budget \$3\.0 spent before bar"):
            run.evaluate([("learned-worker", "foo"), ("learned-worker", "bar")])
        assert run.spent == 3.0
        assert "cost unknown" in run.report.evals["foo"]
        assert run.report.evals["foo"] == "with 1.00 vs without 0.00 (cost unknown)"

    env = Env(tmp_path / "full-run", evals=[{"cases": cases}])
    report = env.run()
    assert report.exit_code == 0, report.stop_reason
    text = (env.out / "report.md").read_text()
    assert "foo: with 1.00 vs without 0.00 (cost unknown)" in text


def test_claude_update_failure_stops_after_merge(tmp_path):
    env = Env(tmp_path, director=True, plugin_rc=1)
    before = git(env.repo, "rev-parse", "master")
    report = env.run()
    assert report.exit_code == 1
    assert report.stop_reason == ""
    assert report.outcome == "released, install failed: claude plugin update: update refused"
    assert git(env.repo, "rev-parse", "master") != before
    assert len(git(env.repo, "log", "-1", "--format=%P", "master").split()) == 2
    assert ["claude", "plugin", "update", "learned-director@agent-plugins"] not in env.fake.calls
    assert env.last_run.read_text().strip() == NOW.isoformat()
    assert not env.worktree.exists()
    assert env.branches() == ["master"]


def test_install_failure_after_merge_finishes_run(tmp_path):
    env = Env(tmp_path, install_rc=1)
    env.cfg.notify = True
    before = git(env.repo, "rev-parse", "master")
    report = env.run()
    assert report.exit_code == 1
    assert report.stop_reason == ""
    assert report.outcome == "released, install failed: hub install --apply: install refused"
    assert git(env.repo, "rev-parse", "master") != before
    assert len(git(env.repo, "log", "-1", "--format=%P", "master").split()) == 2
    assert not env.worktree.exists()
    assert env.branches() == ["master"]
    assert env.last_run.read_text().strip() == NOW.isoformat()
    text = (env.out / "report.md").read_text()
    assert "Outcome: released, install failed: hub install --apply: install refused" in text
    assert "install: failed" in text
    notified = [c[2] for c in env.fake.words("notify-send")]
    assert notified == [f"skills weekly {DATE}: released, install failed: hub install --apply: install refused"]


def test_unexpected_exception_writes_report(tmp_path):
    env = Env(tmp_path, crash=True)
    env.cfg.notify = True
    before = git(env.repo, "rev-parse", "master")
    report = env.run()
    assert report.exit_code == 1
    assert report.outcome == "crashed: RuntimeError: gate exploded"
    assert git(env.repo, "rev-parse", "master") == before
    assert not env.last_run.exists()
    text = (env.out / "report.md").read_text()
    assert "Outcome: crashed: RuntimeError: gate exploded" in text
    assert "crash: failed" in text
    crash_text = (env.out / "crash.txt").read_text()
    assert "Traceback" in crash_text
    assert "RuntimeError: gate exploded" in crash_text
    notified = [c[2] for c in env.fake.words("notify-send")]
    assert notified == [f"skills weekly {DATE}: crashed: RuntimeError: gate exploded"]


def test_cleanup_failure_after_merge_still_finishes(tmp_path):
    env = Env(tmp_path, cleanup_rc=1)
    before = git(env.repo, "rev-parse", "master")
    report = env.run()
    assert report.exit_code == 0, report.stop_reason
    assert git(env.repo, "rev-parse", "master") != before
    assert len(git(env.repo, "log", "-1", "--format=%P", "master").split()) == 2
    assert BRANCH in env.branches()
    assert env.last_run.read_text().strip() == NOW.isoformat()
    text = (env.out / "report.md").read_text()
    assert "cleanup: warning" in text
    assert "git worktree remove: cleanup refused" in text
    assert "Outcome: released learned-worker 0.1.1" in text


def test_never_pushes(tmp_path):
    env = Env(tmp_path)
    env.run()
    assert env.fake.calls
    assert not [c for c in env.fake.calls if "push" in c]


def test_dry_run_runs_no_command_and_writes_nothing(tmp_path):
    env = Env(tmp_path)
    before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    report = env.run(dry_run=True)
    assert env.fake.calls == []
    assert sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*")) == before
    assert report.exit_code == 0
    assert any("would" in step.status for step in report.steps)
    assert any("draft" in step.name for step in report.steps)


def test_report_command_prints_newest(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENT_HUB_STATE", str(tmp_path / "state"))
    assert main(["skills", "report"]) == 1
    for date in ("2026-10-05", "2026-10-12"):
        (tmp_path / "state" / "skills" / date).mkdir(parents=True)
        (tmp_path / "state" / "skills" / date / "report.md").write_text(f"report {date}\n")
    capsys.readouterr()
    assert main(["skills", "report"]) == 0
    assert capsys.readouterr().out == "report 2026-10-12\n"
