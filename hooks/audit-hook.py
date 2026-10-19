#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agenthub.audit.normalize import normalize  # noqa: E402
from agenthub.audit.writer import record_pending, write  # noqa: E402


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_args(argv: list[str]) -> tuple[str, str]:
    host = ""
    event_name = ""
    i = 0
    n = len(argv)
    while i < n:
        arg = argv[i]
        if arg == "--host":
            if i + 1 < n:
                host = argv[i + 1]
                i += 2
                continue
            i += 1
            continue
        if arg.startswith("--host="):
            host = arg.split("=", 1)[1]
            i += 1
            continue
        if arg == "--event":
            if i + 1 < n:
                event_name = argv[i + 1]
                i += 2
                continue
            i += 1
            continue
        if arg.startswith("--event="):
            event_name = arg.split("=", 1)[1]
            i += 1
            continue
        if arg.startswith("-"):
            i += 1
            continue
        if not event_name:
            event_name = arg
        i += 1
    return host.strip().lower(), event_name.strip()


class HookTimeout(Exception):
    def __init__(self, message: str = "hook timeout") -> None:
        super().__init__(message)


_timed_out = False
_acked = False


def _alarm_handler(_signum: int, _frame: Any) -> None:
    global _timed_out
    _timed_out = True
    raise HookTimeout("hook timeout")


def main(argv: list[str] | None = None) -> int:
    global _timed_out, _acked
    _timed_out = False
    _acked = False
    host = ""
    events: list[dict[str, Any]] = []
    i = 0
    try:
        raw_argv = sys.argv[1:] if argv is None else argv
        host, event_name = _parse_args(raw_argv)

        if hasattr(signal, "SIGALRM") and hasattr(signal, "alarm"):
            signal.signal(signal.SIGALRM, _alarm_handler)
            signal.alarm(2)

        if host not in ("claude", "codex", "agy"):
            return 0

        raw_stdin = sys.stdin.read()
        if not raw_stdin or not raw_stdin.strip():
            return 0

        payload = json.loads(raw_stdin)
        if not isinstance(payload, dict):
            return 0

        events = normalize(host, event_name, payload, os.environ)
        while i < len(events):
            ev = events[i]
            if not ev.get("ts"):
                ev["ts"] = _utc_now_iso()
            if _timed_out:
                record_pending(ev, "hook timeout")
                i += 1
                continue
            try:
                write(ev)
            except HookTimeout:
                _timed_out = True
                record_pending(ev, "hook timeout")
            i += 1
    except HookTimeout:
        _timed_out = True
        try:
            if not events:
                # timed out before normalize() returned: keep one trace of the lost call
                record_pending(
                    {"kind": "tool", "actor": {"host": host}, "target": "", "ts": _utc_now_iso()},
                    "hook timeout before normalize",
                )
            for ev in events[i:]:
                if not ev.get("ts"):
                    ev["ts"] = _utc_now_iso()
                record_pending(ev, "hook timeout")
        except Exception:
            pass
        return 0
    except BaseException:
        return 0
    finally:
        try:
            if hasattr(signal, "alarm"):
                signal.alarm(0)
        except BaseException:
            pass
        if host == "agy":
            try:
                sys.stdout.write("{}")
                sys.stdout.flush()
                _acked = True
            except BaseException:
                pass
    return 0


def _safe_main() -> int:
    try:
        return main()
    except BaseException:
        # an alarm that fires after main()'s own handlers: still ack agy and exit 0
        try:
            if not _acked and _parse_args(sys.argv[1:])[0] == "agy":
                sys.stdout.write("{}")
                sys.stdout.flush()
        except BaseException:
            pass
        return 0


if __name__ == "__main__":
    raise SystemExit(_safe_main())
