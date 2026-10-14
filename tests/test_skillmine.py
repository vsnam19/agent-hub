import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agenthub import skillmine, triage
from agenthub.cli import main

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
SINCE = NOW - timedelta(days=28)
RECENT = NOW - timedelta(days=2)
OLD = NOW - timedelta(days=60)


def make_job(
    state: Path,
    job_id: str,
    *,
    kind: str = "review",
    status: str = "ok",
    workdir: str = "/work/repo/.worktrees/feat-one",
    started: datetime = RECENT,
    brief: str = "# Brief: x\n",
    reply: str | None = None,
    verdict: str | None = None,
    agent: str = "claude",
    model: str = "sonnet",
) -> Path:
    job = state / "jobs" / job_id
    attempt = job / "attempts" / f"1-{agent}-{model}"
    attempt.mkdir(parents=True)
    (job / "brief.md").write_text(brief)
    result = {
        "id": job_id,
        "role": "review" if kind == "review" else "implement",
        "kind": kind,
        "status": status,
        "agent": agent,
        "model": model,
        "workdir": workdir,
        "repo": "/work/repo/.git",
        "base": None,
        "started": started.isoformat(),
        "verdict": verdict,
    }
    if reply is not None:
        (attempt / "reply.md").write_text(reply)
        result["reply_path"] = str(attempt / "reply.md")
    (job / "result.json").write_text(json.dumps(result))
    return job


def review_reply(*lines: str, verdict: str = "request-changes") -> str:
    return "## Findings\n" + "\n".join(lines) + f"\n\n## Verdict\n{verdict}\n"


def write_transcript(root: Path, records: list[dict], name: str = "s1") -> Path:
    project = root / "proj"
    project.mkdir(parents=True, exist_ok=True)
    path = project / f"{name}.jsonl"
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")
    return path


def user_msg(text: str, when: datetime = RECENT, session: str = "s1") -> dict:
    return {"type": "user", "message": {"content": text}, "timestamp": when.isoformat(), "sessionId": session}


def bash_msg(command: str, when: datetime = RECENT, session: str = "s1") -> dict:
    block = {"type": "tool_use", "name": "Bash", "input": {"command": command}}
    return {"type": "assistant", "message": {"content": [block]}, "timestamp": when.isoformat(), "sessionId": session}


def run_mine(tmp_path: Path, since: datetime = SINCE) -> skillmine.Extract:
    return skillmine.mine(tmp_path / "state", tmp_path / "claude", since, NOW)


def test_review_findings_parsed_with_both_dash_styles(tmp_path):
    reply = review_reply(
        "- high — agenthub/cli.py:42 — The flag is ignored",
        "- medium - tests/test_x.py:7-9 - A test has no assertion",
        "- low — docs/a.md — Typo in heading",
    )
    make_job(tmp_path / "state", "20261010-100000-review-aaaa", reply=reply, verdict="request-changes")

    findings = run_mine(tmp_path).findings

    assert [(f["severity"], f["file"], f["claim"]) for f in findings] == [
        ("high", "agenthub/cli.py", "The flag is ignored"),
        ("medium", "tests/test_x.py", "A test has no assertion"),
        ("low", "docs/a.md", "Typo in heading"),
    ]
    assert findings[0]["job"] == "20261010-100000-review-aaaa"
    assert findings[0]["branch"] == "feat-one"
    assert findings[0]["repo"] == "repo"


def test_category_normalises_paths_numbers_and_identifiers():
    claim = 'Function `parse_line` in agenthub/cli.py returns 42 for "abc" input'
    assert skillmine.category(claim) == "function <id> in <path> returns <n> for <str> input"
    long = " ".join(f"w{i}x" for i in range(30))
    assert len(skillmine.category(long).split()) == 12


def test_fix_rounds_counted_per_branch(tmp_path):
    state = tmp_path / "state"
    make_job(state, "20261010-100000-review-aaaa", started=RECENT, verdict="request-changes", reply=review_reply())
    make_job(
        state,
        "20261010-110000-implement-bbbb",
        kind="implement",
        started=RECENT + timedelta(hours=1),
        brief="# Brief: hub review round 1 fixes\n",
    )
    make_job(
        state,
        "20261010-120000-implement-cccc",
        kind="implement",
        started=RECENT + timedelta(hours=2),
        brief="# Brief: hub review round 2 fix — x\n",
    )
    make_job(
        state,
        "20261010-130000-review-dddd",
        started=RECENT + timedelta(hours=3),
        verdict="approve",
        reply=review_reply(verdict="approve"),
    )
    make_job(
        state,
        "20261010-140000-review-eeee",
        workdir="/work/repo/.worktrees/other",
        verdict="approve",
        reply=review_reply(verdict="approve"),
    )

    extract = run_mine(tmp_path)

    assert extract.fix_rounds["feat-one"] == {
        "fix_briefs": 2,
        "fix_brief_titles": ["# Brief: hub review round 1 fixes", "# Brief: hub review round 2 fix — x"],
        "reviews": 2,
        "verdicts": ["request-changes", "approve"],
    }
    assert extract.fix_rounds["other"]["reviews"] == 1
    assert extract.fix_rounds["other"]["fix_briefs"] == 0
    m = skillmine.metrics(extract)
    assert m["max_review_rounds"] == 2
    assert m["mean_review_rounds"] == 1.5
    assert m["fix_briefs_total"] == 2


