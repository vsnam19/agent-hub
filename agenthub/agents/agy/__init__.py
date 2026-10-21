import functools
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
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


@functools.cache
def _secret_service_running() -> bool:
    """True when a Secret Service (keyring) owns its name on the session bus."""
    try:
        result = subprocess.run(
            [
                "dbus-send",
                "--session",
                "--print-reply",
                "--dest=org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus.NameHasOwner",
                "string:org.freedesktop.secrets",
            ],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except Exception:
        return False
    return result.returncode == 0 and "boolean true" in result.stdout


def _keyring_env(
    environ: Mapping[str, str], secret_service: Callable[[], bool] = _secret_service_running
) -> dict[str, str] | None:
    """agy uses file token storage under SSH; blank SSH vars send it to the keyring when one runs.
    Override with AGENTHUB_AGY_KEYRING=1 (force blank) or 0 (never blank)."""
    # SSH vars are blanked, not removed, because Invocation.env cannot unset a variable —
    # this relies on agy's truthiness check and breaks if agy switches to a presence check;
    # and that the probe result is per call/process as it is.
    forced = environ.get("AGENTHUB_AGY_KEYRING")
    if forced == "1":
        return {"SSH_CONNECTION": "", "SSH_CLIENT": "", "SSH_TTY": ""}
    if forced == "0":
        return None
    if environ.get("SSH_CONNECTION") and secret_service():
        return {"SSH_CONNECTION": "", "SSH_CLIENT": "", "SSH_TTY": ""}
    return None


class AgyAdapter(Adapter):
    def list_models(self) -> list[str] | None:
        if not self.installed():
            self.models_error = "not installed"
            return None

        output = self._catalog_output(
            ["models"],
            drop_env=("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"),
        )
        if output is None:
            return None

        models: list[str] = []
        for line in output.splitlines():
            if "\t" in line:
                model_id = line.split("\t", 1)[0].strip()
                if model_id:
                    models.append(model_id)

        if not models:
            self.models_error = "no models listed"
            return None

        self.models_error = None
        return models

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
        ]
        if req.role == "review":
            # the reviewer reads diff.patch from the job directory; the implementer must not get it
            argv += ["--add-dir", str(req.brief_path.parent)]

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

        return Invocation(
            argv=argv, stdin_path=None, env=_keyring_env(os.environ, secret_service=_secret_service_running)
        )

    def classify(self, rc: int, attempt_dir: Path) -> Classified:
        stdout_file = attempt_dir / "stdout"
        stderr_text = common_stderr_text(attempt_dir)

        stdout_text = stdout_file.read_text() if stdout_file.is_file() else ""

        reply = stdout_text
        error_field = ""
        denied = []
        try:
            data = json.loads(stdout_text, strict=False)
            reply = data.get("response", "")
            error_field = data.get("error", "")
            denied = data.get("denied_actions") or []
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

        if any(
            x in diag
            for x in [
                "unauthenticated",
                "unauthorized",
                "sign in",
                "please authenticate",
                "reauth",
                "authentication failed or timed out",
                "authentication timed out",
                "not logged into antigravity",
            ]
        ):
            return Classified(Outcome.AUTH, reply=reply, detail=diag)

        if any(x in diag for x in ["not a known model", "is not recognized as a known model", "invalid --model"]):
            return Classified(Outcome.MODEL_UNAVAILABLE, reply=reply, detail=diag)

        if "print timeout after" in diag and "returning partial output" in diag:
            return Classified(Outcome.TIMEOUT, reply=reply, detail=diag)

        # headless mode auto-denies tools that need a permission; a reviewer with no reply and a denial is a failure
        if not reply and (denied or "auto-denied" in diag):
            names = [str(a.get("display_name")) for a in denied if isinstance(a, dict) and a.get("display_name")]
            detail = "permission auto-denied: " + (", ".join(names) or "unknown")
            return Classified(Outcome.FAILED, reply=reply, detail=detail)

        if rc != 0 and is_network_failure(diag):
            return Classified(Outcome.TRANSIENT, reply=reply, detail=diag)

        if rc != 0:
            return Classified(Outcome.FAILED, reply=reply, detail=diag)

        if not reply:
            return Classified(Outcome.EMPTY, reply=reply, detail=diag)

        return Classified(Outcome.OK, reply=reply, detail=diag)


ADAPTER = AgyAdapter
