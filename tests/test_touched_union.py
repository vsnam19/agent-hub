from agenthub.orchestrate import _touched_problems


def test_split_legitimate_entries_cover_group():
    findings = [
        {
            "file": "tests/test_foo.py",
            "kind": "assert-removed",
            "lines": [235, 236, 237, 239, 1409, 1410],
        }
    ]
    judges = [
        {
            "verdict": "approve",
            "touched_review": [
                {
                    "file": "tests/test_foo.py",
                    "kind": "assert-removed",
                    "lines": [235, 236, 237, 239],
                    "judgement": "legitimate",
                },
                {
                    "file": "tests/test_foo.py",
                    "kind": "assert-removed",
                    "lines": [1409, 1410],
                    "judgement": "legitimate",
                },
            ],
        }
    ]
    problems = _touched_problems(findings, judges)
    assert problems == []


def test_partial_cover_is_unjudged():
    findings = [
        {
            "file": "tests/test_foo.py",
            "kind": "assert-removed",
            "lines": [235, 236, 237, 239, 1409, 1410],
        }
    ]
    judges = [
        {
            "verdict": "approve",
            "touched_review": [
                {
                    "file": "tests/test_foo.py",
                    "kind": "assert-removed",
                    "lines": [235, 236],
                    "judgement": "legitimate",
                },
            ],
        }
    ]
    problems = _touched_problems(findings, judges)
    assert problems == ["gate touched: 1 findings not judged by the reviewer"]


def test_weakening_overlap_blocks():
    findings = [
        {
            "file": "tests/test_foo.py",
            "kind": "assert-removed",
            "lines": [235, 236, 237, 239, 1409, 1410],
        }
    ]
    judges = [
        {
            "verdict": "approve",
            "touched_review": [
                {
                    "file": "tests/test_foo.py",
                    "kind": "assert-removed",
                    "lines": [235, 236, 237, 239],
                    "judgement": "legitimate",
                },
                {
                    "file": "tests/test_foo.py",
                    "kind": "assert-removed",
                    "lines": [1409, 1410],
                    "judgement": "legitimate",
                },
                {
                    "file": "tests/test_foo.py",
                    "kind": "assert-removed",
                    "lines": [1409],
                    "judgement": "weakening",
                },
            ],
        }
    ]
    problems = _touched_problems(findings, judges)
    assert problems == ["reviewer judged tests/test_foo.py assert-removed lines 235,236,237,239,1409,1410 as weakening"]


def test_legitimate_from_request_changes_does_not_count():
    findings = [
        {
            "file": "tests/test_foo.py",
            "kind": "assert-removed",
            "lines": [235, 236, 237, 239, 1409, 1410],
        }
    ]
    judges = [
        {
            "verdict": "request-changes",
            "touched_review": [
                {
                    "file": "tests/test_foo.py",
                    "kind": "assert-removed",
                    "lines": [235, 236, 237, 239],
                    "judgement": "legitimate",
                },
                {
                    "file": "tests/test_foo.py",
                    "kind": "assert-removed",
                    "lines": [1409, 1410],
                    "judgement": "legitimate",
                },
            ],
        }
    ]
    problems = _touched_problems(findings, judges)
    assert problems == ["gate touched: 1 findings not judged by the reviewer"]
