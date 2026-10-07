import pytest

from agenthub.env import child_env


@pytest.mark.parametrize(
    "name",
    [
        "GH_TOKEN",
        "OPENAI_API_KEY",
        "MY_SECRET",
        "DB_PASSWORD",
        "FNOX_X",
        "AWS_REGION",
        "GITHUB_X",
        "SSH_PRIVATE_KEY",
    ],
)
def test_removes_secret_like_names(name):
    base = {name: "value", "OK": "yes"}
    filtered, removed = child_env(base)
    assert name not in filtered
    assert "OK" in filtered
    assert removed == [name]


def test_keeps_normal_names():
    base = {"PATH": "/bin", "HOME": "/home", "LANG": "en", "TERM": "xterm", "KEYBOARD": "us"}
    filtered, removed = child_env(base)
    assert filtered == base
    assert removed == []


def test_passthrough_keeps_named():
    base = {"GH_TOKEN": "secret", "MY_SECRET": "shh"}
    filtered, removed = child_env(base, passthrough=["GH_TOKEN"])
    assert "GH_TOKEN" in filtered
    assert "MY_SECRET" not in filtered
    assert removed == ["MY_SECRET"]


def test_claudecode_always_removed():
    base = {"CLAUDECODE": "1", "CLAUDE_CODE_X": "2", "GH_TOKEN": "3"}
    filtered, removed = child_env(base, passthrough=["CLAUDECODE", "CLAUDE_CODE_X", "GH_TOKEN"])
    assert "CLAUDECODE" not in filtered
    assert "CLAUDE_CODE_X" not in filtered
    assert "GH_TOKEN" in filtered
    assert sorted(removed) == ["CLAUDECODE", "CLAUDE_CODE_X"]
