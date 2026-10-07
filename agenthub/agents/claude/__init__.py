import json
import re
from datetime import datetime, timezone
from pathlib import Path

from agenthub.agents import Adapter, Classified, Invocation, Outcome, Request, common_stderr_text


class ClaudeAdapter(Adapter):
    def build(self, req: Request) -> Invocation:
        argv = [
            self.config.binary,
            "-p",
            "--model",
            req.model,
            "--output-format",
            "json",
            "--no-session-persistence",
        ]

        if req.role == "implement":
            argv.append("--permission-mode")
            argv.append("bypassPermissions")
        else:
            argv.append("--allowedTools")
            argv.append('Read Grep Glob "Bash(git diff:*)" "Bash(git log:*)" "Bash(git show:*)" "Bash(git status:*)"')

        return Invocation(argv=argv, stdin_path=req.brief_path)

    def classify(self, rc: int, attempt_dir: Path) -> Classified:
        stdout_file = attempt_dir / "stdout"
        stderr_text = common_stderr_text(attempt_dir)

        stdout_text = stdout_file.read_text() if stdout_file.is_file() else ""

        reply = stdout_text
        error_messages = []
        try:
            data = json.loads(stdout_text)
            reply = data.get("result", "")
            if data.get("is_error"):
                if "subtype" in data:
                    error_messages.append(data["subtype"])
                if "result" in data:
                    error_messages.append(data["result"])
        except Exception:
            pass

        diag = (" ".join(error_messages) + " " + stderr_text).lower()

        if any(x in diag for x in ["usage limit", "hit your limit", "rate limit", "quota"]):
            reset_at = None
            m = re.search(r"(?:resets |\|)(\d{10,})", diag)
            if m:
                reset_at = datetime.fromtimestamp(int(m.group(1)), tz=timezone.utc)
            return Classified(Outcome.QUOTA, reply=reply, reset_at=reset_at, detail=diag)

        if any(x in diag for x in ["invalid api key", "please run /login", "not logged in", "authentication"]):
            return Classified(Outcome.AUTH, reply=reply, detail=diag)

        if any(x in diag for x in ["model", "not found", "invalid model"]) and (
            "not found" in diag or "invalid model" in diag
        ):
            return Classified(Outcome.MODEL_UNAVAILABLE, reply=reply, detail=diag)

        if "print timeout after" in diag and "returning partial output" in diag:
            return Classified(Outcome.TIMEOUT, reply=reply, detail=diag)

        if rc != 0:
            return Classified(Outcome.FAILED, reply=reply, detail=diag)

        if not reply:
            return Classified(Outcome.EMPTY, reply=reply, detail=diag)

        return Classified(Outcome.OK, reply=reply, detail=diag)


ADAPTER = ClaudeAdapter
