from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from agenthub.audit.writer import audit_root, event_hash

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK_SCRIPT = REPO_ROOT / "hooks" / "audit-hook.py"

REAL_CLAUDE_POST_TOOL_USE_PAYLOAD: dict[str, Any] = {
    "session_id": "d376e73f-f8af-4eb8-b73d-29d2672d7737",
    "transcript_path": "/tmp/x/.claude/projects/-tmp-tmpqnq7nmth/d376e73f-f8af-4eb8-b73d-29d2672d7737.jsonl",
    "cwd": "/tmp/tmpqnq7nmth",
    "prompt_id": "74eec515-af83-4fae-850a-84f371e9bb86",
    "permission_mode": "auto",
    "effort": {"level": "medium"},
    "hook_event_name": "PostToolUse",
    "tool_name": "Bash",
    "tool_input": {"command": "echo hi", "description": "Print hi"},
    "tool_response": {
        "stdout": "hi",
        "stderr": "",
        "interrupted": False,
        "isImage": False,
        "noOutputExpected": False,
    },
    "tool_use_id": "toolu_015uMxJsQW9kE6HKBFMR1Tir",
    "duration_ms": 232,
}

REAL_AGY_POST_TOOL_USE_PAYLOAD: dict[str, Any] = {
    "artifactDirectoryPath": "/tmp/x/.gemini/antigravity-cli/brain/4f99fb2c-c829-4ded-a556-16d5e0bf6021",
    "conversationId": "4f99fb2c-c829-4ded-a556-16d5e0bf6021",
    "error": "",
    "modelName": "gemini-4-argon-medium",
    "stepIdx": 2,
    "toolCall": {
        "args": {
            "CommandLine": "echo hi",
            "Cwd": "/tmp/x/Workspaces/tools/agent-hub",
            "WaitMsBeforeAsync": 5000,
            "toolAction": "Running echo hi",
            "toolSummary": "Run echo hi",
        },
        "name": "run_command",
    },
    "transcriptPath": (
        "/tmp/x/.gemini/antigravity-cli/brain/4f99fb2c-c829-4ded-a556-16d5e0bf6021"
        "/.system_generated/logs/transcript_full.jsonl"
    ),
    "workspacePaths": ["/tmp/x/Workspaces/tools/agent-hub"],
}