def test_window_excludes_old_records(tmp_path):
    state = tmp_path / "state"
    old_reply = review_reply("- high — a.py:1 — Old finding")
    make_job(state, "20260801-100000-review-aaaa", started=OLD, reply=old_reply)
    make_job(state, "20261010-100000-review-bbbb", reply=review_reply("- low — b.py:1 — New finding"))
    write_transcript(
        tmp_path / "claude",
        [user_msg("don't do the old thing", OLD), user_msg("don't do the new thing", RECENT)],
    )

    extract = run_mine(tmp_path)

    assert [f["claim"] for f in extract.findings] == ["New finding"]
    assert [c["text"] for c in extract.corrections] == ["don't do the new thing"]
    assert sum(sum(v.values()) for v in extract.outcomes["by_role"].values()) == 1


def test_corrections_found_in_vietnamese_and_english(tmp_path):
    records = [
        user_msg("không được sửa file này"),
        user_msg("bạn phải chạy test"),
        user_msg("đừng commit"),
        user_msg("kết quả sai rồi"),
        user_msg("Don't push"),
        user_msg("never edit the justfile"),
        user_msg("that is wrong"),
        user_msg("you must run lint"),
        user_msg("looks good, continue"),
        user_msg("<command-name>/x</command-name> never"),
        {**user_msg("must meta"), "isMeta": True},
        {"type": "user", "message": {"content": [{"type": "text", "text": "never"}]}, "timestamp": RECENT.isoformat()},
    ]
    write_transcript(tmp_path / "claude", records)

    texts = [c["text"] for c in run_mine(tmp_path).corrections]

    assert texts == [
        "không được sửa file này",
        "bạn phải chạy test",
        "đừng commit",
        "kết quả sai rồi",
        "Don't push",
        "never edit the justfile",
        "that is wrong",
        "you must run lint",
    ]


def test_agenthub_commands_counted_as_3grams_per_session(tmp_path):
    cmds = ["agent-hub run --role x", "cd a && agent-hub wait j", "agent-hub show j", "ls"]
    write_transcript(tmp_path / "claude", [bash_msg(c) for c in cmds])

    commands = run_mine(tmp_path).agenthub_commands

    assert commands == {"run > wait > show": {"count": 1, "sessions": 1}}


def test_redact_removes_each_secret_kind():
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJBAKj34GkxFhD90vcNLYLInFEX6Ppy\n-----END RSA PRIVATE KEY-----"
    cases = {
        "sk": ("key sk-abcdefghijklmnop1234567890", "sk-abcdefghijklmnop1234567890"),
        "ghp": ("tok ghp_abcdefghijklmnopqrstuvwxyz0123456789", "ghp_abcdefghijklmnopqrstuvwxyz0123456789"),
        "xox": ("slack xoxb-123456789012-abcdefghijkl", "xoxb-123456789012-abcdefghijkl"),
        "akia": ("aws AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"),
        "bearer": ("Authorization: Bearer abc.DEF-123_xyz", "abc.DEF-123_xyz"),
        "jwt": ("jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N", "eyJhbGciOiJIUzI1NiJ9"),
        "kv": ("password=hunter2hunter2", "hunter2hunter2"),
        "kv2": ("API_TOKEN=abc123xyz", "abc123xyz"),
        "kv3": ("secret=topsecretvalue", "topsecretvalue"),
        "key": ("key=plainkeyvalue", "plainkeyvalue"),
        "pem": ("before\n" + pem + "\nafter", "MIIBOgIBAAJBAKj34GkxFhD90vcNLYLInFEX6Ppy"),
        "hex": ("sha 0123456789abcdef0123456789abcdef01234567", "0123456789abcdef0123456789abcdef01234567"),
        "b64": (
            "blob QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVowMTIzNDU2Nzg5",
            "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVowMTIzNDU2Nzg5",
        ),
        "email": ("mail someone@example.com now", "someone@example.com"),
    }
    for name, (text, secret) in cases.items():
        out = skillmine.redact(text)
        assert secret not in out, name
    assert skillmine.redact("see /home/alice/work/x.py") == "see ~/work/x.py"
    assert skillmine.redact("plain words and docs/superpowers/specs/design.md") == (
        "plain words and docs/superpowers/specs/design.md"
    )
    assert len(skillmine.clean("word " * 200)) == 300


def test_extract_contains_no_secret_from_any_source(tmp_path):
    state = tmp_path / "state"
    brief_secret = "ghp_" + "B" * 36
    reply_secret = "sk-" + "r3plyS3cr3t0123456789"
    transcript_secret = "AKIAIOSFODNN7EXAMPLE"
    make_job(
        state,
        "20261010-110000-implement-bbbb",
        kind="implement",
        workdir="/home/alice/repo/.worktrees/feat",
        brief=f"# Brief: hub review round 1 fixes\ntoken={brief_secret}\n",
    )
    make_job(
        state,
        "20261010-100000-review-aaaa",
        workdir="/home/alice/repo/.worktrees/feat",
        reply=review_reply(f"- high — a.py:1 — Leaks {reply_secret} to bob@example.com"),
    )
    write_transcript(
        tmp_path / "claude",
        [user_msg(f"don't leak {transcript_secret}"), bash_msg(f"agent-hub run --key={transcript_secret}")],
    )
    out = tmp_path / "out"

    extract = run_mine(tmp_path)
    skillmine.write(extract, out)
    text = (out / "extract.json").read_text() + (out / "extract.md").read_text() + (out / "metrics.json").read_text()

    for secret in (brief_secret, reply_secret, transcript_secret, "bob@example.com", "alice"):
        assert secret not in text
    assert "Leaks" in text


