import shutil
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

FIXTURE_DIR = Path(__file__).parent / "fixture"
TASKS_DIR = Path(__file__).parent / "tasks"


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    dir: Path
    paths_allowed: list[str]
    visible_tests: list[str]

    @property
    def brief(self) -> str:
        return (self.dir / "brief.md").read_text(encoding="utf-8")


def load_tasks() -> dict[str, Task]:
    tasks = {}
    for i in range(1, 7):
        tid = f"t{i}"
        tdir = TASKS_DIR / tid
        with (tdir / "task.toml").open("rb") as f:
            data = tomllib.load(f)
        tasks[tid] = Task(
            id=tid,
            title=data["title"],
            dir=tdir,
            paths_allowed=data["paths_allowed"],
            visible_tests=data["visible_tests"],
        )
    return tasks


def prepare(task: Task, dest: Path) -> str:
    shutil.copytree(FIXTURE_DIR, dest)
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
