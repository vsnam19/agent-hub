from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import time
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agenthub.config import paths

ZERO_HASH = "0" * 64

_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
_DAY_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.jsonl$")
_MANIFEST_FILE_RE = re.compile(r"^(\d{4}-\d{2})\.json$")
_LOCK_RETRY_INTERVAL_S = 0.005
_LOCK_MAX_RETRIES = 40
_SEEK_CHUNK_BYTES = 4096


class AuditLockTimeout(Exception):
    pass


def audit_root(state_dir: Path | str | None = None) -> Path:
    base = paths().state_dir if state_dir is None else Path(state_dir)
    return base / "audit"


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def event_hash(prev_hash: str, event: Mapping[str, Any]) -> str:
    body = {k: v for k, v in event.items() if k != "hash"}
    payload = (prev_hash + canonical_json(body)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _day_from_ts(ts: str) -> str:
    if not isinstance(ts, str) or not ts:
        raise ValueError(f"invalid event ts: {ts!r}")
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%d")


def _ensure_dir_0700(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def _acquire_lock(lock_path: Path) -> int:
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(fd, 0o600)
        for attempt in range(_LOCK_MAX_RETRIES + 1):
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                if attempt == _LOCK_MAX_RETRIES:
                    break
                time.sleep(_LOCK_RETRY_INTERVAL_S)
    except BaseException:
        os.close(fd)
        raise

    os.close(fd)
    raise AuditLockTimeout(f"timed out acquiring audit lock: {lock_path}")


def _read_last_event(day_file: Path) -> dict[str, Any] | None:
    if not day_file.is_file():
        return None
    with day_file.open("rb") as f:
        pos = f.seek(0, os.SEEK_END)
        if pos == 0:
            return None
        buf = b""
        while pos > 0:
            read_len = min(_SEEK_CHUNK_BYTES, pos)
            pos -= read_len
            f.seek(pos, os.SEEK_SET)
            buf = f.read(read_len) + buf
            stripped = buf.rstrip(b"\r\n")
            if b"\n" in stripped:
                last_line = stripped.rsplit(b"\n", 1)[-1]
                return json.loads(last_line.decode("utf-8"))
        stripped = buf.rstrip(b"\r\n")
        if not stripped:
            return None
        last_line = stripped.rsplit(b"\n", 1)[-1]
        return json.loads(last_line.decode("utf-8"))


def _latest_earlier_day_hash(root: Path, month: str, day: str) -> tuple[str, str] | None:
    if not root.is_dir():
        return None
    month_dirs = sorted(
        (p for p in root.iterdir() if p.is_dir() and _MONTH_RE.match(p.name) and p.name <= month),
        key=lambda p: p.name,
        reverse=True,
    )
    for m_dir in month_dirs:
        candidates: list[tuple[str, Path]] = []
        for entry in m_dir.iterdir():
            m = _DAY_FILE_RE.match(entry.name)
            if m and m.group(1) < day and entry.is_file():
                candidates.append((m.group(1), entry))
        candidates.sort(key=lambda item: item[0], reverse=True)
        for day_name, path in candidates:
            last_ev = _read_last_event(path)
            if last_ev is not None:
                return day_name[:7], str(last_ev["hash"])
    return None


def _latest_manifest_hash(root: Path, month: str) -> tuple[str, str] | None:
    manifests_dir = root / "manifests"
    if not manifests_dir.is_dir():
        return None
    candidates: list[tuple[str, Path]] = []
    for entry in manifests_dir.iterdir():
        m = _MANIFEST_FILE_RE.match(entry.name)
        if m and m.group(1) < month and entry.is_file():
            candidates.append((m.group(1), entry))
    candidates.sort(key=lambda item: item[0], reverse=True)
    for m_name, path in candidates:
        data = json.loads(path.read_text(encoding="utf-8"))
        last_hash = data.get("last_hash")
        if isinstance(last_hash, str) and last_hash:
            return m_name, last_hash
    return None


def _initial_prev_hash(root: Path, month: str, day: str) -> str:
    day_hit = _latest_earlier_day_hash(root, month, day)
    manifest_hit = _latest_manifest_hash(root, month)
    if day_hit and manifest_hit:
        return day_hit[1] if day_hit[0] >= manifest_hit[0] else manifest_hit[1]
    if day_hit:
        return day_hit[1]
    if manifest_hit:
        return manifest_hit[1]
    return ZERO_HASH


def append(event: Mapping[str, Any], state_dir: Path | str | None = None) -> dict[str, Any]:
    record: dict[str, Any] = dict(event)
    record["schema_version"] = 1
    if "ts" not in record or record["ts"] is None:
        record["ts"] = _utc_now_iso()
    if "event_id" not in record or record["event_id"] is None:
        record["event_id"] = uuid.uuid4().hex

    day = _day_from_ts(record["ts"])
    month = day[:7]

    root = audit_root(state_dir)
    month_dir = root / month
    _ensure_dir_0700(root)
    _ensure_dir_0700(month_dir)

    day_file = month_dir / f"{day}.jsonl"
    lock_file = month_dir / f"{day}.jsonl.lock"

    lock_fd = _acquire_lock(lock_file)
    try:
        last_ev = _read_last_event(day_file)
        if last_ev is not None:
            seq = int(last_ev["seq"]) + 1
            prev_hash = str(last_ev["hash"])
        else:
            seq = 1
            prev_hash = _initial_prev_hash(root, month, day)

        record["seq"] = seq
        record["prev_hash"] = prev_hash
        record["hash"] = event_hash(prev_hash, record)

        line_bytes = (canonical_json(record) + "\n").encode("utf-8")
        fd = os.open(day_file, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, line_bytes)
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)

    return record


def _pending_gap_path(state_dir: Path | str | None = None) -> Path:
    base = paths().state_dir if state_dir is None else Path(state_dir)
    return base / "audit-pending-gap.jsonl"


def _read_pending_gap_entries(pending_path: Path) -> list[dict[str, Any]]:
    if not pending_path.is_file():
        return []
    try:
        lines = pending_path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    entries: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
            if isinstance(item, dict):
                entries.append(item)
        except Exception:
            continue
    return entries


def _record_pending_failure(event: Mapping[str, Any], exc: Exception, state_dir: Path | str | None = None) -> None:
    try:
        base = paths().state_dir if state_dir is None else Path(state_dir)
        try:
            base.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        path = base / "audit-pending-gap.jsonl"
        event_ts = event.get("ts") if isinstance(event, Mapping) else None
        ts = str(event_ts) if event_ts else _utc_now_iso()
        reason = str(exc) or type(exc).__name__
        line = canonical_json({"ts": ts, "reason": reason}) + "\n"
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, line.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
    except Exception:
        pass


def write(event: Mapping[str, Any], state_dir: Path | str | None = None) -> bool:
    try:
        pending_path = _pending_gap_path(state_dir)
        if pending_path.is_file():
            entries = _read_pending_gap_entries(pending_path)
            if entries:
                first_ts = entries[0].get("ts", "")
                first_reason = entries[0].get("reason", "")
                lost_count = len(entries)
                now = _utc_now_iso()
                gap_event = {
                    "ts": now,
                    "kind": "gap",
                    "actor": {"role": "agent-hub", "host": "agent-hub"},
                    "target": "",
                    "outcome": {"status": "ok"},
                    "detail": {
                        "from": first_ts,
                        "to": now,
                        "reason": first_reason,
                        "lost": lost_count,
                    },
                }
                append(gap_event, state_dir=state_dir)
            pending_path.unlink(missing_ok=True)

        append(event, state_dir=state_dir)
        return True
    except Exception as exc:
        _record_pending_failure(event, exc, state_dir=state_dir)
        return False