def test_unreadable_or_malformed_records_are_skipped(tmp_path):
    state = tmp_path / "state"
    make_job(state, "20261010-100000-review-aaaa", reply=review_reply("- low — a.py:1 — Good finding"))
    bad = state / "jobs" / "20261010-110000-review-bad1"
    bad.mkdir(parents=True)
    (bad / "result.json").write_text("{not json")
    listy = state / "jobs" / "20261010-120000-review-bad2"
    listy.mkdir(parents=True)
    (listy / "result.json").write_text("[1, 2]")
    nostart = state / "jobs" / "20261010-130000-review-bad3"
    nostart.mkdir(parents=True)
    (nostart / "result.json").write_text(json.dumps({"id": "x", "kind": "review", "started": "garbage"}))
    (state / "jobs" / "stray-file").write_text("x")
    binary = make_job(state, "20261010-140000-review-bad4", reply="x")
    (binary / "attempts" / "1-claude-sonnet" / "reply.md").write_bytes(b"\xff\xfe\x00 ## Findings\n")
    (state / "plans" / "p").mkdir(parents=True)
    (state / "plans" / "p" / "plan.json").write_text("nope")
    project = tmp_path / "claude" / "proj"
    project.mkdir(parents=True)
    (project / "s.jsonl").write_bytes(
        b"garbage\n[1]\n"
        + json.dumps(user_msg("never do that")).encode()
        + b"\n\xff\xfe\n"
        + json.dumps({"type": "user", "message": "str", "timestamp": "bad"}).encode()
        + b"\n"
    )

    extract = run_mine(tmp_path)

    assert [f["claim"] for f in extract.findings] == ["Good finding"]
    assert [c["text"] for c in extract.corrections] == ["never do that"]


def test_cli_writes_three_files_and_does_not_touch_last_run(tmp_path, monkeypatch, capsys):
    state = tmp_path / "state"
    monkeypatch.setenv("AGENT_HUB_STATE", str(state))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    now = datetime.now(timezone.utc)
    make_job(
        state,
        "20261010-100000-review-aaaa",
        started=now - timedelta(days=1),
        reply=review_reply("- high — a.py:1 — Something is off"),
    )
    last_run = state / "skills" / "last-run"
    last_run.parent.mkdir(parents=True)
    last_run.write_text("2020-01-01T00:00:00+00:00\n")
    out = tmp_path / "out"

    rc = main(["skills", "mine", "--out", str(out), "--transcripts", str(tmp_path / "claude"), "--days", "7"])

    assert rc == 0
    assert sorted(p.name for p in out.iterdir()) == ["extract.json", "extract.md", "metrics.json"]
    assert json.loads((out / "extract.json").read_text())["findings"][0]["claim"] == "Something is off"
    assert json.loads((out / "metrics.json").read_text())["corrections_total"] == 0
    assert "Findings: 1" in capsys.readouterr().out
    assert last_run.read_text() == "2020-01-01T00:00:00+00:00\n"

    rc = main(["skills", "mine", "--out", str(out), "--transcripts", str(tmp_path / "claude"), "--json"])
    assert rc == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["findings"][0]["claim"] == "Something is off"


def test_write_works_under_c_locale(tmp_path):
    write_transcript(tmp_path / "claude", [user_msg("không được sửa file này")])
    out = tmp_path / "out"
    env = {
        **os.environ,
        "LC_ALL": "C",
        "LANG": "C",
        "PYTHONUTF8": "0",
        "PYTHONPATH": str(Path(skillmine.__file__).parent.parent),
        "AGENT_HUB_STATE": str(tmp_path / "state"),
        "HOME": str(tmp_path / "home"),
    }
    code = "import sys; from agenthub.cli import main; sys.exit(main(sys.argv[1:]))"
    argv = ["skills", "mine", "--out", str(out), "--transcripts", str(tmp_path / "claude"), "--since", "2026-09-01"]
    done = subprocess.run([sys.executable, "-c", code, *argv], env=env, capture_output=True, cwd=tmp_path)

    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
    assert sorted(p.name for p in out.iterdir()) == ["extract.json", "extract.md", "metrics.json"]
    corrections = json.loads((out / "extract.json").read_text(encoding="utf-8"))["corrections"]
    assert [c["text"] for c in corrections] == ["không được sửa file này"]
    assert "không được sửa file này" in (out / "extract.md").read_text(encoding="utf-8")


def test_double_hyphen_separator(tmp_path):
    make_job(tmp_path / "state", "20261010-100000-review-aaaa", reply=review_reply("- high -- a/b.py:3 -- Bad thing"))

    findings = run_mine(tmp_path).findings

    assert [(f["severity"], f["file"], f["claim"]) for f in findings] == [("high", "a/b.py", "Bad thing")]


