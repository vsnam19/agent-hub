import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agenthub.audit.show import (
    format_line,
    outward,
    parse_since,
    read_events,
    select,
)


def test_parse_since_forms_and_junk() -> None:
    fixed_now = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)

    # 1. ISO date
    assert parse_since("2026-10-18", now=fixed_now) == "2026-10-18T00:00:00.000Z"

    # 2. ISO datetime without tz assumes UTC
    assert parse_since("2026-10-18T14:30:00", now=fixed_now) == "2026-10-18T14:30:00.000Z"

    # 3. ISO datetime with Z
    assert parse_since("2026-10-18T10:20:30.400Z", now=fixed_now) == "2026-10-18T10:20:30.400Z"
    assert parse_since("2026-10-18T10:20:30Z", now=fixed_now) == "2026-10-18T10:20:30.000Z"

    # 4. ISO datetime with offset converts to UTC
    assert parse_since("2026-10-18T14:00:00+02:00", now=fixed_now) == "2026-10-18T12:00:00.000Z"

    # 5. Relative forms with fixed now
    assert parse_since("2h", now=fixed_now) == "2026-10-19T10:00:00.000Z"
    assert parse_since("3d", now=fixed_now) == "2026-10-16T12:00:00.000Z"
    assert parse_since("30m", now=fixed_now) == "2026-10-19T11:30:00.000Z"
    assert parse_since("10s", now=fixed_now) == "2026-10-19T11:59:50.000Z"
    assert parse_since("1w", now=fixed_now) == "2026-10-12T12:00:00.000Z"

    # 6. Default now (when now is None) returns comparable UTC ISO string
    recent = parse_since("5m")
    assert recent.endswith("Z")
    assert len(recent) == 24

    # 7. Junk raises ValueError
    junk_inputs = [
        "junk",
        "not-a-date",
        "2x",
        "-2h",
        "",
        "   ",
        "2026-99-99",
        "2026-10-19T99:99:99",
        "h2",
    ]
    for junk in junk_inputs:
        with pytest.raises(ValueError):
            parse_since(junk)


