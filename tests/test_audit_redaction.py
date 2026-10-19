from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agenthub.audit.writer import (
    append,
    audit_root,
    canonical_json,
    event_hash,
    sanitize,
    write,
)


def _get_single_day_file(root: Path) -> Path:
    jsonl_files = list(root.glob("*/*.jsonl"))
    assert len(jsonl_files) == 1, f"Expected 1 day file, found {jsonl_files}"
    return jsonl_files[0]


def test_secret_absent_from_file_bytes(tmp_path: Path) -> None:
    raw_secret = "sec-tok-9876543210fedcba"
    bearer_token = f"Bearer {raw_secret}"
    event = {
        "ts": "2026-10-19T10:00:00.000Z",
        "actor": {"role": "worker", "host": "claude"},
        "kind": "command",
        "target": f"api-call {bearer_token}",
        "quote": f"Executing with {bearer_token} inside",
        "detail": {
            "argv": ["curl", "-H", f"Authorization: {bearer_token}", "https://api.example.com"],
            "stderr_tail": f"Failed authentication: {bearer_token}",
            "nested": {
                "auth_header": bearer_token,
                "items": [f"item with {bearer_token}", 123],
            },
        },
        "outcome": {"status": "ok"},
    }

    append(event, state_dir=tmp_path)

    day_file = _get_single_day_file(audit_root(tmp_path))
    file_bytes = day_file.read_bytes()

    assert raw_secret.encode("utf-8") not in file_bytes
    assert b"Bearer <redacted>" in file_bytes
    assert b"<redacted>" in file_bytes


def test_quote_truncated_to_200_with_ellipsis(tmp_path: Path) -> None:
    long_quote = "A" * 250
    event = {
        "ts": "2026-10-19T10:00:00.000Z",
        "actor": {"role": "worker", "host": "claude"},
        "kind": "command",
        "target": "echo test",
        "quote": long_quote,
        "outcome": {"status": "ok"},
    }

    append(event, state_dir=tmp_path)

    day_file = _get_single_day_file(audit_root(tmp_path))
    stored_event = json.loads(day_file.read_text(encoding="utf-8").strip())

    assert len(stored_event["quote"]) == 200
    assert stored_event["quote"].endswith("…")
    assert stored_event["quote"] == ("A" * 199) + "…"


def test_target_truncated_to_500(tmp_path: Path) -> None:
    long_target = "B" * 600
    event = {
        "ts": "2026-10-19T10:00:00.000Z",
        "actor": {"role": "worker", "host": "claude"},
        "kind": "command",
        "target": long_target,
        "outcome": {"status": "ok"},
    }

    append(event, state_dir=tmp_path)

    day_file = _get_single_day_file(audit_root(tmp_path))
    stored_event = json.loads(day_file.read_text(encoding="utf-8").strip())

    assert len(stored_event["target"]) == 500
    assert stored_event["target"] == "B" * 500


def test_stderr_tail_keeps_last_2000_chars(tmp_path: Path) -> None:
    long_tail = "HEAD_" + ("C" * 2500) + "_TAIL"
    event = {
        "ts": "2026-10-19T10:00:00.000Z",
        "actor": {"role": "worker", "host": "claude"},
        "kind": "command",
        "target": "cat error.log",
        "detail": {
            "stderr_tail": long_tail,
        },
        "outcome": {"status": "error", "exit_code": 1},
    }

    append(event, state_dir=tmp_path)

    day_file = _get_single_day_file(audit_root(tmp_path))
    stored_event = json.loads(day_file.read_text(encoding="utf-8").strip())

    stored_tail = stored_event["detail"]["stderr_tail"]
    assert len(stored_tail) == 2000
    assert stored_tail.endswith("_TAIL")
    assert not stored_tail.startswith("HEAD_")
    assert stored_tail == long_tail[-2000:]


