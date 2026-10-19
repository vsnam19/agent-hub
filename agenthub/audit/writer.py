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
from agenthub.redact import redact, redact_argv

ZERO_HASH = "0" * 64

_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
_DAY_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.jsonl$")
_MANIFEST_FILE_RE = re.compile(r"^(\d{4}-\d{2})\.json$")
_LOCK_RETRY_INTERVAL_S = 0.005
_LOCK_DEADLINE_S = 0.2
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


def _acquire_lock(lock_path: Path, deadline: float) -> int:
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(fd, 0o600)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(_LOCK_RETRY_INTERVAL_S, remaining))
    except BaseException:
        os.close(fd)
        raise

    os.close(fd)
    raise AuditLockTimeout(f"timed out acquiring audit lock: {lock_path}")


def _release_lock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


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


def _read_chain_tail(root: Path) -> tuple[dict[str, Any] | None, str, str]:
    """Return (last event, its day, hash to link to) for the whole chain."""
    month_dirs = sorted(
        (p for p in root.iterdir() if p.is_dir() and _MONTH_RE.match(p.name)),
        key=lambda p: p.name,
        reverse=True,
    )
    for m_dir in month_dirs:
        candidates: list[tuple[str, Path]] = []
        for entry in m_dir.iterdir():
            m = _DAY_FILE_RE.match(entry.name)
            if m and entry.is_file():
                candidates.append((m.group(1), entry))
        candidates.sort(key=lambda item: item[0], reverse=True)
        for day_name, path in candidates:
            last_ev = _read_last_event(path)
            if last_ev is not None:
                return last_ev, day_name, str(last_ev["hash"])
    manifests_dir = root / "manifests"
    if manifests_dir.is_dir():
        manifests: list[tuple[str, Path]] = []
        for entry in manifests_dir.iterdir():
            m = _MANIFEST_FILE_RE.match(entry.name)
            if m and entry.is_file():
                manifests.append((m.group(1), entry))
        manifests.sort(key=lambda item: item[0], reverse=True)
        for _, path in manifests:
            last_hash = json.loads(path.read_text(encoding="utf-8")).get("last_hash")
            if isinstance(last_hash, str) and last_hash:
                return None, "", last_hash
    return None, "", ZERO_HASH


def _parse_ts(ts: Any) -> datetime | None:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _sanitize_nested(v: Any) -> Any:
    if isinstance(v, str):
        return redact(v)
    if isinstance(v, Mapping):
        return {k: _sanitize_nested(val) for k, val in v.items()}
    if isinstance(v, list):
        return [_sanitize_nested(item) for item in v]
    if isinstance(v, tuple):
        return tuple(_sanitize_nested(item) for item in v)
    if isinstance(v, set):
        return {_sanitize_nested(item) for item in v}
    return v


def _sanitize_detail(detail: Mapping[str, Any]) -> dict[str, Any]:
    res: dict[str, Any] = {}
    for k, v in detail.items():
        if k == "argv" and isinstance(v, (list, tuple)):
            items = list(v)
            if all(isinstance(x, str) for x in items):
                redacted = redact_argv(items)
            else:
                redacted = [_sanitize_nested(x) for x in items]
            res[k] = type(v)(redacted) if isinstance(v, tuple) else redacted
        elif k == "stderr_tail" and isinstance(v, str):
            redacted = redact(v)
            if len(redacted) > 2000:
                redacted = redacted[-2000:]
            res[k] = redacted
        else:
            res[k] = _sanitize_nested(v)
    return res


