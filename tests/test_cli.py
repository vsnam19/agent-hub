import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agenthub.cli import main
from agenthub.config import paths
from agenthub.jobs import list_jobs
from agenthub.pool import Cooldowns, Link

BRIEF = "## Input\n\nfoo\n\n## Expected output\n\nbar\n"


def write_brief(tmp_path, text=BRIEF):
    f = tmp_path / "brief.md"
    f.write_text(text)
    return f


def write_pool(env_sandbox, text):
    (env_sandbox.config / "pool.toml").write_text(text)


def test_brief_missing_headings_exit_2(env_sandbox, repo, tmp_path, capsys):
    brief = write_brief(tmp_path, "just do it\n")
    rc = main(["run", "--role", "implement", "--brief", str(brief), "-d", str(repo)])
    assert rc == 2
    err = capsys.readouterr().err
    assert err.startswith("agent-hub: ")
    assert "Input" in err and "Expected output" in err


def test_unknown_agent_in_pool_exit_2(env_sandbox, repo, tmp_path, capsys):
    write_pool(env_sandbox, '[roles.implement]\nchain = ["nosuch:model"]\n')
    brief = write_brief(tmp_path)
    rc = main(["run", "--role", "implement", "--brief", str(brief), "-d", str(repo)])
    assert rc == 2
    assert "nosuch" in capsys.readouterr().err


def test_agent_override_single_link(env_sandbox, repo, tmp_path, capsys):
    brief = write_brief(tmp_path)
    rc = main(["run", "--role", "implement", "--brief", str(brief), "-d", str(repo), "--agent", "claude:haiku"])
    assert rc == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines[0] == "status: ok"
    assert lines[1] == "agent: claude:haiku"
    assert lines[2] == "changed: 0 files"
    assert lines[3].startswith("job: ")
    assert lines[4] == ""
    assert lines[5] == "done"
    argv = json.loads((env_sandbox.fakelog / "claude.1.json").read_text())["argv"]
    assert "haiku" in argv
    assert not (env_sandbox.fakelog / "agy.1.json").exists()


def test_run_json_output_is_result(env_sandbox, repo, tmp_path, capsys):
    brief = write_brief(tmp_path)
    rc = main(["run", "--role", "implement", "--brief", str(brief), "-d", str(repo), "--agent", "codex:m", "--json"])
    assert rc == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "ok" and result["agent"] == "codex"
    on_disk = json.loads((paths().jobs_dir / result["id"] / "result.json").read_text())
    assert on_disk == result


def test_run_brief_from_stdin(env_sandbox, repo, monkeypatch, capsys):
    import io

    monkeypatch.setattr(sys, "stdin", io.StringIO(BRIEF))
    rc = main(["run", "--role", "implement", "--brief", "-", "-d", str(repo), "--agent", "claude:s", "--json"])
    assert rc == 0
    result = json.loads(capsys.readouterr().out)
    assert (paths().jobs_dir / result["id"] / "brief.md").read_text().startswith(BRIEF)


def test_run_refused_exit_3(env_sandbox, tmp_path, capsys):
    brief = write_brief(tmp_path)
    rc = main(["run", "--role", "implement", "--brief", str(brief), "-d", str(tmp_path / "nowhere")])
    assert rc == 3
    assert "status: refused" in capsys.readouterr().out


def test_run_bad_timeout_exit_2(env_sandbox, repo, tmp_path, capsys):
    brief = write_brief(tmp_path)
    rc = main(["run", "--role", "implement", "--brief", str(brief), "-d", str(repo), "--timeout", "5d"])
    assert rc == 2
    assert capsys.readouterr().err.startswith("agent-hub: ")


def test_pool_output_and_clear(env_sandbox, capsys):
    until = datetime.now(timezone.utc) + timedelta(hours=2)
    Cooldowns(paths().cooldowns_file).set(Link("agy", "claude-sonnet-4-6"), until)
    assert main(["pool"]) == 0
    out = capsys.readouterr().out
    assert "implement (implement, escalate → implement-hard):" in out
    assert "review (review):" in out
    sonnet = next(line for line in out.splitlines() if "agy:claude-sonnet-4-6" in line)
    assert line_has(sonnet, "installed", "9.9.9", "cooldown until")
    opus = next(line for line in out.splitlines() if "agy:claude-opus-4-6-thinking" in line)
    assert "cooldown" not in opus and opus.startswith("  ")

    assert main(["pool", "--clear", "agy:claude-sonnet-4-6"]) == 0
    assert capsys.readouterr().out.strip() == "cleared agy:claude-sonnet-4-6"
    assert main(["pool", "--clear", "agy:claude-sonnet-4-6"]) == 0
    assert capsys.readouterr().out.strip() == "no cooldown for agy:claude-sonnet-4-6"