def test_hash_covers_redacted_text(tmp_path: Path) -> None:
    raw_secret = "sec-tok-1122334455667788"
    bearer_token = f"Bearer {raw_secret}"
    long_tail = "ERROR: " + bearer_token + " " + ("X" * 2200)
    event = {
        "ts": "2026-10-19T10:00:00.000Z",
        "actor": {"role": "worker", "host": "claude"},
        "kind": "command",
        "target": f"run {bearer_token}",
        "quote": f"Running with {bearer_token} " + ("Q" * 250),
        "detail": {
            "argv": ["run", bearer_token],
            "stderr_tail": long_tail,
            "extra": f"more {bearer_token}",
        },
        "outcome": {"status": "ok"},
    }

    res = append(event, state_dir=tmp_path)

    day_file = _get_single_day_file(audit_root(tmp_path))
    stored_event = json.loads(day_file.read_text(encoding="utf-8").strip())

    assert res["hash"] == stored_event["hash"]
    assert stored_event["hash"] == event_hash(stored_event["prev_hash"], stored_event)
    assert stored_event["hash"] != event_hash(stored_event["prev_hash"], event)


def test_short_clean_fields_unchanged(tmp_path: Path) -> None:
    actor = {"role": "director", "host": "agy", "session_id": "s-123"}
    parent = {"plan": "plan-1", "task": "task-2"}
    outcome = {"status": "ok", "exit_code": 0, "duration_ms": 120}
    evidence = "sha256:abcd1234ef567890abcd1234ef567890abcd1234ef567890abcd1234ef567890"
    detail = {
        "argv": ["git", "status", "--short"],
        "stderr_tail": "short error message",
        "count": 5,
        "is_ok": True,
        "tags": ["alpha", "beta"],
    }
    event = {
        "ts": "2026-10-19T10:00:00.000Z",
        "actor": actor,
        "parent": parent,
        "kind": "command",
        "target": "git status --short",
        "quote": "Short clean quote without secrets",
        "evidence": evidence,
        "detail": detail,
        "outcome": outcome,
    }

    res = append(event, state_dir=tmp_path)

    day_file = _get_single_day_file(audit_root(tmp_path))
    stored_event = json.loads(day_file.read_text(encoding="utf-8").strip())

    assert res["hash"] == stored_event["hash"]
    assert stored_event["actor"] == actor
    assert stored_event["parent"] == parent
    assert stored_event["outcome"] == outcome
    assert stored_event["evidence"] == evidence
    assert stored_event["target"] == "git status --short"
    assert stored_event["quote"] == "Short clean quote without secrets"
    assert stored_event["detail"] == detail


def test_sanitize_returns_new_dict_and_does_not_mutate() -> None:
    raw_secret = "sec-tok-abcdefabcdef1234"
    detail: dict[str, Any] = {
        "argv": ["test", f"Bearer {raw_secret}"],
        "nested": {"key": f"Bearer {raw_secret}"},
    }
    event: dict[str, Any] = {
        "target": f"cmd {raw_secret}",
        "quote": f"Bearer {raw_secret}",
        "detail": detail,
    }

    sanitized = sanitize(event)

    assert sanitized is not event
    assert sanitized["detail"] is not detail
    assert sanitized["detail"]["nested"] is not detail["nested"]
    assert raw_secret in event["target"]
    assert raw_secret in event["detail"]["nested"]["key"]
    assert raw_secret not in sanitized["target"]
    assert raw_secret not in sanitized["detail"]["nested"]["key"]


def test_write_entrypoint_applies_sanitization(tmp_path: Path) -> None:
    raw_secret = "sec-tok-writeentrypoint1"
    event = {
        "ts": "2026-10-19T10:00:00.000Z",
        "actor": {"role": "worker", "host": "claude"},
        "kind": "command",
        "target": f"run Bearer {raw_secret}",
        "outcome": {"status": "ok"},
    }

    ok = write(event, state_dir=tmp_path)
    assert ok is True

    day_file = _get_single_day_file(audit_root(tmp_path))
    file_bytes = day_file.read_bytes()
    assert raw_secret.encode("utf-8") not in file_bytes
    assert b"Bearer <redacted>" in file_bytes


def test_argv_tuple_and_nested_sanitization() -> None:
    raw_secret = "sec-tok-argvtuple123456"
    event = {
        "detail": {
            "argv": ("app", "--token", raw_secret),
            "other_argv": ["run", {"token": f"--token={raw_secret}"}],
        }
    }
    sanitized = sanitize(event)
    assert sanitized["detail"]["argv"] == ("app", "--token", "<redacted>")
    assert sanitized["detail"]["other_argv"] == ["run", {"token": "<redacted>"}]