def test_markdown_link_file(tmp_path):
    reply = review_reply(
        "- high — [agenthub/x.py](file:///work/agenthub/x.py#L42) — Linked",
        "- low — agenthub/y.py:9 — Plain",
    )
    make_job(tmp_path / "state", "20261010-100000-review-aaaa", reply=reply)

    findings = run_mine(tmp_path).findings

    assert [(f["file"], f["claim"]) for f in findings] == [("agenthub/x.py", "Linked"), ("agenthub/y.py", "Plain")]


def test_home_inside_url_kept():
    assert skillmine.redact("see https://example.org/home/alice/x") == "see https://example.org/home/alice/x"
    assert skillmine.redact('open "/home/alice/x" and (/home/bob/y) and p=/home/carol/z') == (
        'open "~/x" and (~/y) and p=~/z'
    )
    assert skillmine.redact("/home/alice/start") == "~/start"


def test_failed_reviews_not_counted_as_rounds(tmp_path):
    state = tmp_path / "state"
    make_job(state, "20261010-100000-review-aaaa", status="ok", reply=review_reply("- low — a.py:1 — One"))
    make_job(state, "20261010-110000-review-bbbb", status="failed", started=RECENT + timedelta(hours=1))
    make_job(state, "20261010-120000-review-cccc", status="timeout", started=RECENT + timedelta(hours=2))
    make_job(state, "20261010-130000-review-dddd", status="cancelled", started=RECENT + timedelta(hours=3))

    extract = run_mine(tmp_path)

    assert extract.fix_rounds["feat-one"]["reviews"] == 1
    assert extract.review_failures == 3
    assert skillmine.metrics(extract)["review_failures"] == 3
    assert skillmine.metrics(extract)["max_review_rounds"] == 1


def test_fix_brief_variants(tmp_path):
    state = tmp_path / "state"
    briefs = [
        "\n\n# Brief: round 1: fix things\n",
        "# Brief: hub round 2 — more\n",
        "# Brief: round 3 fixes\n",
        "# Brief: round 4 fix\n",
        "# Brief: something else entirely\n",
    ]
    for i, brief in enumerate(briefs):
        make_job(
            state,
            f"20261010-1{i}0000-implement-x{i}",
            kind="implement",
            started=RECENT + timedelta(hours=i),
            brief=brief,
        )

    extract = run_mine(tmp_path)

    assert extract.fix_rounds["feat-one"]["fix_briefs"] == 4


def test_groups_by_severity_and_module(tmp_path):
    state = tmp_path / "state"
    make_job(
        state,
        "20261010-100000-review-aaaa",
        workdir="/work/repo/.worktrees/b1",
        reply=review_reply(
            "- high — tests/bench/x.py:1 — First",
            "- high — tests/bench/y.py:2 — Second",
            "- low — agenthub/skillmine.py:3 — Alone",
        ),
    )
    make_job(
        state,
        "20261010-110000-review-bbbb",
        workdir="/work/repo/.worktrees/b2",
        started=RECENT + timedelta(hours=1),
        reply=review_reply("- high — tests/bench/z.py:3 — Third", "- high — tests/bench/z.py:4 — Fourth"),
    )

    extract = run_mine(tmp_path)

    assert extract.groups[0] == {
        "severity": "high",
        "module": "tests/bench",
        "count": 4,
        "branches": 2,
        "examples": ["First", "Second", "Third"],
    }
    assert {"severity": "low", "module": "agenthub/skillmine.py", "count": 1, "branches": 1, "examples": ["Alone"]} in (
        extract.groups
    )
    md = skillmine.render_md(extract)
    assert "4× high in tests/bench (2 branches)" in md
    assert "Alone" not in md


def test_category_redacts_case_sensitive_secrets(tmp_path):
    aws = "AKIAABCDEFGHIJKLMNOP"
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.SflKxwRJSMeKKF2QT4"
    pem = "-----BEGIN RSA PRIVATE KEY----- MIIEowIBAAKCAQEAsecretbody -----END RSA PRIVATE KEY-----"
    claims = [f"leaks {aws} here", f"token {jwt} here", f"key {pem} here"]
    reply = review_reply(*[f"- high — a.py:{i} — {c}" for i, c in enumerate(claims)])
    make_job(tmp_path / "state", "20261010-100000-review-aaaa", reply=reply)

    extract = run_mine(tmp_path)
    out = tmp_path / "out"
    skillmine.write(extract, out)

    assert len(extract.findings) == 3
    for secret in (aws, jwt, "MIIEowIBAAKCAQEAsecretbody", aws.lower(), jwt.lower()):
        assert all(secret not in f["category"] for f in extract.findings)
        for name in ("extract.json", "metrics.json", "extract.md"):
            assert secret not in (out / name).read_text()


def test_home_in_file_url_and_brackets_redacted():
    assert skillmine.redact("see file:///home/alice/x.py") == "see file://~/x.py"
    assert skillmine.redact("paths [/home/bob/y] done") == "paths [~/y] done"
    assert skillmine.redact("at https://example.org/home/carol/z") == "at https://example.org/home/carol/z"


