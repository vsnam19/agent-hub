from bench.gwen.report import render  # noqa: E501


# noqa: E501
# noqa: E501
def test_render_counts_and_rates():  # noqa: E501
    records = [  # noqa: E501
        {
            "model": "mod1",
            "harness": "h1",
            "task": "t1",
            "pass": True,
            "wall_s": 10,
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            "mem": {"min_free_pct": 20, "max_swap_mb": 100},
        },  # noqa: E501
        {
            "model": "mod1",
            "harness": "h1",
            "task": "t1",
            "pass": True,
            "wall_s": 20,
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            "mem": {"min_free_pct": 30, "max_swap_mb": 50},
        },  # noqa: E501
        {
            "model": "mod1",
            "harness": "h2",
            "task": "t2",
            "pass": False,
            "wall_s": 30,
            "usage": {"prompt_tokens": 20, "completion_tokens": 10},
            "mem": None,
        },  # noqa: E501
    ]  # noqa: E501
    # noqa: E501
    out = render(records)  # noqa: E501
    # noqa: E501
    # 2 runs of t1 on h1, both passed  # noqa: E501
    assert "2/2" in out  # noqa: E501
    # 1 run of t2 on h2, failed  # noqa: E501
    assert "0/1" in out  # noqa: E501
    # h2 on t1 didn't run  # noqa: E501
    assert "–" in out  # noqa: E501


# noqa: E501
# noqa: E501
def test_render_handles_missing_usage_and_mem():  # noqa: E501
    records = [  # noqa: E501
        {"model": "mod1", "harness": "h1", "task": "t1", "pass": True, "wall_s": 10},  # noqa: E501
    ]  # noqa: E501
    out = render(records)  # noqa: E501
    assert "mod1" in out  # noqa: E501
    assert "h1" in out  # noqa: E501


def test_render_skips_infra_errors():
    base = {"harness": "h", "model": "m", "wall_s": 10, "usage": None, "mem": None}
    records = [
        {**base, "task": "t1", "pass": True, "infra_error": False},
        {**base, "task": "t1", "pass": False, "infra_error": True},
    ]
    out = render(records)
    assert "1/1" in out
    assert "1/2" not in out


def test_render_uses_real_newlines():
    records = [
        {"model": "m", "harness": "h", "task": "t1", "pass": True, "wall_s": 10, "usage": None, "mem": None},
    ]
    out = render(records)
    assert "\\n" not in out
    lines = out.splitlines()
    assert len(lines) > 3
    assert any(line.startswith("| Harness") for line in lines)


def test_report_lists_cpp_tasks_by_suite():
    base = {"model": "m", "harness": "h", "wall_s": 10, "usage": None, "mem": None, "infra_error": False}
    records = [
        {**base, "task": "t2", "pass": True},
        {**base, "task": "t10", "pass": False},
        {**base, "task": "t1", "pass": True},
    ]
    records += [{**base, "suite": "cpp", "task": f"c{i}", "pass": True} for i in range(1, 7)]

    out = render(records)

    python_part = out.split("### Suite: python")[1]
    cpp_part = out.split("### Suite: cpp")[1].split("### Suite: python")[0]
    # python: records without a suite; task columns in natural order (t2 before t10)
    python_header = next(line for line in python_part.splitlines() if line.startswith("| Harness"))
    assert [c.strip() for c in python_header.strip("|").split("|")[:4]] == ["Harness", "t1", "t2", "t10"]
    # cpp: all six tasks as their own columns, not dropped
    cpp_header = next(line for line in cpp_part.splitlines() if line.startswith("| Harness"))
    assert [c.strip() for c in cpp_header.strip("|").split("|")[:7]] == ["Harness", "c1", "c2", "c3", "c4", "c5", "c6"]
    cpp_row = next(line for line in cpp_part.splitlines() if line.startswith("| h "))
    assert cpp_row.split("|")[1:7] == [" h ", " 1/1 ", " 1/1 ", " 1/1 ", " 1/1 ", " 1/1 "]
    assert "t1" not in cpp_part
    assert "c1" not in python_part
