import json
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agenthub.agents import Adapter, Classified, Invocation, Outcome, Request, common_stderr_text


class AgyAdapter(Adapter):
    def build(self, req: Request) -> Invocation:
        dest_brief = req.attempt_dir / "brief.md"
        shutil.copy2(req.brief_path, dest_brief)

        argv = [
            self.config.binary,
            "--model",
            req.model,
            "--output-format",
            "json",
            "--add-dir",
            str(req.attempt_dir),
            "--add-dir",
            str(req.brief_path.parent),
        ]

        if req.role == "implement":
            argv.append("--dangerously-skip-permissions")
        else:
            argv.append("--mode")
            argv.append("plan")

        argv.extend(
            [
                "-p",
                f"Read the file {req.attempt_dir}/brief.md and carry out the task it describes. "
                "Work in the current directory.",
            ]
        )

        return Invocation(argv=argv, stdin_path=None)

    def classify(self, rc: int, attempt_dir: Path) -> Classified:
        stdout_file = attempt_dir / "stdout"
        stderr_text = common_stderr_text(attempt_dir)

        stdout_text = stdout_file.read_text() if stdout_file.is_file() else ""

        reply = stdout_text
        error_field = ""
        try:
            data = json.loads(stdout_text, strict=False)
            reply = data.get("response", "")
            error_field = data.get("error", "")
        except Exception:
            pass

        diag = (error_field + " " + stderr_text).lower()

        if any(x in diag for x in ["quota", "rate limit", "resource exhausted", "quota reached"]):
            reset_at = None
            m = re.search(r"resets in (?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", diag, re.IGNORECASE)
            if m and (m.group(1) or m.group(2) or m.group(3)):
                h = int(m.group(1) or 0)
                mn = int(m.group(2) or 0)
                s = int(m.group(3) or 0)
                reset_at = datetime.now(timezone.utc) + timedelta(hours=h, minutes=mn, seconds=s)
            return Classified(Outcome.QUOTA, reply=reply, reset_at=reset_at, detail=diag)

        if any(x in diag for x in ["unauthenticated", "unauthorized", "sign in", "please authenticate", "reauth"]):
            return Classified(Outcome.AUTH, reply=reply, detail=diag)

        if any(x in diag for x in ["not a known model", "is not recognized as a known model", "invalid --model"]):
            return Classified(Outcome.MODEL_UNAVAILABLE, reply=reply, detail=diag)

        if "print timeout after" in diag and "returning partial output" in diag:
            return Classified(Outcome.TIMEOUT, reply=reply, detail=diag)

        if rc != 0:
            return Classified(Outcome.FAILED, reply=reply, detail=diag)

        if not reply:
            return Classified(Outcome.EMPTY, reply=reply, detail=diag)

        return Classified(Outcome.OK, reply=reply, detail=diag)


ADAPTER = AgyAdapter
