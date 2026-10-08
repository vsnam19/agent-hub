import subprocess

from agenthub import gateguard, guards


def run_git(repo_path, *args):
    subprocess.run(["git", *args], cwd=repo_path, check=True)


def test_clean_diff_empty(repo):
    since = guards.head(guards.inspect(repo))
    res = gateguard.inspect_diff(guards.inspect(repo), since)
    assert res == []


def test_assert_removed_in_test_file(repo):
    test_file = repo / "test_a.py"
    test_file.write_text("def test_foo():\n    assert True\n")
    run_git(repo, "add", "test_a.py")
    run_git(repo, "commit", "-m", "add test")
    since = guards.head(guards.inspect(repo))

    test_file.write_text("def test_foo():\n    pass\n")
    res = gateguard.inspect_diff(guards.inspect(repo), since)
    assert res == [{"file": "test_a.py", "kind": "assert-removed", "lines": [2]}]


def test_assert_removed_in_src_ignored(repo):
    src_file = repo / "src.py"
    src_file.write_text("def foo():\n    assert True\n")
    run_git(repo, "add", "src.py")
    run_git(repo, "commit", "-m", "add src")
    since = guards.head(guards.inspect(repo))

    src_file.write_text("def foo():\n    pass\n")
    res = gateguard.inspect_diff(guards.inspect(repo), since)
    assert res == []


def test_skip_and_xfail_added(repo):
    test_file = repo / "test_b.py"
    test_file.write_text("def test_foo():\n    pass\n")
    run_git(repo, "add", "test_b.py")
    run_git(repo, "commit", "-m", "add test")
    since = guards.head(guards.inspect(repo))

    test_file.write_text("import pytest\n@pytest.mark.skip\ndef test_foo():\n    pass\npytest.mark.xfail\n")
    res = gateguard.inspect_diff(guards.inspect(repo), since)
    assert {"file": "test_b.py", "kind": "skip-added", "lines": [2, 5]} in res


def test_ignore_and_deselect_added(repo):
    test_file = repo / "tests" / "test_c.py"
    test_file.parent.mkdir()
    test_file.write_text("pass\n")
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-m", "add test")
    since = guards.head(guards.inspect(repo))

    test_file.write_text("pass\n# --ignore=foo --deselect=bar\n")
    res = gateguard.inspect_diff(guards.inspect(repo), since)
    assert {"file": "tests/test_c.py", "kind": "skip-added", "lines": [2]} in res


def test_test_function_deleted(repo):
    test_file = repo / "test_d.py"
    test_file.write_text("def test_something():\n    pass\n")
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-m", "add test")
    since = guards.head(guards.inspect(repo))

    test_file.write_text("\n")
    res = gateguard.inspect_diff(guards.inspect(repo), since)
    assert res == [{"file": "test_d.py", "kind": "test-deleted", "lines": [1]}]


def test_justfile_change_flagged(repo):
    f = repo / "justfile"
    f.write_text("test:\n  pytest\n")
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-m", "add")
    since = guards.head(guards.inspect(repo))

    f.write_text("test:\n  pytest -q\n")
    res = gateguard.inspect_diff(guards.inspect(repo), since)
    # The output has an added line and a removed line. Either one being flagged is fine, or both (deduped).
    # Since inspect_diff returns a list of dictionaries, let's just check length.
    assert len(res) == 1  # added and removed lines are both flagged
    assert all(r["file"] == "justfile" and r["kind"] == "gate-config-changed" for r in res)


def test_pyproject_ruff_section_flagged(repo):
    f = repo / "pyproject.toml"
    f.write_text("[tool.ruff]\nline-length = 88\n[tool.poetry]\nname = 'a'\n")
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-m", "add")
    since = guards.head(guards.inspect(repo))

    f.write_text("[tool.ruff]\nline-length = 100\n[tool.poetry]\nname = 'a'\n")
    res = gateguard.inspect_diff(guards.inspect(repo), since)
    # line 2 changed
    assert {"file": "pyproject.toml", "kind": "gate-config-changed", "lines": [2]} in res


def test_pyproject_other_section_ignored(repo):
    f = repo / "pyproject.toml"
    f.write_text("[tool.ruff]\nline-length = 88\n[tool.poetry]\nname = 'a'\n")
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-m", "add")
    since = guards.head(guards.inspect(repo))

    f.write_text("[tool.ruff]\nline-length = 88\n[tool.poetry]\nname = 'b'\n")
    res = gateguard.inspect_diff(guards.inspect(repo), since)
    assert res == []


def test_conftest_change_flagged(repo):
    f = repo / "conftest.py"
    f.write_text("def pytest_configure():\n  pass\n")
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-m", "add")
    since = guards.head(guards.inspect(repo))

    f.write_text("def pytest_configure():\n  pass\n  pass\n")
    res = gateguard.inspect_diff(guards.inspect(repo), since)
    assert {"file": "conftest.py", "kind": "gate-config-changed", "lines": [3]} in res


def test_uncommitted_and_committed_both_seen(repo):
    f = repo / "test_e.py"
    f.write_text("def test_1():\n  assert 1\n")
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-m", "add")
    since = guards.head(guards.inspect(repo))

    # committed change
    f.write_text("def test_1():\n  pass\n")
    run_git(repo, "commit", "-am", "mod1")

    # uncommitted track
    f.write_text("def test_1():\n  pass\n  pass\n")

    # untracked
    f2 = repo / "test_f.py"
    f2.write_text("def test_2():\n  skip\n")

    res = gateguard.inspect_diff(guards.inspect(repo), since)
    assert {"file": "test_e.py", "kind": "assert-removed", "lines": [2]} in res
    assert {"file": "test_f.py", "kind": "skip-added", "lines": [2]} in res


def test_nested_tests_dir_assert_removed(repo):
    test_file = repo / "a" / "b" / "tests" / "x.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("def test_foo():\n    assert True\n")
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-m", "add test")
    since = guards.head(guards.inspect(repo))

    test_file.write_text("def test_foo():\n    pass\n")
    res = gateguard.inspect_diff(guards.inspect(repo), since)
    assert {"file": "a/b/tests/x.py", "kind": "assert-removed", "lines": [2]} in res


def test_test_file_name_matched_anywhere(repo):
    test_file = repo / "plugins" / "token-audit" / "tests" / "test_token_audit.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("def test_foo():\n    assert True\n")
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-m", "add test")
    since = guards.head(guards.inspect(repo))

    test_file.write_text("def test_foo():\n    pass\n")
    res = gateguard.inspect_diff(guards.inspect(repo), since)
    assert {"file": "plugins/token-audit/tests/test_token_audit.py", "kind": "assert-removed", "lines": [2]} in res


def test_findings_grouped_per_file_and_kind(repo):
    test_file = repo / "test_grouping.py"
    test_file.write_text("def test_foo():\n    assert 1\n    assert 2\n")
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-m", "add test")
    since = guards.head(guards.inspect(repo))

    test_file.write_text("def test_foo():\n    pass\n    pass\n")
    res = gateguard.inspect_diff(guards.inspect(repo), since)
    assert res == [{"file": "test_grouping.py", "kind": "assert-removed", "lines": [2, 3]}]
