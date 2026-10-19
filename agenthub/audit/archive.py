from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tarfile
import time
import tomllib
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from agenthub.audit import writer as _writer
from agenthub.audit.emit import emit
from agenthub.audit.verify import verify
from agenthub.audit.writer import audit_root
from agenthub.config import paths

_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")
_DAY_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.jsonl$")

_CHAIN_LOCK_WAIT_S = 30.0  # a batch job may wait for busy writers longer than a hook can


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


@dataclass(frozen=True)
class ArchiveResult:
    status: str
    message: str
    exit_code: int


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


def _acquire_chain_lock(root: Path) -> int:
    root.mkdir(parents=True, exist_ok=True)
    try:
        return _writer._acquire_lock(root / "chain.lock", time.monotonic() + _CHAIN_LOCK_WAIT_S)
    except _writer.AuditLockTimeout as e:
        raise ArchiveError(str(e)) from e


def _release_lock(fd: int) -> None:
    try:
        _writer._release_lock(fd)
    except OSError:
        pass


def build_archive(
    month: str,
    state_dir: Path | str | None = None,
    tmp_dir: Path | str | None = None,
) -> BuiltArchive:
    if not isinstance(month, str) or not _MONTH_RE.match(month):
        raise ArchiveError(f"invalid month format: {month!r}, expected YYYY-MM")
    lock_fd = _acquire_chain_lock(audit_root(state_dir))
    try:
        return _build_archive_locked(month, state_dir, tmp_dir)
    finally:
        _release_lock(lock_fd)


def _build_archive_locked(
    month: str,
    state_dir: Path | str | None = None,
    tmp_dir: Path | str | None = None,
) -> BuiltArchive:
    """Build the month archive; the month must be a past one, or the caller must hold chain.lock."""
    root = audit_root(state_dir)
    month_dir = root / month
    if not month_dir.is_dir():
        raise ArchiveError(f"month directory not found: {month_dir}")

    day_files = sorted(p for p in month_dir.iterdir() if p.is_file() and _DAY_FILE_RE.match(p.name))
    if not day_files:
        raise ArchiveError(f"no day files found in {month_dir}")

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


