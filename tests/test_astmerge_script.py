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


def test_refuses_unparsable_side(tmp_path: Path):
    repo_dir = tmp_path / "repo"
    make_repo(repo_dir, default="master", branch=None)
    script = Path(__file__).resolve().parent.parent / "scripts" / "astmerge.py"

    pyfile = repo_dir / "mod.py"
    pyfile.write_text("def foo():\n    return 0\n", encoding="utf-8")
    subprocess.run(["git", "add", "mod.py"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "init mod"], cwd=repo_dir, check=True)

    subprocess.run(["git", "checkout", "-b", "branch-a"], cwd=repo_dir, check=True)
    pyfile.write_text("def foo():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-am", "edit foo in branch-a"], cwd=repo_dir, check=True)

    subprocess.run(["git", "checkout", "master"], cwd=repo_dir, check=True)
    subprocess.run(["git", "checkout", "-b", "branch-b"], cwd=repo_dir, check=True)
    pyfile.write_text("def foo():\n    return (unparsable syntax\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-am", "unparsable syntax in branch-b"], cwd=repo_dir, check=True)

    merge_proc = subprocess.run(
        ["git", "merge", "--no-commit", "--no-ff", "branch-a"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    )
    assert merge_proc.returncode != 0
    initial_content = pyfile.read_text(encoding="utf-8")
    assert "<<<<<<< HEAD" in initial_content
    assert "=======" in initial_content
    assert ">>>>>>>" in initial_content

    res = subprocess.run(
        [sys.executable, str(script), "mod.py"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    )
    assert res.returncode != 0
    after_content = pyfile.read_text(encoding="utf-8")
    assert "<<<<<<< HEAD" in after_content
    assert "=======" in after_content
    assert ">>>>>>>" in after_content
    assert after_content == initial_content


def test_refuses_duplicate_keys(tmp_path: Path):
    repo_dir = tmp_path / "repo"
    make_repo(repo_dir, default="master", branch=None)
    script = Path(__file__).resolve().parent.parent / "scripts" / "astmerge.py"

    pyfile = repo_dir / "mod.py"
    pyfile.write_text("def foo():\n    return 0\n", encoding="utf-8")
    subprocess.run(["git", "add", "mod.py"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "init mod"], cwd=repo_dir, check=True)

    subprocess.run(["git", "checkout", "-b", "branch-a"], cwd=repo_dir, check=True)
    pyfile.write_text("def foo():\n    return 1\n\ndef foo():\n    return 2\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-am", "duplicate foo in branch-a"], cwd=repo_dir, check=True)

    subprocess.run(["git", "checkout", "master"], cwd=repo_dir, check=True)
    subprocess.run(["git", "checkout", "-b", "branch-b"], cwd=repo_dir, check=True)
    pyfile.write_text("def foo():\n    return 3\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-am", "edit foo in branch-b"], cwd=repo_dir, check=True)

    merge_proc = subprocess.run(
        ["git", "merge", "--no-commit", "--no-ff", "branch-a"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    )
    assert merge_proc.returncode != 0
    initial_content = pyfile.read_text(encoding="utf-8")
    assert "<<<<<<< HEAD" in initial_content
    assert "=======" in initial_content
    assert ">>>>>>>" in initial_content

    res = subprocess.run(
        [sys.executable, str(script), "mod.py"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    )
    assert res.returncode != 0
    after_content = pyfile.read_text(encoding="utf-8")
    assert "<<<<<<< HEAD" in after_content
    assert "=======" in after_content
    assert ">>>>>>>" in after_content
    assert after_content == initial_content


def test_refuses_differing_unattached_comments(tmp_path: Path):
    repo_dir = tmp_path / "repo"
    make_repo(repo_dir, default="master", branch=None)
    script = Path(__file__).resolve().parent.parent / "scripts" / "astmerge.py"

    pyfile = repo_dir / "mod.py"
    pyfile.write_text("# Base comment\n\n\ndef foo():\n    return 0\n", encoding="utf-8")
    subprocess.run(["git", "add", "mod.py"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "init mod"], cwd=repo_dir, check=True)

    subprocess.run(["git", "checkout", "-b", "branch-a"], cwd=repo_dir, check=True)
    pyfile.write_text("# Comment A\n\n\ndef foo():\n    return 0\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-am", "comment A in branch-a"], cwd=repo_dir, check=True)

    subprocess.run(["git", "checkout", "master"], cwd=repo_dir, check=True)
    subprocess.run(["git", "checkout", "-b", "branch-b"], cwd=repo_dir, check=True)
    pyfile.write_text("# Comment B\n\n\ndef foo():\n    return 0\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-am", "comment B in branch-b"], cwd=repo_dir, check=True)

    merge_proc = subprocess.run(
        ["git", "merge", "--no-commit", "--no-ff", "branch-a"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    )
    assert merge_proc.returncode != 0
    initial_content = pyfile.read_text(encoding="utf-8")
    assert "<<<<<<< HEAD" in initial_content
    assert "=======" in initial_content
    assert ">>>>>>>" in initial_content

    res = subprocess.run(
        [sys.executable, str(script), "mod.py"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    )
    assert res.returncode != 0
    after_content = pyfile.read_text(encoding="utf-8")
    assert "<<<<<<< HEAD" in after_content
    assert "=======" in after_content
    assert ">>>>>>>" in after_content
    assert after_content == initial_content


def test_refuses_unnamed_statement_edits(tmp_path: Path):
    repo_dir = tmp_path / "repo"
    make_repo(repo_dir, default="master", branch=None)
    script = Path(__file__).resolve().parent.parent / "scripts" / "astmerge.py"

    pyfile = repo_dir / "mod.py"
    pyfile.write_text("def foo():\n    return 0\n\nif __name__ == '__main__':\n    print('init')\n", encoding="utf-8")
    subprocess.run(["git", "add", "mod.py"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "init mod"], cwd=repo_dir, check=True)

    subprocess.run(["git", "checkout", "-b", "branch-a"], cwd=repo_dir, check=True)
    pyfile.write_text("def foo():\n    return 0\n\nif __name__ == '__main__':\n    print('from a')\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-am", "unnamed statement edit in branch-a"], cwd=repo_dir, check=True)

    subprocess.run(["git", "checkout", "master"], cwd=repo_dir, check=True)
    subprocess.run(["git", "checkout", "-b", "branch-b"], cwd=repo_dir, check=True)
    pyfile.write_text("def foo():\n    return 0\n\nif __name__ == '__main__':\n    print('from b')\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-am", "unnamed statement edit in branch-b"], cwd=repo_dir, check=True)

    merge_proc = subprocess.run(
        ["git", "merge", "--no-commit", "--no-ff", "branch-a"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    )
    assert merge_proc.returncode != 0
    initial_content = pyfile.read_text(encoding="utf-8")
    assert "<<<<<<< HEAD" in initial_content
    assert "=======" in initial_content
    assert ">>>>>>>" in initial_content

    res = subprocess.run(
        [sys.executable, str(script), "mod.py"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    )
    assert res.returncode != 0
    after_content = pyfile.read_text(encoding="utf-8")
    assert "<<<<<<< HEAD" in after_content
    assert "=======" in after_content
    assert ">>>>>>>" in after_content
    assert after_content == initial_content


def test_merges_disjoint_definitions_in_conflict(tmp_path: Path):
    repo_dir = tmp_path / "repo"
    make_repo(repo_dir, default="master", branch=None)
    script = Path(__file__).resolve().parent.parent / "scripts" / "astmerge.py"

    pyfile = repo_dir / "mod.py"
    pyfile.write_text("def func_a(): return 'base_a'\ndef func_b(): return 'base_b'\n", encoding="utf-8")
    subprocess.run(["git", "add", "mod.py"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "init mod"], cwd=repo_dir, check=True)

    subprocess.run(["git", "checkout", "-b", "branch-a"], cwd=repo_dir, check=True)
    pyfile.write_text("def func_a(): return 'edited_a'\ndef func_b(): return 'base_b'\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-am", "edit func_a in branch-a"], cwd=repo_dir, check=True)

    subprocess.run(["git", "checkout", "master"], cwd=repo_dir, check=True)
    subprocess.run(["git", "checkout", "-b", "branch-b"], cwd=repo_dir, check=True)
    pyfile.write_text("def func_a(): return 'base_a'\ndef func_b(): return 'edited_b'\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-am", "edit func_b in branch-b"], cwd=repo_dir, check=True)

    merge_proc = subprocess.run(
        ["git", "merge", "--no-commit", "--no-ff", "branch-a"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    )
    assert merge_proc.returncode != 0
    initial_content = pyfile.read_text(encoding="utf-8")
    assert "<<<<<<< HEAD" in initial_content

    res = subprocess.run(
        [sys.executable, str(script), "mod.py"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0
    merged_content = pyfile.read_text(encoding="utf-8")
    assert "<<<<<<< HEAD" not in merged_content
    assert "edited_a" in merged_content
    assert "edited_b" in merged_content
