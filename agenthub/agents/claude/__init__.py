import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agenthub.agents import (
    Adapter,
    Classified,
    Invocation,
    Outcome,
    Request,
    common_stderr_text,
    is_network_failure,
)

WORKER_PROMPT = (
    "You are a non-interactive worker run by agent-hub. Write in English. "
    "Do not run commands in the background and do not wait for notifications: "
    "run every command in the foreground until it finishes. "
    "Your final message is parsed by a program: end with the handoff exactly as the brief asks."
)

SESSION_LIMIT_RE = re.compile(r"hit your (?:session|weekly|usage)? ?limit", re.IGNORECASE)
# "resets 12:20pm (Asia/Ho_Chi_Minh)" or "resets Oct 14, 9am (Asia/Ho_Chi_Minh)"
RESET_RE = re.compile(
    r"resets (?:(?P<mon>[A-Za-z]{3,9}) (?P<day>\d{1,2}), )?(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>am|pm)"
    r" \((?P<zone>[^)]+)\)",
    re.IGNORECASE,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _reset_from_text(text: str) -> datetime | None:
    """Next wall-clock time named by a session-limit message, as UTC; None if unknown."""
    m = RESET_RE.search(text)
    if not m:
        return None
    try:
        zone = ZoneInfo(m["zone"])
    except (ZoneInfoNotFoundError, ValueError):
        return None
    hour, minute = int(m["h"]), int(m["m"] or 0)
    if not (1 <= hour <= 12 and minute <= 59):
        return None
    hour = hour % 12 + (12 if m["ap"].lower() == "pm" else 0)

    now = _now()
    local_now = now.astimezone(zone)
    try:
        if m["mon"]:
            month = datetime.strptime(m["mon"][:3], "%b").month
            candidate = datetime(local_now.year, month, int(m["day"]), hour, minute, tzinfo=zone)
            if candidate.astimezone(timezone.utc) < now:
                candidate = candidate.replace(year=local_now.year + 1)
        else:
            candidate = datetime(local_now.year, local_now.month, local_now.day, hour, minute, tzinfo=zone)
            if candidate.astimezone(timezone.utc) < now:
                candidate += timedelta(days=1)
    except ValueError:
        return None
    return candidate.astimezone(timezone.utc)


class ClaudeAdapter(Adapter):
    def list_models(self) -> list[str] | None:
        # Claude Code has no list command; the catalog is the static alias list in agent.toml.
        return list(self.config.known_models) or None

    def build(self, req: Request) -> Invocation:
        argv = [
            self.config.binary,
            "-p",
            "--model",
            req.model,
            "--output-format",
            "json",
            "--no-session-persistence",
            "--setting-sources",
            "project,local",
            "--append-system-prompt",
            WORKER_PROMPT,
        ]

        if req.role == "implement":
            argv.append("--permission-mode")
            argv.append("bypassPermissions")
        else:
            # --tools removes every other built-in tool from the session (no Bash, so no shell at all).
            # --allowedTools only pre-approves, and Claude Code still auto-allows read-only Bash without
            # --tools, so it is not enough on its own.
            # Skill is kept on purpose: --plugin-dir entries provide skills the reviewer must be able to load.
            argv.append("--tools")
            argv.append("Read,Grep,Glob,Skill")
            argv.append("--allowedTools")
            argv.append("Read,Grep,Glob,Skill")
            # --tools only limits built-in tools. With no --mcp-config, this loads no MCP server,
            # so project .mcp.json and claude.ai connectors cannot add tools to the reviewer.
            argv.append("--strict-mcp-config")
            argv.append("--add-dir")
            argv.append(str(req.brief_path.parent))

        if req.effort:
            argv.append("--effort")
            argv.append(req.effort)

        for plugin_dir in req.plugin_dirs:
            argv.append("--plugin-dir")
            argv.append(str(plugin_dir))

        return Invocation(argv=argv, stdin_path=req.brief_path)

    def classify(self, rc: int, attempt_dir: Path) -> Classified:
        stdout_file = attempt_dir / "stdout"
        stderr_text = common_stderr_text(attempt_dir)

        stdout_text = stdout_file.read_text() if stdout_file.is_file() else ""

        reply = stdout_text
        error_messages = []
        limit_text = ""
        api_status = None
        is_error = False
        try:
            data = json.loads(stdout_text)
            reply = data.get("result", "")
            is_error = bool(data.get("is_error"))
            if is_error:
                api_status = data.get("api_error_status")
                if "subtype" in data:
                    error_messages.append(data["subtype"])
                if "result" in data:
                    error_messages.append(data["result"])
                    limit_text = str(data["result"])
        except Exception:
            pass

        diag = (" ".join(error_messages) + " " + stderr_text).lower()

        session_limit = api_status == 429 or bool(SESSION_LIMIT_RE.search(limit_text))
        if session_limit or any(x in diag for x in ["usage limit", "hit your limit", "rate limit", "quota"]):
            reset_at = None
            m = re.search(r"(?:resets |\|)(\d{10,})", diag)
            if m:
                reset_at = datetime.fromtimestamp(int(m.group(1)), tz=timezone.utc)
            else:
                reset_at = _reset_from_text(limit_text)
            return Classified(Outcome.QUOTA, reply=reply, reset_at=reset_at, detail=diag)

        if (rc != 0 or is_error) and is_network_failure(diag):
            return Classified(Outcome.TRANSIENT, reply=reply, detail=diag)

        if any(x in diag for x in ["invalid api key", "please run /login", "not logged in", "authentication"]):
            return Classified(Outcome.AUTH, reply=reply, detail=diag)

        if re.search(r"model\b.{0,60}\b(not found|does not exist)|invalid model", diag):
            return Classified(Outcome.MODEL_UNAVAILABLE, reply=reply, detail=diag)

        if "print timeout after" in diag and "returning partial output" in diag:
            return Classified(Outcome.TIMEOUT, reply=reply, detail=diag)

        if rc != 0:
            return Classified(Outcome.FAILED, reply=reply, detail=diag)

        if not reply:
            return Classified(Outcome.EMPTY, reply=reply, detail=diag)

        return Classified(Outcome.OK, reply=reply, detail=diag)


ADAPTER = ClaudeAdapter
