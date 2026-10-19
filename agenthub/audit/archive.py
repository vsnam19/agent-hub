from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tarfile
import time
import tomllib
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from agenthub.audit.verify import verify
from agenthub.audit.writer import audit_root
from agenthub.config import paths

_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")
_DAY_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.jsonl$")
_LOCK_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.jsonl\.lock$")

_LOCK_RETRY_INTERVAL_S = 0.005
_LOCK_MAX_RETRIES = 1000  # wait up to 5 seconds


class ArchiveError(Exception):
    pass


@dataclass(frozen=True)
class BuiltArchive:
    path: Path
    sha256: str
    files: list[str]
    count: int
    first_hash: str
    first_prev_hash: str
    last_hash: str


def load_vault(config_dir: Path | str | None = None) -> Path:
    if config_dir is None:
        base_dir = paths().config_dir
    else:
        base_dir = Path(config_dir)

    audit_file = base_dir / "audit.toml"
    if not audit_file.is_file():
        raise ArchiveError(f"missing audit configuration file: {audit_file}")

    try:
        data = tomllib.loads(audit_file.read_text(encoding="utf-8"))
    except Exception as e:
        raise ArchiveError(f"failed to parse audit config {audit_file}: {e}") from e

    if not isinstance(data, dict) or "vault" not in data:
        raise ArchiveError(f"missing 'vault' key in {audit_file}")

    vault_val = data["vault"]
    if not isinstance(vault_val, str) or isinstance(vault_val, bool):
        raise ArchiveError(f"'vault' in {audit_file} must be a string, got {type(vault_val).__name__}")

    vault_str = vault_val.strip()
    if not vault_str:
        raise ArchiveError(f"'vault' in {audit_file} cannot be empty")

    return Path(vault_str).expanduser()


def default_month(today: date | datetime | None = None) -> str:
    if today is None:
        cur_date = datetime.now(timezone.utc).date()
    elif isinstance(today, datetime):
        cur_date = today.date()
    elif isinstance(today, date):
        cur_date = today
    else:
        raise ArchiveError(f"invalid today parameter: {today!r}")

    year = cur_date.year
    month = cur_date.month

    if month == 1:
        return f"{year - 1:04d}-12"
    return f"{year:04d}-{month - 1:02d}"


def check_month(month: str, today: date | datetime | None = None) -> str:
    if not isinstance(month, str):
        raise ArchiveError(f"invalid month format: {month!r}, expected YYYY-MM")

    match = _MONTH_RE.match(month)
    if not match:
        raise ArchiveError(f"invalid month format: {month!r}, expected YYYY-MM")

    m_val = int(match.group(2))
    if m_val < 1 or m_val > 12:
        raise ArchiveError(f"invalid month format: {month!r}, expected month in 01..12")

    if today is None:
        cur_date = datetime.now(timezone.utc).date()
    elif isinstance(today, datetime):
        cur_date = today.date()
    elif isinstance(today, date):
        cur_date = today
    else:
        raise ArchiveError(f"invalid today parameter: {today!r}")

    cur_month_str = f"{cur_date.year:04d}-{cur_date.month:02d}"
    if month >= cur_month_str:
        raise ArchiveError(
            f"refusing month {month!r}: current or future month cannot be archived (current is {cur_month_str})"
        )

    return month


def _acquire_lock(lock_path: Path) -> int:
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            pass
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
    raise ArchiveError(f"timed out acquiring audit lock: {lock_path}")


def _release_lock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        os.close(fd)


def build_archive(
    month: str,
    state_dir: Path | str | None = None,
    tmp_dir: Path | str | None = None,
) -> BuiltArchive:
    if not isinstance(month, str) or not _MONTH_RE.match(month):
        raise ArchiveError(f"invalid month format: {month!r}, expected YYYY-MM")

    root = audit_root(state_dir)
    month_dir = root / month
    if not month_dir.is_dir():
        raise ArchiveError(f"month directory not found: {month_dir}")

    day_files = sorted(p for p in month_dir.iterdir() if p.is_file() and _DAY_FILE_RE.match(p.name))
    if not day_files:
        raise ArchiveError(f"no day files found in {month_dir}")

    # Discover all days with .jsonl files or .lock files to lock every day in that month
    day_names: set[str] = set()
    for entry in month_dir.iterdir():
        m_day = _DAY_FILE_RE.match(entry.name)
        if m_day:
            day_names.add(m_day.group(1))
        m_lock = _LOCK_FILE_RE.match(entry.name)
        if m_lock:
            day_names.add(m_lock.group(1))

    sorted_days = sorted(day_names)
    locked_fds: list[int] = []

    try:
        # Take every day lock of that month in turn before verifying and building
        for day in sorted_days:
            lock_path = month_dir / f"{day}.jsonl.lock"
            locked_fds.append(_acquire_lock(lock_path))

        # Verify month log
        verify_result = verify(state_dir=state_dir, month=month)
        if not verify_result.ok:
            raise ArchiveError(f"audit verification failed for {month}: {verify_result.message}")

        # Prepare tmp destination
        if tmp_dir is None:
            tmp_path = root / "tmp"
        else:
            tmp_path = Path(tmp_dir)

        tmp_path.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(tmp_path, 0o700)
        except OSError:
            pass

        archive_path = tmp_path / f"{month}.tar.xz"
        created_file: Path | None = None

        try:
            fd = os.open(archive_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
            created_file = archive_path
            try:
                os.fchmod(fd, 0o600)
            except OSError:
                pass

            with open(fd, "wb") as fileobj:
                with tarfile.open(fileobj=fileobj, mode="w:xz") as tar:
                    for day_file in day_files:
                        tar.add(day_file, arcname=f"{month}/{day_file.name}")

            try:
                os.chmod(archive_path, 0o600)
            except OSError:
                pass

            # Calculate sha256 of the tarball
            hasher = hashlib.sha256()
            with archive_path.open("rb") as f:
                while chunk := f.read(65536):
                    hasher.update(chunk)
            archive_sha256 = hasher.hexdigest()

            # Compute event count and boundary hashes
            total_count = 0
            first_prev_hash: str | None = None
            first_hash: str | None = None
            last_hash: str | None = None

            for day_file in day_files:
                with day_file.open("r", encoding="utf-8") as f:
                    for line in f:
                        line_str = line.strip()
                        if not line_str:
                            continue
                        record = json.loads(line_str)
                        total_count += 1
                        if first_hash is None:
                            first_prev_hash = str(record["prev_hash"])
                            first_hash = str(record["hash"])
                        last_hash = str(record["hash"])

            if total_count == 0 or first_hash is None or first_prev_hash is None or last_hash is None:
                raise ArchiveError(f"no audit events found in month {month}")

            return BuiltArchive(
                path=archive_path,
                sha256=archive_sha256,
                files=[p.name for p in day_files],
                count=total_count,
                first_hash=first_hash,
                first_prev_hash=first_prev_hash,
                last_hash=last_hash,
            )
        except BaseException:
            if created_file is not None and created_file.exists():
                created_file.unlink(missing_ok=True)
            raise

    finally:
        for fd in locked_fds:
            _release_lock(fd)
