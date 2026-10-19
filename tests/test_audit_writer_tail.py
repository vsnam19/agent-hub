import json
from pathlib import Path

from agenthub.audit.verify import verify
from agenthub.audit.writer import ZERO_HASH, append, audit_root


def _all_day_lines(day_file: Path) -> list[str]:
    return [line for line in day_file.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_append_after_torn_tail_line(tmp_path: Path) -> None:
    ev1 = append(
        {"ts": "2026-10-18T10:00:00.000Z", "kind": "tool", "target": "first"},
        state_dir=tmp_path,
    )
    day_file = audit_root(tmp_path) / "2026-10" / "2026-10-18.jsonl"
    assert day_file.is_file()

    # Append a torn tail line without a closing brace or newline
    with day_file.open("a", encoding="utf-8") as f:
        f.write('{"ts": "2026-10-18T10:01:00.000Z", "kind": "torn')

    ev2 = append(
        {"ts": "2026-10-18T10:02:00.000Z", "kind": "tool", "target": "second"},
        state_dir=tmp_path,
    )

    assert ev2["kind"] == "tool"
    assert ev2["target"] == "second"
    assert ev2["seq"] == 3
    assert ev2["prev_hash"] != ZERO_HASH
    assert ev2["prev_hash"] != ev1["hash"]


def test_corrupt_tail_writes_one_gap(tmp_path: Path) -> None:
    ev1 = append(
        {"ts": "2026-10-18T10:00:00.000Z", "kind": "tool", "target": "first"},
        state_dir=tmp_path,
    )
    day_file = audit_root(tmp_path) / "2026-10" / "2026-10-18.jsonl"
    with day_file.open("a", encoding="utf-8") as f:
        f.write("{broken-json-tail\n")

    ev2 = append(
        {"ts": "2026-10-18T10:02:00.000Z", "kind": "tool", "target": "second"},
        state_dir=tmp_path,
    )

    lines = _all_day_lines(day_file)
    assert len(lines) == 4
    # Line 1: ev1
    # Line 2: corrupt line
    # Line 3: gap event
    # Line 4: ev2
    gap_ev = json.loads(lines[2])
    assert gap_ev["kind"] == "gap"
    assert gap_ev["seq"] == 2
    assert gap_ev["prev_hash"] == ev1["hash"]
    assert gap_ev["detail"]["reason"] == "corrupt-tail 2026-10/2026-10-18.jsonl:2"
    assert ev2["prev_hash"] == gap_ev["hash"]

    # Subsequent append must not repeat the gap event
    ev3 = append(
        {"ts": "2026-10-18T10:03:00.000Z", "kind": "tool", "target": "third"},
        state_dir=tmp_path,
    )
    lines_after = _all_day_lines(day_file)
    assert len(lines_after) == 5
    gaps = [json.loads(line) for line in lines_after if line.startswith('{"') and '"kind":"gap"' in line]
    assert len(gaps) == 1
    assert ev3["seq"] == 4
    assert ev3["prev_hash"] == ev2["hash"]

    # Also verify multiple corrupt lines emit one gap per corrupt line
    dir2 = tmp_path / "sub2"
    ev_a = append(
        {"ts": "2026-10-18T10:00:00.000Z", "kind": "tool", "target": "a"},
        state_dir=dir2,
    )
    df2 = audit_root(dir2) / "2026-10" / "2026-10-18.jsonl"
    with df2.open("a", encoding="utf-8") as f:
        f.write("{torn-line-2\n{torn-line-3\n")

    ev_b = append(
        {"ts": "2026-10-18T10:02:00.000Z", "kind": "tool", "target": "b"},
        state_dir=dir2,
    )
    lines_multi = _all_day_lines(df2)
    assert len(lines_multi) == 6
    g2 = json.loads(lines_multi[3])
    g3 = json.loads(lines_multi[4])
    assert g2["kind"] == "gap" and g2["seq"] == 2
    assert g2["detail"]["reason"] == "corrupt-tail 2026-10/2026-10-18.jsonl:2"
    assert g3["kind"] == "gap" and g3["seq"] == 3
    assert g3["detail"]["reason"] == "corrupt-tail 2026-10/2026-10-18.jsonl:3"
    assert g2["prev_hash"] == ev_a["hash"]
    assert g3["prev_hash"] == g2["hash"]
    assert ev_b["prev_hash"] == g3["hash"]
    assert ev_b["seq"] == 4


def test_corrupt_tail_lines_are_kept(tmp_path: Path) -> None:
    append(
        {"ts": "2026-10-18T10:00:00.000Z", "kind": "tool", "target": "first"},
        state_dir=tmp_path,
    )
    day_file = audit_root(tmp_path) / "2026-10" / "2026-10-18.jsonl"
    corrupt_text = "corrupt-line-never-delete-or-rewrite-me"
    with day_file.open("a", encoding="utf-8") as f:
        f.write(corrupt_text + "\n")

    append(
        {"ts": "2026-10-18T10:02:00.000Z", "kind": "tool", "target": "second"},
        state_dir=tmp_path,
    )

    raw_lines = day_file.read_text(encoding="utf-8").splitlines()
    assert len(raw_lines) == 4
    assert raw_lines[1] == corrupt_text


def test_verify_still_reports_corrupt_line(tmp_path: Path) -> None:
    append(
        {"ts": "2026-10-18T10:00:00.000Z", "kind": "tool", "target": "first"},
        state_dir=tmp_path,
    )
    day_file = audit_root(tmp_path) / "2026-10" / "2026-10-18.jsonl"
    with day_file.open("a", encoding="utf-8") as f:
        f.write("torn-line-at-line-2\n")

    append(
        {"ts": "2026-10-18T10:02:00.000Z", "kind": "tool", "target": "second"},
        state_dir=tmp_path,
    )

    res = verify(state_dir=tmp_path)
    assert res.ok is False
    assert "2026-10/2026-10-18.jsonl:2" in res.message


def test_unreadable_manifest_falls_back(tmp_path: Path) -> None:
    manifests_dir = audit_root(tmp_path) / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)

    older_manifest = manifests_dir / "2026-08.json"
    older_manifest.write_text(
        json.dumps(
            {
                "month": "2026-08",
                "count": 10,
                "first_hash": "a" * 64,
                "first_prev_hash": ZERO_HASH,
                "last_hash": "b" * 64,
                "sha256": "c" * 64,
            }
        ),
        encoding="utf-8",
    )

    # Newer manifest is unreadable
    newer_manifest = manifests_dir / "2026-09.json"
    newer_manifest.write_text("{broken-json\n", encoding="utf-8")

    ev = append(
        {"ts": "2026-10-01T10:00:00.000Z", "kind": "tool", "target": "first"},
        state_dir=tmp_path,
    )

    assert ev["seq"] == 1
    assert ev["prev_hash"] == "b" * 64
