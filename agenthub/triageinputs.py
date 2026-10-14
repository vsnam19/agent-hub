from dataclasses import dataclass
from pathlib import Path

from agenthub.triage import ReviewFinding, review_findings


@dataclass
class TriageInputs:
    review_job: str
    review_reply: str
    findings: list[ReviewFinding]
    gate_tail: str
    diff_path: str
    implementers: list[str]
    job_ids: set[str]


def collect(jobs: list[dict], workdir: Path, head: str) -> TriageInputs | None:
    workdir_resolved = Path(workdir).resolve()
    mine = [j for j in jobs if j.get("workdir") and Path(j["workdir"]).resolve() == workdir_resolved]

    implementers = sorted(
        {
            f"{j.get('agent')}:{j.get('model')}"
            for j in mine
            if j.get("kind") == "implement" and j.get("status") in ("ok", "gate-failed")
        }
    )
    job_ids = {str(j["id"]) for j in jobs if j.get("id")}

    matching = [j for j in mine if j.get("kind") in ("review", "gate") and j.get("head") == head]
    if not matching:
        return None

    newest = matching[0]
    kind = newest.get("kind")
    status = newest.get("status")

    if kind == "review" and status == "ok" and newest.get("verdict") == "request-changes":
        review_job = str(newest.get("id") or "")
        reply_path = newest.get("reply_path")
        review_reply = ""
        findings: list[ReviewFinding] = []
        diff_path = ""
        if reply_path:
            p = Path(reply_path)
            try:
                review_reply = p.read_text()
                findings = review_findings(review_reply)
            except OSError:
                pass
            job_dir = p.parents[2] if len(p.parents) > 2 else p.parent
            diff_path = str(job_dir / "diff.patch")

        return TriageInputs(
            review_job=review_job,
            review_reply=review_reply,
            findings=findings,
            gate_tail="",
            diff_path=diff_path,
            implementers=implementers,
            job_ids=job_ids,
        )

    if kind == "gate" and status != "ok":
        gate_tail = ""
        for g in newest.get("gate") or []:
            if (g.get("rc") or 0) != 0:
                cmd = g.get("cmd") or ""
                tail = g.get("tail") or ""
                gate_tail = f"{cmd}\n{tail}"
                break

        return TriageInputs(
            review_job="",
            review_reply="",
            findings=[],
            gate_tail=gate_tail,
            diff_path="",
            implementers=implementers,
            job_ids=job_ids,
        )

    return None
