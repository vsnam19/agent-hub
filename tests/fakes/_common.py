import json
import os
import sys
from pathlib import Path


def record_call(name: str) -> str:
    if "--version" in sys.argv:
        print(f"{name} 9.9.9")
        sys.exit(0)

    fakelog = Path(os.environ["FAKE_LOG_DIR"])
    count_file = fakelog / f"{name}.count"

    count = 1
    if count_file.exists():
        count = int(count_file.read_text()) + 1
    count_file.write_text(str(count))

    stdin_content = ""
    if not sys.stdin.isatty():
        stdin_content = sys.stdin.read()

    env_keys = ["CLAUDECODE", "GH_TOKEN", "HOME", "PATH"]
    env_recorded = {k: os.environ[k] for k in env_keys if k in os.environ}

    call_data = {
        "argv": sys.argv,
        "stdin": stdin_content,
        "cwd": os.getcwd(),
        "env": env_recorded,
    }

    log_file = fakelog / f"{name}.{count}.json"
    log_file.write_text(json.dumps(call_data))

    scenarios = os.environ.get(f"FAKE_{name.upper()}_SCENARIO", "ok").split(",")
    scenario_idx = min(count - 1, len(scenarios) - 1)
    return scenarios[scenario_idx]