def test_non_dict_tool_input_skipped(tmp_path):
    root = tmp_path / "claude"
    records = []
    for i, bad in enumerate(["agent-hub run x", ["agent-hub run x"], None, 7]):
        block = {"type": "tool_use", "name": "Bash", "input": bad}
        records.append(
            {
                "type": "assistant",
                "message": {"content": [block]},
                "timestamp": RECENT.isoformat(),
                "sessionId": f"s{i}",
            }
        )
    records += [bash_msg("agent-hub run a && agent-hub review b && agent-hub merge c")]
    write_transcript(root, records)

    extract = run_mine(tmp_path)

    assert extract.agenthub_commands == {"run > review > merge": {"count": 1, "sessions": 1}}


def test_fix_brief_titles_extracted(tmp_path):
    state = tmp_path / "state"
    make_job(
        state,
        "20261010-100000-implement-aaaa",
        kind="implement",
        brief="# Brief: round 1: fix things in /home/alice/repo\n",
    )
    make_job(
        state,
        "20261010-110000-implement-bbbb",
        kind="implement",
        started=RECENT + timedelta(hours=1),
        brief="# Brief: other\n",
    )
    make_job(state, "20261010-120000-review-cccc", started=RECENT + timedelta(hours=2), reply=review_reply())
    make_job(state, "20261010-130000-review-dddd", started=RECENT + timedelta(hours=3), reply=review_reply())

    extract = run_mine(tmp_path)
    out = tmp_path / "out"
    summary = skillmine.write(extract, out)

    entry = extract.fix_rounds["feat-one"]
    assert entry["fix_brief_titles"] == ["# Brief: round 1: fix things in ~/repo"]
    assert all(len(t) <= 300 for t in entry["fix_brief_titles"])
    assert "round 1: fix things in ~/repo" in summary
    assert "alice" not in (out / "extract.json").read_text()


def test_bold_and_alternate_severity_scales(tmp_path):
    reply = review_reply(
        "- **high** — agenthub/a.py:1 — Bold",
        "- critical — agenthub/b.py — Critical",
        "- **Important** - agenthub/c.py - Important",
        "- minor — agenthub/d.py — Minor",
    )
    make_job(tmp_path / "state", "20261010-100000-review-aaaa", reply=reply)

    findings = run_mine(tmp_path).findings

    assert [(f["severity"], f["file"]) for f in findings] == [
        ("high", "agenthub/a.py"),
        ("high", "agenthub/b.py"),
        ("medium", "agenthub/c.py"),
        ("low", "agenthub/d.py"),
    ]


def test_unparsed_findings_counted(tmp_path):
    reply = review_reply(
        "- high — agenthub/a.py — Fine",
        "- something odd without severity",
        "- high — no separator after file",
        "not a bullet line",
    )
    make_job(tmp_path / "state", "20261010-100000-review-aaaa", reply=reply)

    extract = run_mine(tmp_path)

    # one parser (triage.review_findings): a severity bullet is a finding
    assert len(extract.findings) == 2
    assert extract.findings[1]["file"] == ""
    assert extract.unparsed_findings == 1
    assert skillmine.metrics(extract)["unparsed_findings"] == 1


def test_redact_colon_and_flag_passwords():
    cases = [
        ("password: hunter2hunter2", "hunter2hunter2"),
        ("db_password : 'two words'", "two words"),
        ("run --password hunter2hunter2 --verbose", "hunter2hunter2"),
        ("--api-token abcd1234", "abcd1234"),
        ('{"client_secret": "s3cr3tvalue"}', "s3cr3tvalue"),
        ('{"PWD": "pwdvalue1"}', "pwdvalue1"),
        ("credential=credvalue1", "credvalue1"),
        ("user_credentials: credvalue2", "credvalue2"),
    ]
    for text, secret in cases:
        assert secret not in skillmine.redact(text), text
    assert skillmine.redact("--password --verbose") == "--password --verbose"
    assert skillmine.redact("run --password hunter2 --verbose") == "run --password <redacted> --verbose"


def test_redact_macos_and_root_home():
    assert skillmine.redact("see /Users/alice/work/x.py") == "see ~/work/x.py"
    assert skillmine.redact("cd /root/work and p=/root") == "cd ~/work and p=~"
    assert skillmine.redact("/rootfs/x and https://e.org/Users/alice/x") == "/rootfs/x and https://e.org/Users/alice/x"


def test_long_paths_not_redacted():
    path = "agenthub/superpowers/specs/phase1/design/notes/overview2"
    assert skillmine.redact(f"see {path}") == f"see {path}"
    blob = "QUJDREVGR0hJSktMTU5P/UFFSU1RVVldYWVowMTIzNDU2Nzg5abcdefghijklmnop"
    assert skillmine.redact(f"blob {blob}") == "blob <redacted>"
    assert skillmine.redact("blob aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bC1dE3fG5hI7k/aB3dE5gH") == "blob <redacted>"


def test_redact_basic_auth():
    out = skillmine.redact("Authorization: Basic dXNlcjpwYXNzd29yZA==")
    assert "dXNlcjpwYXNzd29yZA" not in out
    assert skillmine.redact("curl -H 'Authorization: Basic QWxhZGRpbjpvcGVuIHNlc2FtZQ=='").count("QWxhZGRpbjpv") == 0
    assert skillmine.redact("Basic usage of Basic Authentication") == "Basic usage of Basic Authentication"


