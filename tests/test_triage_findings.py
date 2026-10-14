from agenthub.triage import ReviewFinding, review_findings


def test_extracts_in_order_with_ids():
    reply = """
## Findings
- high — agenthub/a.py:12 — x is never closed — a long run leaks fds — read a.py:5-20
- **Medium** — agenthub/b.py:44 — missing check — crash on None — inspect b.py
- <low> — agenthub/c.py:1 — typo in comment — minor — read c.py

## Verdict
request-changes
"""
    findings = review_findings(reply)
    assert len(findings) == 3
    assert findings[0] == ReviewFinding(
        id="F1",
        severity="high",
        text="high — agenthub/a.py:12 — x is never closed — a long run leaks fds — read a.py:5-20",
    )
    assert findings[1] == ReviewFinding(
        id="F2",
        severity="medium",
        text="**Medium** — agenthub/b.py:44 — missing check — crash on None — inspect b.py",
    )
    assert findings[2] == ReviewFinding(
        id="F3",
        severity="low",
        text="<low> — agenthub/c.py:1 — typo in comment — minor — read c.py",
    )


def test_none_gives_empty_list():
    reply = """
## Findings
(none)

## Verdict
approve
"""
    assert review_findings(reply) == []


def test_no_findings_heading_gives_empty_list():
    reply = """
## Verdict
approve
"""
    assert review_findings(reply) == []
    assert review_findings("") == []


def test_stops_at_next_heading():
    reply = """
### Findings
- high — agenthub/a.py:10 — problem — scenario — evidence

## Verdict
request-changes
- high — agenthub/ignored.py:1 — this is under verdict, should be ignored

### Other Section
- medium — agenthub/ignored2.py:2 — should also be ignored
"""
    findings = review_findings(reply)
    assert len(findings) == 1
    assert findings[0] == ReviewFinding(
        id="F1",
        severity="high",
        text="high — agenthub/a.py:10 — problem — scenario — evidence",
    )


def test_unknown_severity_line_is_skipped():
    reply = """
## Findings
Some intro text before findings.
- critical — agenthub/a.py:1 — unknown severity
- high — agenthub/b.py:2 — valid finding
not a bullet line
- note — agenthub/c.py:3 — another unknown
* high — asterisk bullet not dash
- low — agenthub/d.py:4 — valid finding 2

## Verdict
request-changes
"""
    findings = review_findings(reply)
    assert len(findings) == 2
    assert findings[0] == ReviewFinding(
        id="F1",
        severity="high",
        text="high — agenthub/b.py:2 — valid finding",
    )
    assert findings[1] == ReviewFinding(
        id="F2",
        severity="low",
        text="low — agenthub/d.py:4 — valid finding 2",
    )