def test_oldest_first_and_format_line(tmp_path: Path) -> None:
    # 1. Test format_line unit cases
    e1 = {
        "ts": "2026-10-18T10:00:00.000Z",
        "actor": {"role": "director", "host": "claude"},
        "kind": "command",
        "target": "git status",
        "outcome": {"status": "ok", "exit_code": 0},
        "quote": "",
    }
    assert format_line(e1) == "2026-10-18T10:00:00.000Z director:claude command git status ok exit=0"

    e2 = {
        "ts": "2026-10-18T10:05:00.000Z",
        "actor": {"agent": "agy:m2", "role": "worker", "host": "agy"},
        "kind": "tool",
        "target": "run_command",
        "outcome": {"status": "ok"},
        "quote": "running tests",
    }
    assert format_line(e2) == "2026-10-18T10:05:00.000Z agy:m2 tool run_command ok running tests"

    e3 = {
        "ts": "2026-10-18T10:10:00.000Z",
        "actor": {"role": "worker", "host": "claude"},
        "kind": "command",
        "target": "pytest",
        "outcome": {"status": "error", "exit_code": 1},
    }
    assert format_line(e3) == "2026-10-18T10:10:00.000Z worker:claude command pytest error exit=1"

    # 2. Test read_events ordering (oldest first) and unparsable line skipping
    audit_dir = tmp_path / "audit"
    m_sep = audit_dir / "2026-09"
    m_oct = audit_dir / "2026-10"
    m_sep.mkdir(parents=True, exist_ok=True)
    m_oct.mkdir(parents=True, exist_ok=True)

    # Write out-of-order files across months:
    # 2026-09-30 has e_sep
    # 2026-10-01 has e_oct1 and unparsable lines
    # 2026-10-02 has e_oct2
    e_sep = {
        "ts": "2026-09-30T23:59:00.000Z",
        "actor": {"role": "director", "host": "claude"},
        "kind": "session",
        "target": "start",
        "outcome": {"status": "ok"},
    }
    e_oct1_a = {
        "ts": "2026-10-01T08:00:00.000Z",
        "actor": {"role": "director", "host": "claude"},
        "kind": "command",
        "target": "ls",
        "outcome": {"status": "ok", "exit_code": 0},
    }
    e_oct1_b = {
        "ts": "2026-10-01T09:00:00.000Z",
        "actor": {"agent": "codex", "role": "worker", "host": "codex"},
        "kind": "tool",
        "target": "edit",
        "outcome": {"status": "ok"},
    }
    e_oct2 = {
        "ts": "2026-10-02T12:00:00.000Z",
        "actor": {"role": "director", "host": "agy"},
        "kind": "outward",
        "target": "git push",
        "outcome": {"status": "ok", "exit_code": 0},
    }

    (m_sep / "2026-09-30.jsonl").write_text(json.dumps(e_sep) + "\n", encoding="utf-8")

    # File with unparsable lines, non-dict lines, and empty lines
    oct1_content = (
        "\n".join(
            [
                json.dumps(e_oct1_a),
                "this is junk json line {{{{",
                "",
                "12345",
                "   ",
                json.dumps(e_oct1_b),
            ]
        )
        + "\n"
    )
    (m_oct / "2026-10-01.jsonl").write_text(oct1_content, encoding="utf-8")
    (m_oct / "2026-10-01.jsonl.lock").write_text("lock", encoding="utf-8")

    (m_oct / "2026-10-02.jsonl").write_text(json.dumps(e_oct2) + "\n", encoding="utf-8")

    # Also an unrelated directory or file in audit/
    (audit_dir / "manifests").mkdir(parents=True, exist_ok=True)
    (audit_dir / "manifests" / "2026-09.json").write_text('{"month": "2026-09"}', encoding="utf-8")

    events = list(read_events(state_dir=tmp_path))
    assert len(events) == 4
    assert events == [e_sep, e_oct1_a, e_oct1_b, e_oct2]

    # Check format_line on read events
    formatted = [format_line(ev) for ev in events]
    assert formatted == [
        "2026-09-30T23:59:00.000Z director:claude session start ok",
        "2026-10-01T08:00:00.000Z director:claude command ls ok exit=0",
        "2026-10-01T09:00:00.000Z codex tool edit ok",
        "2026-10-02T12:00:00.000Z director:agy outward git push ok exit=0",
    ]


def test_select_by_plan_and_job() -> None:
    events = [
        {
            "ts": "2026-10-18T10:00:00.000Z",
            "actor": {"role": "director", "host": "claude"},
            "parent": {"plan": "plan-A"},
            "kind": "plan",
        },
        {
            "ts": "2026-10-18T10:01:00.000Z",
            "actor": {"role": "worker", "host": "claude", "job_id": "job-1"},
            "parent": {"plan": "plan-A", "job": "job-1"},
            "kind": "job",
        },
        {
            "ts": "2026-10-18T10:02:00.000Z",
            "actor": {"role": "worker", "host": "agy", "job_id": "job-2"},
            "parent": {"plan": "plan-A", "job": "job-2"},
            "kind": "command",
        },
        {
            "ts": "2026-10-18T10:03:00.000Z",
            "actor": {"role": "worker", "host": "codex", "job_id": "job-standalone"},
            "parent": {},
            "kind": "command",
        },
        {
            "ts": "2026-10-18T10:04:00.000Z",
            "actor": {"role": "worker", "host": "claude"},
            "parent": {"plan": "plan-B", "job": "job-3"},
            "kind": "command",
        },
    ]

    # Empty filters return all events in order
    assert select(events) == events

    # Filter by plan only
    plan_a_events = select(events, plan="plan-A")
    assert plan_a_events == [events[0], events[1], events[2]]

    # Filter by job matching parent.job
    job_3_events = select(events, job="job-3")
    assert job_3_events == [events[4]]

    # Filter by job matching actor.job_id
    job_standalone_events = select(events, job="job-standalone")
    assert job_standalone_events == [events[3]]

    # Filter by both plan and job
    plan_a_job_2 = select(events, plan="plan-A", job="job-2")
    assert plan_a_job_2 == [events[2]]

    # Mismatched combination returns empty
    assert select(events, plan="plan-B", job="job-2") == []


