from __future__ import annotations

import hashlib
import json
import re
import tarfile
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


def _has_local_month(root: Path, month: str) -> bool:
    m_dir = root / month
    return m_dir.is_dir() and any(p.is_file() and _DAY_FILE_RE.match(p.name) for p in m_dir.iterdir())


def _has_manifest(root: Path, month: str) -> bool:
    manifest_file = root / "manifests" / f"{month}.json"
    return manifest_file.is_file()


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


@dataclass
class _ChainState:
    expected_prev_hash: str | None = None
    prev_dt: datetime | None = None
    prev_ts_str: str | None = None
    total_events: int = 0
    first_prev_hash: str | None = None
    last_hash: str | None = None


def _verify_day_lines(
    rel_path: str,
    lines: list[str],
    current_month: str,
    state: _ChainState,
    initial_check_reason: str | None = None,
    allow_zero_hash_predecessor_message: bool = True,
) -> VerifyResult | None:
    if not lines:
        return VerifyResult(
            ok=False,
            message=f"{rel_path}:1 seq=1: empty day file",
        )

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
                    f"{rel_path}:{line_no} seq={seq_val}: invalid schema_version: expected 1, got {schema_version!r}"
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

        if allow_zero_hash_predecessor_message and actual_prev_hash != state.expected_prev_hash and line_no == 1:
            prev_m = _prev_month(current_month)
            return VerifyResult(
                ok=False,
                message=(
                    f"{rel_path}:{line_no} seq={seq_val}: "
                    f"prev_hash mismatch (missing local month or manifest for {prev_m}): "
                    f"expected {state.expected_prev_hash}, got {actual_prev_hash!r}"
                ),
            )

        if actual_prev_hash != state.expected_prev_hash:
            return VerifyResult(
                ok=False,
                message=(
                    f"{rel_path}:{line_no} seq={seq_val}: "
                    f"prev_hash mismatch: expected {state.expected_prev_hash}, got {actual_prev_hash!r}"
                ),
            )

        # 5. hash recomputes
        computed_hash = event_hash(str(actual_prev_hash), record)
        actual_hash = record.get("hash")
        if actual_hash != computed_hash:
            return VerifyResult(
                ok=False,
                message=(
                    f"{rel_path}:{line_no} seq={seq_val}: hash mismatch: expected {computed_hash}, got {actual_hash!r}"
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

        if state.prev_dt is not None:
            skew_s = (state.prev_dt - cur_dt).total_seconds()
            if skew_s > 2.0:
                return VerifyResult(
                    ok=False,
                    message=(
                        f"{rel_path}:{line_no} seq={seq_val}: "
                        f"clock skew: ts {raw_ts} is earlier than previous ts {state.prev_ts_str} "
                        f"by {skew_s:.3f}s (> 2s)"
                    ),
                )

        if state.first_prev_hash is None:
            state.first_prev_hash = str(actual_prev_hash)
        state.last_hash = str(actual_hash)
        state.prev_dt = cur_dt
        state.prev_ts_str = str(raw_ts)
        state.expected_prev_hash = str(actual_hash)
        expected_seq += 1
        state.total_events += 1

    return None


def _verify_archived_month(
    root: Path,
    month: str,
    vault: Path | str | None = None,
    config_dir: Path | str | None = None,
) -> VerifyResult:
    manifest_path = root / "manifests" / f"{month}.json"
    if not manifest_path.is_file():
        return VerifyResult(ok=True, message="ok: 0 events")

    try:
        manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as e:
        return VerifyResult(ok=False, message=f"invalid manifest for {month}: {e}")

    if not isinstance(manifest_data, dict):
        return VerifyResult(ok=False, message=f"invalid manifest for {month}: manifest is not a JSON object")

    # Load vault
    if vault is not None:
        vault_root = Path(vault).expanduser()
    else:
        try:
            from agenthub.audit.archive import load_vault

            vault_root = load_vault(config_dir=config_dir)
        except Exception as e:
            return VerifyResult(ok=False, message=f"archive for {month} needs the vault: {e}")

    if not vault_root.exists():
        return VerifyResult(
            ok=False,
            message=f"archive for {month} needs the vault: vault directory does not exist: {vault_root}",
        )
    if not vault_root.is_dir():
        return VerifyResult(
            ok=False,
            message=f"archive for {month} needs the vault: vault path is not a directory: {vault_root}",
        )

    vault_dir = vault_root / "agent-hub-audit"
    if not vault_dir.is_dir():
        return VerifyResult(
            ok=False,
            message=f"archive for {month} needs the vault: vault audit directory not found: {vault_dir}",
        )

    tar_path = vault_dir / f"{month}.tar.xz"
    if not tar_path.is_file():
        return VerifyResult(
            ok=False,
            message=f"archive for {month} needs the vault: archive file not found: {tar_path}",
        )

    # Check sha256 against manifest
    hasher = hashlib.sha256()
    with tar_path.open("rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    tar_sha256 = hasher.hexdigest()

    expected_sha256 = manifest_data.get("sha256")
    if tar_sha256 != expected_sha256:
        return VerifyResult(
            ok=False,
            message=f"archive sha256 mismatch for {month}: expected {expected_sha256}, got {tar_sha256}",
        )

    # Extract day files in memory
    try:
        with tarfile.open(tar_path, "r:xz") as tar:
            day_members: list[tuple[str, tarfile.TarInfo]] = []
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                match = _DAY_FILE_RE.match(Path(member.name).name)
                if match:
                    day_members.append((match.group(1), member))

            day_members.sort(key=lambda item: item[0])
            if not day_members:
                return VerifyResult(ok=False, message=f"no day files found in archive for {month}")

            first_prev_hash = manifest_data.get("first_prev_hash")
            state = _ChainState(expected_prev_hash=str(first_prev_hash) if first_prev_hash is not None else None)

            for _, member in day_members:
                f = tar.extractfile(member)
                if f is None:
                    return VerifyResult(ok=False, message=f"cannot read {member.name} from archive for {month}")
                try:
                    content = f.read()
                    text = content.decode("utf-8")
                    lines = text.splitlines(keepends=True)
                except UnicodeDecodeError as e:
                    rel_p = f"{month}/{Path(member.name).name}"
                    return VerifyResult(ok=False, message=f"{rel_p}:1 seq=1: invalid UTF-8: {e}")

                rel_path = f"{month}/{Path(member.name).name}"
                res = _verify_day_lines(
                    rel_path=rel_path,
                    lines=lines,
                    current_month=month,
                    state=state,
                    initial_check_reason=None,
                    allow_zero_hash_predecessor_message=False,
                )
                if res is not None:
                    return res
    except Exception as e:
        return VerifyResult(ok=False, message=f"corrupt archive for {month}: {e}")

    # Boundary checks against manifest
    manifest_first_prev_hash = manifest_data.get("first_prev_hash")
    if manifest_first_prev_hash is not None and state.first_prev_hash != manifest_first_prev_hash:
        return VerifyResult(
            ok=False,
            message=(
                f"archive for {month} first_prev_hash mismatch: "
                f"expected {manifest_first_prev_hash}, got {state.first_prev_hash}"
            ),
        )

    manifest_last_hash = manifest_data.get("last_hash")
    if manifest_last_hash is not None and state.last_hash != manifest_last_hash:
        return VerifyResult(
            ok=False,
            message=(f"archive for {month} last_hash mismatch: expected {manifest_last_hash}, got {state.last_hash}"),
        )

    manifest_count = manifest_data.get("count")
    if manifest_count is not None and state.total_events != manifest_count:
        return VerifyResult(
            ok=False,
            message=(f"archive for {month} count mismatch: expected {manifest_count}, got {state.total_events}"),
        )

    return VerifyResult(ok=True, message=f"ok: {state.total_events} events")


def verify(
    state_dir: Path | str | None = None,
    month: str | None = None,
    vault: Path | str | None = None,
    config_dir: Path | str | None = None,
) -> VerifyResult:
    root = audit_root(state_dir)
    if not root.is_dir():
        return VerifyResult(ok=True, message="ok: 0 events")

    if month is not None:
        if not _MONTH_RE.match(month):
            raise ValueError(f"invalid month format: {month!r}, expected YYYY-MM")
        month_dir = root / month
        if not month_dir.is_dir():
            manifest_file = root / "manifests" / f"{month}.json"
            if manifest_file.is_file():
                return _verify_archived_month(
                    root=root,
                    month=month,
                    vault=vault,
                    config_dir=config_dir,
                )
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

    state = _ChainState()
    prev_hash_for_next_day: str | None = None
    last_verified_day: str | None = None

    for day_file in target_day_files:
        rel_path = day_file.relative_to(root).as_posix()
        match = _DAY_FILE_RE.match(day_file.name)
        assert match is not None
        current_day = match.group(1)
        current_month = day_file.parent.name

        with day_file.open("r", encoding="utf-8") as f:
            lines = f.readlines()

        # Determine expected prev_hash for line 1 of this day file
        is_month_start = last_verified_day is None or last_verified_day[:7] != current_month
        if not is_month_start:
            # Continuing within the same month from previous verified day
            state.expected_prev_hash = prev_hash_for_next_day
            initial_check_reason: str | None = None
            allow_zero_msg = False
        else:
            # First day of this month
            prev_m = _prev_month(current_month)
            allow_zero_msg = not _has_local_month(root, prev_m) and not _has_manifest(root, prev_m)
            if last_verified_day is not None and prev_hash_for_next_day is not None:
                day_hit: tuple[str, str] | None = (last_verified_day[:7], prev_hash_for_next_day)
            else:
                day_hit = _latest_earlier_local_day_hash(root, current_day)

            manifests_dir = root / "manifests"
            manifest_candidates: list[tuple[str, Path]] = []
            if manifests_dir.is_dir():
                for entry in manifests_dir.iterdir():
                    m = _MANIFEST_FILE_RE.match(entry.name)
                    if m and m.group(1) < current_month and entry.is_file():
                        manifest_candidates.append((m.group(1), entry))
            manifest_candidates.sort(key=lambda item: item[0], reverse=True)

            if manifest_candidates:
                latest_m_name, latest_m_path = manifest_candidates[0]
                if day_hit is None or day_hit[0] < latest_m_name:
                    try:
                        m_data = json.loads(latest_m_path.read_text(encoding="utf-8"))
                        last_h = m_data.get("last_hash")
                        if isinstance(last_h, str) and last_h:
                            state.expected_prev_hash = last_h
                            initial_check_reason = None
                        else:
                            state.expected_prev_hash = None
                            initial_check_reason = f"manifest for {latest_m_name} missing last_hash"
                    except Exception as e:
                        state.expected_prev_hash = None
                        initial_check_reason = f"invalid manifest for {latest_m_name}: {e}"
                else:
                    state.expected_prev_hash = day_hit[1]
                    initial_check_reason = None
            else:
                if day_hit is not None:
                    state.expected_prev_hash = day_hit[1]
                    initial_check_reason = None
                else:
                    state.expected_prev_hash = ZERO_HASH
                    initial_check_reason = None

        res = _verify_day_lines(
            rel_path=rel_path,
            lines=lines,
            current_month=current_month,
            state=state,
            initial_check_reason=initial_check_reason,
            allow_zero_hash_predecessor_message=allow_zero_msg,
        )
        if res is not None:
            return res

        prev_hash_for_next_day = state.expected_prev_hash
        last_verified_day = current_day

    return VerifyResult(ok=True, message=f"ok: {state.total_events} events")
