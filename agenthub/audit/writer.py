from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shlex
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


def _is_valid_event(record: Any) -> bool:
    return (
        isinstance(record, dict)
        and isinstance(record.get("hash"), str)
        and bool(record["hash"])
        and isinstance(record.get("seq"), int)
        and not isinstance(record.get("seq"), bool)
    )


def _parse_valid_event(raw_line: str | bytes) -> dict[str, Any] | None:
    if isinstance(raw_line, bytes):
        try:
            raw_line = raw_line.decode("utf-8")
        except Exception:
            return None
    stripped = raw_line.strip()
    if not stripped:
        return None
    try:
        record = json.loads(stripped)
    except Exception:
        return None
    if _is_valid_event(record):
        return record
    return None


def _read_last_event(day_file: Path) -> dict[str, Any] | None:
    if not day_file.is_file():
        return None
    try:
        with day_file.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception:
        return None
    for line in reversed(lines):
        record = _parse_valid_event(line)
        if record is not None:
            return record
    return None


def _collect_day_files(root: Path) -> list[tuple[str, Path]]:
    month_dirs = sorted(
        (p for p in root.iterdir() if p.is_dir() and _MONTH_RE.match(p.name)),
        key=lambda p: p.name,
        reverse=True,
    )
    candidates: list[tuple[str, Path]] = []
    for m_dir in month_dirs:
        m_candidates: list[tuple[str, Path]] = []
        for entry in m_dir.iterdir():
            m = _DAY_FILE_RE.match(entry.name)
            if m and entry.is_file():
                m_candidates.append((m.group(1), entry))
        m_candidates.sort(key=lambda item: item[0], reverse=True)
        candidates.extend(m_candidates)
    return candidates


def _latest_manifest_hash(root: Path) -> str:
    manifests_dir = root / "manifests"
    if manifests_dir.is_dir():
        manifests: list[tuple[str, Path]] = []
        for entry in manifests_dir.iterdir():
            m = _MANIFEST_FILE_RE.match(entry.name)
            if m and entry.is_file():
                manifests.append((m.group(1), entry))
        manifests.sort(key=lambda item: item[0], reverse=True)
        for _, path in manifests:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            last_hash = data.get("last_hash")
            if isinstance(last_hash, str) and last_hash:
                return last_hash
    return ZERO_HASH


def _read_chain_tail_with_corrupt(
    root: Path,
) -> tuple[dict[str, Any] | None, str, str, list[tuple[str, int]]]:
    candidates = _collect_day_files(root)
    if not candidates:
        prev_hash = _latest_manifest_hash(root)
        return None, "", prev_hash, []

    last_day_str, last_day_path = candidates[0]
    try:
        with last_day_path.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception:
        lines = []

    if not lines:
        for day_name, path in candidates[1:]:
            ev = _read_last_event(path)
            if ev is not None:
                return ev, day_name, str(ev["hash"]), []
        prev_hash = _latest_manifest_hash(root)
        return None, "", prev_hash, []

    if ev := _parse_valid_event(lines[-1]):
        return ev, last_day_str, str(ev["hash"]), []

    # Corrupt tail: scan back to find last valid event
    corrupt_indices: list[int] = []
    valid_ev: dict[str, Any] | None = None
    for idx in range(len(lines) - 1, -1, -1):
        parsed = _parse_valid_event(lines[idx])
        if parsed is not None:
            valid_ev = parsed
            break
        corrupt_indices.append(idx)

    rel_day_file = f"{last_day_path.parent.name}/{last_day_path.name}"
    corrupt_lines = [(rel_day_file, idx + 1) for idx in reversed(corrupt_indices)]

    if valid_ev is not None:
        return valid_ev, last_day_str, str(valid_ev["hash"]), corrupt_lines

    for day_name, path in candidates[1:]:
        ev = _read_last_event(path)
        if ev is not None:
            return ev, day_name, str(ev["hash"]), corrupt_lines
    prev_hash = _latest_manifest_hash(root)
    return None, "", prev_hash, corrupt_lines


def _read_chain_tail(root: Path) -> tuple[dict[str, Any] | None, str, str]:
    """Return (last event, its day, hash to link to) for the whole chain."""
    last_ev, last_day, prev_hash, _ = _read_chain_tail_with_corrupt(root)
    return last_ev, last_day, prev_hash


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


