import json
import time
import urllib.error
import urllib.request

import pytest

from bench.gwen.proxy import RecordingProxy
from tests.bench.fakeserver import FakeServer


@pytest.fixture
def fake_upstream():
    srv = FakeServer()
    srv.start()
    yield srv
    srv.stop()


def test_forwards_json_and_records_usage(tmp_path, fake_upstream):
    log_path = tmp_path / "proxy.jsonl"
    proxy = RecordingProxy(fake_upstream.url, log_path)
    url = proxy.start()

    req = urllib.request.Request(f"{url}/v1/chat/completions_json", method="POST", data=b"{}")
    req.add_header("Authorization", "Bearer secret")
    with urllib.request.urlopen(req) as resp:
        body = resp.read()

    proxy.stop()

    data = json.loads(body)
    assert data["usage"]["prompt_tokens"] == 9

    logs = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert len(logs) == 1
    log = logs[0]
    assert log["path"] == "/v1/chat/completions_json"
    assert log["usage"]["prompt_tokens"] == 9
    assert log["usage"]["completion_tokens"] == 12
    assert log["usage"]["cached_tokens"] == 5

    summary = proxy.summary()
    assert summary["requests"] == 1
    assert summary["prompt_tokens"] == 9
    assert summary["completion_tokens"] == 12
    assert summary["cached_tokens"] == 5


def test_streams_sse_incrementally(tmp_path, fake_upstream):
    log_path = tmp_path / "proxy.jsonl"
    proxy = RecordingProxy(fake_upstream.url, log_path)
    url = proxy.start()

    req = urllib.request.Request(f"{url}/v1/chat/completions_sse", method="POST", data=b"{}")
    start_time = time.time()

    with urllib.request.urlopen(req) as resp:
        _ = resp.read(10)
        first_chunk_time = time.time()

        # Read the rest
        resp.read()
        end_time = time.time()

    proxy.stop()

    # Should take at least 3 * 0.3 = 0.9s for the whole response
    # But first chunk should arrive quickly (within 0.3s)
    assert first_chunk_time - start_time < 0.5
    assert end_time - start_time >= 0.9

    logs = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert len(logs) == 1
    log = logs[0]
    assert log["usage"]["prompt_tokens"] == 10
    assert log["usage"]["completion_tokens"] == 15
    assert log["usage"]["cached_tokens"] == 4


def test_anthropic_usage_summed(tmp_path, fake_upstream):
    log_path = tmp_path / "proxy.jsonl"
    proxy = RecordingProxy(fake_upstream.url, log_path)
    url = proxy.start()

    req = urllib.request.Request(f"{url}/v1/messages_sse", method="POST", data=b"{}")
    with urllib.request.urlopen(req) as resp:
        resp.read()

    proxy.stop()

    logs = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert len(logs) == 1
    log = logs[0]
    assert log["usage"]["prompt_tokens"] == 20
    assert log["usage"]["completion_tokens"] == 15
    assert log["usage"]["cached_tokens"] == 10


def test_upstream_down_gives_502(tmp_path):
    log_path = tmp_path / "proxy.jsonl"
    proxy = RecordingProxy("http://127.0.0.1:1", log_path)
    url = proxy.start()

    req = urllib.request.Request(f"{url}/v1/test", method="POST", data=b"{}")
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(req)
    assert excinfo.value.code == 502

    proxy.stop()

    logs = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert len(logs) == 1
    assert "error" in logs[0]


def test_log_has_no_headers_or_bodies(tmp_path, fake_upstream):
    log_path = tmp_path / "proxy.jsonl"
    proxy = RecordingProxy(fake_upstream.url, log_path)
    url = proxy.start()

    req = urllib.request.Request(f"{url}/v1/chat/completions_json", method="POST", data=b'{"super_secret_code": "42"}')
    req.add_header("Authorization", "Bearer TOP_SECRET_KEY")
    with urllib.request.urlopen(req) as resp:
        resp.read()

    proxy.stop()

    log_text = log_path.read_text()
    assert "TOP_SECRET_KEY" not in log_text
    assert "super_secret_code" not in log_text


def _post_through_proxy(tmp_path, fake_upstream, path, payload):
    proxy = RecordingProxy(fake_upstream.url, tmp_path / "proxy.jsonl")
    url = proxy.start()
    req = urllib.request.Request(f"{url}{path}", method="POST", data=json.dumps(payload).encode())
    try:
        urllib.request.urlopen(req).read()
    except urllib.error.HTTPError:
        pass
    proxy.stop()
    return json.loads(fake_upstream.server.requests[-1]["body"])


def test_streaming_chat_request_asks_for_usage(tmp_path, fake_upstream):
    sent = _post_through_proxy(tmp_path, fake_upstream, "/v1/chat/completions", {"model": "m", "stream": True})
    assert sent["stream_options"] == {"include_usage": True}
    assert sent["model"] == "m"


def test_non_streaming_and_other_paths_unchanged(tmp_path, fake_upstream):
    plain = _post_through_proxy(tmp_path, fake_upstream, "/v1/chat/completions", {"model": "m"})
    assert "stream_options" not in plain
    other = _post_through_proxy(tmp_path, fake_upstream, "/v1/messages", {"model": "m", "stream": True})
    assert "stream_options" not in other


def test_client_disconnect_mid_stream_is_logged_not_raised(tmp_path, fake_upstream, capsys):
    import socket

    log_path = tmp_path / "proxy.jsonl"
    proxy = RecordingProxy(fake_upstream.url, log_path)
    url = proxy.start()
    host, port = url.removeprefix("http://").split(":")
    sock = socket.create_connection((host, int(port)))
    sock.sendall(b"POST /v1/chat/completions_sse HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\n\r\n{}")
    sock.recv(64)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
    sock.close()
    deadline = time.time() + 5
    while time.time() < deadline and not log_path.exists():
        time.sleep(0.1)
    proxy.stop()

    logs = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert logs[-1]["error"] == "client disconnected"
    assert "Traceback" not in capsys.readouterr().err
