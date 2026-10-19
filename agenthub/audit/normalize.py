from __future__ import annotations

import hashlib
import os
import stat
import time
from collections.abc import Mapping
from typing import Any

from agenthub.audit.classify import classify
from agenthub.audit.trace import trace_context

_SCRUB_SUBSTRINGS = ("thinking", "reasoning", "signature")
_SCRUB_EXACT_KEYS = {"transcript_path", "transcriptPath"}
_WHITELISTED_TOOL_INPUT_KEYS = (
    "command",
    "description",
    "cwd",
    "file_path",
    "notebook_path",
    "path",
    "url",
    "Url",
    "domain",
)
_HASH_CHUNK_BYTES = 65536


def _should_drop_key(key: Any) -> bool:
    key_str = str(key)
    if key_str in _SCRUB_EXACT_KEYS:
        return True
    key_lower = key_str.lower()
    if key_lower in ("transcript_path", "transcriptpath"):
        return True
    return any(sub in key_lower for sub in _SCRUB_SUBSTRINGS)


def scrub(payload: Any) -> Any:
    if isinstance(payload, Mapping):
        return {k: scrub(v) for k, v in payload.items() if not _should_drop_key(k)}
    if isinstance(payload, list):
        return [scrub(item) for item in payload]
    if isinstance(payload, tuple):
        return tuple(scrub(item) for item in payload)
    return payload


def file_sha256(path: str | os.PathLike[str], budget_ms: float = 50) -> str | None:
    if not path or budget_ms <= 0:
        return None
    deadline = time.monotonic() + (budget_ms / 1000.0)
    try:
        st = os.stat(path)
        if not stat.S_ISREG(st.st_mode):
            return None
        if time.monotonic() > deadline:
            return None
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while True:
                if time.monotonic() > deadline:
                    return None
                chunk = f.read(_HASH_CHUNK_BYTES)
                if not chunk:
                    break
                h.update(chunk)
        if time.monotonic() > deadline:
            return None
        return h.hexdigest()
    except (OSError, ValueError, TypeError):
        return None


def _extract_error_text(clean_payload: Mapping[str, Any]) -> str | None:
    err = clean_payload.get("error")
    if isinstance(err, str) and err:
        return err
    if isinstance(err, Mapping):
        for k in ("stderr", "message", "error"):
            val = err.get(k)
            if isinstance(val, str) and val:
                return val
    stderr = clean_payload.get("stderr")
    if isinstance(stderr, str) and stderr:
        return stderr
    resp = clean_payload.get("tool_response")
    if isinstance(resp, Mapping):
        for k in ("stderr", "error"):
            val = resp.get(k)
            if isinstance(val, str) and val:
                return val
    return None


def normalize(
    host: str,
    event_name: str,
    payload: Mapping[str, Any] | None,
    env: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if (host or "").strip().lower() != "claude":
        return []
    if not isinstance(payload, Mapping):
        return []

    clean = scrub(payload)

    session_id = clean.get("session_id")
    cwd = clean.get("cwd")
    ev_name = event_name if (isinstance(event_name, str) and event_name) else clean.get("hook_event_name")
    model = clean.get("model")

    trace = trace_context(env)
    actor: dict[str, Any] = {
        "role": trace.actor_role,
        "host": "claude",
    }
    if isinstance(session_id, str) and session_id:
        actor["session_id"] = session_id
    if isinstance(model, str) and model.strip():
        m = model.strip()
        actor["agent"] = m if m.startswith("claude:") else f"claude:{m}"
    if trace.pool_role is not None:
        actor["pool_role"] = trace.pool_role

    parent: dict[str, Any] = dict(trace.parent)

    if ev_name in ("SessionStart", "Stop"):
        session_ev = "start" if ev_name == "SessionStart" else "stop"
        return [
            {
                "actor": dict(actor),
                "parent": dict(parent),
                "kind": "session",
                "target": session_ev,
                "detail": {"event": session_ev},
                "outcome": {"status": "ok"},
            }
        ]

    if ev_name not in ("PostToolUse", "PostToolUseFailure"):
        return []

    tool_name = clean.get("tool_name")
    if not isinstance(tool_name, str) or not tool_name:
        return []

    raw_tool_input = clean.get("tool_input")
    tool_input: dict[str, Any] = {}
    if isinstance(raw_tool_input, Mapping):
        for k in _WHITELISTED_TOOL_INPUT_KEYS:
            if k in raw_tool_input:
                tool_input[k] = raw_tool_input[k]
    if "cwd" not in tool_input and isinstance(cwd, str) and cwd:
        tool_input["cwd"] = cwd

    classified_items = classify("claude", tool_name, tool_input)
    status = "ok" if ev_name == "PostToolUse" else "error"
    description = tool_input.get("description")
    quote = description if (isinstance(description, str) and description) else None
    error_text = _extract_error_text(clean) if ev_name == "PostToolUseFailure" else None

    events: list[dict[str, Any]] = []
    for item in classified_items:
        detail = dict(item.detail)
        if item.kind == "command" and ev_name == "PostToolUseFailure" and error_text:
            detail["stderr_tail"] = error_text[-2000:]
        if item.kind in ("file_read", "file_write"):
            detail.pop("sha256_before", None)
            file_path = detail.get("path") or item.target
            if isinstance(file_path, str) and file_path:
                sha = file_sha256(file_path)
                if sha is None and isinstance(cwd, str) and cwd and not os.path.isabs(file_path):
                    sha = file_sha256(os.path.join(cwd, file_path))
                if sha is not None:
                    detail["sha256_after"] = sha

        ev: dict[str, Any] = {
            "actor": dict(actor),
            "parent": dict(parent),
            "kind": item.kind,
            "target": item.target,
            "detail": detail,
            "outcome": {"status": status},
        }
        if quote is not None:
            ev["quote"] = quote
        events.append(ev)

    return events