def sanitize(event: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = dict(event)
    if "target" in out:
        target = out["target"]
        if isinstance(target, str):
            redacted_target = redact(target)
            if len(redacted_target) > 500:
                redacted_target = redacted_target[:500]
            out["target"] = redacted_target
    if "quote" in out:
        quote = out["quote"]
        if isinstance(quote, str):
            redacted_quote = redact(quote)
            if len(redacted_quote) > 200:
                redacted_quote = redacted_quote[:199] + "…"
            out["quote"] = redacted_quote
    if "detail" in out:
        detail = out["detail"]
        if isinstance(detail, Mapping):
            out["detail"] = _sanitize_detail(detail)
        elif isinstance(detail, str):
            out["detail"] = redact(detail)
    return out


def _append_locked(event: Mapping[str, Any], root: Path) -> dict[str, Any]:
    """Append one event; the caller holds the chain lock."""
    record: dict[str, Any] = sanitize(event)
    record["schema_version"] = 1
    if "event_id" not in record or record["event_id"] is None:
        record["event_id"] = uuid.uuid4().hex

    last_ev, last_day, prev_hash = _read_chain_tail(root)
    if record.get("ts") is None:
        record["ts"] = _utc_now_iso()
    elif last_ev is not None:
        new_dt = _parse_ts(record["ts"])
        last_dt = _parse_ts(last_ev.get("ts"))
        if new_dt is not None and last_dt is not None and new_dt < last_dt:
            record["event_ts"] = record["ts"]
            record["ts"] = last_ev["ts"]

    day = _day_from_ts(record["ts"])
    month_dir = root / day[:7]
    _ensure_dir_0700(month_dir)
    day_file = month_dir / f"{day}.jsonl"

    if last_ev is not None and last_day == day:
        seq = int(last_ev["seq"]) + 1
    else:
        seq = 1
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
    return record


def _locked_root(state_dir: Path | str | None, deadline: float | None) -> tuple[Path, int]:
    if deadline is None:
        deadline = time.monotonic() + _LOCK_DEADLINE_S
    root = audit_root(state_dir)
    _ensure_dir_0700(root)
    return root, _acquire_lock(root / "chain.lock", deadline)


def append(
    event: Mapping[str, Any],
    state_dir: Path | str | None = None,
    *,
    deadline: float | None = None,
) -> dict[str, Any]:
    root, lock_fd = _locked_root(state_dir, deadline)
    try:
        return _append_locked(event, root)
    finally:
        _release_lock(lock_fd)


def _pending_gap_path(state_dir: Path | str | None = None) -> Path:
    base = paths().state_dir if state_dir is None else Path(state_dir)
    return base / "audit-pending-gap.jsonl"


def _claim_pending_and_emit_gap(root: Path, state_dir: Path | str | None) -> None:
    """Move the pending file aside, report one gap for it; the caller holds the chain lock."""
    pending_path = _pending_gap_path(state_dir)
    claimed = pending_path.with_name(f"audit-pending-gap.{os.getpid()}.claimed")
    if pending_path.is_file():
        if claimed.exists():
            # leftover from a crashed claim by a process with the same pid: merge, never overwrite
            with claimed.open("ab") as out:
                out.write(pending_path.read_bytes())
            pending_path.unlink(missing_ok=True)
        else:
            os.replace(pending_path, claimed)
    if not claimed.is_file():
        return
    try:
        raw = claimed.read_bytes()
    except Exception:
        raw = b""
    entries: list[dict[str, Any]] = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except Exception:
            continue
        if isinstance(item, dict):
            entries.append(item)
    if not entries and not raw.strip():
        claimed.unlink(missing_ok=True)
        return
    now = _utc_now_iso()
    gap_event = {
        "ts": now,
        "kind": "gap",
        "actor": {"role": "agent-hub", "host": "agent-hub"},
        "target": "",
        "outcome": {"status": "ok"},
        "detail": {
            "from": entries[0].get("ts", now) if entries else now,
            "to": now,
            "reason": entries[0].get("reason", "") if entries else "unreadable pending file",
            "lost": len(entries) if entries else "unknown",
        },
    }
    try:
        _append_locked(gap_event, root)
    except BaseException:
        fd = os.open(pending_path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, raw if raw.endswith(b"\n") or not raw else raw + b"\n")
            os.fsync(fd)
        finally:
            os.close(fd)
        claimed.unlink(missing_ok=True)
        raise
    claimed.unlink(missing_ok=True)


def record_pending(event: Mapping[str, Any], reason: str, state_dir: Path | str | None = None) -> None:
    try:
        base = paths().state_dir if state_dir is None else Path(state_dir)
        try:
            base.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        path = base / "audit-pending-gap.jsonl"
        event_ts = event.get("ts") if isinstance(event, Mapping) else None
        ts = str(event_ts) if event_ts else _utc_now_iso()
        line = canonical_json({"ts": ts, "reason": str(reason)}) + "\n"
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, line.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
    except Exception:
        pass


def _record_pending_failure(event: Mapping[str, Any], exc: Exception, state_dir: Path | str | None = None) -> None:
    record_pending(event, str(exc) or type(exc).__name__, state_dir=state_dir)


def write(event: Mapping[str, Any], state_dir: Path | str | None = None) -> bool:
    try:
        deadline = time.monotonic() + _LOCK_DEADLINE_S
        root, lock_fd = _locked_root(state_dir, deadline)
        try:
            _claim_pending_and_emit_gap(root, state_dir)
            _append_locked(event, root)
        finally:
            _release_lock(lock_fd)
        return True
    except Exception as exc:
        record_pending(event, str(exc) or type(exc).__name__, state_dir=state_dir)
        return False