def line_has(line, *needles):
    return all(n in line for n in needles)


def test_pool_json(env_sandbox, capsys):
    Cooldowns(paths().cooldowns_file).set(
        Link("codex", "gpt-5.6-terra"), datetime.now(timezone.utc) + timedelta(hours=1)
    )
    assert main(["pool", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert set(data["roles"]) == {"implement-small", "implement", "implement-hard", "review"}
    entry = next(e for e in data["roles"]["review"]["chain"] if e["link"] == "codex:gpt-5.6-terra")
    assert set(entry) == {"link", "installed", "version", "cooldown_until"}
    assert entry["link"] == "codex:gpt-5.6-terra"
    assert entry["installed"] is True and entry["version"] == "codex 9.9.9"
    assert entry["cooldown_until"] is not None
    other = next(e for e in data["roles"]["review"]["chain"] if e["link"] == "agy:gemini-3.1-pro-high")
    assert other["cooldown_until"] is None


def test_run_accepts_custom_implement_role(env_sandbox, repo, tmp_path, capsys):
    write_pool(
        env_sandbox,
        '[roles.quick]\nkind = "implement"\nchain = ["claude:haiku"]\n',
    )
    brief = write_brief(tmp_path, "missing headings")

    assert main(["run", "--role", "quick", "--brief", str(brief), "-d", str(repo)]) == 2
    assert "Input" in capsys.readouterr().err

    brief = write_brief(tmp_path)
    assert main(["run", "--role", "quick", "--brief", str(brief), "-d", str(repo), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["role"] == "quick"


def test_run_unknown_role_exit_2(env_sandbox, repo, tmp_path, capsys):
    rc = main(["run", "--role", "missing", "--brief", str(write_brief(tmp_path)), "-d", str(repo)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "unknown role 'missing'" in err
    assert "implement-small" in err and "review" in err


def test_pool_shows_kind_and_escalate(env_sandbox, capsys):
    assert main(["pool"]) == 0
    out = capsys.readouterr().out
    assert "implement-small (implement, escalate → implement):" in out
    assert "implement-hard (implement):" in out


def test_profile_prints_and_unknown_exit_2(env_sandbox, capsys):
    assert main(["profile", "codex"]) == 0
    assert "codex" in capsys.readouterr().out.lower()
    assert main(["profile", "nosuch"]) == 2
    assert capsys.readouterr().err.startswith("agent-hub: ")


def test_jobs_and_show(env_sandbox, repo, tmp_path, capsys):
    brief = write_brief(tmp_path)
    ids = []
    for model in ("a", "b"):
        main(
            [
                "run",
                "--role",
                "implement",
                "--brief",
                str(brief),
                "-d",
                str(repo),
                "--agent",
                f"claude:{model}",
                "--json",
            ]
        )
        ids.append(json.loads(capsys.readouterr().out)["id"])
        time.sleep(1.1)  # job ids have one-second resolution
    assert main(["jobs", "--limit", "5"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 2
    assert lines[0].startswith(max(ids)) and "claude:b" in lines[0] and "implement" in lines[0] and "ok" in lines[0]
    assert str(repo) in lines[0]
    assert main(["jobs", "--limit", "1"]) == 0
    assert len(capsys.readouterr().out.strip().splitlines()) == 1

    assert main(["show", ids[0]]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "status: ok" and "agent: claude:a" in out and out.rstrip().endswith("done")
    assert main(["show", ids[0], "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["id"] == ids[0]


def test_show_unknown_exit_2(env_sandbox, capsys):
    assert main(["show", "nope"]) == 2
    assert capsys.readouterr().err == "agent-hub: unknown job: nope\n"
    assert main(["cancel", "nope"]) == 2
    assert capsys.readouterr().err == "agent-hub: unknown job: nope\n"


def test_cancel_marks_orphaned_running_job(env_sandbox, capsys):
    from agenthub.jobs import new_job

    job = new_job(paths(), "implement", datetime.now(timezone.utc))
    job.write_json("result.json", {"id": job.id, "role": "implement", "status": "running", "attempts": []})
    (job.dir / "pid").write_text("999999999\n")
    assert main(["cancel", job.id]) == 0
    assert job.read_json("result.json")["status"] == "cancelled"
    assert job.read_json("result.json")["exit_code"] == 130


def test_cancel_running_job(env_sandbox, repo, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "slow")
    brief = write_brief(tmp_path)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "agenthub",
            "run",
            "--role",
            "implement",
            "--brief",
            str(brief),
            "-d",
            str(repo),
            "--agent",
            "claude:s",
            "--json",
        ],
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=Path(__file__).parent.parent,
    )
    try:
        deadline = time.time() + 20
        attempt = None
        while time.time() < deadline:
            found = list(paths().jobs_dir.glob("*/attempts/*/attempt.json"))
            if found:
                attempt = found[0]
                break
            time.sleep(0.1)
        assert attempt is not None, "attempt never started"
        job_id = attempt.parents[2].name
        pgid = json.loads(attempt.read_text())["pgid"]
        assert main(["cancel", job_id]) == 0
        out, _err = proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 130
    assert json.loads(out)["status"] == "cancelled"
    result = json.loads((paths().jobs_dir / job_id / "result.json").read_text())
    assert result["status"] == "cancelled"
    time.sleep(0.2)
    try:
        os.killpg(pgid, 0)
        group_alive = True
    except ProcessLookupError:
        group_alive = False
    assert not group_alive


def _running_job_with_pid_file(content):
    from agenthub.jobs import new_job

    job = new_job(paths(), "implement", datetime.now(timezone.utc))
    job.write_json("result.json", {"id": job.id, "role": "implement", "status": "running", "attempts": []})
    (job.dir / "pid").write_text(content)
    return job


def _record_signals(monkeypatch):
    calls = []
    monkeypatch.setattr(os, "kill", lambda *a: calls.append(("kill", a)))
    monkeypatch.setattr(os, "killpg", lambda *a: calls.append(("killpg", a)))
    return calls


def test_cancel_stale_pid_does_not_signal(env_sandbox, monkeypatch, capsys):
    from agenthub.procinfo import start_time

    wrong = (start_time(os.getpid()) or 0) + 1
    job = _running_job_with_pid_file(json.dumps({"pid": os.getpid(), "start_time": wrong}))
    calls = _record_signals(monkeypatch)
    assert main(["cancel", job.id]) == 0
    assert calls == []
    result = job.read_json("result.json")
    assert result["status"] == "cancelled"
    assert result["exit_code"] == 130


def test_cancel_legacy_int_pid_does_not_signal(env_sandbox, monkeypatch, capsys):
    job = _running_job_with_pid_file(f"{os.getpid()}\n")
    calls = _record_signals(monkeypatch)
    assert main(["cancel", job.id]) == 0
    assert calls == []
    assert job.read_json("result.json")["status"] == "cancelled"


def test_safe_strips_escape_on_tty(monkeypatch):
    from agenthub.cli import _safe

    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    assert _safe("a\x1b[31mb\x07c\n") == "a[31mbc\n"


def test_safe_passthrough_when_not_tty(monkeypatch):
    from agenthub.cli import _safe

    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    assert _safe("a\x1b[31mb\x07c\n") == "a\x1b[31mb\x07c\n"


def test_pool_output_sanitised_on_tty(env_sandbox, monkeypatch, capsys):
    from agenthub.agents.agy import AgyAdapter

    monkeypatch.setattr(AgyAdapter, "version", lambda self: "x\x1b[2Jy")
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    assert main(["pool"]) == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert "x[2Jy" in out


def test_error_message_sanitised_on_tty(env_sandbox, repo, tmp_path, monkeypatch, capsys):
    write_pool(env_sandbox, '[roles.implement]\nchain = ["bad\\u001bagent:m"]\n')
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    rc = main(["run", "--role", "implement", "--brief", str(write_brief(tmp_path)), "-d", str(repo)])
    assert rc == 2
    err = capsys.readouterr().err
    assert err.startswith("agent-hub: ")
    assert "bad" in err and "\x1b" not in err


def test_profile_sanitised_on_tty(env_sandbox, monkeypatch, capsys):
    class P:
        def read_text(self):
            return "profile\x1b[2Jtext\n"

    class Stub:
        profile_path = P()

    monkeypatch.setattr("agenthub.cli.registry", lambda: {"stub": Stub()})
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    assert main(["profile", "stub"]) == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert "profile[2Jtext" in out


def test_wait_returns_finished_job_result(env_sandbox, repo, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_AGY_SCENARIO", "ok")
    write_brief(repo)
    main(["run", "--role", "implement", "-d", str(repo), "--brief", str(repo / "brief.md")])
    capsys.readouterr()

    assert main(["wait", "-d", str(repo)]) == 0
    out, err = capsys.readouterr()
    assert "status: ok" in out


def test_wait_times_out_with_124(env_sandbox, repo, tmp_path, monkeypatch, capsys):
    import subprocess
    import sys

    monkeypatch.setenv("FAKE_AGY_SCENARIO", "slow")
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "slow")
    write_brief(repo)

    env = os.environ.copy()
    env["FAKE_AGY_SCENARIO"] = "slow"
    env["FAKE_CLAUDE_SCENARIO"] = "slow"
    p = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "agenthub",
            "run",
            "--role",
            "implement",
            "-d",
            str(repo),
            "--brief",
            str(repo / "brief.md"),
        ],
        env=env,
    )

    try:
        assert main(["wait", "-d", str(repo), "--timeout", "2s"]) == 124
        out, err = capsys.readouterr()
        assert "still running:" in out

        job_id = [r["id"] for r in list_jobs(paths(), 10)][0]
        main(["cancel", job_id])
    finally:
        p.wait()


def test_wait_follows_job_to_completion(env_sandbox, repo, tmp_path, capsys):
    import subprocess
    import sys

    write_brief(repo)
    write_pool(env_sandbox, '[roles.implement]\nchain = ["agy:claude-sonnet-4-6"]\n')

    env = os.environ.copy()
    env["FAKE_AGY_SCENARIO"] = "slow2"
    p = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "agenthub",
            "run",
            "--role",
            "implement",
            "-d",
            str(repo),
            "--brief",
            str(repo / "brief.md"),
        ],
        env=env,
    )

    try:
        assert main(["wait", "-d", str(repo), "--timeout", "30s"]) == 0
        out, err = capsys.readouterr()
        assert "status: ok" in out
    finally:
        p.wait()


def test_wait_no_job_exit_2(env_sandbox, repo, monkeypatch, capsys):
    import agenthub.cli as cli

    monkeypatch.setattr(cli, "WAIT_APPEAR_S", 0.5)
    assert main(["wait", "-d", str(repo)]) == 2
    out, err = capsys.readouterr()
    assert f"agent-hub: no job for {repo}" in err


def test_wait_json_output(env_sandbox, repo, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_AGY_SCENARIO", "ok")
    write_brief(repo)
    main(["run", "--role", "implement", "-d", str(repo), "--brief", str(repo / "brief.md")])
    capsys.readouterr()

    assert main(["wait", "-d", str(repo), "--json"]) == 0
    out, err = capsys.readouterr()
    import json

    data = json.loads(out)
    assert data["status"] == "ok"
    assert data["workdir"] == str(repo.resolve())


def test_wait_picks_newest_job_for_dir(env_sandbox, repo, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_AGY_SCENARIO", "ok,empty")
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "ok,empty")
    write_brief(repo)

    # Run first job (status: ok)
    main(["run", "--role", "implement", "-d", str(repo), "--brief", str(repo / "brief.md")])

    import time

    time.sleep(1.1)  # ensure a newer timestamp for the next job ID

    # Run second job (status: empty)
    main(["run", "--role", "implement", "-d", str(repo), "--brief", str(repo / "brief.md")])
    capsys.readouterr()

    assert main(["wait", "-d", str(repo)]) == 7
    out, err = capsys.readouterr()
    assert "status: empty" in out


def test_run_gate_flag_and_summary(capsys, monkeypatch, env_sandbox, repo, tmp_path):
    monkeypatch.setenv("FAKE_AGY_SCENARIO", "write")
    brief = tmp_path / "b"
    brief.write_text("## Input\n\n## Expected output\n")
    ret = main(
        [
            "run",
            "--role",
            "implement",
            "-d",
            str(repo),
            "--brief",
            str(brief),
            "--agent",
            "agy:m1",
            "--gate",
            "exit 0",
            "--gate",
            "echo ok",
        ]
    )
    assert ret == 0
    out = capsys.readouterr().out
    assert "gate: passed (2)" in out


def test_run_summary_shows_handoff(capsys, monkeypatch, env_sandbox, repo, tmp_path):
    monkeypatch.setenv("FAKE_AGY_SCENARIO", "handoff")
    brief = tmp_path / "b"
    brief.write_text("## Input\n\n## Expected output\n")
    ret = main(["run", "--role", "implement", "-d", str(repo), "--brief", str(brief), "--agent", "agy:m1"])
    assert ret == 0
    out = capsys.readouterr().out
    assert "handoff: success · unit-test-verified" in out


def test_wait_ignores_record_without_workdir(env_sandbox, repo, tmp_path, monkeypatch, capsys):
    import agenthub.cli as cli

    # Create a real finished job for repo
    monkeypatch.setenv("FAKE_AGY_SCENARIO", "ok")
    write_brief(repo)
    main(["run", "--role", "implement", "-d", str(repo), "--brief", str(repo / "brief.md")])
    capsys.readouterr()

    job1_dir = paths().jobs_dir / "job1"
    job1_dir.mkdir(parents=True)
    (job1_dir / "result.json").write_text(json.dumps({"status": "ok", "exit_code": 0}))

    # Wait for the real repo should find the real job, not the stray record
    assert main(["wait", "-d", str(repo)]) == 0
    out, err = capsys.readouterr()
    assert "status: ok" in out

    # From a cwd with only the stray record, it should timeout/exit 2
    monkeypatch.setattr(cli, "WAIT_APPEAR_S", 0.5)
    monkeypatch.chdir(tmp_path)
    assert main(["wait", "-d", "."]) == 2
    out, err = capsys.readouterr()
    assert "agent-hub: no job for ." in err


def test_orders_table_lists_rules_and_mechanisms(env_sandbox, repo, capsys):
    user_file = env_sandbox.config / "standing-orders.md"
    user_file.write_text(
        "1. Run the lint — enforced by: gate\n"
        "2. Never edit gate config — enforced by: gate-guard\n"
        "3. No network from tests — enforced by: none\n"
    )
    repo_file = repo / ".agent-hub" / "standing-orders.md"
    repo_file.parent.mkdir()
    repo_file.write_text("1. Only touch the brief's files — enforced by: scope\n")

    assert main(["orders", "-d", str(repo)]) == 0
    out, err = capsys.readouterr()
    assert "Run the lint" in out and "gate" in out
    assert "Never edit gate config" in out and "gate-guard" in out
    assert "No network from tests" in out and "none" in out
    assert "Only touch the brief's files" in out and "scope" in out
    assert str(user_file) in out and str(repo_file) in out
    assert out.index("Run the lint") < out.index("Only touch the brief's files")
    assert err == ""


def test_orders_exit_1_on_bare_line(env_sandbox, repo, capsys):
    (env_sandbox.config / "standing-orders.md").write_text("1. Enforced — enforced by: gate\n2. Bare rule\n")

    assert main(["orders", "-d", str(repo)]) == 1
    out, err = capsys.readouterr()
    assert "Bare rule" in out
    assert "order 2" in err and "Bare rule" in err


def test_orders_json_has_null_enforced_by_for_bare_line(env_sandbox, repo, capsys):
    (env_sandbox.config / "standing-orders.md").write_text("1. Enforced — enforced by: gate\n2. Bare rule\n")

    assert main(["orders", "-d", str(repo), "--json"]) == 1
    out, err = capsys.readouterr()
    rows = json.loads(out)["orders"]
    assert [(r["number"], r["rule"], r["enforced_by"]) for r in rows] == [
        (1, "Enforced", "gate"),
        (2, "Bare rule", None),
    ]


def test_orders_outside_repo_lists_user_file(env_sandbox, tmp_path, capsys):
    (env_sandbox.config / "standing-orders.md").write_text("1. Run the lint — enforced by: gate\n")
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()

    assert main(["orders", "-d", str(not_a_repo)]) == 0
    out, err = capsys.readouterr()
    assert "Run the lint" in out
    assert "not a git work tree" in err


def test_suite_uses_temp_state(tmp_path):
    import os

    state_dir = paths().state_dir
    assert str(tmp_path) in str(state_dir)
    assert os.environ.get("AGENT_HUB_STATE") is not None
