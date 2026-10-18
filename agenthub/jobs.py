import json
import os
import re
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from agenthub.config import Paths

JOB_ID_RE = re.compile(r"^\d{8}-\d{6}-.*-[0-9a-f]{4}$")


@dataclass
class Job:
    id: str
    dir: Path

    def write_json(self, name: str, data: dict) -> None:
        target = self.dir / name
        fd, temp_path = tempfile.mkstemp(dir=self.dir, prefix=f"{name}.tmp", text=True)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
                f.write("\n")
            os.replace(temp_path, target)
        except Exception:
            os.unlink(temp_path)
            raise

    def read_json(self, name: str) -> dict:
        with open(self.dir / name) as f:
            return json.load(f)

    def attempt_dir(self, n: int, agent: str, model: str) -> Path:
        safe_model = model.replace("/", "_").replace(":", "_")
        d = self.dir / "attempts" / f"{n}-{agent}-{safe_model}"
        d.mkdir(parents=True, exist_ok=True)
        return d


def new_job(p: Paths, role: str, now: datetime) -> Job:
    ts = now.strftime("%Y%m%d-%H%M%S")
    rand_hex = uuid.uuid4().hex[:4]
    job_id = f"{ts}-{role}-{rand_hex}"
    d = p.jobs_dir / job_id
    d.mkdir(parents=True, exist_ok=True)
    return Job(id=job_id, dir=d)


def get_job(p: Paths, job_id: str) -> Job:
    d = p.jobs_dir / job_id
    if not d.is_dir():
        raise KeyError(job_id)
    return Job(id=job_id, dir=d)


def list_jobs(p: Paths, limit: int) -> list[dict]:
    if not p.jobs_dir.is_dir():
        return []

    jobs = []
    for entry in os.scandir(p.jobs_dir):
        if entry.is_dir() and JOB_ID_RE.match(entry.name):
            result_path = Path(entry.path) / "result.json"
            if result_path.is_file():
                jobs.append((entry.name, result_path))

    jobs.sort(key=lambda x: x[0], reverse=True)

    results = []
    for _name, result_path in jobs[:limit]:
        try:
            with open(result_path) as f:
                results.append(json.load(f))
        except Exception:
            continue

    return results


def dead_job_result(result: dict, pid: int | None) -> dict:
    return {
        **result,
        "status": "failed",
        "exit_code": 1,
        "reason": f"job process {pid} is not running; result.json was never finished",
    }
