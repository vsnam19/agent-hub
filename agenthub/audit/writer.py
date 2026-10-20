from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shlex
import signal
import threading
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
_CLAIM_STALE_S = 60.0


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
        except UnicodeDecodeError:
            return None
    stripped = raw_line.strip()
    if not stripped:
        return None
    try:
        record = json.loads(stripped)
    except (ValueError, RecursionError):
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
    except (OSError, ValueError):
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


def _latest_manifest(root: Path) -> tuple[str, str] | None:
    """Return (month, last_hash) of the newest readable manifest."""
    manifests_dir = root / "manifests"
    if not manifests_dir.is_dir():
        return None
    manifests: list[tuple[str, Path]] = []
    for entry in manifests_dir.iterdir():
        m = _MANIFEST_FILE_RE.match(entry.name)
        if m and entry.is_file():
            manifests.append((m.group(1), entry))
    manifests.sort(key=lambda item: item[0], reverse=True)
    for month, path in manifests:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
            continue
        if not isinstance(data, dict):
            continue
        last_hash = data.get("last_hash")
        if isinstance(last_hash, str) and last_hash:
            return month, last_hash
    return None


def _latest_manifest_hash(root: Path) -> str:
    manifest = _latest_manifest(root)
    return manifest[1] if manifest is not None else ZERO_HASH


def _last_line(path: Path) -> bytes | None:
    """Last non-blank line of a file, read backwards in chunks; None if the file has no text."""
    with path.open("rb") as f:
        pos = f.seek(0, os.SEEK_END)
        buf = b""
        while pos > 0:
            step = min(_SEEK_CHUNK_BYTES, pos)
            pos -= step
            f.seek(pos)
            buf = f.read(step) + buf
            if b"\n" in buf.rstrip():
                break
    stripped = buf.rstrip()
    if not stripped:
        return None
    return stripped.rsplit(b"\n", 1)[-1]


def _scan_day_file(path: Path) -> tuple[dict[str, Any] | None, list[tuple[str, int]]]:
    """Last valid event of a day file plus the corrupt trailing lines after it (1-based)."""
    try:
        last = _last_line(path)
    except OSError:
        last = None
    if last is not None:
        ev = _parse_valid_event(last.decode("utf-8", errors="replace"))
        if ev is not None:
            return ev, []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return None, []
    corrupt_indices: list[int] = []
    valid_ev: dict[str, Any] | None = None
    for idx in range(len(lines) - 1, -1, -1):
        parsed = _parse_valid_event(lines[idx])
        if parsed is not None:
            valid_ev = parsed
            break
        corrupt_indices.append(idx)
    rel_day_file = f"{path.parent.name}/{path.name}"
    return valid_ev, [(rel_day_file, idx + 1) for idx in reversed(corrupt_indices)]


def _read_chain_tail_with_corrupt(
    root: Path,
) -> tuple[dict[str, Any] | None, str, str, list[tuple[str, int]]]:
    candidates = _collect_day_files(root)
    local_ev: dict[str, Any] | None = None
    local_day = ""
    corrupt_lines: list[tuple[str, int]] = []
    if candidates:
        local_ev, corrupt_lines = _scan_day_file(candidates[0][1])
        local_day = candidates[0][0]
        if local_ev is None:
            local_day = ""
            for day_name, path in candidates[1:]:
                ev = _read_last_event(path)
                if ev is not None:
                    local_ev, local_day = ev, day_name
                    break

    manifest = _latest_manifest(root)
    if manifest is not None and (local_ev is None or local_day[:7] < manifest[0]):
        return None, "", manifest[1], corrupt_lines
    if local_ev is not None:
        return local_ev, local_day, str(local_ev["hash"]), corrupt_lines
    return None, "", ZERO_HASH, corrupt_lines


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
            res.append(_sanitize_nested(x))
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


def _still_changes(text: str) -> bool:
    """Re-tokenise `text` like `_redact_target` does; True if `redact_argv` still changes any token."""
    try:
        tokens = shlex.split(text)
    except ValueError:
        tokens = text.split()
    redacted = redact_argv(tokens)
    return len(redacted) != len(tokens) or any(tok != red for tok, red in zip(tokens, redacted, strict=True))


