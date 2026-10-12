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


def test_signature_change_is_not_test_deleted(repo):
    test_file = repo / "test_e.py"
    test_file.write_text("def test_x(a):\n    assert a\n")
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-m", "add test")
    since = guards.head(guards.inspect(repo))

    test_file.write_text("def test_x(a, b):\n    assert a\n")
    res = gateguard.inspect_diff(guards.inspect(repo), since)
    assert res == []


def test_renamed_away_test_is_test_deleted(repo):
    test_file = repo / "test_f.py"
    test_file.write_text("def test_old():\n    assert 1\n")
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-m", "add test")
    since = guards.head(guards.inspect(repo))

    test_file.write_text("def test_new():\n    assert 1\n")
    res = gateguard.inspect_diff(guards.inspect(repo), since)
    assert res == [{"file": "test_f.py", "kind": "test-deleted", "lines": [1]}]


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


def test_assertion_free_test_flagged(repo):
    since = guards.head(guards.inspect(repo))

    test_file = repo / "test_g.py"
    test_file.write_text("def test_nothing():\n    pass\n")
    res = gateguard.inspect_diff(guards.inspect(repo), since)
    assert {"file": "test_g.py", "kind": "assertion-free-test", "lines": [1]} in res


def test_pytest_raises_counts_as_assertion(repo):
    since = guards.head(guards.inspect(repo))

    test_file = repo / "test_h.py"
    test_file.write_text(
        "import pytest\n\n\ndef test_raises():\n    with pytest.raises(ValueError):\n        int('a')\n"
    )
    res = gateguard.inspect_diff(guards.inspect(repo), since)
    assert not [r for r in res if r["kind"] == "assertion-free-test"]


def test_unittest_assert_method_counts(repo):
    since = guards.head(guards.inspect(repo))

    test_file = repo / "test_i.py"
    test_file.write_text(
        "import unittest\n\n\n"
        "class TestA(unittest.TestCase):\n    def test_equal(self):\n        self.assertEqual(1, 1)\n"
    )
    res = gateguard.inspect_diff(guards.inspect(repo), since)
    assert not [r for r in res if r["kind"] == "assertion-free-test"]


def test_existing_test_not_flagged(repo):
    test_file = repo / "test_j.py"
    test_file.write_text("def test_old():\n    pass\n")
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-m", "add test")
    since = guards.head(guards.inspect(repo))

    test_file.write_text("def test_old():\n    x = 1\n")
    res = gateguard.inspect_diff(guards.inspect(repo), since)
    assert not [r for r in res if r["kind"] == "assertion-free-test"]


def test_commented_def_test_ignored(repo):
    (repo / "test_c.py").write_text("# def test_old():\n    pass\n")
    (repo / "test_d.py").write_text("def test_x():\n    assert 1\n")
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-m", "add")
    since = guards.head(guards.inspect(repo))

    # removing a commented-out def is not a removal of a test
    (repo / "test_c.py").write_text("\n")
    # a commented-out def re-added does not count as re-adding test_x
    (repo / "test_d.py").write_text("# def test_x():\n    assert 1\n")
    res = gateguard.inspect_diff(guards.inspect(repo), since)
    assert res == [{"file": "test_d.py", "kind": "test-deleted", "lines": [1]}]


def test_agent_hub_config_is_gate_config(repo):
    cfg = repo / ".agent-hub" / "config.toml"
    cfg.parent.mkdir()
    cfg.write_text('gates = ["true"]\n')
    orders = repo / ".agent-hub" / "standing-orders.md"
    orders.write_text("1. Be kind.\n")
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-m", "add")
    since = guards.head(guards.inspect(repo))

    cfg.write_text('gates = ["false"]\n')
    orders.write_text("1. Be unkind.\n")
    res = gateguard.inspect_diff(guards.inspect(repo), since)
    assert {"file": ".agent-hub/config.toml", "kind": "gate-config-changed", "lines": [1]} in res
    assert {"file": ".agent-hub/standing-orders.md", "kind": "gate-config-changed", "lines": [1]} in res


def test_assert_replaced_by_stronger_not_flagged(repo):
    test_file = repo / "test_g.py"
    test_file.write_text("def test_a():\n    assert x == 1\n")
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-m", "add test")
    since = guards.head(guards.inspect(repo))

    test_file.write_text("def test_a():\n    assert x == 1 and y == 2\n")
    res = gateguard.inspect_diff(guards.inspect(repo), since)
    assert res == []


def test_assert_count_drop_flagged(repo):
    test_file = repo / "test_h.py"
    test_file.write_text("def test_a():\n    assert 1\n    assert 2\n")
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-m", "add test")
    since = guards.head(guards.inspect(repo))

    test_file.write_text("def test_a():\n    assert 1\n")
    res = gateguard.inspect_diff(guards.inspect(repo), since)
    assert res == [{"file": "test_h.py", "kind": "assert-removed", "lines": [3]}]


def test_same_name_other_class_deleted_flagged(repo):
    test_file = repo / "test_i.py"
    test_file.write_text(
        "class TestA:\n    def test_x(self):\n        assert 1\nclass TestB:\n    def test_x(self):\n        assert 1\n"
    )
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-m", "add tests")
    since = guards.head(guards.inspect(repo))

    test_file.write_text("class TestB:\n    def test_x(self):\n        assert 1\n")
    res = gateguard.inspect_diff(guards.inspect(repo), since)
    assert res == [{"file": "test_i.py", "kind": "test-deleted", "lines": [2]}]


def test_unparsable_test_file_falls_back_to_line_rules(repo):
    test_file = repo / "test_j.py"
    test_file.write_text("def test_a():\n    assert 1\n")
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-m", "add test")
    since = guards.head(guards.inspect(repo))

    test_file.write_text("def test_a(:\n    pass\n")
    res = gateguard.inspect_diff(guards.inspect(repo), since)
    assert {"file": "test_j.py", "kind": "assert-removed", "lines": [2]} in res
