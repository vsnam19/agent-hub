import json
import sys
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from agenthub.agents import Invocation, Outcome, Request
from agenthub.agents.gwen import GwenAdapter
from agenthub.config import paths
from agenthub.pool import Cooldowns, parse_link
from agenthub.process import run
from tests.test_run import go, job_dir, now, scenario

KEY = "sekrit-key-123"


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.auth.append(self.headers.get("Authorization"))
        if self.server.status != 200:
            self.send_response(self.server.status)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = json.dumps({"data": [{"id": m} for m in self.server.models]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.models = ["qwen-test"]
    srv.status = 200
    srv.auth = []
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def _configure(tmp_path, monkeypatch, port, key=KEY):
    cfg = tmp_path / "gwencfg"
    cfg.mkdir()
    key_file = tmp_path / "keyfile"
    if key is not None:
        key_file.write_text(key + "\n")
    (cfg / "gwen.toml").write_text(f'base_url = "http://127.0.0.1:{port}"\napi_key_file = "{key_file}"\n')
    monkeypatch.setenv("AGENT_HUB_CONFIG", str(cfg))
    return key_file


def _adapter():
    a = GwenAdapter()
    a.config = type("C", (), {"binary": "opencode"})()
    return a


def _req(tmp_path, role="implement", model="qwen-test"):
    brief = tmp_path / "b.md"
    brief.write_text("do the thing\nplease")
    attempt = tmp_path / "attempt"
    attempt.mkdir(exist_ok=True)
    return Request(
        role=role,
        model=model,
        workdir=tmp_path,
        brief_path=brief,
        attempt_dir=attempt,
        git_common_dir=tmp_path / ".git",
    )


def test_gwen_preflight_ok(tmp_path, monkeypatch, server):
    _configure(tmp_path, monkeypatch, server.server_address[1])
    assert _adapter().preflight(_req(tmp_path)) is None
    assert server.auth == [f"Bearer {KEY}"]


def test_gwen_preflight_unreachable_falls_through(env_sandbox, repo, tmp_path, monkeypatch, server):
    port = server.server_address[1]
    server.shutdown()
    server.server_close()
    _configure(env_sandbox.config.parent, monkeypatch, port)
    scenario(monkeypatch, "claude", "ok")
    result = go(repo, ["gwen:qwen-test", "claude:haiku"])
    assert result["status"] == "ok"
    assert result["agent"] == "claude"
    first = result["attempts"][0]
    assert first["agent"] == "gwen" and first["outcome"] == "model-unavailable"
    assert "gwen host unreachable" in first["reason"]
    assert result["attempts"][1]["outcome"] == "ok"
    cooldowns_file = paths().cooldowns_file
    assert "gwen" not in (cooldowns_file.read_text() if cooldowns_file.exists() else "")
    assert Cooldowns(cooldowns_file).until(parse_link("gwen:qwen-test"), now()) is None


def test_gwen_preflight_wrong_model(tmp_path, monkeypatch, server):
    _configure(tmp_path, monkeypatch, server.server_address[1])
    cl = _adapter().preflight(_req(tmp_path, model="other"))
    assert cl.outcome == Outcome.MODEL_UNAVAILABLE
    assert cl.detail == "gwen serves qwen-test, not other"


def test_gwen_preflight_missing_key_file(tmp_path, monkeypatch, server):
    key_file = _configure(tmp_path, monkeypatch, server.server_address[1], key=None)
    assert not key_file.exists()
    cl = _adapter().preflight(_req(tmp_path))
    assert cl.outcome == Outcome.MODEL_UNAVAILABLE
    assert "gwen api key" in cl.detail
    assert server.auth == []


def test_gwen_preflight_rejected_key(tmp_path, monkeypatch, server):
    server.status = 401
    _configure(tmp_path, monkeypatch, server.server_address[1])
    cl = _adapter().preflight(_req(tmp_path))
    assert cl.outcome == Outcome.MODEL_UNAVAILABLE
    assert "gwen api key" in cl.detail


def test_gwen_refuses_review(tmp_path, monkeypatch, server):
    _configure(tmp_path, monkeypatch, server.server_address[1])
    cl = _adapter().preflight(_req(tmp_path, role="review"))
    assert cl.outcome == Outcome.MODEL_UNAVAILABLE
    assert cl.detail == "gwen does not review"
    assert server.auth == []


def test_gwen_build_argv_and_env(tmp_path, monkeypatch, server):
    _configure(tmp_path, monkeypatch, server.server_address[1])
    req = _req(tmp_path)
    inv = _adapter().build(req)
    assert inv.argv == ["opencode", "run", "--model", "gwen/qwen-test", "do the thing\nplease"]
    assert inv.stdin_path is None
    assert inv.env["GWEN_API_KEY"] == KEY
    cfg_path = Path(inv.env["OPENCODE_CONFIG"])
    assert cfg_path == req.attempt_dir / "opencode.json"
    assert KEY not in " ".join(inv.argv)
    text = cfg_path.read_text()
    assert KEY not in text
    cfg = json.loads(text)
    opts = cfg["provider"]["gwen"]["options"]
    assert opts["apiKey"] == "{env:GWEN_API_KEY}"
    assert opts["baseURL"] == f"http://127.0.0.1:{server.server_address[1]}/v1"
    assert cfg["provider"]["gwen"]["models"] == {"qwen-test": {}}
    assert (cfg_path.stat().st_mode & 0o777) == 0o600


def test_gwen_key_not_in_job_records(env_sandbox, repo, monkeypatch, server):
    _configure(env_sandbox.config.parent, monkeypatch, server.server_address[1])
    scenario(monkeypatch, "opencode", "ok")
    result = go(repo, ["gwen:qwen-test"])
    assert result["status"] == "ok"
    files = [f for f in job_dir(result).rglob("*") if f.is_file()]
    assert any(f.name == "opencode.json" for f in files)
    for f in files:
        assert KEY not in f.read_text(errors="replace"), f
    assert KEY not in (env_sandbox.fakelog / "opencode.1.json").read_text()
    # the key did reach the child, through the environment only
    received = json.loads((env_sandbox.fakelog / "opencode.env.1.json").read_text())
    assert received["GWEN_API_KEY"] == KEY
    assert received["OPENCODE_CONFIG"] == str(job_dir(result) / "attempts" / "1-gwen-qwen-test" / "opencode.json")


def test_gwen_classify_ok(tmp_path):
    (tmp_path / "stdout").write_text("all done\n")
    cl = _adapter().classify(0, tmp_path)
    assert cl.outcome == Outcome.OK
    assert cl.reply == "all done\n"


def test_gwen_classify_context_exceeded(tmp_path):
    (tmp_path / "stdout").write_text("")
    (tmp_path / "stderr").write_text("400: maximum context length is 32768 tokens")
    cl = _adapter().classify(1, tmp_path)
    assert cl.outcome == Outcome.FAILED
    assert cl.detail == "context exceeded"


def test_gwen_classify_empty(tmp_path):
    (tmp_path / "stdout").write_text("  \n")
    assert _adapter().classify(0, tmp_path).outcome == Outcome.EMPTY


def test_gwen_classify_connection_refused_and_failure(tmp_path):
    (tmp_path / "stderr").write_text("fetch failed: ECONNREFUSED")
    assert _adapter().classify(1, tmp_path).outcome == Outcome.MODEL_UNAVAILABLE
    (tmp_path / "stderr").write_text("boom")
    assert _adapter().classify(2, tmp_path).outcome == Outcome.FAILED


def test_invocation_env_merged_into_child(tmp_path):
    inv = Invocation(
        argv=[sys.executable, "-c", "import os; print(os.environ['EXTRA'], os.environ['BASE'])"],
        stdin_path=None,
        env={"EXTRA": "x"},
    )
    ran = run(inv, tmp_path, {"BASE": "b", "PATH": "/usr/bin:/bin"}, tmp_path, timedelta(seconds=20))
    assert ran.rc == 0
    assert (tmp_path / "stdout").read_text().strip() == "x b"


def test_default_pool_implement_small_starts_with_gwen():
    from agenthub.config import load_pool

    pool = load_pool()
    chain = pool.roles["implement-small"].chain
    assert [str(link) for link in chain] == ["gwen:qwen3.5-9b-4bit", "claude:haiku"]
