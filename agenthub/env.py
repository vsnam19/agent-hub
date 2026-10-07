import re
from typing import Iterable, Mapping

SECRET_NAME = re.compile(r"(?i)(TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|PRIVATE_?KEY|CREDENTIAL)")
SECRET_PREFIXES = ("FNOX_", "AWS_", "GH_", "GITHUB_")


def child_env(base: Mapping[str, str], passthrough: Iterable[str] = ()) -> tuple[dict[str, str], list[str]]:
    passthrough_set = set(passthrough)
    filtered = {}
    removed = []

    for k, v in base.items():
        if k == "CLAUDECODE" or k.startswith("CLAUDE_CODE_"):
            removed.append(k)
            continue

        if k in passthrough_set:
            filtered[k] = v
            continue

        if SECRET_NAME.search(k) or any(k.startswith(p) for p in SECRET_PREFIXES):
            removed.append(k)
        else:
            filtered[k] = v

    return filtered, sorted(removed)
