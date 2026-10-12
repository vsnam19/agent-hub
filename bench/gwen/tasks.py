import re
import shutil
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

SUITES_FILE = Path(__file__).parent / "suites.toml"
# The Python suite's paths (tests import them).
FIXTURE_DIR = Path(__file__).parent / "fixture"
TASKS_DIR = Path(__file__).parent / "tasks"


@dataclass(frozen=True)
class Suite:
    name: str
    fixture: Path
    tasks_dir: Path
    gate: dict[str, list[str]]
    harness: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    dir: Path
    paths_allowed: list[str]
    visible_tests: list[str]
    suite: str

    @property
    def brief(self) -> str:
        return (self.dir / "brief.md").read_text(encoding="utf-8")


def load_suites(path: Path = SUITES_FILE) -> dict[str, Suite]:
    with path.open("rb") as f:
        data = tomllib.load(f)
    base = path.parent
    return {
        name: Suite(
            name=name,
            fixture=base / body["fixture"],
            tasks_dir=base / body["tasks"],
            gate=body.get("gate", {}),
            harness=body.get("harness", {}),
        )
        for name, body in data.items()
    }


def natural_key(name: str) -> list[tuple[int, int | str]]:
    """Sort key that orders embedded numbers by value: t2 before t10."""
    return [(0, int(part)) if part.isdigit() else (1, part) for part in re.split(r"(\d+)", name)]


def load_tasks(suites: dict[str, Suite] | None = None) -> dict[str, Task]:
    if suites is None:
        suites = load_suites()
    tasks: dict[str, Task] = {}
    for suite in suites.values():
        task_dirs = [d for d in suite.tasks_dir.iterdir() if (d / "task.toml").is_file()]
        for tdir in sorted(task_dirs, key=lambda d: natural_key(d.name)):
            if tdir.name in tasks:
                raise ValueError(
                    f"duplicate task id {tdir.name!r} in suites {tasks[tdir.name].suite!r} and {suite.name!r}"
                )
            with (tdir / "task.toml").open("rb") as f:
                data = tomllib.load(f)
            tasks[tdir.name] = Task(
                id=tdir.name,
                title=data["title"],
                dir=tdir,
                paths_allowed=data["paths_allowed"],
                visible_tests=data["visible_tests"],
                suite=suite.name,
            )
    return tasks


def prepare(task: Task, dest: Path, suite: Suite | None = None) -> str:
    if suite is None:
        suite = load_suites()[task.suite]
    shutil.copytree(suite.fixture, dest)
    env = {
        "GIT_AUTHOR_NAME": "Bench",
        "GIT_AUTHOR_EMAIL": "bench@example.com",
        "GIT_COMMITTER_NAME": "Bench",
        "GIT_COMMITTER_EMAIL": "bench@example.com",
        "GIT_TERMINAL_PROMPT": "0",
    }

    def _git(*args):
        subprocess.run(
            [
                "git",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "core.hooksPath=/dev/null",
                *args,
            ],
            cwd=dest,
            env=env,
            check=True,
            capture_output=True,
        )

    _git("init", "-b", "master")
    _git("add", ".")
    _git("commit", "-m", "fixture")

    patch = task.dir / "setup.patch"
    if patch.exists() and patch.stat().st_size > 0:
        _git("apply", str(patch.resolve()))
        _git("add", ".")
        _git("commit", "-m", "setup")
    else:
        _git("commit", "--allow-empty", "-m", "setup")

    out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=dest, check=True, capture_output=True, text=True)
    return out.stdout.strip()


def install_hidden(task: Task, repo: Path) -> None:
    hidden_dest = repo / "tests" / "hidden"
    if hidden_dest.exists():
        shutil.rmtree(hidden_dest)
    shutil.copytree(task.dir / "hidden", hidden_dest)
