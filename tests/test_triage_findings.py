from agenthub.triage import ReviewFinding, review_findings, unparsed_findings


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
    assert len(findings) == 4
    assert findings[0] == ReviewFinding(
        id="F1",
        severity="high",
        text="critical — agenthub/a.py:1 — unknown severity",
    )
    assert findings[1] == ReviewFinding(
        id="F2",
        severity="high",
        text="high — agenthub/b.py:2 — valid finding not a bullet line",
    )
    assert findings[2] == ReviewFinding(
        id="F3",
        severity="high",
        text="high — asterisk bullet not dash",
    )
    assert findings[3] == ReviewFinding(
        id="F4",
        severity="low",
        text="low — agenthub/d.py:4 — valid finding 2",
    )
    assert unparsed_findings(reply) == 1


def test_critical_important_minor_map():
    reply = """
## Findings
- critical — bug 1 — critical bug
- IMPORTANT — bug 2 — important bug
- Minor — bug 3 — minor bug
"""
    findings = review_findings(reply)
    assert len(findings) == 3
    assert findings[0] == ReviewFinding(
        id="F1",
        severity="high",
        text="critical — bug 1 — critical bug",
    )
    assert findings[1] == ReviewFinding(
        id="F2",
        severity="medium",
        text="IMPORTANT — bug 2 — important bug",
    )
    assert findings[2] == ReviewFinding(
        id="F3",
        severity="low",
        text="Minor — bug 3 — minor bug",
    )


def test_wrapped_severity_shapes():
    reply = """
## Findings
- **medium** — agenthub/triage.py:29 — Findings
- `high` — a.py:3 — leak
- <low> — c.py:1 — typo
- [high] x
- high: x
- **critical**: y
- [important]: z
- `minor`: w
"""
    findings = review_findings(reply)
    assert len(findings) == 8
    assert [f.severity for f in findings] == [
        "medium",
        "high",
        "low",
        "high",
        "high",
        "high",
        "medium",
        "low",
    ]
    assert findings[0].text == "**medium** — agenthub/triage.py:29 — Findings"
    assert findings[1].text == "`high` — a.py:3 — leak"
    assert findings[2].text == "<low> — c.py:1 — typo"
    assert findings[3].text == "[high] x"
    assert findings[4].text == "high: x"
    assert findings[5].text == "**critical**: y"
    assert findings[6].text == "[important]: z"
    assert findings[7].text == "`minor`: w"


def test_star_and_indented_bullets():
    reply = """
## Findings
* high — asterisk bullet
  - medium — indented dash
  * low — indented star
    - critical — four spaces indent
"""
    findings = review_findings(reply)
    assert len(findings) == 4
    assert findings[0] == ReviewFinding(
        id="F1",
        severity="high",
        text="high — asterisk bullet",
    )
    assert findings[1] == ReviewFinding(
        id="F2",
        severity="medium",
        text="medium — indented dash",
    )
    assert findings[2] == ReviewFinding(
        id="F3",
        severity="low",
        text="low — indented star",
    )
    assert findings[3] == ReviewFinding(
        id="F4",
        severity="high",
        text="critical — four spaces indent",
    )


def test_continuation_lines_appended():
    reply = """
## Findings
- high — agenthub/a.py:12 — file descriptor leak
  - **Failure scenario:** leaks fds on long runs.
  Evidence from run.
- medium: agenthub/b.py:44 — missing none check
    wrapped line indented
    another wrapped line
"""
    findings = review_findings(reply)
    assert len(findings) == 2
    assert findings[0] == ReviewFinding(
        id="F1",
        severity="high",
        text=(
            "high — agenthub/a.py:12 — file descriptor leak "
            "- **Failure scenario:** leaks fds on long runs. "
            "Evidence from run."
        ),
    )
    assert findings[1] == ReviewFinding(
        id="F2",
        severity="medium",
        text=("medium: agenthub/b.py:44 — missing none check wrapped line indented another wrapped line"),
    )


def test_heading_variants():
    cases = [
        ("# Findings\n- high — x", "high"),
        ("## Findings:\n- medium — x", "medium"),
        ("### Findings\n- low — x", "low"),
        ("## Findings (3)\n- critical — x", "high"),
        ("**Findings**\n- high — x", "high"),
        ("**Findings:**\n- medium — x", "medium"),
        ("**Findings (1)**\n- low — x", "low"),
        ("## findings\n- high — x", "high"),
    ]
    for reply, expected_sev in cases:
        findings = review_findings(reply)
        assert len(findings) == 1, f"Failed for heading in reply: {reply!r}"
        assert findings[0].severity == expected_sev

    multi_reply = """
## Findings
- high — old finding

## Findings (1)
- medium — new finding

## Verdict
request-changes
"""
    findings = review_findings(multi_reply)
    assert len(findings) == 1
    assert findings[0] == ReviewFinding(id="F1", severity="medium", text="medium — new finding")

    bold_end_reply = """
## Findings
- low — finding 1

**Verdict**
request-changes
- high — ignored finding under bold verdict
"""
    findings = review_findings(bold_end_reply)
    assert len(findings) == 1
    assert findings[0] == ReviewFinding(id="F1", severity="low", text="low — finding 1")


def test_unparsed_findings_counts_top_level_bullets():
    assert unparsed_findings("## Findings\n- high — x\n* medium — y\n") == 0
    assert unparsed_findings("## Findings\n(none)\n") == 0
    assert unparsed_findings("## Findings\n- (none)\n") == 0
    assert unparsed_findings("## Findings\n* None\n") == 0
    assert unparsed_findings("## Findings\n- no findings\n") == 0
    assert unparsed_findings("## Findings\n- N/A\n") == 0
    assert unparsed_findings("## Verdict\napprove\n") == 0
    assert unparsed_findings("") == 0

    reply = """
## Findings
- high — valid finding
  - **Failure scenario:** indented sub-bullet should not count
  plain wrapped continuation line
- note — top-level bullet unknown severity (counts)
* something else — another top-level bullet unknown (counts)
- (none) — none-like bullet (should not count)
* low — valid finding 2

## Verdict
request-changes
- note — under verdict, should not count
"""
    assert unparsed_findings(reply) == 2
