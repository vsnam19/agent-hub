from agenthub.tasksize import _paths, check
from tests.test_tasksize import make_brief


def test_dotted_module_name_is_not_a_file():
    text = "Stdlib plus `agenthub.triage` and `agenthub.planstate`. Also edits `agenthub/triage.py` and `README.md`."
    assert _paths(text) == {"agenthub/triage.py", "README.md"}

    brief = make_brief(
        files=["agenthub/triage.py", "README.md"],
        extra_expected=["   Stdlib plus `agenthub.triage` and `agenthub.planstate`."],
    )
    assert check(brief) == []

    # All 16 allowed extensions are counted even without a slash:
    allowed = (
        ".py",
        ".md",
        ".toml",
        ".json",
        ".sh",
        ".yml",
        ".yaml",
        ".txt",
        ".cpp",
        ".h",
        ".hpp",
        ".c",
        ".js",
        ".ts",
        ".cfg",
        ".ini",
    )
    for ext in allowed:
        assert _paths(f"`file{ext}`") == {f"file{ext}"}
    assert _paths("`file.unknown` `module.sub`") == set()


def test_extensionless_path_with_slash_counts():
    text = "Run `bin/agent-hub` to start."
    assert _paths(text) == {"bin/agent-hub"}

    brief = make_brief(
        files=["agenthub/tasksize.py", "tests/test_tasksize.py"],
        extra_expected=["   Updates `bin/agent-hub`."],
    )
    assert check(brief) == []

    brief_over = make_brief(
        files=["agenthub/tasksize.py", "tests/test_tasksize.py"],
        extra_expected=["   Updates `bin/agent-hub` and `bin/worker`."],
    )
    assert check(brief_over) == ["too many files: 4 (max 3)"]
