from agenthub.handoff import FOOTER, REVIEW_FOOTER, parse, parse_touched_review, parse_verdict
from agenthub.orchestrate import _touched_section


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


def test_footer_has_escape_hatch():
    assert "do not weaken a test, a gate or the brief's conditions" in FOOTER
    assert 'Report Status "blocked"' in FOOTER
    assert FOOTER.index("do not weaken") < FOOTER.index("When you are done")


def test_parse_inconclusive_verification():
    reply = """
## Status
blocked

## Verification
inconclusive
"""
    res = parse(reply)
    assert res is not None
    assert res["verification"] == "inconclusive"
    assert res["status"] == "blocked"


def test_parse_review_verdict():
    assert parse_verdict("findings\n\n## Verdict\napprove\n") == "approve"
    assert parse_verdict("## verdict\n\nRequest-Changes.\n") == "request-changes"
    assert parse_verdict("## Verdict\napprove\n\n## Verdict\nrequest-changes\n") == "request-changes"
    assert parse_verdict("## Verdict\nmaybe\n") is None
    assert parse_verdict("no findings") is None


def test_review_footer_asks_for_findings_and_verdict_in_final_message():
    assert "## Findings" in REVIEW_FOOTER
    assert "## Verdict" in REVIEW_FOOTER
    assert "only part of your answer that is kept" in REVIEW_FOOTER
    assert REVIEW_FOOTER.index("## Findings") < REVIEW_FOOTER.index("## Verdict")


def test_parse_touched_review():
    reply = (
        "## Findings\n(none)\n\n## Gate-touched\n"
        "- a/test_x.py assert-removed lines 4,2 — legitimate — assertion moved into a helper\n"
        "b.toml gate-config-changed lines 1 — weakening — drops the lint gate\n"
        "garbage line\n"
        "c.py test-deleted — maybe — unsure\n"
        "d.py assert-removed lines 3 — legitimate — ok\n"
        "d.py assert-removed lines 3 — weakening — but not really\n\n"
        "## Verdict\napprove\n"
    )
    assert parse_touched_review(reply) == {
        ("a/test_x.py", "assert-removed", (2, 4)): "legitimate",
        ("b.toml", "gate-config-changed", (1,)): "weakening",
        ("d.py", "assert-removed", (3,)): "weakening",
    }
    assert parse_touched_review("## Verdict\napprove\n") == {}


def test_review_brief_lines_format_parses():
    brief = _touched_section([{"file": "t.py", "kind": "assert-removed", "lines": [152, 165]}])
    assert "- t.py assert-removed lines 152,165\n" in brief
    assert "lines [" not in brief
    assert parse_touched_review("## Gate-touched\nt.py assert-removed lines 152,165 — legitimate — ok\n") == {
        ("t.py", "assert-removed", (152, 165)): "legitimate",
    }
    assert parse_touched_review("## Gate-touched\nt.py assert-removed lines [152, 165] — weakening — no\n") == {
        ("t.py", "assert-removed", (152, 165)): "weakening",
    }
