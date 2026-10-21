import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agenthub.agents import (
    Adapter,
    Classified,
    Invocation,
    Outcome,
    Request,
    common_stderr_text,
    is_network_failure,
)


def parse_try_again_at(message: str) -> datetime | None:
    """'... try again at Nov 7th, 2026 2:18 AM.' -> that local time as an aware UTC datetime; None if absent."""
    m = re.search(
        r"try again at ([A-Z][a-z]{2}) (\d{1,2})(?:st|nd|rd|th)?, (\d{4}) (\d{1,2}):(\d{2}) ?(AM|PM)",
        message,
    )
    if m is None:
        return None
    mon, day, year, hh, mm, ampm = m.groups()
    local = datetime.strptime(f"{mon} {day} {year} {hh}:{mm} {ampm}", "%b %d %Y %I:%M %p")
    return local.astimezone().astimezone(timezone.utc)


class CodexAdapter(Adapter):
    def list_models(self) -> list[str] | None:
        if not self.installed():
            self.models_error = "not installed"
            return None

        output = self._catalog_output(["debug", "models"])
        if output is None:
            return None

        try:
            data = json.loads(output)
        except Exception:
            self.models_error = "bad json"
            return None

        if not isinstance(data, dict) or "models" not in data:
            self.models_error = "no models key"
            return None

        raw_models = data["models"]
        if not isinstance(raw_models, list):
            self.models_error = "no models listed"
            return None

        slugs: list[str] = []
        for item in raw_models:
            if isinstance(item, dict):
                slug = item.get("slug")
                if isinstance(slug, str) and slug:
                    slugs.append(slug)

        if not slugs:
            self.models_error = "no models listed"
            return None

        self.models_error = None
        return slugs

    def build(self, req: Request) -> Invocation:
        argv = [
            self.config.binary,
            "exec",
            "-m",
            req.model,
            "-C",
            str(req.workdir),
            "--json",
            "-o",
            str(req.attempt_dir / "reply.md"),
            "--skip-git-repo-check",
        ]

        if req.role == "implement":
            argv.extend(
                [
                    "-s",
                    "workspace-write",
                    "-c",
                    "sandbox_workspace_write.network_access=true",
                ]
            )
        else:
            argv.extend(["-s", "read-only"])

        return Invocation(argv=argv, stdin_path=req.brief_path)

    def classify(self, rc: int, attempt_dir: Path) -> Classified:
        stdout_file = attempt_dir / "stdout"
        stderr_text = common_stderr_text(attempt_dir)

        reply_file = attempt_dir / "reply.md"
        reply = reply_file.read_text() if reply_file.is_file() else ""

        stdout_text = stdout_file.read_text() if stdout_file.is_file() else ""

        error_messages = []
        reset_seconds = None
        for line in stdout_text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                if data.get("type") in ["error", "turn.failed"]:
                    if "message" in data:
                        error_messages.append(data["message"])
                    if "resets_in_seconds" in data:
                        reset_seconds = data["resets_in_seconds"]
            except Exception:
                pass

        diag = (" ".join(error_messages) + " " + stderr_text).lower()

        if any(x in diag for x in ["usage limit", "usage_limit_reached", "rate limit", "quota"]):
            reset_at = None
            if reset_seconds is not None:
                reset_at = datetime.now(timezone.utc) + timedelta(seconds=int(reset_seconds))
            else:
                reset_at = parse_try_again_at(" ".join(error_messages))
            return Classified(Outcome.QUOTA, reply=reply, reset_at=reset_at, detail=diag)

        if any(x in diag for x in ["not logged in", "login", "401", "unauthorized"]):
            return Classified(Outcome.AUTH, reply=reply, detail=diag)

        if re.search(r"model\b.{0,60}\b(not found|does not exist|unsupported)|unsupported model", diag):
            return Classified(Outcome.MODEL_UNAVAILABLE, reply=reply, detail=diag)

        if "print timeout after" in diag and "returning partial output" in diag:
            return Classified(Outcome.TIMEOUT, reply=reply, detail=diag)

        if rc != 0 and is_network_failure(diag):
            return Classified(Outcome.TRANSIENT, reply=reply, detail=diag)

        if rc != 0:
            return Classified(Outcome.FAILED, reply=reply, detail=diag)

        if not reply:
            return Classified(Outcome.EMPTY, reply=reply, detail=diag)

        return Classified(Outcome.OK, reply=reply, detail=diag)


ADAPTER = CodexAdapter
