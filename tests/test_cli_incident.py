import json

from agenthub.cli import main
from agenthub.config import paths

OLD_TS = "2020-01-01T00:00:00.000Z"


def _seed(rows):
    path = paths().incidents_file
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def test_incident_add_writes_line(env_sandbox, capsys):
    assert main(["incident", "add", "--kind", "review-stall", "--what", "reviewer hung"]) == 0
    assert capsys.readouterr().out == "logged incident review-stall\n"

    lines = paths().incidents_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["kind"] == "review-stall"
    assert row["what"] == "reviewer hung"
    assert row["source"] == "director"


def test_incident_add_missing_what_exits_2(env_sandbox, capsys):
    assert main(["incident", "add", "--kind", "review-stall"]) == 2
    assert not paths().incidents_file.exists()


def test_incident_add_json_prints_record(env_sandbox, capsys):
    assert (
        main(["incident", "add", "--kind", "stall", "--what", "hung", "--source", "tool", "--attempts", "3", "--json"])
        == 0
    )
    record = json.loads(capsys.readouterr().out)
    assert record["kind"] == "stall"
    assert record["what"] == "hung"
    assert record["source"] == "tool"
    assert record["cost"] == {"attempts": 3}
    assert record["ts"].endswith("Z")


def test_incident_list_prints_oldest_first(env_sandbox, capsys):
    _seed([{"ts": OLD_TS, "kind": "first", "source": "director", "what": "old one"}])
    assert main(["incident", "add", "--kind", "second", "--what", "new one", "--lesson", "check early"]) == 0
    capsys.readouterr()

    assert main(["incident", "list"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2
    assert lines[0].endswith("director  first  old one")
    assert lines[1].endswith("director  second  new one  — check early")


def test_incident_list_filters_by_kind(env_sandbox, capsys):
    _seed(
        [
            {"ts": OLD_TS, "kind": "keep", "source": "director", "what": "kept"},
            {"ts": OLD_TS, "kind": "drop", "source": "director", "what": "dropped"},
        ]
    )

    assert main(["incident", "list", "--kind", "keep", "--json"]) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [r["what"] for r in rows] == ["kept"]


def test_incident_list_since_filters_old_rows(env_sandbox, capsys):
    _seed([{"ts": OLD_TS, "kind": "old", "source": "director", "what": "ancient"}])
    assert main(["incident", "add", "--kind", "recent", "--what", "fresh"]) == 0
    capsys.readouterr()

    assert main(["incident", "list", "--since", "7d", "--json"]) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [r["kind"] for r in rows] == ["recent"]