def test_secret_key_value_redacted_any_depth() -> None:
    event = {
        "actor": {
            "role": "worker",
            "host": "local",
            "auth_token": "tok123",
            "nested_actor": {"PASSWORD": "pass"},
        },
        "parent": {
            "plan": "p1",
            "user_credential": 9999,
            "deep": {"api_key": "key123"},
        },
        "detail": {
            "passwd": "short",
            "my_secret": ["a", "b"],
            "api-key": True,
            "apikey": {"sub": "obj"},
            "authorization": "Bearer xyz",
            "session_cookie": "cookie-val",
            "private_key": "private",
            "deep_nested": {
                "level2": {
                    "token_item": "abc",
                },
                "items": [
                    {"normal": "ok", "password_field": "secret1"},
                ],
            },
            "safe_key": "safe_value",
        },
    }

    sanitized = sanitize(event)

    # Actor checks
    assert sanitized["actor"]["role"] == "worker"
    assert sanitized["actor"]["host"] == "local"
    assert sanitized["actor"]["auth_token"] == "<redacted>"
    assert sanitized["actor"]["nested_actor"]["PASSWORD"] == "<redacted>"

    # Parent checks
    assert sanitized["parent"]["plan"] == "p1"
    assert sanitized["parent"]["user_credential"] == "<redacted>"
    assert sanitized["parent"]["deep"]["api_key"] == "<redacted>"

    # Detail checks - all 11 secret substrings and various types
    detail = sanitized["detail"]
    assert detail["passwd"] == "<redacted>"
    assert detail["my_secret"] == "<redacted>"
    assert detail["api-key"] == "<redacted>"
    assert detail["apikey"] == "<redacted>"
    assert detail["authorization"] == "<redacted>"
    assert detail["session_cookie"] == "<redacted>"
    assert detail["private_key"] == "<redacted>"
    assert detail["deep_nested"]["level2"]["token_item"] == "<redacted>"
    assert detail["deep_nested"]["items"][0]["normal"] == "ok"
    assert detail["deep_nested"]["items"][0]["password_field"] == "<redacted>"
    assert detail["safe_key"] == "safe_value"


def test_actor_parent_evidence_redacted() -> None:
    raw_secret = "sec-tok-actorparent123456"
    bearer = f"Bearer {raw_secret}"

    event_dict_fields = {
        "actor": {"role": "worker", "info": f"running with {bearer}"},
        "parent": {"plan": f"plan for {bearer}", "task": "clean-task"},
        "evidence": f"diff containing {bearer}",
    }
    sanitized = sanitize(event_dict_fields)
    assert sanitized["actor"]["role"] == "worker"
    assert sanitized["actor"]["info"] == "running with Bearer <redacted>"
    assert sanitized["parent"]["plan"] == "plan for Bearer <redacted>"
    assert sanitized["parent"]["task"] == "clean-task"
    assert sanitized["evidence"] == "diff containing Bearer <redacted>"

    event_str_fields = {
        "actor": f"actor-string-{bearer}",
        "parent": f"parent-string-{bearer}",
        "evidence": f"evidence-string-{bearer}",
    }
    sanitized_str = sanitize(event_str_fields)
    assert sanitized_str["actor"] == "actor-string-Bearer <redacted>"
    assert sanitized_str["parent"] == "parent-string-Bearer <redacted>"
    assert sanitized_str["evidence"] == "evidence-string-Bearer <redacted>"


def test_argv_mixed_types_keeps_pairing() -> None:
    event = {
        "detail": {
            "argv": [
                "curl",
                123,
                "--token",
                "short_token",
                False,
                "--password",
                "short_pass",
                Path("/path/to/cert"),
            ]
        }
    }
    sanitized = sanitize(event)
    expected_argv = [
        "curl",
        123,
        "--token",
        "<redacted>",
        False,
        "--password",
        "<redacted>",
        "/path/to/cert",
    ]
    assert sanitized["detail"]["argv"] == expected_argv

    event_tuple = {
        "detail": {
            "argv": ("api", 456, "--token", "secret_val"),
        }
    }
    sanitized_tuple = sanitize(event_tuple)
    assert sanitized_tuple["detail"]["argv"] == ("api", 456, "--token", "<redacted>")