_SECRET_KEY_SUBSTRINGS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "api-key",
    "authorization",
    "cookie",
    "credential",
    "private_key",
    "private-key",
    "privatekey",
)

_DIGEST_KEYS = {
    "sha256",
    "sha256_before",
    "sha256_after",
    "sha",
    "hash",
    "prev_hash",
    "first_hash",
    "first_prev_hash",
    "last_hash",
}
_HEX_40_OR_64 = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")
_EVIDENCE_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _is_secret_key(k: Any) -> bool:
    name = str(k).lower()
    return any(sub in name for sub in _SECRET_KEY_SUBSTRINGS)


def _sanitize_argv(v: Any) -> Any:
    if not isinstance(v, (list, tuple)):
        return _sanitize_value(v)
    items = list(v)
    str_items = [x for x in items if isinstance(x, str)]
    redacted_strings = iter(redact_argv(str_items))
    res: list[Any] = []
    for x in items:
        if isinstance(x, str):
            res.append(next(redacted_strings))
        else:
            res.append(str(x))
    return type(v)(res) if isinstance(v, tuple) else res


def _sanitize_mapping(m: Mapping[str, Any]) -> dict[str, Any]:
    res: dict[str, Any] = {}
    for k, v in m.items():
        if _is_secret_key(k):
            res[k] = "<redacted>"
        elif k == "argv" and isinstance(v, (list, tuple)):
            res[k] = _sanitize_argv(v)
        elif k == "stderr_tail" and isinstance(v, str):
            redacted = redact(v)
            if len(redacted) > 2000:
                redacted = redacted[-2000:]
            res[k] = redacted
        elif k in _DIGEST_KEYS and isinstance(v, str) and _HEX_40_OR_64.fullmatch(v):
            res[k] = v
        elif k == "evidence" and isinstance(v, str) and (_EVIDENCE_SHA256.fullmatch(v) or _HEX_40_OR_64.fullmatch(v)):
            res[k] = v
        elif k == "session_id" and isinstance(v, str):
            res[k] = v
        else:
            res[k] = _sanitize_value(v)
    return res


def _sanitize_value(v: Any) -> Any:
    if isinstance(v, str):
        if _UUID_RE.fullmatch(v):
            return v
        return redact(v)
    if isinstance(v, Mapping):
        return _sanitize_mapping(v)
    if isinstance(v, (set, frozenset)):
        return [_sanitize_value(item) for item in sorted(v, key=str)]
    if isinstance(v, tuple):
        return [_sanitize_value(item) for item in v]
    if isinstance(v, list):
        return [_sanitize_value(item) for item in v]
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        return v
    return redact(str(v))


def _sanitize_nested(v: Any) -> Any:
    return _sanitize_value(v)


def _sanitize_detail(detail: Mapping[str, Any]) -> dict[str, Any]:
    return _sanitize_mapping(detail)


