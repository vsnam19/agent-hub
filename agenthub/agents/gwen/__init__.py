import json
import os
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

from agenthub.agents import Adapter, Classified, Invocation, Outcome, Request, common_stderr_text

DEFAULT_BASE_URL = "http://100.86.30.90:8642"
DEFAULT_KEY_FILE = "~/.config/gwen/api-key"
PREFLIGHT_TIMEOUT_S = 3.0


def _settings() -> tuple[str, Path]:
    cfg_dir = os.environ.get("AGENT_HUB_CONFIG")
    path = Path(cfg_dir) / "gwen.toml" if cfg_dir else Path("~/.config/agent-hub/gwen.toml").expanduser()
    data: dict = {}
    if path.is_file():
        with path.open("rb") as f:
            data = tomllib.load(f)
    base = str(data.get("base_url", DEFAULT_BASE_URL)).rstrip("/")
    key_file = Path(str(data.get("api_key_file", DEFAULT_KEY_FILE))).expanduser()
    return base, key_file


def _read_key(key_file: Path) -> str | None:
    try:
        lines = key_file.read_text().strip().splitlines()
    except OSError:
        return None
    return lines[0].strip() if lines and lines[0].strip() else None


def _unavailable(detail: str) -> Classified:
    return Classified(Outcome.MODEL_UNAVAILABLE, reply="", detail=detail)


class GwenAdapter(Adapter):
    def preflight(self, req: Request) -> Classified | None:
        if req.role == "review":
            return _unavailable("gwen does not review")
        base, key_file = _settings()
        key = _read_key(key_file)
        if key is None:
            return _unavailable(f"gwen api key missing or empty: {key_file}")
        http_req = urllib.request.Request(f"{base}/v1/models", headers={"Authorization": f"Bearer {key}"})
        try:
            with urllib.request.urlopen(http_req, timeout=PREFLIGHT_TIMEOUT_S) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            ids = [m["id"] for m in data["data"]]
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return _unavailable(f"gwen api key rejected (HTTP {e.code})")
            return _unavailable(f"gwen host unreachable: HTTP {e.code}")
        except (OSError, ValueError, KeyError, TypeError) as e:
            return _unavailable(f"gwen host unreachable: {e}")
        if req.model not in ids:
            return _unavailable(f"gwen serves {', '.join(ids) or 'nothing'}, not {req.model}")
        return None

    def build(self, req: Request) -> Invocation:
        base, key_file = _settings()
        key = _read_key(key_file) or ""
        config = {
            "provider": {
                "gwen": {
                    "npm": "@ai-sdk/openai-compatible",
                    "options": {"baseURL": f"{base}/v1", "apiKey": "{env:GWEN_API_KEY}"},
                    "models": {req.model: {}},
                }
            }
        }
        cfg_path = req.attempt_dir / "opencode.json"
        fd = os.open(cfg_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(config, f, indent=2)
        os.chmod(cfg_path, 0o600)
        text = req.brief_path.read_text()
        return Invocation(
            argv=[self.config.binary, "run", "--model", f"gwen/{req.model}", text],
            stdin_path=None,
            env={"OPENCODE_CONFIG": str(cfg_path), "GWEN_API_KEY": key},
        )

    def classify(self, rc: int, attempt_dir: Path) -> Classified:
        stdout_file = attempt_dir / "stdout"
        reply = stdout_file.read_text(errors="replace") if stdout_file.is_file() else ""
        stderr_text = common_stderr_text(attempt_dir)
        diag = (stderr_text + "\n" + reply).lower()

        if "maximum context length" in diag:
            return Classified(Outcome.FAILED, reply=reply, detail="context exceeded")
        if any(x in diag for x in ("connection refused", "econnrefused", "fetch failed")):
            return Classified(Outcome.MODEL_UNAVAILABLE, reply=reply, detail=stderr_text.strip()[-500:])
        if rc != 0:
            return Classified(Outcome.FAILED, reply=reply, detail=stderr_text.strip()[-500:])
        if not reply.strip():
            return Classified(Outcome.EMPTY, reply=reply)
        return Classified(Outcome.OK, reply=reply)


ADAPTER = GwenAdapter