def test_redact_pass_suffix_names():
    cases = [
        ("DB_PASS=hunter2x", "hunter2x"),
        ("ADMIN_PASS: s3cretvalue", "s3cretvalue"),
        ("MYSQL_PWD=abc", "abc"),
        ("export DB_PASS hunter2x", "hunter2x"),
        ("session_id=abcd", "abcd"),
        ("cookie: sid=zz", "zz"),
        ("salt=xy1", "xy1"),
        ("signature: a1b2", "a1b2"),
    ]
    for text, secret in cases:
        out = skillmine.redact(text)
        assert secret not in out, text
        assert "<redacted>" in out, text


def test_redact_b64_after_plain_equals():
    blob = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldY"
    out = skillmine.redact(f"data={blob} and x:{blob}")
    assert blob not in out
    assert out == "data=<redacted> and x:<redacted>"


def test_redact_base64url():
    blob = "dXNlcjpwYXNzLXdvcmQ_YWJjZGVmZ2hpamts-bW5vcA"
    out = skillmine.redact(f"value {blob} end")
    assert blob not in out
    assert out == "value <redacted> end"


REAL_FALSE_POSITIVES = [
    "**Documentation**",
    "..._foreign_repo_refused_exit_3",
    "./plugins/token-audit",
    ".git/worktrees/.../index.lock",
    "///path/to/cli.py#L42",
    "/accepted-touched.jsonl",
    "/plans/*/plan.json",
    "/tmp/.../wt-slugify",
    "0005_drop_search_text_index.py",
    "ASSUMED_generic_*.txt",
    "Adapter.preflight",
    "AgyAdapter.classify",
    "Evidence.balance_after_minor",
    "Extract.review_failures",
    "FileNotFoundError",
    "GenericSmsAdapter",
    "HubConfig.marketplace_registered",
    "LEARNED_PLUGINS.",
    "LoadsTheDataFile",
    "PrivacyInfo.xcprivacy",
    "accepted-touched.jsonl",
    "agent-hub@agent-plugins",
    "agenthub.agents.claude._now",
]


def test_entropy_rule_spares_paths_identifiers_and_job_ids():
    keep = [
        "agenthub/skillmine.py",
        "orch/phased/skills-mine",
        "20261012-082712-review-8e6a",
        "test_redact_basic_auth",
        "CMAKE_COMPILE_WARNING_AS_ERROR",
        "docs/superpowers/specs/design.md",
        "RedactionChokePointHandler",
    ]
    for text in keep:
        assert skillmine.redact(f"see {text} now") == f"see {text} now", text
    assert skillmine.redact("k Xk9fQ2mP7vLz4TnR8wYb") == "k <redacted>"
    assert skillmine.redact("aaaaaaaaaaaaaaaaaaaaaaaa1") == "aaaaaaaaaaaaaaaaaaaaaaaa1"


def test_real_false_positives_stay():
    keep = [
        *REAL_FALSE_POSITIVES,
        "20261011-140234-haiku-impl-7a9a",
        "20261011-190458-implement-hard-f179",
        "docs/superpowers/plans/2026-10-11-model-routing.md",
        "docs/superpowers/specs/2026-10-12-phase-b-skill-hub-design.md",
        "docs/superpowers/specs/2026-10-12-phase-b-skill-hub-design.md.",
        "FastAPI+SQLite+React",
        "test_hash_token_is_sha256_hex",
        "test_new_id_is_uuid4_string",
        "test_no_merge_outside_24h_or_different_account",
        "test_orders_exit_1_on_bare_line",
        "test_parse_multipart_and_base64",
        "test_payload_codec_valid_utf8_is_stored_verbatim",
        "test_plan_wait_no_state_exit_2",
        "test_python_suite_loads_t1_to_t6",
        "test_redact_base64url",
        "test_uploaded_file_unique_per_tenant_sha256",
        "✔Validationpassed",
    ]
    for text in keep:
        assert skillmine.redact(f"see {text} now") == f"see {text} now", text
    assert "<redacted>" in skillmine.redact("AKIAIOSFODNN7EXAMPLE")
    assert skillmine.redact("k aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bC1dE3fG5hI7kLm") == "k <redacted>"


def test_real_secrets_still_redacted_after_false_positive_rules():
    assert skillmine.redact("see AKIAIOSFODNN7EXAMPLE now") == "see <redacted> now"
    assert skillmine.redact("see cfg.aB3dE5gH7jK9mN1pQ3sT5vW7y now") == "see cfg.<redacted> now"
    assert skillmine.redact("auth Basic dXNlcjpodW50ZXIy") == "auth Basic <redacted>"


def test_random_snake_chunks_redacted():
    assert skillmine.redact("see k3f9x2m8q7z1_p4w6n0b5v8c2_a9d7e now") == "see <redacted> now"
    assert skillmine.redact("see ab3k9_zq7w2x8_m5n1p4r6t9 now") == "see <redacted> now"
    for text in ("test_new_id_is_uuid4_string", "test_python_suite_loads_t1_to_t6", "test_redact_base64url"):
        assert skillmine.redact(f"see {text} now") == f"see {text} now", text


