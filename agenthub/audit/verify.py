from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from agenthub.audit.writer import ZERO_HASH, audit_root, event_hash

_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
_DAY_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.jsonl$")
_MANIFEST_FILE_RE = re.compile(r"^(\d{4}-\d{2})\.json$")


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    message: str


def _prev_month(month_str: str) -> str:
    year, month = map(int, month_str.split("-"))
    if month == 1:
        return f"{year - 1:04d}-12"
    return f"{year:04d}-{month - 1:02d}"


def _parse_ts(ts: str) -> datetime:
    if not isinstance(ts, str) or not ts:
        raise ValueError(f"invalid ts: {ts!r}")
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    else:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _read_last_event_hash(day_file: Path) -> str | None:
    if not day_file.is_file():
        return None
    with day_file.open("r", encoding="utf-8") as f:
        last_line = ""
        for line in f:
            stripped = line.strip()
            if stripped:
                last_line = stripped
        if not last_line:
            return None
        try:
            record = json.loads(last_line)
            h = record.get("hash")
            return str(h) if isinstance(h, str) and h else None
        except Exception:
            return None


def _latest_earlier_local_day_hash(root: Path, before_day: str) -> tuple[str, str] | None:
    if not root.is_dir():
        return None
    month_dirs = sorted(
        (p for p in root.iterdir() if p.is_dir() and _MONTH_RE.match(p.name) and p.name <= before_day[:7]),
        key=lambda p: p.name,
        reverse=True,
    )
    for m_dir in month_dirs:
        candidates: list[tuple[str, Path]] = []
        for entry in m_dir.iterdir():
            m = _DAY_FILE_RE.match(entry.name)
            if m and m.group(1) < before_day and entry.is_file():
                candidates.append((m.group(1), entry))
        candidates.sort(key=lambda item: item[0], reverse=True)
        for _, path in candidates:
            h = _read_last_event_hash(path)
            if h is not None:
                return path.parent.name, h
    return None