def _redact_target(target: str) -> str:
    try:
        tokens = shlex.split(target)
    except ValueError:
        tokens = target.split()
    redacted_tokens = redact_argv(tokens)
    if len(tokens) != len(redacted_tokens):
        return redact(target)
    changed: dict[str, str] = {}  # original token -> redacted token
    for tok, red in zip(tokens, redacted_tokens, strict=True):
        if tok != red and tok:
            changed[tok] = red
    if not changed:
        return redact(target)
    ordered = sorted(changed, key=len, reverse=True)
    patterns = [re.compile(r"(?<![^\s'\"=])" + re.escape(tok) + r"(?![^\s'\"])") for tok in ordered]
    if any(pat.search(target) is None for pat in patterns):
        return redact(shlex.join(redacted_tokens))
    combined = re.compile(r"(?<![^\s'\"=])(?:" + "|".join(re.escape(tok) for tok in ordered) + r")(?![^\s'\"])")
    result = combined.sub(lambda m: changed[m.group(0)], target)  # one pass: never rewrites an inserted <redacted>
    if _still_changes(result):  # a quoted/escaped copy of a secret survived the replacement
        return redact(shlex.join(redacted_tokens))
    return redact(result)


def sanitize(event: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = dict(event)
    if "target" in out:
        target = out["target"]
        if isinstance(target, str):
            redacted_target = _redact_target(target)
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


class _CallState(threading.local):
    """Per-thread, per-call flags: two threads writing at once never see each other's state."""

    caller_written = False
    emitting_gap = False
    gap_written = False


_state = _CallState()


def _write_record_to_file(
    record: dict[str, Any],
    root: Path,
    last_ev: dict[str, Any] | None,
    last_day: str,
    prev_hash: str,
    *,
    caller: bool = False,
    claimed_gap: bool = False,
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
    fd = os.open(day_file, os.O_CREAT | os.O_RDWR | os.O_APPEND, 0o600)
    try:
        os.fchmod(fd, 0o600)
        size = os.fstat(fd).st_size
        if size > 0 and os.pread(fd, 1, size - 1) != b"\n":
            # torn tail: separate it from our record in the same write
            line_bytes = b"\n" + line_bytes
        os.write(fd, line_bytes)
        if caller:
            _state.caller_written = True
        if claimed_gap:
            _state.gap_written = True
        os.fsync(fd)
    finally:
        os.close(fd)
    return record, day, str(record["hash"])


def _stamp_ts(record: dict[str, Any], last_ev: dict[str, Any] | None) -> None:
    """Fill a missing ts with now and keep ts monotonic; the earlier value moves to event_ts."""
    if not record.get("ts"):
        record["ts"] = _utc_now_iso()
    if last_ev is None:
        return
    new_dt = _parse_ts(record["ts"])
    last_dt = _parse_ts(last_ev.get("ts"))
    if new_dt is not None and last_dt is not None and new_dt < last_dt:
        record["event_ts"] = record["ts"]
        record["ts"] = last_ev["ts"]


def _append_locked(event: Mapping[str, Any], root: Path) -> dict[str, Any]:
    """Append one event; the caller holds the chain lock."""
    last_ev, last_day, prev_hash, corrupt_lines = _read_chain_tail_with_corrupt(root)

    for rel_day_file, line_no in corrupt_lines:
        corrupt_day = Path(rel_day_file).stem
        last_ts = last_ev.get("ts") if last_ev else None
        gap_event: dict[str, Any] = {
            "ts": event.get("ts"),
            "kind": "gap",
            "actor": {"role": "agent-hub", "host": "agent-hub"},
            "target": "",
            "outcome": {"status": "ok"},
            "detail": {"reason": f"corrupt-tail {rel_day_file}:{line_no}", "lost": 1},
            "schema_version": 1,
            "event_id": uuid.uuid4().hex,
        }
        _stamp_ts(gap_event, last_ev)
        if _day_from_ts(gap_event["ts"]) < corrupt_day:
            # keep the corrupt file from staying the latest one (clock far behind)
            gap_event.setdefault("event_ts", gap_event["ts"])
            gap_event["ts"] = f"{corrupt_day}T00:00:00.000Z"
        gap_event["detail"]["from"] = last_ts or gap_event["ts"]
        gap_event["detail"]["to"] = gap_event["ts"]
        last_ev, last_day, prev_hash = _write_record_to_file(gap_event, root, last_ev, last_day, prev_hash)

    record: dict[str, Any] = sanitize(event)
    record["schema_version"] = 1
    if "event_id" not in record or record["event_id"] is None:
        record["event_id"] = uuid.uuid4().hex
    _stamp_ts(record, last_ev)

    caller_rec, _, _ = _write_record_to_file(
        record, root, last_ev, last_day, prev_hash, caller=not _state.emitting_gap, claimed_gap=_state.emitting_gap
    )
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


def _pid_is_dead(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def _claim_is_stale(path: Path) -> bool:
    try:
        return time.time() - path.stat().st_mtime > _CLAIM_STALE_S
    except OSError:
        return False


def _orphaned_claims(pending_path: Path) -> list[Path]:
    """Claimed pending files left by processes that no longer exist."""
    found: list[Path] = []
    for path in sorted(pending_path.parent.glob("audit-pending-gap.*.claimed")):
        pid_text = path.name[len("audit-pending-gap.") : -len(".claimed")]
        if not pid_text.isdigit() or int(pid_text) == os.getpid():
            continue
        if _pid_is_dead(int(pid_text)) or _claim_is_stale(path):
            found.append(path)
    return found


def _release_claims(claims: list[Path], sizes: dict[Path, int], pending_path: Path) -> None:
    """Unlink claims; bytes that landed in a claimed inode after it was read go back to the pending file."""
    for path in claims:
        try:
            extra = path.read_bytes()[sizes[path] :] if path in sizes else b""
        except (OSError, ValueError):
            extra = b""
        if extra:
            fd = os.open(pending_path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
            try:
                os.fchmod(fd, 0o600)
                os.write(fd, extra)
                os.fsync(fd)
            finally:
                os.close(fd)
        path.unlink(missing_ok=True)


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
    claims = _orphaned_claims(pending_path)
    if claimed.is_file():
        claims.append(claimed)
    if not claims:
        return
    raw = b""
    unreadable = False
    sizes: dict[Path, int] = {}
    for path in claims:
        try:
            chunk = path.read_bytes()
        except (OSError, ValueError):
            unreadable = True
            continue
        sizes[path] = len(chunk)
        raw += chunk if chunk.endswith(b"\n") or not chunk else chunk + b"\n"
    entries: list[dict[str, Any]] = []
    non_empty = 0
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        non_empty += 1
        try:
            item = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(item, dict):
            entries.append(item)
    unparsable = non_empty - len(entries)
    if non_empty == 0 and not raw.strip() and not unreadable:
        for path in claims:
            path.unlink(missing_ok=True)
        return
    now = _utc_now_iso()
    gap_event: dict[str, Any] = {
        "ts": now,
        "kind": "gap",
        "actor": {"role": "agent-hub", "host": "agent-hub"},
        "target": "",
        "outcome": {"status": "ok"},
        "detail": {
            "from": entries[0].get("ts", now) if entries else now,
            "to": now,
            "reason": entries[0].get("reason", "") if entries else "unreadable pending file",
            "lost": non_empty if not unreadable else "unknown",
        },
    }
    if unparsable:
        gap_event["detail"]["unparsable"] = unparsable
    _state.emitting_gap = True
    _state.gap_written = False
    try:
        _append_locked(gap_event, root)
    except BaseException:
        _state.emitting_gap = False
        if _state.gap_written:
            # the gap is in the chain: the claimed lines are reported, not restored
            _release_claims(claims, sizes, pending_path)
            raise
        if raw:
            fd = os.open(pending_path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
            try:
                os.fchmod(fd, 0o600)
                os.write(fd, raw)
                os.fsync(fd)
            finally:
                os.close(fd)
        # an unreadable claim stays on disk: the orphan rule picks it up later
        _release_claims([path for path in claims if path in sizes], sizes, pending_path)
        raise
    _state.emitting_gap = False
    _release_claims(claims, sizes, pending_path)


def record_pending(event: Mapping[str, Any], reason: str, state_dir: Path | str | None = None) -> None:
    # a hook timeout (SIGALRM) must not interrupt the write that saves this event
    blocked = False
    if hasattr(signal, "pthread_sigmask") and hasattr(signal, "SIGALRM"):
        try:
            signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
            blocked = True
        except (OSError, ValueError):
            pass
    try:
        base = paths().state_dir if state_dir is None else Path(state_dir)
        try:
            base.mkdir(parents=True, exist_ok=True)
        except OSError:
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
    finally:
        if blocked:
            try:
                signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGALRM})
            except Exception:
                pass


def _record_pending_failure(event: Mapping[str, Any], exc: Exception, state_dir: Path | str | None = None) -> None:
    record_pending(event, str(exc) or type(exc).__name__, state_dir=state_dir)


def write(event: Mapping[str, Any], state_dir: Path | str | None = None) -> bool:
    _state.caller_written = False
    _state.emitting_gap = False
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
        if not _state.caller_written:
            record_pending(event, str(exc) or type(exc).__name__, state_dir=state_dir)
        return False
