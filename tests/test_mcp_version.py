import importlib.metadata

from agenthub.mcpserver import build_server


def test_server_info_has_version():
    server = build_server()
    expected_version = importlib.metadata.version("agent-hub")
    assert server.version == expected_version
    assert server.version != ""


def test_server_version_fallback_on_package_not_found(monkeypatch):
    def fake_version(pkg):
        raise importlib.metadata.PackageNotFoundError

    monkeypatch.setattr(importlib.metadata, "version", fake_version)
    server = build_server()
    assert server.version == "0.0.0"
