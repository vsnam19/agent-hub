from agenthub import jevclient


def test_missing_binary_returns_none(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_HUB_JEV_BIN", str(tmp_path / "nonexistent"))
    monkeypatch.setenv("PATH", "")
    assert jevclient.classify_reply("foo") is None


def test_classify_reply_parses_result(monkeypatch, tmp_path):
    bin_path = tmp_path / "fake-jev"
    bin_path.write_text("""#!/bin/sh
echo '{"results": [{"id": "reply", "label": "success", "probability": 0.9, "decision": "auto"}], "model": "test-model"}'
""")
    bin_path.chmod(0o755)
    monkeypatch.setenv("AGENT_HUB_JEV_BIN", str(bin_path))

    res = jevclient.classify_reply("foo")
    assert res == {"label": "success", "probability": 0.9, "decision": "auto", "model": "test-model"}


def test_bad_json_returns_none(monkeypatch, tmp_path):
    bin_path = tmp_path / "fake-jev"
    bin_path.write_text("""#!/bin/sh
echo 'not json'
""")
    bin_path.chmod(0o755)
    monkeypatch.setenv("AGENT_HUB_JEV_BIN", str(bin_path))
    assert jevclient.classify_reply("foo") is None


def test_timeout_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(jevclient, "JEV_TIMEOUT_S", 0.1)
    bin_path = tmp_path / "fake-jev"
    bin_path.write_text("""#!/bin/sh
sleep 1
""")
    bin_path.chmod(0o755)
    monkeypatch.setenv("AGENT_HUB_JEV_BIN", str(bin_path))
    assert jevclient.classify_reply("foo") is None
