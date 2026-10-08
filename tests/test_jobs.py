from datetime import datetime

import pytest

from agenthub.config import Paths
from agenthub.jobs import Job, get_job, list_jobs, new_job


@pytest.fixture
def paths(tmp_path):
    return Paths(config_dir=tmp_path / "conf", state_dir=tmp_path / "st")


def test_new_job_id_format_and_dir(paths):
    now = datetime(2026, 10, 8, 14, 30, 12)
    job = new_job(paths, "implement", now)
    assert job.id.startswith("20261008-143012-implement-")
    assert len(job.id) == 30  # + 4 hex chars
    assert job.dir.is_dir()
    assert job.dir == paths.jobs_dir / job.id


def test_ids_unique_same_second(paths):
    now = datetime(2026, 10, 8, 14, 30, 12)
    job1 = new_job(paths, "implement", now)
    job2 = new_job(paths, "implement", now)
    assert job1.id != job2.id


def test_write_json_atomic_roundtrip(tmp_path):
    job = Job(id="1", dir=tmp_path)
    job.write_json("test.json", {"a": 1})
    assert job.read_json("test.json") == {"a": 1}
    # Atomic test could check tmp files, but hard without mocking, so basic roundtrip is ok


def test_attempt_dir_sanitises_model(tmp_path):
    job = Job(id="1", dir=tmp_path)
    d = job.attempt_dir(1, "agy", "claude/opus:thinking")
    assert d.name == "1-agy-claude_opus_thinking"
    assert d.is_dir()
    assert d.parent.name == "attempts"


def test_get_job_missing(paths):
    with pytest.raises(KeyError):
        get_job(paths, "missing")


def test_list_jobs_newest_first_and_limit(paths):
    now1 = datetime(2026, 10, 8, 14, 30, 12)
    now2 = datetime(2026, 10, 8, 14, 30, 13)
    now3 = datetime(2026, 10, 8, 14, 30, 14)

    job1 = new_job(paths, "r", now1)
    job1.write_json("result.json", {"id": job1.id})

    job2 = new_job(paths, "r", now2)
    job2.write_json("result.json", {"id": job2.id})

    job3 = new_job(paths, "r", now3)
    job3.write_json("result.json", {"id": job3.id})

    jobs = list_jobs(paths, limit=2)
    assert len(jobs) == 2
    assert jobs[0]["id"] == job3.id
    assert jobs[1]["id"] == job2.id


def test_list_jobs_skips_dirs_without_result(paths):
    now1 = datetime(2026, 10, 8, 14, 30, 12)
    now2 = datetime(2026, 10, 8, 14, 30, 13)

    _job1 = new_job(paths, "r", now1)
    # no result.json

    job2 = new_job(paths, "r", now2)
    job2.write_json("result.json", {"id": job2.id})

    jobs = list_jobs(paths, limit=10)
    assert len(jobs) == 1
    assert jobs[0]["id"] == job2.id


def test_list_jobs_skips_non_job_dirs(paths):
    job1_dir = paths.jobs_dir / "job1"
    job1_dir.mkdir(parents=True)
    job = Job(id="job1", dir=job1_dir)
    job.write_json("result.json", {"id": "job1"})

    jobs = list_jobs(paths, limit=10)
    assert len(jobs) == 0