def sanitize(event: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = dict(event)
    if "target" in out:
        target = out["target"]
        if isinstance(target, str):
            try:
                tokens = shlex.split(target)
                redacted_tokens = redact_argv(tokens)
                redacted_target = shlex.join(redacted_tokens).replace("'<redacted>'", "<redacted>")
            except ValueError:
                redacted_target = redact(target)
            if len(redacted_target) > 500:
                redacted_target = redacted_target[:500]
            out["target"] = redacted_target
        else:
            out["target"] = _sanitize_value(target)
    if "quote" in out:
        quote = out["quote"]
        if isinstance(quote, str):
            redacted_quote = redact(quote)
            if len(redacted_quote) > 200:
                redacted_quote = redacted_quote[:199] + "…"
            out["quote"] = redacted_quote
        else:
            out["quote"] = _sanitize_value(quote)
    if "actor" in out:
        actor = out["actor"]
        if isinstance(actor, Mapping):
            out["actor"] = _sanitize_mapping(actor)
        elif isinstance(actor, str):
            out["actor"] = redact(actor)
        else:
            out["actor"] = _sanitize_value(actor)
    if "parent" in out:
        parent = out["parent"]
        if isinstance(parent, Mapping):
            out["parent"] = _sanitize_mapping(parent)
        elif isinstance(parent, str):
            out["parent"] = redact(parent)
        else:
            out["parent"] = _sanitize_value(parent)
    if "evidence" in out:
        evidence = out["evidence"]
        if isinstance(evidence, str):
            if _EVIDENCE_SHA256.fullmatch(evidence) or _HEX_40_OR_64.fullmatch(evidence):
                out["evidence"] = evidence
            else:
                out["evidence"] = redact(evidence)
        elif isinstance(evidence, Mapping):
            out["evidence"] = _sanitize_mapping(evidence)
        else:
            out["evidence"] = _sanitize_value(evidence)
    if "detail" in out:
        detail = out["detail"]
        if isinstance(detail, Mapping):
            out["detail"] = _sanitize_mapping(detail)
        elif isinstance(detail, str):
            out["detail"] = redact(detail)
        else:
            out["detail"] = _sanitize_value(detail)
    return out


def _write_record_to_file(
    record: dict[str, Any],
    root: Path,
    last_ev: dict[str, Any] | None,
    last_day: str,
    prev_hash: str,
) -> tuple[dict[str, Any], str, str]:
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
    if day_file.is_file() and day_file.stat().st_size > 0:
        try:
            with day_file.open("rb") as f:
                f.seek(-1, os.SEEK_END)
                last_byte = f.read(1)
            if last_byte != b"\n":
                with day_file.open("ab") as f:
                    f.write(b"\n")
        except Exception:
            pass

    fd = os.open(day_file, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, line_bytes)
        os.fsync(fd)
    finally:
        os.close(fd)
    return record, day, str(record["hash"])


def _append_locked(event: Mapping[str, Any], root: Path) -> dict[str, Any]:
    """Append one event; the caller holds the chain lock."""
    last_ev, last_day, prev_hash, corrupt_lines = _read_chain_tail_with_corrupt(root)

    if corrupt_lines:
        for rel_day_file, line_no in corrupt_lines:
            corrupt_day = Path(rel_day_file).stem
            last_ts = last_ev.get("ts") if (last_ev and "ts" in last_ev) else None
            caller_ts = event.get("ts")
            if isinstance(caller_ts, str) and caller_ts:
                c_day = _day_from_ts(caller_ts)
                if c_day == corrupt_day:
                    gap_ts = caller_ts
                elif c_day > corrupt_day:
                    gap_ts = (
                        last_ts
                        if (last_ts and _day_from_ts(last_ts) == corrupt_day)
                        else f"{corrupt_day}T23:59:59.999Z"
                    )
                else:
                    gap_ts = last_ts or f"{corrupt_day}T00:00:00.000Z"
            else:
                now = _utc_now_iso()
                if _day_from_ts(now) == corrupt_day:
                    gap_ts = now
                else:
                    gap_ts = (
                        last_ts
                        if (last_ts and _day_from_ts(last_ts) == corrupt_day)
                        else f"{corrupt_day}T23:59:59.999Z"
                    )

            gap_event = {
                "ts": gap_ts,
                "kind": "gap",
                "actor": {"role": "agent-hub", "host": "agent-hub"},
                "target": "",
                "outcome": {"status": "ok"},
                "detail": {
                    "from": last_ts or gap_ts,
                    "to": gap_ts,
                    "reason": f"corrupt-tail {rel_day_file}:{line_no}",
                    "lost": 1,
                },
                "schema_version": 1,
                "event_id": uuid.uuid4().hex,
            }
            last_ev, last_day, prev_hash = _write_record_to_file(gap_event, root, last_ev, last_day, prev_hash)

    record: dict[str, Any] = sanitize(event)
    record["schema_version"] = 1
    if "event_id" not in record or record["event_id"] is None:
        record["event_id"] = uuid.uuid4().hex

    if record.get("ts") is None:
        record["ts"] = _utc_now_iso()
    elif last_ev is not None:
        new_dt = _parse_ts(record["ts"])
        last_dt = _parse_ts(last_ev.get("ts"))
        if new_dt is not None and last_dt is not None and new_dt < last_dt:
            record["event_ts"] = record["ts"]
            record["ts"] = last_ev["ts"]

    caller_rec, _, _ = _write_record_to_file(record, root, last_ev, last_day, prev_hash)
    return caller_rec


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
