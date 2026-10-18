from agenthub.config import load_pool, paths


def test_default_pool_has_no_removed_models(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_HUB_CONFIG", str(tmp_path / "conf"))
    p = paths()
    pool = load_pool(p)

    removed = "gemini" + "-3.1-pro"
    for role_name, role in pool.roles.items():
        for model in role.chain:
            assert removed not in model, f"role {role_name} contains removed model: {model}"

    assert "agy:gemini-3.8-flash-high" in pool.roles["implement"].chain
