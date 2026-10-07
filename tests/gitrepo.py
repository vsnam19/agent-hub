import subprocess
from pathlib import Path


def make_repo(path: Path, default: str = "master", branch: str | None = "feat/x") -> Path:
    path.mkdir(parents=True, exist_ok=True)

    def run_git(*args):
        subprocess.run(
            ["git", *args],
            cwd=path,
            check=True,
            capture_output=True,
            text=True,
            env={"GIT_TERMINAL_PROMPT": "0"},
        )

    run_git("init", "-b", default)
    run_git("config", "user.name", "Test User")
    run_git("config", "user.email", "test@example.com")
    run_git("config", "commit.gpgsign", "false")

    (path / "README.md").write_text("Hello\n")
    run_git("add", "README.md")
    run_git("commit", "-m", "Initial commit")

    if branch is not None:
        run_git("checkout", "-b", branch)

    return path