def test_select_by_session_and_since() -> None:
    events = [
        {
            "ts": "2026-10-18T10:00:00.000Z",
            "actor": {"role": "director", "host": "claude", "session_id": "sess-alpha"},
            "parent": {},
            "kind": "session",
        },
        {
            "ts": "2026-10-18T11:00:00.000Z",
            "actor": {"role": "worker", "host": "claude"},
            "parent": {"director_session": "sess-alpha"},
            "kind": "command",
        },
        {
            "ts": "2026-10-18T12:00:00.000Z",
            "actor": {"role": "director", "host": "claude", "session_id": "sess-beta"},
            "parent": {},
            "kind": "session",
        },
        {
            "ts": "2026-10-18T13:00:00.000Z",
            "actor": {"role": "worker", "host": "agy"},
            "parent": {"director_session": "sess-alpha"},
            "kind": "command",
        },
    ]

    # Matches actor.session_id and parent.director_session
    alpha_events = select(events, session="sess-alpha")
    assert alpha_events == [events[0], events[1], events[3]]

    # Matches actor.session_id for beta
    beta_events = select(events, session="sess-beta")
    assert beta_events == [events[2]]

    # Filter by since
    since_11 = select(events, since="2026-10-18T11:00:00.000Z")
    assert since_11 == [events[1], events[2], events[3]]

    # Combine session and since
    alpha_since_11 = select(events, session="sess-alpha", since="2026-10-18T11:00:00.000Z")
    assert alpha_since_11 == [events[1], events[3]]

    # Since beyond all events
    assert select(events, since="2026-10-18T14:00:00.000Z") == []


def test_kind_filter_repeatable_and_gap() -> None:
    events = [
        {"ts": "2026-10-18T10:00:00.000Z", "kind": "session"},
        {"ts": "2026-10-18T10:01:00.000Z", "kind": "gap"},
        {"ts": "2026-10-18T10:02:00.000Z", "kind": "command"},
        {"ts": "2026-10-18T10:03:00.000Z", "kind": "gap"},
        {"ts": "2026-10-18T10:04:00.000Z", "kind": "tool"},
    ]

    # kinds=() means all kinds allowed
    assert select(events, kinds=()) == events

    # Single kind "gap"
    gaps = select(events, kinds=["gap"])
    assert gaps == [events[1], events[3]]

    # Repeatable kind filter (set/tuple/list of allowed kinds)
    subset = select(events, kinds=("gap", "command"))
    assert subset == [events[1], events[2], events[3]]

    # Kind filter with no matches
    assert select(events, kinds=["outward"]) == []


def test_outward_only_with_since() -> None:
    events = [
        {"ts": "2026-10-18T09:00:00.000Z", "kind": "command", "target": "git fetch"},
        {"ts": "2026-10-18T10:00:00.000Z", "kind": "outward", "target": "git push origin main"},
        {"ts": "2026-10-18T11:00:00.000Z", "kind": "command", "target": "cargo test"},
        {"ts": "2026-10-18T12:00:00.000Z", "kind": "outward", "target": "gh pr create"},
    ]

    # outward with since=None
    all_outward = outward(events)
    assert all_outward == [events[1], events[3]]

    # outward with since
    recent_outward = outward(events, since="2026-10-18T11:00:00.000Z")
    assert recent_outward == [events[3]]

    # outward with since before all
    early_outward = outward(events, since="2026-10-18T08:00:00.000Z")
    assert early_outward == [events[1], events[3]]

    # outward with since after all
    none_outward = outward(events, since="2026-10-18T13:00:00.000Z")
    assert none_outward == []


def test_parse_since_overflow_is_value_error() -> None:
    overflow_inputs = [
        "999999999999999999999999999999999999999d",
        "100000000000000d",
        "100000000d",
    ]
    for expr in overflow_inputs:
        with pytest.raises(ValueError, match="invalid since expression"):
            parse_since(expr)


def test_parse_since_bare_month() -> None:
    assert parse_since("2026-09") == "2026-09-01T00:00:00.000Z"
    assert parse_since("2025-01") == "2025-01-01T00:00:00.000Z"
    assert parse_since("  2026-12  ") == "2026-12-01T00:00:00.000Z"

    invalid_months = ["2026-00", "2026-13", "2026-99"]
    for inv in invalid_months:
        with pytest.raises(ValueError, match="invalid since expression"):
            parse_since(inv)
