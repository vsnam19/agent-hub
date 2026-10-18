import subprocess
import sys
from pathlib import Path

from tests.gitrepo import make_repo


def test_astmerge_script_no_merge_in_progress(tmp_path: Path):
    repo_dir = tmp_path / "repo"
    make_repo(repo_dir, default="master", branch=None)
    script = Path(__file__).resolve().parent.parent / "scripts" / "astmerge.py"

    pyfile = repo_dir / "mod.py"
    pyfile.write_text("def foo(): return 1\n", encoding="utf-8")

    res = subprocess.run(
        [sys.executable, str(script), "mod.py"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 2
    assert "no merge in progress" in (res.stdout + res.stderr)


def test_astmerge_script_merges_disjoint_definitions(tmp_path: Path):
    repo_dir = tmp_path / "repo"
    make_repo(repo_dir, default="master", branch=None)
    script = Path(__file__).resolve().parent.parent / "scripts" / "astmerge.py"

    # Base definition
    pyfile = repo_dir / "mod.py"
    pyfile.write_text("def base():\n    return 0\n", encoding="utf-8")
    subprocess.run(["git", "add", "mod.py"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "init mod"], cwd=repo_dir, check=True)

    # Branch a: append func_a
    subprocess.run(["git", "checkout", "-b", "branch-a"], cwd=repo_dir, check=True)
    pyfile.write_text("def base():\n    return 0\n\n\ndef func_a():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-am", "add func_a"], cwd=repo_dir, check=True)

    # Branch b from master: append func_b
    subprocess.run(["git", "checkout", "master"], cwd=repo_dir, check=True)
    subprocess.run(["git", "checkout", "-b", "branch-b"], cwd=repo_dir, check=True)
    pyfile.write_text("def base():\n    return 0\n\n\ndef func_b():\n    return 2\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-am", "add func_b"], cwd=repo_dir, check=True)

    # Merge branch-a into branch-b (conflicts at line level because both append at end)
    merge_proc = subprocess.run(
        ["git", "merge", "--no-commit", "--no-ff", "branch-a"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    )
    assert merge_proc.returncode != 0

    # Run astmerge.py
    res = subprocess.run(
        [sys.executable, str(script), "mod.py"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0
    assert "merged mod.py 3 top-level items" in res.stdout

    content = pyfile.read_text(encoding="utf-8")
    assert "def base():" in content
    assert "def func_a():" in content
    assert "def func_b():" in content


def test_astmerge_script_aborts_on_conflicting_definitions(tmp_path: Path):
    repo_dir = tmp_path / "repo"
    make_repo(repo_dir, default="master", branch=None)
    script = Path(__file__).resolve().parent.parent / "scripts" / "astmerge.py"

    pyfile = repo_dir / "mod.py"
    pyfile.write_text("def compute():\n    return 0\n", encoding="utf-8")
    subprocess.run(["git", "add", "mod.py"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "init mod"], cwd=repo_dir, check=True)

    subprocess.run(["git", "checkout", "-b", "branch-a"], cwd=repo_dir, check=True)
    pyfile.write_text("def compute():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-am", "compute returns 1"], cwd=repo_dir, check=True)

    subprocess.run(["git", "checkout", "master"], cwd=repo_dir, check=True)
    subprocess.run(["git", "checkout", "-b", "branch-b"], cwd=repo_dir, check=True)
    pyfile.write_text("def compute():\n    return 2\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-am", "compute returns 2"], cwd=repo_dir, check=True)

    subprocess.run(
        ["git", "merge", "--no-commit", "--no-ff", "branch-a"],
        cwd=repo_dir,
        capture_output=True,
    )

    res = subprocess.run(
        [sys.executable, str(script), "mod.py"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    )
    assert res.returncode != 0
    assert "both sides changed: compute" in (res.stdout + res.stderr)
