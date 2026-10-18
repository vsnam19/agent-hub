import asyncio

from agenthub.mcpserver import MAX_WAIT_S, build_server, clamp_timeout


def test_wait_timeout_capped_below_120():
    assert MAX_WAIT_S == 100
    assert clamp_timeout(300) == 100
    assert clamp_timeout("9m") == 100
    assert clamp_timeout(30) == 30


def test_wait_descriptions_say_call_again():
    server = build_server()
    tools = asyncio.run(server.list_tools())
    wait_tools = [t for t in tools if "wait" in t.name]
    assert {t.name for t in wait_tools} == {"wait_job", "plan_wait", "plan_review_wait"}
    for tool in wait_tools:
        assert "call again" in tool.description
        assert "100" in tool.description
        assert "returns state running when not finished — call again while running" in tool.description