def test_target_argv_aware() -> None:
    # 1. mysql -p password pair
    ev1 = {"target": "mysql -u root -p my_secret_pw"}
    s1 = sanitize(ev1)
    assert "my_secret_pw" not in s1["target"]
    assert s1["target"] == "mysql -u root -p <redacted>"

    # 2. Flag with space
    ev2 = {"target": "app --token my_short_token --verbose"}
    s2 = sanitize(ev2)
    assert "my_short_token" not in s2["target"]
    assert s2["target"] == "app --token <redacted> --verbose"

    # 3. Flag with equals
    ev3 = {"target": "curl --api-key=secret123 https://example.com"}
    s3 = sanitize(ev3)
    assert "secret123" not in s3["target"]
    assert s3["target"] == "curl --api-key=<redacted> https://example.com"

    # 4. ValueError on unmatched quote falls back to redact()
    raw_secret = "sec-tok-targetunmatched1"
    ev4 = {"target": f"bash -c 'unmatched Bearer {raw_secret}"}
    s4 = sanitize(ev4)
    assert raw_secret not in s4["target"]
    assert "Bearer <redacted>" in s4["target"]

    # 5. Long target truncated to 500 chars
    long_target = "cmd " + ("arg " * 200)
    ev5 = {"target": long_target}
    s5 = sanitize(ev5)
    assert len(s5["target"]) == 500


def test_non_json_values_coerced() -> None:
    class CustomObj:
        def __str__(self) -> str:
            return "custom-object-value"

    event = {
        "detail": {
            "set_strings": {"cherry", "apple", "banana"},
            "set_mixed": {10, 2, 1},
            "my_frozenset": frozenset({"b", "a"}),
            "my_tuple": (1, "two", (3, 4)),
            "nested": {
                "inner_set": {5, 6},
                "path": Path("/var/log/audit.log"),
                "custom": CustomObj(),
            },
            "int_val": 42,
            "bool_val": True,
            "float_val": 3.14,
            "none_val": None,
        }
    }

    sanitized = sanitize(event)
    detail = sanitized["detail"]

    # set/frozenset/tuple -> list (sets sorted by str)
    assert detail["set_strings"] == ["apple", "banana", "cherry"]
    assert detail["set_mixed"] == [1, 10, 2]
    assert detail["my_frozenset"] == ["a", "b"]
    assert detail["my_tuple"] == [1, "two", [3, 4]]
    assert detail["nested"]["inner_set"] == [5, 6]

    # anything else -> str()
    assert detail["nested"]["path"] == "/var/log/audit.log"
    assert detail["nested"]["custom"] == "custom-object-value"

    # JSON types preserved
    assert detail["int_val"] == 42
    assert detail["bool_val"] is True
    assert detail["float_val"] == 3.14
    assert detail["none_val"] is None

    # canonical_json serializes without TypeError
    serialized = canonical_json(sanitized)
    assert isinstance(serialized, str)
    assert "/var/log/audit.log" in serialized
    assert "custom-object-value" in serialized


def test_digest_fields_not_redacted(tmp_path: Path) -> None:
    hex64 = "0123456789abcdef" * 4
    hex40 = "0123456789abcdef" * 2 + "01234567"

    event = {
        "ts": "2026-10-19T10:00:00.000Z",
        "actor": {"role": "worker", "host": "claude"},
        "kind": "archive",
        "evidence": f"sha256:{hex64}",
        "detail": {
            "sha256": hex64,
            "sha256_before": hex64,
            "sha256_after": hex64,
            "sha": hex40,
            "hash": hex64,
            "prev_hash": hex64,
            "first_hash": hex64,
            "first_prev_hash": hex64,
            "last_hash": hex64,
            "nested": {
                "sha256": hex64,
            },
            # Non-lowercase-hex or invalid lengths should still be redacted
            "non_hex_digest": "sec-tok-digestnonhex12345678",
            "upper_hex_sha256": ("0123456789ABCDEF" * 4),
        },
        "outcome": {"status": "ok"},
    }

    sanitized = sanitize(event)
    detail = sanitized["detail"]

    assert sanitized["evidence"] == f"sha256:{hex64}"
    assert detail["sha256"] == hex64
    assert detail["sha256_before"] == hex64
    assert detail["sha256_after"] == hex64
    assert detail["sha"] == hex40
    assert detail["hash"] == hex64
    assert detail["prev_hash"] == hex64
    assert detail["first_hash"] == hex64
    assert detail["first_prev_hash"] == hex64
    assert detail["last_hash"] == hex64
    assert detail["nested"]["sha256"] == hex64

    # Non-lowercase-hex or random token is redacted
    assert detail["non_hex_digest"] == "<redacted>"
    assert detail["upper_hex_sha256"] == "<redacted>"

    # Invalid evidence format gets redacted
    invalid_ev = sanitize({"evidence": "sha256:Bearer sec-tok-1234567890123456"})
    assert invalid_ev["evidence"] == "sha256:Bearer <redacted>"

    # Writing to file keeps the plain digest
    append(event, state_dir=tmp_path)
    day_file = _get_single_day_file(audit_root(tmp_path))
    file_bytes = day_file.read_bytes()
    assert hex64.encode("utf-8") in file_bytes
    assert hex40.encode("utf-8") in file_bytes
    assert f"sha256:{hex64}".encode("utf-8") in file_bytes
    assert b"sec-tok-digestnonhex" not in file_bytes