def verify(
    state_dir: Path | str | None = None,
    month: str | None = None,
) -> VerifyResult:
    root = audit_root(state_dir)
    if not root.is_dir():
        return VerifyResult(ok=True, message="ok: 0 events")

    if month is not None:
        if not _MONTH_RE.match(month):
            raise ValueError(f"invalid month format: {month!r}, expected YYYY-MM")
        month_dir = root / month
        if not month_dir.is_dir():
            return VerifyResult(ok=True, message="ok: 0 events")
        target_months = [month]
    else:
        target_months = sorted(p.name for p in root.iterdir() if p.is_dir() and _MONTH_RE.match(p.name))

    target_day_files: list[Path] = []
    for m in target_months:
        m_dir = root / m
        days = sorted(p for p in m_dir.iterdir() if p.is_file() and _DAY_FILE_RE.match(p.name))
        target_day_files.extend(days)

    if not target_day_files:
        return VerifyResult(ok=True, message="ok: 0 events")

    total_events = 0
    prev_hash_for_next_day: str | None = None
    last_verified_day: str | None = None
    prev_dt: datetime | None = None
    prev_ts_str: str | None = None

    for day_file in target_day_files:
        rel_path = day_file.relative_to(root).as_posix()
        match = _DAY_FILE_RE.match(day_file.name)
        assert match is not None
        current_day = match.group(1)
        current_month = day_file.parent.name

        with day_file.open("r", encoding="utf-8") as f:
            lines = f.readlines()

        if not lines:
            return VerifyResult(
                ok=False,
                message=f"{rel_path}:1 seq=1: empty day file",
            )

        # Determine expected prev_hash for line 1 of this day file
        if last_verified_day is not None and last_verified_day[:7] == current_month:
            # Continuing within the same month from previous verified day
            expected_prev_hash = prev_hash_for_next_day
            initial_check_reason: str | None = None
        else:
            # First day of this month
            prev_m = _prev_month(current_month)
            prev_m_dir = root / prev_m
            has_local_prev_m = prev_m_dir.is_dir() and any(
                p.is_file() and _DAY_FILE_RE.match(p.name) for p in prev_m_dir.iterdir()
            )

            if has_local_prev_m:
                valid_prev_link = (
                    prev_hash_for_next_day is not None
                    and last_verified_day is not None
                    and last_verified_day[:7] == prev_m
                )
                if valid_prev_link:
                    expected_prev_hash = prev_hash_for_next_day
                    initial_check_reason = None
                else:
                    hit = _latest_earlier_local_day_hash(root, current_day)
                    if hit is not None:
                        expected_prev_hash = hit[1]
                        initial_check_reason = None
                    else:
                        expected_prev_hash = None
                        initial_check_reason = f"cannot read last hash of previous local month {prev_m}"
            else:
                # Previous month is not local; check manifest
                manifests_dir = root / "manifests"
                manifest_file = manifests_dir / f"{prev_m}.json"
                if manifest_file.is_file():
                    try:
                        m_data = json.loads(manifest_file.read_text(encoding="utf-8"))
                        last_h = m_data.get("last_hash")
                        if isinstance(last_h, str) and last_h:
                            expected_prev_hash = last_h
                            initial_check_reason = None
                        else:
                            expected_prev_hash = None
                            initial_check_reason = f"manifest for {prev_m} missing last_hash"
                    except Exception as e:
                        expected_prev_hash = None
                        initial_check_reason = f"invalid manifest for {prev_m}: {e}"
                else:
                    # Previous month manifest does not exist
                    # Check if earlier manifests or earlier local months exist
                    has_earlier_manifests = manifests_dir.is_dir() and any(
                        _MANIFEST_FILE_RE.match(p.name)
                        and _MANIFEST_FILE_RE.match(p.name).group(1) < current_month  # type: ignore[union-attr]
                        and p.is_file()
                        for p in manifests_dir.iterdir()
                    )
                    has_earlier_local = any(
                        p.name < current_month
                        and p.is_dir()
                        and _MONTH_RE.match(p.name)
                        and any(_DAY_FILE_RE.match(f.name) and f.is_file() for f in p.iterdir())
                        for p in root.iterdir()
                    )
                    if has_earlier_manifests or has_earlier_local:
                        expected_prev_hash = None
                        initial_check_reason = f"missing local month or manifest for {prev_m}"
                    else:
                        # Very first event ever
                        expected_prev_hash = ZERO_HASH
                        initial_check_reason = None

        expected_seq = 1

        for line_no, raw_line in enumerate(lines, 1):
            # 1. JSON parses
            try:
                record = json.loads(raw_line)
            except Exception as e:
                return VerifyResult(
                    ok=False,
                    message=f"{rel_path}:{line_no} seq={expected_seq}: invalid JSON: {e}",
                )
            if not isinstance(record, dict):
                return VerifyResult(
                    ok=False,
                    message=f"{rel_path}:{line_no} seq={expected_seq}: event is not a JSON object",
                )

            record_seq = record.get("seq")
            seq_val = record_seq if isinstance(record_seq, int) else expected_seq

            # 2. schema_version == 1
            schema_version = record.get("schema_version")
            if schema_version != 1:
                return VerifyResult(
                    ok=False,
                    message=(
                        f"{rel_path}:{line_no} seq={seq_val}: "
                        f"invalid schema_version: expected 1, got {schema_version!r}"
                    ),
                )

            # 3. seq is previous + 1 (first line of a day file is 1)
            if record_seq != expected_seq:
                return VerifyResult(
                    ok=False,
                    message=(
                        f"{rel_path}:{line_no} seq={seq_val}: seq mismatch: expected {expected_seq}, got {record_seq!r}"
                    ),
                )

            # 4. prev_hash equals expected
            actual_prev_hash = record.get("prev_hash")
            if initial_check_reason is not None and line_no == 1:
                return VerifyResult(
                    ok=False,
                    message=f"{rel_path}:{line_no} seq={seq_val}: {initial_check_reason}",
                )

            if expected_prev_hash == ZERO_HASH and actual_prev_hash != ZERO_HASH and line_no == 1:
                prev_m = _prev_month(current_month)
                return VerifyResult(
                    ok=False,
                    message=(
                        f"{rel_path}:{line_no} seq={seq_val}: "
                        f"prev_hash mismatch (missing local month or manifest for {prev_m}): "
                        f"expected {expected_prev_hash}, got {actual_prev_hash!r}"
                    ),
                )

            if actual_prev_hash != expected_prev_hash:
                return VerifyResult(
                    ok=False,
                    message=(
                        f"{rel_path}:{line_no} seq={seq_val}: "
                        f"prev_hash mismatch: expected {expected_prev_hash}, got {actual_prev_hash!r}"
                    ),
                )

            # 5. hash recomputes
            computed_hash = event_hash(str(actual_prev_hash), record)
            actual_hash = record.get("hash")
            if actual_hash != computed_hash:
                return VerifyResult(
                    ok=False,
                    message=(
                        f"{rel_path}:{line_no} seq={seq_val}: "
                        f"hash mismatch: expected {computed_hash}, got {actual_hash!r}"
                    ),
                )

            # 6. ts is not earlier than the previous ts by more than 2 seconds
            raw_ts = record.get("ts")
            try:
                cur_dt = _parse_ts(str(raw_ts))
            except Exception as e:
                return VerifyResult(
                    ok=False,
                    message=f"{rel_path}:{line_no} seq={seq_val}: invalid ts {raw_ts!r}: {e}",
                )

            if prev_dt is not None:
                skew_s = (prev_dt - cur_dt).total_seconds()
                if skew_s > 2.0:
                    return VerifyResult(
                        ok=False,
                        message=(
                            f"{rel_path}:{line_no} seq={seq_val}: "
                            f"clock skew: ts {raw_ts} is earlier than previous ts {prev_ts_str} "
                            f"by {skew_s:.3f}s (> 2s)"
                        ),
                    )

            prev_dt = cur_dt
            prev_ts_str = str(raw_ts)
            expected_prev_hash = str(actual_hash)
            expected_seq += 1
            total_events += 1

        prev_hash_for_next_day = expected_prev_hash
        last_verified_day = current_day

    return VerifyResult(ok=True, message=f"ok: {total_events} events")
