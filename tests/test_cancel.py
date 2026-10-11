import subprocess
from datetime import datetime, timezone

from agenthub import guards
from agenthub.cli import main
from agenthub.config import paths
from agenthub.jobs import new_job
from tests.gitrepo import make_repo


def _repo_id(repo):
    return str(guards.inspect(repo).common_dir)


def _job(workdir, repo=None, status="running"):
    """A job record in the sandbox state; `repo` is left out of the record when None (an older record)."""
    job = new_job(paths(), "implement", datetime.now(timezone.utc))
    record = {"id": job.id, "role": "implement", "status": status, "attempts": [], "workdir": str(workdir)}
    if repo is not None:
        record["repo"] = repo
    job.write_json("result.json", record)
    return job


def test_cancel_foreign_job_refused_exit_3(env_sandbox, repo, tmp_path, capsys):
    other = make_repo(tmp_path / "other")
    job = _job(other, repo=_repo_id(other))

    assert main(["cancel", job.id, "-d", str(repo)]) == 3

    err = capsys.readouterr().err
    assert f"job {job.id} belongs to {_repo_id(other)}; run cancel from that repository" in err
    assert job.read_json("result.json")["status"] == "running"


def test_cancel_own_job_from_other_worktree_same_repo(env_sandbox, repo, tmp_path, capsys):
    wt = tmp_path / "wt"
    subprocess.run(["git", "worktree", "add", "-b", "feat/y", str(wt)], cwd=repo, check=True, capture_output=True)
    assert _repo_id(wt) == _repo_id(repo)
    job = _job(wt, repo=_repo_id(repo))

    assert main(["cancel", job.id, "-d", str(wt)]) == 0

    assert job.read_json("result.json")["status"] == "cancelled"


def test_cancel_all_only_own_repo(env_sandbox, repo, tmp_path, capsys):
    other = make_repo(tmp_path / "other")
    mine = [_job(repo, repo=_repo_id(repo)) for _ in range(2)]
    theirs = _job(other, repo=_repo_id(other))

    assert main(["cancel", "--all", "-d", str(repo)]) == 0

    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 2
    assert all(job.read_json("result.json")["status"] == "cancelled" for job in mine)
    assert theirs.read_json("result.json")["status"] == "running"


def test_jobs_default_scoped_all_flag_lists_everything(env_sandbox, repo, tmp_path, capsys):
    other = make_repo(tmp_path / "other")
    mine = _job(repo, repo=_repo_id(repo), status="ok")
    theirs = _job(other, repo=_repo_id(other), status="ok")

    assert main(["jobs", "-d", str(repo)]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1 and lines[0].startswith(mine.id)

    assert main(["jobs", "--all"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert {line.split()[0] for line in lines} == {mine.id, theirs.id}


def test_legacy_job_without_repo_derived_from_workdir(env_sandbox, repo, tmp_path, capsys):
    legacy = _job(repo)
    gone = _job(tmp_path / "deleted")

    assert main(["cancel", legacy.id, "-d", str(repo)]) == 0
    assert legacy.read_json("result.json")["status"] == "cancelled"

    assert main(["cancel", gone.id, "-d", str(repo)]) == 3
    assert gone.read_json("result.json")["status"] == "running"
    assert "belongs to an unknown repository" in capsys.readouterr().err
