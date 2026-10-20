import json

from agenthub.cli import main
from agenthub.config import Paths, load_pool, paths

MARK = "(same as default)"


def default_role(env_sandbox, role):
    p = Paths(config_dir=env_sandbox.state / "no-owner-pool", state_dir=env_sandbox.state)
    return load_pool(p).roles[role]


def default_chain(env_sandbox, role):
    return list(default_role(env_sandbox, role).chain)


def write_owner(env_sandbox, roles):
    lines = []
    for name, (kind, chain) in roles.items():
        lines.append(f"[roles.{name}]")
        lines.append(f'kind = "{kind}"')
        lines.append(f"chain = {json.dumps(chain)}")
        lines.append("")
    (paths().config_dir / "pool.toml").write_text("\n".join(lines))


def header(out, role):
    return next(line for line in out.splitlines() if line.startswith(f"{role} ("))


def test_pool_marks_owner_role_same_as_default(env_sandbox, capsys):
    chain = default_chain(env_sandbox, "review")
    write_owner(env_sandbox, {"review": ("review", chain)})

    assert main(["pool"]) == 0
    out = capsys.readouterr().out
    assert "review (review):" in out
    assert header(out, "review") == f"review (review):  {MARK}"


def test_pool_does_not_mark_changed_owner_role(env_sandbox, capsys):
    chain = default_chain(env_sandbox, "review")
    assert chain
    changed = [*chain, chain[0]]
    write_owner(env_sandbox, {"review": ("review", changed)})

    assert main(["pool"]) == 0
    out = capsys.readouterr().out
    assert "review (review):" in out
    assert MARK not in out


def test_pool_marks_only_owner_roles_equal_to_default(env_sandbox, capsys):
    chain = default_chain(env_sandbox, "implement")
    assert chain
    default = default_role(env_sandbox, "implement")
    lines = [
        "[roles.implement]",
        'kind = "implement"',
        f"chain = {json.dumps(chain)}",
        f'escalate = "{default.escalate}"',
        "",
    ]
    (paths().config_dir / "pool.toml").write_text("\n".join(lines))

    assert main(["pool"]) == 0
    out = capsys.readouterr().out
    assert header(out, "implement").endswith(MARK)
    assert header(out, "review") == "review (review):"


def test_pool_does_not_mark_owner_role_with_extra_escalate(env_sandbox, capsys):
    chain = default_chain(env_sandbox, "implement")
    assert chain
    lines = [
        "[roles.implement]",
        'kind = "implement"',
        f"chain = {json.dumps(chain)}",
        'escalate = "implement-small"',
        "",
    ]
    (paths().config_dir / "pool.toml").write_text("\n".join(lines))

    assert main(["pool"]) == 0
    out = capsys.readouterr().out
    assert "implement (implement, escalate → implement-small):" in out
    assert MARK not in out


def test_pool_without_pool_toml_marks_nothing(env_sandbox, capsys):
    assert not (paths().config_dir / "pool.toml").exists()

    assert main(["pool"]) == 0
    out = capsys.readouterr().out
    assert "review (review):" in out
    assert MARK not in out
