import json
import os
import shutil
import subprocess
import tempfile

from agenthub import env

JEV_TIMEOUT_S = 600


def classify_reply(text: str) -> dict | None:
    bin_path = os.environ.get("AGENT_HUB_JEV_BIN")
    if not bin_path:
        bin_path = shutil.which("jev-code")
    if not bin_path:
        return None

    payload = {
        "instructions": "Classify the assistant's reply.",
        "items": [{"id": "reply", "text": text}],
        "classes": {
            "success": "says the task is complete as asked",
            "partial": "says some of the task is done but something requested is unfinished or failing",
            "blocked": (
                "says it could not do the task (missing input, environment "
                "or permission problem) and made no or almost no changes"
            ),
        },
    }

    child_env, _ = env.child_env(os.environ, ())
    child_env["JEV_CODE_PROVIDER"] = "ollama"
    child_env["JEV_CODE_TIMEOUT_MS"] = "600000"

    with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
        json.dump(payload, f)
        temp_path = f.name

    try:
        res = subprocess.run(
            [bin_path, "classify", "--input", temp_path],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            env=child_env,
            timeout=JEV_TIMEOUT_S,
        )
        if res.returncode != 0:
            return None

        out = json.loads(res.stdout)
        results = out.get("results", [])
        if not results:
            return None

        r = results[0]
        return {
            "label": r.get("label"),
            "probability": r.get("probability"),
            "decision": r.get("decision"),
            "model": out.get("model"),
        }
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return None
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
