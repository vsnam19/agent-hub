from agenthub.triage import ReviewFinding, review_findings, split_severity, unparsed_findings


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
- note — top-level bullet — unknown severity (counts)
* something else — another top-level — bullet unknown (counts)
- (none) — none-like bullet (should not count)
* low — valid finding 2

## Verdict
request-changes
- note — under verdict, should not count
"""
    assert unparsed_findings(reply) == 2


def test_bold_label_does_not_end_section():
    reply = """
## Findings
- high — agenthub/a.py:12 — leak
  **Failure scenario:**
  leaks fds on long runs.
- low — agenthub/b.py:44 — typo
"""
    findings = review_findings(reply)
    assert len(findings) == 2
    assert findings[0].id == "F1"
    assert findings[0].severity == "high"
    assert findings[0].text == "high — agenthub/a.py:12 — leak **Failure scenario:** leaks fds on long runs."
    assert findings[1].id == "F2"
    assert findings[1].severity == "low"
    assert findings[1].text == "low — agenthub/b.py:44 — typo"


def test_subheadings_inside_section_keep_findings():
    reply = """
## Findings
### High
- high — agenthub/a.py:10 — severe issue
### Low
- low — agenthub/b.py:20 — minor issue

## Verdict
request-changes
"""
    findings = review_findings(reply)
    assert len(findings) == 2
    assert findings[0].id == "F1"
    assert findings[0].severity == "high"
    assert findings[0].text == "high — agenthub/a.py:10 — severe issue"
    assert findings[1].id == "F2"
    assert findings[1].severity == "low"
    assert findings[1].text == "low — agenthub/b.py:20 — minor issue"


def test_section_ends_at_verdict_bold_line():
    reply = """
## Findings
- high — agenthub/a.py:10 — problem

**Verdict**
request-changes
- high — under verdict, should be ignored
"""
    findings = review_findings(reply)
    assert len(findings) == 1
    assert findings[0].id == "F1"
    assert findings[0].severity == "high"
    assert findings[0].text == "high — agenthub/a.py:10 — problem"
    assert unparsed_findings(reply) == 0

    reply_colon = """
## Findings
- high — agenthub/a.py:10 — problem

**Verdict:**
request-changes
- high — under verdict, should be ignored
"""
    findings_colon = review_findings(reply_colon)
    assert len(findings_colon) == 1
    assert findings_colon[0].id == "F1"


def test_last_findings_heading_with_bullets_wins():
    reply = """
## Findings
- high — agenthub/a.py:10 — real finding

## Verdict
request-changes

**Findings**
(none)
"""
    findings = review_findings(reply)
    assert len(findings) == 1
    assert findings[0].id == "F1"
    assert findings[0].severity == "high"
    assert findings[0].text == "high — agenthub/a.py:10 — real finding"

    reply_no_bullets = """
## Findings
(none)

**Findings**
no findings
"""
    assert review_findings(reply_no_bullets) == []


def test_head_is_first_line_only():
    reply = """
## Findings
- high — agenthub/a.py:12 — leak
  **Failure scenario:**
  leaks fds on long runs.
- low — agenthub/b.py:44 — typo
"""
    findings = review_findings(reply)
    assert len(findings) == 2
    assert findings[0].head == "high — agenthub/a.py:12 — leak"
    assert findings[0].text == "high — agenthub/a.py:12 — leak **Failure scenario:** leaks fds on long runs."
    assert findings[1].head == "low — agenthub/b.py:44 — typo"
    assert findings[1].text == "low — agenthub/b.py:44 — typo"
    assert findings[0] == ReviewFinding(
        id="F1",
        severity="high",
        text=findings[0].text,
    )


def test_severity_word_prefix():
    reply = """
## Findings
- severity low — agenthub/triage.py:195-201 — Duplicate finding ids — scenario — evidence
- Severity: high — agenthub/a.py:1 — problem — scenario — evidence
- **severity** medium — agenthub/b.py:2 — problem — scenario — evidence
- `severity:` low — agenthub/c.py:3 — problem — scenario — evidence
- **Severity:** critical — agenthub/d.py:4 — problem — scenario — evidence
- `Severity` Important — agenthub/e.py:5 — problem — scenario — evidence
- severity: minor — agenthub/f.py:6 — problem — scenario — evidence
"""
    findings = review_findings(reply)
    assert len(findings) == 7
    assert [f.severity for f in findings] == [
        "low",
        "high",
        "medium",
        "low",
        "high",
        "medium",
        "low",
    ]
    assert findings[0].id == "F1"
    assert findings[0].head == "severity low — agenthub/triage.py:195-201 — Duplicate finding ids — scenario — evidence"
    assert findings[0].text == "severity low — agenthub/triage.py:195-201 — Duplicate finding ids — scenario — evidence"
    assert findings[1].head == "Severity: high — agenthub/a.py:1 — problem — scenario — evidence"
    assert findings[2].head == "**severity** medium — agenthub/b.py:2 — problem — scenario — evidence"


def test_checked_ok_bullets_not_unparsed():
    reply = """
## Findings
- The `triage_task` signature `(plan, …)` matches the CLI call (`triagerun.py:27-3…`)
- **Spec match:** working-tree filtering … is correct
- Exit codes for blocked tasks … match spec §4 (…)
- Only one separator — so not finding shaped
Some prose line
"""
    assert unparsed_findings(reply) == 0


def test_finding_shaped_unknown_bullet_is_unparsed():
    reply = """
## Findings
- unknown_sev — agenthub/a.py:1 — something broken — scenario — evidence
- something – other – detail
- bullet -- with -- two dashes
- normal bullet without separators
"""
    assert unparsed_findings(reply) == 3


def test_split_severity_shapes():
    assert split_severity("high — x") == ("high", "x")
    assert split_severity("**medium**: x") == ("medium", "x")
    assert split_severity("severity low — x") == ("low", "x")
    assert split_severity("`critical` x") == ("high", "x")
    assert split_severity("note — x") == (None, "note — x")