def test_read_reply_uses_reply_path_and_numeric_order(tmp_path):
    job = tmp_path / "job"
    for name, body in (("2-a-x", "two"), ("10-a-x", "ten"), ("9-a-x", "nine")):
        (job / "attempts" / name).mkdir(parents=True)
        (job / "attempts" / name / "reply.md").write_text(body)

    assert skillmine._read_reply(job, {}) == "ten"
    assert skillmine._read_reply(job, {"reply_path": str(job / "attempts" / "2-a-x" / "reply.md")}) == "two"
    outside = tmp_path / "outside.md"
    outside.write_text("outside")
    assert skillmine._read_reply(job, {"reply_path": str(outside)}) == "ten"
    assert skillmine._read_reply(job, {"reply_path": str(job / "missing.md")}) == "ten"


def test_group_ties_by_severity_rank():
    findings = [
        {"severity": sev, "file": f"{mod}/sub/x.py", "claim": sev, "branch": "b"}
        for sev, mod in (("low", "aaa"), ("medium", "zzz"), ("high", "mmm"), ("high", "bbb"))
    ]

    groups = skillmine.group_findings(findings)

    assert [(g["severity"], g["module"]) for g in groups] == [
        ("high", "bbb/sub"),
        ("high", "mmm/sub"),
        ("medium", "zzz/sub"),
        ("low", "aaa/sub"),
    ]


def test_secret_names_match_whole_parts():
    secrets = [
        ("DB_PASS=hunter2hunter2", "hunter2hunter2"),
        ("api-key: hunter2hunter2", "hunter2hunter2"),
        ("apiKey=hunter2hunter2", "hunter2hunter2"),
        ("API_TOKEN=hunter2hunter2", "hunter2hunter2"),
        ("passwd: hunter2hunter2", "hunter2hunter2"),
        ("user_credentials: hunter2hunter2", "hunter2hunter2"),
        ("sessionId=hunter2hunter2", "hunter2hunter2"),
        ("Authorization: hunter2hunter2", "hunter2hunter2"),
    ]
    for text, secret in secrets:
        out = skillmine.redact(text)
        assert secret not in out, text
        assert "<redacted>" in out, text
    prose = [
        "Design: the flag",
        "author: alice",
        "keyboard: layout",
        "monkey=banana",
        "signal: stop",
        "passage: a corridor",
    ]
    for text in prose:
        assert skillmine.redact(text) == text, text


def test_glued_secret_names_redacted():
    secrets = [
        ("PGPASSWORD=hunter2hunter2", "hunter2hunter2"),
        ("DBPASSWORD: hunter2hunter2", "hunter2hunter2"),
        ("SECRETKEY=hunter2hunter2", "hunter2hunter2"),
        ("adminpass=hunter2hunter2", "hunter2hunter2"),
    ]
    for text, secret in secrets:
        out = skillmine.redact(text)
        assert secret not in out, text
        assert out.endswith("<redacted>"), text


def test_url_credentials_redacted():
    assert (
        skillmine.redact("postgres://admin:s3cretpass@localhost:5432/app")
        == "postgres://admin:<redacted>@localhost:5432/app"
    )
    assert skillmine.redact("redis://:pw12345@db:6379") == "redis://:<redacted>@db:6379"


def test_url_password_with_at_and_slash():
    out = skillmine.redact("postgres://admin:p@ss@db/app")
    assert out == "postgres://admin:<redacted>@db/app"
    assert "ss" not in out
    out = skillmine.redact("postgres://u:a/b@h/x")
    assert out == "postgres://u:<redacted>@h/x"
    assert "a/b" not in out


def test_url_creds_after_punctuation():
    assert skillmine.redact("Cloning.https://u:pw12345@h/x") == "Cloning.https://u:<redacted>@h/x"
    assert skillmine.redact("1.https://u:pw12345@h/x") == "1.https://u:<redacted>@h/x"
    assert skillmine.redact("_https://u:pw12345@h/x") == "_https://u:<redacted>@h/x"
    assert skillmine.redact("+https://u:pw12345@h/x") == "+https://u:<redacted>@h/x"
    assert skillmine.redact("-https://u:pw12345@h/x") == "-https://u:<redacted>@h/x"


def test_long_url_password_redacted():
    secret = "s" * 300
    out = skillmine.redact(f"postgres://admin:{secret}@db/app")
    assert out == "postgres://admin:<redacted>@db/app"
    assert secret not in out
    out = skillmine.redact(f"postgres://admin:{secret}@db/app and postgres://u:short@h/x")
    assert out == "postgres://admin:<redacted>@db/app and postgres://u:<redacted>@h/x"


def test_mine_write_error_exit_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENT_HUB_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory")
    out = blocker / "out"

    rc = main(["skills", "mine", "--out", str(out), "--transcripts", str(tmp_path / "claude"), "--days", "7"])

    assert rc == 2
    err = capsys.readouterr().err
    assert err.startswith(f"agent-hub: cannot write {out}: "), err
    assert "Traceback" not in err


def test_mine_days_out_of_range_exit_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENT_HUB_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    for days in ("0", "1000000"):
        argv = ["skills", "mine", "--out", str(tmp_path / "out"), "--transcripts", str(tmp_path / "claude")]
        rc = main([*argv, "--days", days])

        assert rc == 2, days
        err = capsys.readouterr().err
        assert err == "agent-hub: --days must be between 1 and 3650\n", err
        assert "Traceback" not in err


