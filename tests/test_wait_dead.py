import json
import subprocess
import time
from datetime import datetime, timezone

from agenthub.cli import main
from agenthub.config import paths
from agenthub.jobs import dead_job_result, new_job


def test_dead_job_result_shape():
    running = {"id": "job-1", "role": "implement", "status": "running", "workdir": "/tmp/work"}
    res = dead_job_result(running, 12345)
    assert res["id"] == "job-1"
    assert res["role"] == "implement"
    assert res["workdir"] == "/tmp/work"
    assert res["status"] == "failed"
    assert res["exit_code"] == 1
    assert res["reason"] == "job process 12345 is not running; result.json was never finished"

    res_none = dead_job_result({"status": "running"}, None)
    assert res_none["status"] == "failed"
    assert res_none["exit_code"] == 1
    assert res_none["reason"] == "job process None is not running; result.json was never finished"


def test_wait_json_dead_job_is_json(env_sandbox, repo, capsys):
    proc = subprocess.Popen(["true"])
    proc.wait()

    job = new_job(paths(), "implement", datetime.now(timezone.utc))
    job.write_json(
        "result.json",
        {"id": job.id, "role": "implement", "status": "running", "workdir": str(repo.resolve())},
    )
    (job.dir / "pid").write_text(json.dumps({"pid": proc.pid, "start_time": 123456}))

    t0 = time.monotonic()
    ret = main(["wait", "-d", str(repo), "--timeout", "5s", "--json"])
    elapsed = time.monotonic() - t0

    out, err = capsys.readouterr()
    assert elapsed < 3.0
    assert ret == 1
    data = json.loads(out)
    assert data["status"] == "failed"
    assert data["exit_code"] == 1
    assert data["reason"] == f"job process {proc.pid} is not running; result.json was never finished"
    assert data["id"] == job.id
    assert data["role"] == "implement"
    assert job.read_json("result.json")["status"] == "running"
