import json

from agenthub.cli import _size_warnings, main

SMALL = (
    "## Goal\n\nfix the thing\n\n"
    "## Input\n\nfoo\n\n"
    "## Expected output\n\n1. bar\n\n"
    "## Constraints\n\n- Only change `agenthub/x.py`.\n\n"
    "## Gate\n\njust lint\n"
)
LARGE = (
    "## Goal\n\nbig change\n\n"
    "## Input\n\nfoo\n\n"
    "## Expected output\n\n1. a\n2. b\n3. c\n4. d\n5. e\n\n"
    "## Constraints\n\nnone\n\n"
    "## Gate\n\njust lint\n"
)
HINT = "agent-hub: warning: a large brief may be on purpose; split it or use 'agent-hub plan draft'"


def write_brief(tmp_path, text):
    f = tmp_path / "brief.md"
    f.write_text(text)
    return f


def test_run_large_brief_warns_and_still_runs(env_sandbox, repo, tmp_path, capsys):
    brief = write_brief(tmp_path, LARGE)
    rc = main(["run", "--role", "implement", "--brief", str(brief), "-d", str(repo), "--agent", "codex:m"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "agent-hub: warning: brief size:" in err
    assert HINT in err.splitlines()


def test_run_json_large_brief_no_warning(env_sandbox, repo, tmp_path, capsys):
    brief = write_brief(tmp_path, LARGE)
    rc = main(["run", "--role", "implement", "--brief", str(brief), "-d", str(repo), "--agent", "codex:m", "--json"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "warning" not in captured.err
    assert json.loads(captured.out)["status"] == "ok"


def test_run_small_brief_no_warning(env_sandbox, repo, tmp_path, capsys):
    brief = write_brief(tmp_path, SMALL)
    rc = main(["run", "--role", "implement", "--brief", str(brief), "-d", str(repo), "--agent", "codex:m"])
    assert rc == 0
    assert "warning" not in capsys.readouterr().err


def test_run_review_large_brief_no_warning(env_sandbox, repo, tmp_path, capsys):
    brief = write_brief(tmp_path, LARGE)
    rc = main(["run", "--role", "review", "--brief", str(brief), "-d", str(repo), "--agent", "codex:m"])
    assert rc == 0
    assert "warning" not in capsys.readouterr().err


def test_size_warnings_prefix():
    lines = _size_warnings(LARGE, "task t1: ")
    problems = lines[:-1]
    assert problems, "a large brief must produce at least one problem line"
    for line in problems:
        assert line.startswith("agent-hub: warning: task t1: brief size: ")
    assert lines[-1] == HINT