def test_target_kv_space_rule_still_applies() -> None:
    event = {"target": "git config user.password hunter2"}
    sanitized = sanitize(event)
    assert "hunter2" not in sanitized["target"]
    assert sanitized["target"] == "git config user.password <redacted>"


def test_target_keeps_original_text() -> None:
    event = {"target": "ls | grep x && rm y"}
    sanitized = sanitize(event)
    assert sanitized["target"] == "ls | grep x && rm y"

    event_with_secret = {"target": "ls | grep --password my_secret && rm y"}
    sanitized_secret = sanitize(event_with_secret)
    assert "my_secret" not in sanitized_secret["target"]
    assert sanitized_secret["target"] == "ls | grep --password <redacted> && rm y"


def test_argv_non_string_items_sanitized() -> None:
    home_path = Path("/home/alice/secret_file.txt")
    secret_dict = {"password": "secret_pw", "other_key": "safe_val"}
    event = {
        "detail": {
            "argv": [
                "run",
                home_path,
                secret_dict,
                123,
                True,
            ]
        }
    }
    sanitized = sanitize(event)
    argv = sanitized["detail"]["argv"]

    assert "/home/alice" not in str(argv)
    assert argv[0] == "run"
    assert argv[1] == "~/secret_file.txt"

    assert argv[2]["password"] == "<redacted>"
    assert argv[2]["other_key"] == "safe_val"
    assert "secret_pw" not in str(argv)

    assert argv[3] == 123
    assert argv[4] is True

    serialized = canonical_json(sanitized)
    assert "/home/alice" not in serialized
    assert "secret_pw" not in serialized
    assert "~/secret_file.txt" in serialized
    assert '"password":"<redacted>"' in serialized


def test_target_short_value_token_bound() -> None:
    sanitized = sanitize({"target": "cmd --token=a --name abc banana"})
    assert sanitized["target"] == "cmd --token=<redacted> --name abc banana"

    two = sanitize({"target": "cmd --token=ab --password=abc x"})
    assert two["target"] == "cmd --token=<redacted> --password=<redacted> x"


def test_target_escaped_value_falls_back() -> None:
    sanitized = sanitize({"target": "app --token sec\\ ret42 --verbose"})
    assert "sec" not in sanitized["target"]
    assert "ret42" not in sanitized["target"]
    assert "<redacted>" in sanitized["target"]
    assert "--verbose" in sanitized["target"]


def test_target_secret_with_at_sign_fully_redacted() -> None:
    sanitized = sanitize({"target": "mysql -pabc@Xk9pLmQ2vR7tY4wZq db"})
    assert "abc" not in sanitized["target"]
    assert "Xk9pLmQ2vR7tY4wZq" not in sanitized["target"]
    assert sanitized["target"] == "mysql -p<redacted> db"


def test_target_shlex_failure_still_applies_argv_rules() -> None:
    sanitized = sanitize({"target": "mysql -phunter2 db # don't"})
    assert "hunter2" not in sanitized["target"]
    assert sanitized["target"].startswith("mysql -p<redacted> db")

    spaced = sanitize({"target": "mysql -p hunter2 db # don't"})
    assert "hunter2" not in spaced["target"]

    flagged = sanitize({"target": "app --token abcd1234 run # it's"})
    assert "abcd1234" not in flagged["target"]
