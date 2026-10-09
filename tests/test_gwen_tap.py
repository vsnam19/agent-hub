import importlib.machinery
import importlib.util
import json
import socket
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from tests.bench.fakeserver import FakeServer

TAP_PATH = Path(__file__).parent.parent / "scripts" / "gwen-host" / "gwen-tap"


def load_tap(monkeypatch, tmp_path, upstream_port):
    monkeypatch.setenv("GWEN_UPSTREAM_PORT", str(upstream_port))
    monkeypatch.setenv("GWEN_OUTPUT_LOG", str(tmp_path / "output.log"))
    loader = importlib.machinery.SourceFileLoader("gwen_tap", str(TAP_PATH))
    spec = importlib.util.spec_from_loader("gwen_tap", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture
def upstream():
    srv = FakeServer()
    srv.start()
    yield srv
    srv.stop()


@pytest.fixture
def tap(monkeypatch, tmp_path, upstream):
    mod = load_tap(monkeypatch, tmp_path, upstream.server.server_port)
    server = ThreadingHTTPServer(("127.0.0.1", 0), mod.Tap)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", tmp_path / "output.log", upstream
    server.shutdown()


def post(url, path, payload, headers=None):
    req = urllib.request.Request(url + path, data=json.dumps(payload).encode(), method="POST", headers=headers or {})
    with urllib.request.urlopen(req) as resp:
        return resp.status, resp.read()


def test_streamed_text_is_passed_through_and_logged(tap):
    url, log, upstream = tap
    status, body = post(url, "/v1/chat/completions_sse", {"model": "qwen-x", "stream": True})
    assert status == 200
    assert b'"content":"Hello"' in body and b'"usage"' in body
    text = log.read_text()
    assert "qwen-x" in text
    assert "Hello there" in text


def test_json_reply_logged_and_auth_header_forwarded_not_logged(tap):
    url, log, upstream = tap
    status, body = post(url, "/v1/chat/completions_json", {"model": "m"}, {"Authorization": "Bearer sekret-123"})
    assert status == 200
    assert json.loads(body)["choices"][0]["message"]["content"] == "Hello there."
    assert upstream.server.requests[-1]["headers"]["Authorization"] == "Bearer sekret-123"
    assert "Hello there." in log.read_text()
    assert "sekret-123" not in log.read_text()


def test_upstream_down_gives_502(monkeypatch, tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]
    mod = load_tap(monkeypatch, tmp_path, free_port)
    server = ThreadingHTTPServer(("127.0.0.1", 0), mod.Tap)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with pytest.raises(urllib.error.HTTPError) as err:
            post(f"http://127.0.0.1:{server.server_port}", "/v1/chat/completions", {"model": "m"})
        assert err.value.code == 502
    finally:
        server.shutdown()


def test_pieces_for_three_apis(monkeypatch, tmp_path):
    mod = load_tap(monkeypatch, tmp_path, 1)
    call = {"function": {"name": "edit", "arguments": '{"a"'}}
    openai = {"choices": [{"delta": {"reasoning_content": "hmm", "content": "hi", "tool_calls": [call]}}]}
    assert mod.pieces(openai) == [("think", "hmm"), ("text", "hi"), ("tool", "\n[tool call] edit "), ("tool", '{"a"')]
    anthropic = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "yo"}}
    assert mod.pieces(anthropic) == [("text", "yo")]
    responses = {"type": "response.output_text.delta", "delta": "ok"}
    assert mod.pieces(responses) == [("text", "ok")]
    assert mod.pieces({"type": "ping"}) == []