def test_command_key_collisions_summed(tmp_path):
    first = "aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bC1dE3fG5hI7k"
    second = "aX9kL2mN4pQ6rS8tV0wY1zA3bC5dE7fG9hJ1kL3"
    records = [
        bash_msg(f"agent-hub run && agent-hub {first} && agent-hub show", session="s1"),
        bash_msg(f"agent-hub run && agent-hub {second} && agent-hub show", session="s2"),
    ]
    write_transcript(tmp_path / "claude", records)

    commands = run_mine(tmp_path).agenthub_commands

    assert commands == {"run > <redacted> > show": {"count": 2, "sessions": 2}}


def test_repo_at_home_root_not_user_name(tmp_path):
    state = tmp_path / "state"
    reply = review_reply("- low — docs/a.md — Typo in heading")
    home_job = make_job(state, "20261010-100000-review-aaaa", reply=reply, verdict="request-changes")
    nested_job = make_job(state, "20261010-110000-review-bbbb", reply=reply, verdict="request-changes")
    for job, repo in ((home_job, "/home/alice/.git"), (nested_job, "/home/alice/project/.git")):
        result = json.loads((job / "result.json").read_text())
        result["repo"] = repo
        (job / "result.json").write_text(json.dumps(result))

    repos = {f["job"]: f["repo"] for f in run_mine(tmp_path).findings}

    assert repos == {"20261010-100000-review-aaaa": "~", "20261010-110000-review-bbbb": "project"}
    assert "alice" not in json.dumps(repos)


def test_branch_at_home_root_not_user_name(tmp_path):
    state = tmp_path / "state"
    reply = review_reply("- low — docs/a.md — Typo in heading")
    make_job(state, "20261010-100000-review-aaaa", workdir="/home/alice", reply=reply, verdict="request-changes")
    make_job(
        state,
        "20261010-110000-review-bbbb",
        workdir="/home/alice/repo/.worktrees/feat",
        reply=reply,
        verdict="request-changes",
    )

    branches = {f["job"]: f["branch"] for f in run_mine(tmp_path).findings}

    assert branches == {"20261010-100000-review-aaaa": "~", "20261010-110000-review-bbbb": "feat"}
    assert "alice" not in json.dumps(branches)


def test_session_uuid_kept_short(tmp_path):
    uuid = "3f2a9c1e-7b4d-4e8a-9f10-2c5d6e7f8a9b"
    records = [
        user_msg("never edit the justfile", session=uuid),
        user_msg("that is wrong", session=uuid),
        user_msg("you must run lint", session="s1"),
    ]
    write_transcript(tmp_path / "claude", records)

    corrections = run_mine(tmp_path).corrections

    assert [c["session"] for c in corrections] == ["3f2a9c1e", "3f2a9c1e", "s1"]
    assert uuid not in json.dumps(corrections)


def _best_redact_time(text: str, runs: int = 2) -> float:
    best = float("inf")
    for _ in range(runs):
        start = time.perf_counter()
        skillmine.redact(text)
        best = min(best, time.perf_counter() - start)
    return best


def _assert_linear(make_text) -> None:
    # a ratio, not an absolute bound: 4x the input may take up to 6x as long (quadratic would take ~16x)
    small = _best_redact_time(make_text(10000))
    large = _best_redact_time(make_text(40000))
    assert large <= 6 * small + 0.05


def test_redact_scales_linearly():
    for make_text in (
        lambda n: "x" * n,
        lambda n: "A" * n,
        lambda n: "a." * (n // 2),
        lambda n: "a://:" * (n // 5),
    ):
        _assert_linear(make_text)
        assert skillmine.redact(make_text(10000)) == make_text(10000)


def test_word_check_is_linear_on_long_capitals():
    # the bare runs and their `Basic ` forms: only the latter reach the word check
    makers = (
        lambda n: "A" * (n // 2) + "b1",
        lambda n: "ABCDEFGHIJ" * (n // 10) + "x",
        lambda n: "Basic " + "A" * (n // 2) + "b1",
        lambda n: "Basic " + "ABCDEFGHIJ" * (n // 10) + "x",
    )
    for make_text in makers:
        _assert_linear(make_text)
        assert skillmine.redact(make_text(10000))


def test_findings_match_triage_parser():
    reply = """
## Findings
- **critical** — agenthub/a.py:1 — critical bug
- `high` — agenthub/b.py:2 — high bug
* low — agenthub/c.py:3 — low bug
  - indented sub-bullet continuation line
"""
    triage_findings = triage.review_findings(reply)
    skillmine_findings, _ = skillmine._findings_of(reply)

    assert [f.severity for f in triage_findings] == ["high", "high", "low"]
    assert [s for s, _, _ in skillmine_findings] == ["high", "high", "low"]


def test_continuation_does_not_change_file():
    reply = """## Findings
- high: memory leak in parser
  - see a.py
"""
    found, _ = skillmine._findings_of(reply)
    assert found == [("high", "", "memory leak in parser")]


def test_wrapped_severity_stripped():
    reply = """## Findings
- `high`: a.py:3 — leak
"""
    found, _ = skillmine._findings_of(reply)
    assert found == [("high", "a.py", "leak")]
    assert "high" not in found[0][1]
