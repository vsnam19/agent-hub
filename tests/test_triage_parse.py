import json
import re

import pytest

from agenthub.triage import Triage, TriageError, TriageFinding, parse


def _reply(data: object, prefix: str = "Triage response:\n") -> str:
    return f"{prefix}```json\n{json.dumps(data)}\n```\n"


def _finding(
    id: str = "F1",
    severity: str = "high",
    decision: str = "fix",
    evidence: str = "agenthub/triage.py:10",
) -> dict:
    return {
        "id": id,
        "severity": severity,
        "decision": decision,
        "evidence": evidence,
    }


def test_parses_valid_reply():
    data = {
        "findings": [
            _finding("F1", "high", "fix", "agenthub/a.py:10"),
            _finding("F2", "medium", "reject", "tests/test_a.py:20"),
            _finding("F3", "low", "blocked", "spec section 2"),
        ],
        "fix_brief": "Fix F1 in agenthub/a.py",
    }
    reply = _reply(data)

    result = parse(reply)

    assert isinstance(result, Triage)
    assert result.fix_brief == "Fix F1 in agenthub/a.py"
    assert len(result.findings) == 3
    assert result.findings[0] == TriageFinding(
        id="F1",
        severity="high",
        decision="fix",
        evidence="agenthub/a.py:10",
    )
    assert result.findings[1] == TriageFinding(
        id="F2",
        severity="medium",
        decision="reject",
        evidence="tests/test_a.py:20",
    )
    assert result.findings[2] == TriageFinding(
        id="F3",
        severity="low",
        decision="blocked",
        evidence="spec section 2",
    )

    # Empty findings and empty fix_brief are valid
    empty_result = parse(_reply({"findings": [], "fix_brief": ""}))
    assert isinstance(empty_result, Triage)
    assert empty_result.findings == []
    assert empty_result.fix_brief == ""


def test_no_block_raises():
    with pytest.raises(TriageError, match="no ```json block"):
        parse("No block in this text.")


def test_two_blocks_raise():
    block = json.dumps({"findings": [], "fix_brief": ""})
    reply = f"```json\n{block}\n```\nand\n```json\n{block}\n```\n"

    with pytest.raises(TriageError, match="2 ```json blocks, expected exactly one"):
        parse(reply)


def test_bad_decision_raises():
    reply = _reply({"findings": [_finding(decision="ignore")], "fix_brief": ""})

    with pytest.raises(TriageError, match="bad decision 'ignore'"):
        parse(reply)


def test_missing_field_raises():
    with pytest.raises(TriageError, match="missing field: fix_brief"):
        parse(_reply({"findings": []}))

    with pytest.raises(TriageError, match="missing field: findings"):
        parse(_reply({"fix_brief": ""}))

    for field in ("id", "severity", "decision", "evidence"):
        bad_finding = _finding()
        del bad_finding[field]
        with pytest.raises(TriageError, match=f"missing field: {field}"):
            parse(_reply({"findings": [bad_finding], "fix_brief": ""}))


def test_fix_brief_must_be_string():
    for bad in (123, None, True, [], {}):
        with pytest.raises(TriageError, match="fix_brief must be a string"):
            parse(_reply({"findings": [], "fix_brief": bad}))


def test_unclosed_block_raises():
    body = json.dumps({"findings": [], "fix_brief": ""})
    with pytest.raises(TriageError, match="the ```json block is not closed"):
        parse(f"Prefix\n```json\n{body}\n")


def test_invalid_json_raises():
    with pytest.raises(TriageError, match="not valid JSON"):
        parse("```json\n{not valid json}\n```")


def test_not_an_object_raises():
    for bad in ([], "string", 123):
        with pytest.raises(TriageError, match="must be an object"):
            parse(_reply(bad))


def test_findings_must_be_list():
    for bad in ("string", 123, {}, None):
        with pytest.raises(TriageError, match="findings must be a list"):
            parse(_reply({"findings": bad, "fix_brief": ""}))


def test_finding_must_be_object():
    with pytest.raises(TriageError, match="finding 0 must be an object"):
        parse(_reply({"findings": ["not-a-dict"], "fix_brief": ""}))


def test_bad_severity_raises():
    with pytest.raises(TriageError, match="bad severity 'critical'"):
        parse(_reply({"findings": [_finding(severity="critical")], "fix_brief": ""}))


def test_evidence_must_be_string():
    for bad in (123, None, True, []):
        with pytest.raises(TriageError, match="evidence must be a string"):
            parse(_reply({"findings": [_finding(evidence=bad)], "fix_brief": ""}))


def test_finding_id_must_be_string():
    for bad in (123, None, True, []):
        with pytest.raises(TriageError, match="id must be a string"):
            parse(_reply({"findings": [_finding(id=bad)], "fix_brief": ""}))


def test_severity_and_decision_case_insensitive():
    reply = _reply(
        {
            "findings": [
                _finding(id="F1", severity="High", decision="Fix"),
                _finding(id="F2", severity="  MEDIUM  ", decision="  REJECT  "),
                _finding(id="F3", severity="LOW", decision="BLOCKED"),
            ],
            "fix_brief": "Fix F1",
        }
    )
    result = parse(reply)
    assert result.findings[0].severity == "high"
    assert result.findings[0].decision == "fix"
    assert result.findings[1].severity == "medium"
    assert result.findings[1].decision == "reject"
    assert result.findings[2].severity == "low"
    assert result.findings[2].decision == "blocked"


def test_non_string_severity_rejected():
    for bad in (123, None, True, ["high"], {"bad": "severity"}):
        reply = _reply({"findings": [_finding(severity=bad)], "fix_brief": ""})
        with pytest.raises(TriageError, match=rf"bad severity {re.escape(repr(bad))}"):
            parse(reply)
