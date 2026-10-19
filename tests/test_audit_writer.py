import fcntl
import json
import multiprocessing
import os
import re
import stat
import time
from pathlib import Path

import pytest

from agenthub.audit import writer
from agenthub.audit.verify import verify
from agenthub.audit.writer import (
    ZERO_HASH,
    AuditLockTimeout,
    append,
    audit_root,
    canonical_json,
    event_hash,
)


def _worker_append_batch(state_dir_str: str, worker_id: int, count: int) -> None:
    state_dir = Path(state_dir_str)
    for i in range(count):
        append(
            {
                "ts": "2026-10-19T12:00:00.000Z",
                "actor": {"role": "worker", "host": "claude", "job_id": f"job-{worker_id}"},
                "kind": "command",
                "target": f"cmd-{worker_id}-{i}",
                "outcome": {"status": "ok", "exit_code": 0},
            },
            state_dir=state_dir,
        )


def test_first_event_ever_uses_zero_hash(tmp_path: Path) -> None:
    assert ZERO_HASH == "0" * 64
    ev = append(
        {
            "ts": "2026-10-18T10:20:30.400Z",
            "actor": {"role": "director", "host": "claude", "session_id": "s1"},
            "kind": "command",
            "target": "git status",
            "outcome": {"status": "ok", "exit_code": 0},
        },
        state_dir=tmp_path,
    )
    assert ev["seq"] == 1
    assert ev["prev_hash"] == ZERO_HASH
    assert ev["hash"] == event_hash(ZERO_HASH, ev)


