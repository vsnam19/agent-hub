from agenthub.config import load_pool, paths
from agenthub.pool import parse_link


def test_default_pool_has_no_removed_models(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_HUB_CONFIG", str(tmp_path / "conf"))
    p = paths()
    pool = load_pool(p)

    removed = ("gemini" + "-3.1-pro", "claude-sonnet-4-6", "claude-opus-4-6")
    for role_name, role in pool.roles.items():
        for model in role.chain:
            for item in removed:
                assert item not in model, f"role {role_name} contains removed model: {model}"

    assert any(parse_link(link).agent == "agy" for link in pool.roles["implement"].chain)


def test_default_pool_uses_argon_for_hard_and_review(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_HUB_CONFIG", str(tmp_path / "conf"))
    p = paths()
    pool = load_pool(p)

    assert pool.roles["implement-hard"].chain
    assert pool.roles["review"].chain
    assert parse_link(pool.roles["implement-hard"].chain[0]).agent == "agy"
    assert parse_link(pool.roles["review"].chain[0]).agent == "agy"
