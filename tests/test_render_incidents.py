from __future__ import annotations

from agenthub import skillmine


def _extract(incidents: list[dict], counts: dict[str, int]) -> skillmine.Extract:
    return skillmine.Extract(
        since="2026-10-13T00:00:00+00:00",
        until="2026-10-20T00:00:00+00:00",
        incidents=incidents,
        incident_counts=counts,
    )


def _incidents_section(md: str) -> list[str]:
    lines = md.splitlines()
    start = lines.index("## Incidents")
    return lines[start + 1 :]


def test_render_lists_counts_per_kind() -> None:
    extract = _extract(
        [
            {"kind": "review-stall", "what": "a"},
            {"kind": "review-stall", "what": "b"},
            {"kind": "wrong-model", "what": "c"},
        ],
        {"wrong-model": 1, "review-stall": 2},
    )

    section = _incidents_section(skillmine.render_md(extract))

    assert section[0] == "Counts: review-stall ×2, wrong-model ×1"


def test_render_top_three_by_cost_with_lessons() -> None:
    extract = _extract(
        [
            {"kind": "low", "what": "lowest", "cost": {"attempts": 1}},
            {"kind": "top", "what": "highest", "cost": {"attempts": 5}, "lesson": "pin the model"},
            {"kind": "mid", "what": "middle", "cost": {"attempts": 3}},
            {"kind": "mid2", "what": "second", "cost": {"attempts": 4}, "lesson": "retry sooner"},
        ],
        {"low": 1, "top": 1, "mid": 1, "mid2": 1},
    )

    section = _incidents_section(skillmine.render_md(extract))

    assert section[1:] == [
        "- top (cost 5): highest — lesson: pin the model",
        "- mid2 (cost 4): second — lesson: retry sooner",
        "- mid (cost 3): middle",
    ]


def test_render_cost_counts_minutes_over_ten() -> None:
    extract = _extract(
        [
            {"kind": "slow", "what": "x", "cost": {"minutes": 25}},
            {"kind": "busy", "what": "y", "cost": {"attempts": 2, "rounds": 1}},
        ],
        {"slow": 1, "busy": 1},
    )

    section = _incidents_section(skillmine.render_md(extract))

    assert section[1:] == [
        "- busy (cost 3): y",
        "- slow (cost 2.5): x",
    ]


def test_render_without_incidents_shows_none() -> None:
    md = skillmine.render_md(_extract([], {}))

    assert _incidents_section(md) == ["- (none)"]


def test_render_keeps_existing_sections() -> None:
    extract = _extract([{"kind": "review-stall", "what": "reviewer hung"}], {"review-stall": 1})
    extract.corrections = [{"session": "s", "time": "t", "text": "never skip the gate"}]
    extract.corrections_total = 1

    md = skillmine.render_md(extract)

    assert md.startswith("# Skill mining extract (2026-10-13 .. 2026-10-20)\n\nFindings: 0; corrections: 1\n")
    assert "## Top finding groups (severity, module)\n- (none)\n" in md
    assert "## Branches with 2+ review rounds\n- (none)\n" in md
    assert "## Corrections\n- never skip the gate\n" in md
    assert md.index("## Corrections") < md.index("## Incidents")