def test_append_sets_chain_fields_and_modes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_HUB_STATE", str(tmp_path))
    old_umask = os.umask(0o000)
    try:
        ev = append(
            {
                "actor": {"role": "director", "host": "agy", "session_id": "conv-1"},
                "kind": "tool",
                "target": "search",
                "outcome": {"status": "ok"},
            }
        )
    finally:
        os.umask(old_umask)

    assert ev["schema_version"] == 1
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", ev["ts"])
    assert re.fullmatch(r"[0-9a-f]{32}", ev["event_id"])
    assert ev["seq"] == 1
    assert ev["prev_hash"] == ZERO_HASH
    assert re.fullmatch(r"[0-9a-f]{64}", ev["hash"])
    assert ev["hash"] == event_hash(ZERO_HASH, ev)
    assert ev["actor"] == {"role": "director", "host": "agy", "session_id": "conv-1"}
    assert ev["kind"] == "tool"
    assert ev["target"] == "search"
    assert ev["outcome"] == {"status": "ok"}

    day = ev["ts"][:10]
    month = day[:7]
    root = audit_root()
    assert root == tmp_path / "audit"
    monkeypatch.delenv("AGENT_HUB_STATE", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    assert audit_root() == tmp_path / "xdg" / "agent-hub" / "audit"
    monkeypatch.setenv("AGENT_HUB_STATE", str(tmp_path))
    month_dir = root / month
    day_file = month_dir / f"{day}.jsonl"
    lock_file = root / "chain.lock"

    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(month_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(day_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(lock_file.stat().st_mode) == 0o600

    lines = day_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == ev
    assert lines[0] == canonical_json(ev)

    # Explicit ts and event_id are preserved when provided by the caller
    ev2 = append(
        {
            "ts": f"{day}T23:59:59.999Z",
            "event_id": "deadbeef" * 4,
            "kind": "session",
            "target": "stop",
            "outcome": {"status": "ok"},
        },
        state_dir=tmp_path,
    )
    assert ev2["ts"] == f"{day}T23:59:59.999Z"
    assert ev2["event_id"] == "deadbeef" * 4
    assert ev2["seq"] == 2
    assert ev2["prev_hash"] == ev["hash"]

    # Lock contention raises AuditLockTimeout after retrying
    with lock_file.open("r+b") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with pytest.raises(AuditLockTimeout):
                append({"ts": f"{day}T23:59:59.999Z", "kind": "tool"}, state_dir=tmp_path)
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


def test_append_seq_increments_and_links(tmp_path: Path) -> None:
    ts = "2026-10-18T14:00:00.000Z"
    e1 = append({"ts": ts, "kind": "command", "target": "one"}, state_dir=tmp_path)
    e2 = append({"ts": ts, "kind": "command", "target": "two"}, state_dir=tmp_path)
    e3 = append({"ts": ts, "kind": "command", "target": "three"}, state_dir=tmp_path)

    assert [e1["seq"], e2["seq"], e3["seq"]] == [1, 2, 3]
    assert e1["prev_hash"] == ZERO_HASH
    assert e2["prev_hash"] == e1["hash"]
    assert e3["prev_hash"] == e2["hash"]
    for ev in (e1, e2, e3):
        assert ev["hash"] == event_hash(ev["prev_hash"], ev)

    day_file = audit_root(tmp_path) / "2026-10" / "2026-10-18.jsonl"
    on_disk = [json.loads(line) for line in day_file.read_text(encoding="utf-8").splitlines()]
    assert on_disk == [e1, e2, e3]


def test_day_comes_from_event_ts(tmp_path: Path) -> None:
    e1 = append(
        {"ts": "2026-09-30T23:59:59.900Z", "kind": "command", "target": "sep"},
        state_dir=tmp_path,
    )
    e2 = append(
        {"ts": "2026-10-01T00:00:00.100Z", "kind": "command", "target": "oct"},
        state_dir=tmp_path,
    )

    sep_file = audit_root(tmp_path) / "2026-09" / "2026-09-30.jsonl"
    oct_file = audit_root(tmp_path) / "2026-10" / "2026-10-01.jsonl"
    assert sep_file.is_file()
    assert oct_file.is_file()

    sep_events = [json.loads(line) for line in sep_file.read_text(encoding="utf-8").splitlines()]
    oct_events = [json.loads(line) for line in oct_file.read_text(encoding="utf-8").splitlines()]
    assert sep_events == [e1]
    assert oct_events == [e2]
    assert e1["seq"] == 1
    assert e2["seq"] == 1
    assert e2["prev_hash"] == e1["hash"]


def test_first_event_links_previous_day_and_archive_manifest(tmp_path: Path) -> None:
    # 1. Links to the most recent earlier day file
    state_a = tmp_path / "state_a"
    d1_first = append({"ts": "2026-10-15T08:00:00.000Z", "kind": "command", "target": "a"}, state_dir=state_a)
    d1_last = append({"ts": "2026-10-15T09:00:00.000Z", "kind": "command", "target": "b"}, state_dir=state_a)
    assert d1_first["seq"] == 1
    assert d1_last["seq"] == 2

    d2_first = append({"ts": "2026-10-18T10:00:00.000Z", "kind": "command", "target": "c"}, state_dir=state_a)
    assert d2_first["seq"] == 1
    assert d2_first["prev_hash"] == d1_last["hash"]
    assert d2_first["hash"] == event_hash(d1_last["hash"], d2_first)

    # 2. When earlier month directory is gone, links to most recent archived month manifest
    state_b = tmp_path / "state_b"
    manifests_dir = audit_root(state_b) / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)
    older_manifest_hash = "1" * 64
    recent_manifest_hash = "2" * 64
    (manifests_dir / "2026-08.json").write_text(
        json.dumps({"month": "2026-08", "last_hash": older_manifest_hash}),
        encoding="utf-8",
    )
    (manifests_dir / "2026-09.json").write_text(
        json.dumps({"month": "2026-09", "last_hash": recent_manifest_hash}),
        encoding="utf-8",
    )

    oct_first = append(
        {"ts": "2026-10-01T00:00:01.000Z", "kind": "command", "target": "after-archive"},
        state_dir=state_b,
    )
    assert oct_first["seq"] == 1
    assert oct_first["prev_hash"] == recent_manifest_hash
    assert oct_first["hash"] == event_hash(recent_manifest_hash, oct_first)


def test_concurrent_processes_keep_chain(tmp_path: Path) -> None:
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_worker_append_batch, args=(str(tmp_path), worker_id, 25)) for worker_id in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)
        assert p.exitcode == 0

    day_file = audit_root(tmp_path) / "2026-10" / "2026-10-19.jsonl"
    lines = day_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 100

    events = [json.loads(line) for line in lines]
    assert [ev["seq"] for ev in events] == list(range(1, 101))

    expected_prev = ZERO_HASH
    for ev in events:
        assert ev["prev_hash"] == expected_prev
        assert ev["hash"] == event_hash(ev["prev_hash"], ev)
        expected_prev = ev["hash"]


