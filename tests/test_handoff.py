from agenthub.handoff import parse


def test_parse_full_handoff():
    reply = """
## Status
success

## What I did
- wrote code
- wrote tests

## Verification
unit-test-verified

## Notes
- note 1

## Follow-ups
- follow 1
"""
    res = parse(reply)
    assert res == {
        "status": "success",
        "verification": "unit-test-verified",
        "did": ["wrote code", "wrote tests"],
        "notes": ["note 1"],
        "followups": ["follow 1"],
    }


def test_parse_uses_last_status_heading():
    reply = """
## Status
blocked

Some text here.

## Status
success

## What I did
- X
"""
    res = parse(reply)
    assert res is not None
    assert res["status"] == "success"


def test_parse_any_heading_level_and_case():
    reply = """
#### sTaTuS
partial

### WHAT I DID
* star bullet
"""
    res = parse(reply)
    assert res is not None
    assert res["status"] == "partial"
    assert res["did"] == ["star bullet"]


def test_parse_invalid_status_returns_none():
    reply = """
## Status
unknown

## What I did
- X
"""
    assert parse(reply) is None


def test_parse_missing_sections_are_empty():
    reply = """
## Status
success
"""
    res = parse(reply)
    assert res == {"status": "success", "verification": "not-verified", "did": [], "notes": [], "followups": []}


def test_parse_none_marker():
    reply = """
## Status
success
## What I did
(none)
"""
    res = parse(reply)
    assert res["did"] == []


def test_parse_unknown_verification_defaults():
    reply = """
## Status
success
## Verification
something-else
"""
    res = parse(reply)
    assert res["verification"] == "not-verified"


def test_parse_no_handoff_returns_none():
    reply = "just some text"
    assert parse(reply) is None