def _month_snapshot(month_dir: Path) -> dict[str, tuple[int, int]]:
    snap: dict[str, tuple[int, int]] = {}
    for p in month_dir.iterdir():
        if p.is_file() and _DAY_FILE_RE.match(p.name):
            st = p.stat()
            snap[p.name] = (st.st_size, st.st_mtime_ns)
    return snap


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def _copy_file(src: Path, dst: Path) -> None:
    fd = os.open(dst, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            pass
        with src.open("rb") as fsrc:
            while chunk := fsrc.read(65536):
                os.write(fd, chunk)
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_file_0600(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            pass
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def archive_month(
    month: str | None = None,
    state_dir: Path | str | None = None,
    vault: Path | str | None = None,
    today: date | datetime | None = None,
) -> ArchiveResult:
    resolved_state_dir = paths().state_dir if state_dir is None else Path(state_dir)
    root = audit_root(resolved_state_dir)
    target_month = month if isinstance(month, str) else ""

    built: BuiltArchive | None = None
    chain_fd: int | None = None
    part_path: Path | None = None
    vault_manifest_part: Path | None = None
    local_manifest_part: Path | None = None

    try:
        if month is None:
            resolved_month = default_month(today=today)
        else:
            resolved_month = month
        target_month = str(resolved_month)
        resolved_month = check_month(resolved_month, today=today)
        target_month = resolved_month

        vault_root = load_vault() if vault is None else Path(vault).expanduser()
        if not vault_root.exists():
            raise ArchiveError(f"vault directory does not exist: {vault_root}")
        if not vault_root.is_dir():
            raise ArchiveError(f"vault path is not a directory: {vault_root}")

        vault_dir = vault_root / "agent-hub-audit"
        if vault_dir.exists() and not vault_dir.is_dir():
            raise ArchiveError(f"vault audit path is not a directory: {vault_dir}")

        vault_tar = vault_dir / f"{resolved_month}.tar.xz"
        vault_manifest = vault_dir / f"{resolved_month}.json"

        # (a) If the vault archive and manifest exist and the manifest sha256 matches the file -> noop
        if vault_dir.is_dir() and vault_tar.is_file() and vault_manifest.is_file():
            try:
                existing_manifest = json.loads(vault_manifest.read_text(encoding="utf-8"))
            except Exception:
                existing_manifest = None
            if isinstance(existing_manifest, dict):
                manifest_sha256 = existing_manifest.get("sha256")
                if isinstance(manifest_sha256, str) and manifest_sha256:
                    if _sha256_file(vault_tar) == manifest_sha256:
                        noop_detail: dict[str, Any] = {
                            "month": resolved_month,
                            "sha256": manifest_sha256,
                        }
                        if isinstance(existing_manifest.get("count"), int):
                            noop_detail["count"] = existing_manifest["count"]
                        emit(
                            "archive",
                            "archive",
                            state_dir=resolved_state_dir,
                            target=resolved_month,
                            status="noop",
                            exit_code=0,
                            evidence=f"sha256:{manifest_sha256}",
                            actor_role="archiver",
                            detail=noop_detail,
                        )
                        return ArchiveResult(
                            status="noop",
                            message=f"month {resolved_month} already archived",
                            exit_code=0,
                        )

        # Create vault subdirectory if the vault itself is an existing writable directory
        if not os.access(vault_root, os.W_OK | os.X_OK) and not vault_dir.exists():
            raise ArchiveError(f"vault directory is not writable: {vault_root}")
        vault_dir.mkdir(exist_ok=True)
        if not os.access(vault_dir, os.W_OK | os.X_OK):
            raise ArchiveError(f"vault audit directory is not writable: {vault_dir}")

        # (b) A past month is immutable (writer ts is monotonic): take chain.lock only to confirm the month is not the
        # current one, then pack and copy without it so writers keep their deadline
        chain_fd = _acquire_chain_lock(root)
        try:
            check_month(resolved_month, today=today)
        finally:
            _release_lock(chain_fd)
            chain_fd = None
        month_dir = root / resolved_month
        packed_snapshot = _month_snapshot(month_dir) if month_dir.is_dir() else {}
        built = _build_archive_locked(resolved_month, state_dir=resolved_state_dir)

        # (c) Copy to <name>.tar.xz.part in vault, fsync, rename, and compare sha256
        part_path = vault_dir / f"{resolved_month}.tar.xz.part"
        _copy_file(built.path, part_path)
        if not part_path.is_file():
            raise ArchiveError(f"failed to create archive part file: {part_path}")

        part_fd = os.open(part_path, os.O_RDONLY)
        try:
            os.fsync(part_fd)
        except OSError:
            pass
        finally:
            os.close(part_fd)

        part_sha256 = _sha256_file(part_path)
        if part_sha256 != built.sha256:
            raise ArchiveError(f"checksum mismatch for {part_path}: expected {built.sha256}, got {part_sha256}")

        os.replace(part_path, vault_tar)

        copied_sha256 = _sha256_file(vault_tar)
        if copied_sha256 != built.sha256:
            raise ArchiveError(f"checksum mismatch for {vault_tar}: expected {built.sha256}, got {copied_sha256}")

        # (d) Write the manifest and copy it to <state>/audit/manifests/YYYY-MM.json; it and the delete hold the lock
        chain_fd = _acquire_chain_lock(root)
        if _month_snapshot(month_dir) != packed_snapshot:
            vault_tar.unlink(missing_ok=True)
            vault_manifest.unlink(missing_ok=True)
            _release_lock(chain_fd)
            chain_fd = None
            retry_reason = "month changed during archive"
            emit(
                "archive",
                "archive",
                state_dir=resolved_state_dir,
                target=resolved_month,
                status="retry",
                exit_code=1,
                actor_role="archiver",
                detail={"month": resolved_month, "reason": retry_reason},
            )
            return ArchiveResult(status="retry", message=retry_reason, exit_code=1)
        created_iso = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        manifest_obj = {
            "month": resolved_month,
            "files": built.files,
            "count": built.count,
            "first_hash": built.first_hash,
            "first_prev_hash": built.first_prev_hash,
            "last_hash": built.last_hash,
            "sha256": built.sha256,
            "created": created_iso,
        }
        manifest_bytes = (json.dumps(manifest_obj, indent=2, sort_keys=True) + "\n").encode("utf-8")

        manifests_dir = root / "manifests"
        manifests_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(manifests_dir, 0o700)
        except OSError:
            pass

        local_manifest = manifests_dir / f"{resolved_month}.json"
        vault_manifest_part = vault_dir / f"{resolved_month}.json.part"
        local_manifest_part = manifests_dir / f"{resolved_month}.json.part"
        _write_file_0600(vault_manifest_part, manifest_bytes)
        shutil.copyfile(vault_manifest_part, local_manifest_part)
        try:
            os.chmod(local_manifest_part, 0o600)
        except OSError:
            pass
        os.replace(vault_manifest_part, vault_manifest)
        os.replace(local_manifest_part, local_manifest)

        # (e) ONLY THEN delete the local month directory <state>/audit/YYYY-MM/ and write an archive event status ok
        shutil.rmtree(month_dir)
        _release_lock(chain_fd)
        chain_fd = None

        emit(
            "archive",
            "archive",
            state_dir=resolved_state_dir,
            target=resolved_month,
            status="ok",
            exit_code=0,
            evidence=f"sha256:{built.sha256}",
            actor_role="archiver",
            detail={
                "month": resolved_month,
                "count": built.count,
                "sha256": built.sha256,
            },
        )
        return ArchiveResult(
            status="ok",
            message=f"archived {resolved_month} ({built.count} events, sha256={built.sha256})",
            exit_code=0,
        )
    except Exception as exc:
        if chain_fd is not None:
            _release_lock(chain_fd)
            chain_fd = None
        for p in (part_path, vault_manifest_part, local_manifest_part):
            if p is not None and p.exists():
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass
        reason = str(exc) or type(exc).__name__
        emit(
            "archive",
            "archive",
            state_dir=resolved_state_dir,
            target=target_month,
            status="error",
            exit_code=1,
            actor_role="archiver",
            detail={
                "month": target_month,
                "reason": reason,
            },
        )
        return ArchiveResult(
            status="error",
            message=reason,
            exit_code=1,
        )
    finally:
        if chain_fd is not None:
            _release_lock(chain_fd)
        if built is not None and built.path.exists():
            try:
                built.path.unlink(missing_ok=True)
            except OSError:
                pass