def _worker_two_days(state_dir_str: str, worker_id: int, count: int) -> None:
    for i in range(count):
        day = "2026-10-18" if (worker_id + i) % 2 == 0 else "2026-10-19"
        append(
            {"ts": f"{day}T12:00:00.000Z", "kind": "command", "target": f"cmd-{worker_id}-{i}"},
            state_dir=Path(state_dir_str),
        )


def test_single_chain_lock_across_days(tmp_path: Path) -> None:
    append({"ts": "2026-10-18T10:00:00.000Z", "kind": "command", "target": "a"}, state_dir=tmp_path)
    append({"ts": "2026-10-19T10:00:00.000Z", "kind": "command", "target": "b"}, state_dir=tmp_path)
    root = audit_root(tmp_path)
    assert list(root.rglob("*.lock")) == [root / "chain.lock"]
    assert stat.S_IMODE((root / "chain.lock").stat().st_mode) == 0o600
    with (root / "chain.lock").open("r+b") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with pytest.raises(AuditLockTimeout):
                append({"ts": "2026-10-20T10:00:00.000Z", "kind": "command"}, state_dir=tmp_path)
            with pytest.raises(AuditLockTimeout):
                append({"ts": "2026-10-18T10:00:00.000Z", "kind": "command"}, state_dir=tmp_path)
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


def test_late_ts_is_raised_and_kept_as_event_ts(tmp_path: Path) -> None:
    e1 = append({"ts": "2026-10-19T10:00:00.000Z", "kind": "command", "target": "new"}, state_dir=tmp_path)
    late = "2026-10-18T09:00:00.000Z"
    e2 = append({"ts": late, "kind": "command", "target": "late"}, state_dir=tmp_path)
    assert e2["ts"] == e1["ts"]
    assert e2["event_ts"] == late
    assert e2["seq"] == 2
    assert e2["prev_hash"] == e1["hash"]
    assert "event_ts" not in e1
    assert not (audit_root(tmp_path) / "2026-10" / "2026-10-18.jsonl").exists()
    assert verify(tmp_path).ok


def test_concurrent_writers_two_days_keep_chain(tmp_path: Path) -> None:
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_worker_two_days, args=(str(tmp_path), w, 15)) for w in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0
    root = audit_root(tmp_path)
    events = [
        json.loads(line) for f in sorted(root.glob("*/*.jsonl")) for line in f.read_text(encoding="utf-8").splitlines()
    ]
    assert len(events) == 60
    assert verify(tmp_path).ok


def test_lock_deadline_shared_by_gap_and_event(tmp_path: Path) -> None:
    root = audit_root(tmp_path)
    root.mkdir(parents=True)
    (tmp_path / "audit-pending-gap.jsonl").write_text('{"ts":"2026-10-18T00:00:00.000Z","reason":"x"}\n')
    with (root / "chain.lock").open("a+b") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            start = time.monotonic()
            ok = writer.write({"ts": "2026-10-19T10:00:00.000Z", "kind": "command"}, state_dir=tmp_path)
            elapsed = time.monotonic() - start
            with pytest.raises(AuditLockTimeout):
                append({"kind": "command"}, state_dir=tmp_path, deadline=time.monotonic() - 1)
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
    assert ok is False
    assert 0.18 <= elapsed
    # the claim never happened: the old entry and the new failure are both still pending
    lines = (tmp_path / "audit-pending-gap.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert not list(tmp_path.glob("*.claimed"))