def _clean_env(state_dir: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_HUB_TRACE_")}
    env["AGENT_HUB_STATE"] = str(state_dir)
    if extra:
        env.update(extra)
    return env


def _run_hook(
    args: list[str],
    stdin_text: str,
    state_dir: Path,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", str(HOOK_SCRIPT), *args],
        input=stdin_text,
        text=True,
        capture_output=True,
        env=_clean_env(state_dir, extra_env),
        check=False,
    )


def _read_day_events(state_dir: Path) -> list[dict[str, Any]]:
    root = audit_root(state_dir)
    if not root.is_dir():
        return []
    day_files = sorted(root.glob("*/*.jsonl"))
    events: list[dict[str, Any]] = []
    for df in day_files:
        for line in df.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(json.loads(line))
    return events


def test_claude_posttooluse_writes_event_exit_zero_no_stdout(tmp_path: Path) -> None:
    transcript_file = tmp_path / "transcript.jsonl"
    transcript_secret = "TRANSCRIPT_SECRET_DO_NOT_READ_99182"
    transcript_file.write_text(transcript_secret, encoding="utf-8")

    payload = {
        **REAL_CLAUDE_POST_TOOL_USE_PAYLOAD,
        "transcript_path": str(transcript_file),
        "thinking": "PRIVATE_THINKING_CONTENT_77123",
    }

    res = _run_hook(["--host", "claude", "PostToolUse"], json.dumps(payload), tmp_path)
    assert res.returncode == 0
    assert res.stdout == ""
    assert res.stderr == ""

    events = _read_day_events(tmp_path)
    assert len(events) == 1
    ev = events[0]
    assert ev["schema_version"] == 1
    assert ev["seq"] == 1
    assert ev["kind"] == "command"
    assert ev["target"] == "echo hi"
    assert ev["detail"] == {"argv": ["echo", "hi"], "cwd": "/tmp/tmpqnq7nmth"}
    assert ev["outcome"] == {"status": "ok"}
    assert ev["quote"] == "Print hi"
    assert ev["actor"] == {
        "role": "director",
        "host": "claude",
        "session_id": "d376e73f-f8af-4eb8-b73d-29d2672d7737",
    }
    assert ev["parent"] == {}
    assert ev["prev_hash"] == "0" * 64
    assert ev["hash"] == event_hash(ev["prev_hash"], ev)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", ev["ts"])

    raw_dump = json.dumps(ev)
    assert transcript_secret not in raw_dump
    assert "PRIVATE_THINKING_CONTENT_77123" not in raw_dump

    # Multi-event command (command + outward) with worker trace environment writes one record per event
    outward_payload = {
        **REAL_CLAUDE_POST_TOOL_USE_PAYLOAD,
        "tool_input": {"command": "cd /tmp/x && git push origin main", "description": "Push branch"},
    }
    trace = {
        "AGENT_HUB_TRACE_PLAN": "phase-g",
        "AGENT_HUB_TRACE_TASK": "audit-hook-entry",
        "AGENT_HUB_TRACE_JOB": "20261019-022237-implement-hard-1f02",
        "AGENT_HUB_TRACE_ATTEMPT": "1",
        "AGENT_HUB_TRACE_ROLE": "implement-hard",
    }
    res2 = _run_hook(["--host", "claude", "PostToolUse"], json.dumps(outward_payload), tmp_path, extra_env=trace)
    assert res2.returncode == 0
    assert res2.stdout == ""
    assert res2.stderr == ""

    events_after = _read_day_events(tmp_path)
    assert len(events_after) == 3
    assert events_after[1]["seq"] == 2
    assert events_after[1]["kind"] == "command"
    assert events_after[1]["prev_hash"] == events_after[0]["hash"]
    assert events_after[1]["actor"]["role"] == "worker"
    assert events_after[1]["actor"]["pool_role"] == "implement-hard"
    assert events_after[1]["parent"] == {
        "plan": "phase-g",
        "task": "audit-hook-entry",
        "job": "20261019-022237-implement-hard-1f02",
        "attempt": 1,
    }
    assert events_after[2]["seq"] == 3
    assert events_after[2]["kind"] == "outward"
    assert events_after[2]["target"] == "git push origin main"
    assert events_after[2]["prev_hash"] == events_after[1]["hash"]


def test_agy_prints_exactly_empty_object(tmp_path: Path) -> None:
    res = _run_hook(["--host", "agy", "PostToolUse"], json.dumps(REAL_AGY_POST_TOOL_USE_PAYLOAD), tmp_path)
    assert res.returncode == 0
    assert res.stdout == "{}"
    assert res.stderr == ""

    events = _read_day_events(tmp_path)
    assert len(events) == 1
    assert events[0]["kind"] == "command"
    assert events[0]["actor"]["host"] == "agy"
    assert events[0]["target"] == "echo hi"

    # SessionStart and Stop also print {}
    res_start = _run_hook(
        ["--host", "agy", "SessionStart"],
        json.dumps({"conversationId": "conv-1", "modelName": "gemini-4-argon-high"}),
        tmp_path,
    )
    assert res_start.returncode == 0
    assert res_start.stdout == "{}"
    assert res_start.stderr == ""

    res_stop = _run_hook(
        ["--host", "agy", "Stop"],
        json.dumps({"conversationId": "conv-1", "terminationReason": "NO_TOOL_CALL"}),
        tmp_path,
    )
    assert res_stop.returncode == 0
    assert res_stop.stdout == "{}"
    assert res_stop.stderr == ""

    assert len(_read_day_events(tmp_path)) == 3

    # Bad payload on agy still prints {} and exits 0
    res_bad = _run_hook(["--host", "agy", "PostToolUse"], "{not-valid-json", tmp_path)
    assert res_bad.returncode == 0
    assert res_bad.stdout == "{}"
    assert res_bad.stderr == ""

    # Write failure on agy still prints {} and exits 0
    blocked_state = tmp_path / "blocked_state"
    blocked_state.mkdir(parents=True, exist_ok=True)
    (blocked_state / "audit").write_text("file blocking audit dir", encoding="utf-8")
    res_fail = _run_hook(
        ["--host", "agy", "PostToolUse"],
        json.dumps(REAL_AGY_POST_TOOL_USE_PAYLOAD),
        blocked_state,
    )
    assert res_fail.returncode == 0
    assert res_fail.stdout == "{}"
    assert res_fail.stderr == ""


def test_unwritable_audit_dir_silent_exit_zero_then_gap_after_recovery(tmp_path: Path) -> None:
    audit_blocker = tmp_path / "audit"
    audit_blocker.write_text("not a directory", encoding="utf-8")

    res1 = _run_hook(
        ["--host", "claude", "PostToolUse"],
        json.dumps(REAL_CLAUDE_POST_TOOL_USE_PAYLOAD),
        tmp_path,
    )
    assert res1.returncode == 0
    assert res1.stdout == ""
    assert res1.stderr == ""

    pending_file = tmp_path / "audit-pending-gap.jsonl"
    assert pending_file.is_file()

    # Recover by removing the blocking file
    audit_blocker.unlink()

    recovered_payload = {
        **REAL_CLAUDE_POST_TOOL_USE_PAYLOAD,
        "tool_input": {"command": "echo after recovery", "description": "Recovered command"},
    }
    res2 = _run_hook(
        ["--host", "claude", "PostToolUse"],
        json.dumps(recovered_payload),
        tmp_path,
    )
    assert res2.returncode == 0
    assert res2.stdout == ""
    assert res2.stderr == ""
    assert not pending_file.exists()

    events = _read_day_events(tmp_path)
    assert len(events) == 2

    gap_ev, cmd_ev = events[0], events[1]
    assert gap_ev["kind"] == "gap"
    assert gap_ev["seq"] == 1
    assert gap_ev["detail"]["lost"] == 1
    assert gap_ev["detail"]["from"]
    assert gap_ev["detail"]["to"] >= gap_ev["detail"]["from"]
    assert gap_ev["detail"]["reason"]

    assert cmd_ev["kind"] == "command"
    assert cmd_ev["target"] == "echo after recovery"
    assert cmd_ev["seq"] == 2
    assert cmd_ev["prev_hash"] == gap_ev["hash"]


def test_malformed_stdin_exit_zero_no_output(tmp_path: Path) -> None:
    malformed_inputs = [
        "",
        "   \n\t  ",
        "{not valid json",
        "[1, 2, 3]",
        '"just a string"',
        "12345",
        "null",
    ]
    for host in ("claude", "codex"):
        for raw_in in malformed_inputs:
            res = _run_hook(["--host", host, "PostToolUse"], raw_in, tmp_path)
            assert res.returncode == 0
            assert res.stdout == ""
            assert res.stderr == ""

    assert not (tmp_path / "audit").exists()
    assert not (tmp_path / "audit-pending-gap.jsonl").exists()


def _resolve_agenthub_module_path(module_name: str) -> Path:
    parts = module_name.split(".")
    candidate = REPO_ROOT.joinpath(*parts)
    if candidate.is_dir():
        init_py = candidate / "__init__.py"
        assert init_py.is_file(), f"missing package init for {module_name}: {init_py}"
        return init_py
    py_file = candidate.with_suffix(".py")
    assert py_file.is_file(), f"missing module file for {module_name}: {py_file}"
    return py_file


def _reachable_ast_nodes(tree: ast.Module, requested_names: set[str] | None) -> list[ast.AST]:
    if requested_names is None:
        return list(ast.walk(tree))

    defs_by_name: dict[str, ast.AST] = {}
    top_level_other: list[ast.AST] = []
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defs_by_name[stmt.name] = stmt
        else:
            top_level_other.append(stmt)

    visited_defs: set[str] = set()
    queue = [name for name in requested_names if name in defs_by_name]
    nodes_to_scan: list[ast.AST] = []
    for stmt in top_level_other:
        nodes_to_scan.extend(ast.walk(stmt))

    while queue:
        name = queue.pop(0)
        if name in visited_defs:
            continue
        visited_defs.add(name)
        defn = defs_by_name[name]
        for sub in ast.walk(defn):
            nodes_to_scan.append(sub)
            if isinstance(sub, ast.Name) and sub.id in defs_by_name and sub.id not in visited_defs:
                queue.append(sub.id)

    return nodes_to_scan


def _is_allowed_import(module_name: str) -> bool:
    top = module_name.split(".", 1)[0]
    if top in sys.stdlib_module_names or top == "__future__":
        return True
    if module_name == "agenthub.audit" or module_name.startswith("agenthub.audit."):
        return True
    if module_name in ("agenthub.redact", "agenthub.config"):
        return True
    return False


def test_import_closure_is_stdlib_and_audit_only() -> None:
    assert HOOK_SCRIPT.is_file(), f"missing {HOOK_SCRIPT}"

    # Track (file_path, requested_symbols_or_None_for_all)
    queue: list[tuple[Path, set[str] | None]] = [(HOOK_SCRIPT, None)]
    visited: dict[Path, set[str] | None] = {}
    all_imports: set[str] = set()
    reached_agenthub_modules: set[str] = set()

    while queue:
        path, req = queue.pop(0)
        if path in visited:
            prev_req = visited[path]
            if prev_req is None:
                continue
            if req is not None and req.issubset(prev_req):
                continue
            req = None if req is None else (prev_req | req)
        visited[path] = None if req is None else set(req)

        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        nodes = _reachable_ast_nodes(tree, req)

        for node in nodes:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    mod = alias.name
                    all_imports.add(mod)
                    assert _is_allowed_import(mod), f"disallowed import {mod!r} in {path}"
                    if mod == "agenthub" or mod.startswith("agenthub."):
                        reached_agenthub_modules.add(mod)
                        queue.append((_resolve_agenthub_module_path(mod), None))
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0 and node.module is not None, f"relative import in {path}"
                mod = node.module
                all_imports.add(mod)
                assert _is_allowed_import(mod), f"disallowed import from {mod!r} in {path}"
                if mod == "agenthub" or mod.startswith("agenthub."):
                    imported_names = {a.name for a in node.names}
                    # Check if any imported name is itself a submodule
                    submod_matched = False
                    for name in imported_names:
                        sub_candidate = f"{mod}.{name}"
                        sub_py = REPO_ROOT.joinpath(*sub_candidate.split(".")).with_suffix(".py")
                        if sub_py.is_file():
                            submod_matched = True
                            assert _is_allowed_import(sub_candidate), (
                                f"disallowed submodule import {sub_candidate!r} in {path}"
                            )
                            reached_agenthub_modules.add(sub_candidate)
                            queue.append((sub_py, None))
                    if not submod_matched:
                        reached_agenthub_modules.add(mod)
                        target_req = None if "*" in imported_names else imported_names
                        queue.append((_resolve_agenthub_module_path(mod), target_req))

    assert reached_agenthub_modules == {
        "agenthub.audit.normalize",
        "agenthub.audit.writer",
        "agenthub.audit.classify",
        "agenthub.audit.trace",
        "agenthub.redact",
        "agenthub.config",
    }
    assert all(_is_allowed_import(m) for m in all_imports)

    # Also verify signal.alarm(2) is present in hooks/audit-hook.py AST
    hook_tree = ast.parse(HOOK_SCRIPT.read_text(encoding="utf-8"), filename=str(HOOK_SCRIPT))
    alarm_calls = [
        node
        for node in ast.walk(hook_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "signal"
        and node.func.attr == "alarm"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == 2
    ]
    assert len(alarm_calls) == 1


def test_unknown_host_and_event_exit_zero(tmp_path: Path) -> None:
    valid_json = json.dumps(REAL_CLAUDE_POST_TOOL_USE_PAYLOAD)

    for args in (
        ["--host", "unknown", "PostToolUse"],
        ["--host", "opencode", "PostToolUse"],
        ["--host", "claude", "UnknownEvent"],
        ["--host", "claude", "PreToolUse"],
        ["--host", "codex", "PostToolUse"],
        ["--host", "codex", "UnknownEvent"],
        ["PostToolUse"],
        [],
    ):
        res = _run_hook(args, valid_json, tmp_path)
        assert res.returncode == 0
        assert res.stdout == ""
        assert res.stderr == ""

    # Unknown event on agy exits 0 and still prints {}
    for agy_event in ("UnknownEvent", "PreToolUse", ""):
        args = ["--host", "agy", agy_event] if agy_event else ["--host", "agy"]
        res_agy = _run_hook(args, valid_json, tmp_path)
        assert res_agy.returncode == 0
        assert res_agy.stdout == "{}"
        assert res_agy.stderr == ""

    assert _read_day_events(tmp_path) == []
