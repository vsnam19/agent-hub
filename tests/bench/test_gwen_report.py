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
