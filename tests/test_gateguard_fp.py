import subprocess

from agenthub import gateguard, guards


def run_git(repo_path, *args):
    subprocess.run(["git", *args], cwd=repo_path, check=True)


def test_fmt_skip_not_flagged(repo):
    test_file = repo / "test_fp.py"
    test_file.write_text("def test_foo():\n    assert (\n        1 == 1\n    )\n")
    run_git(repo, "add", "test_fp.py")
    run_git(repo, "commit", "-m", "add test")
    since = guards.head(guards.inspect(repo))

    # Adding lines with fmt skip/off/on should not trigger skip-added
    test_file.write_text(
        "def test_foo():\n    assert (\n        1 == 1\n    )  # fmt: skip\n    # fmt: off\n    x = 2\n    # fmt: on\n"
    )
    res = gateguard.inspect_diff(guards.inspect(repo), since)
    skip_findings = [f for f in res if f["kind"] == "skip-added"]
    assert skip_findings == []

    # But adding @pytest.mark.skip is still flagged
    test_file.write_text(
        "import pytest\n@pytest.mark.skip\ndef test_foo():\n    assert (\n        1 == 1\n    )  # fmt: skip\n"
    )
    res2 = gateguard.inspect_diff(guards.inspect(repo), since)
    skip_findings2 = [f for f in res2 if f["kind"] == "skip-added"]
    assert len(skip_findings2) == 1
    assert skip_findings2[0]["file"] == "test_fp.py"
    assert skip_findings2[0]["lines"] == [2]


def _weaken_test(repo, before, after):
    test_file = repo / "test_k.py"
    test_file.write_text(before)
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-m", "add test")
    since = guards.head(guards.inspect(repo))

    test_file.write_text(after)
    return gateguard.inspect_diff(guards.inspect(repo), since)


def test_grown_expected_set_not_weakening(repo):
    # Set literal grown: not weakened
    res = _weaken_test(
        repo,
        "def test_a():\n    assert set(x) == {'a', 'b'}\n",
        "def test_a():\n    assert set(x) == {'a', 'b', 'c'}\n",
    )
    assert res == []

    # Set literal shrunk: weakened
    res = _weaken_test(
        repo,
        "def test_a():\n    assert set(x) == {'a', 'b', 'c'}\n",
        "def test_a():\n    assert set(x) == {'a', 'b'}\n",
    )
    assert res == [{"file": "test_k.py", "kind": "assert-removed", "lines": [2]}]

    # List literal grown: not weakened
    res = _weaken_test(
        repo,
        "def test_a():\n    assert x == [1, 2]\n",
        "def test_a():\n    assert x == [1, 2, 3]\n",
    )
    assert res == []

    # List literal shrunk: weakened
    res = _weaken_test(
        repo,
        "def test_a():\n    assert x == [1, 2, 3]\n",
        "def test_a():\n    assert x == [1, 2]\n",
    )
    assert res == [{"file": "test_k.py", "kind": "assert-removed", "lines": [2]}]

    # Tuple literal grown: not weakened
    res = _weaken_test(
        repo,
        "def test_a():\n    assert x == (1, 2)\n",
        "def test_a():\n    assert x == (1, 2, 3)\n",
    )
    assert res == []

    # Tuple literal shrunk: weakened
    res = _weaken_test(
        repo,
        "def test_a():\n    assert x == (1, 2, 3)\n",
        "def test_a():\n    assert x == (1, 2)\n",
    )
    assert res == [{"file": "test_k.py", "kind": "assert-removed", "lines": [2]}]

    # Dict literal grown: weakened (dicts out of scope)
    res = _weaken_test(
        repo,
        "def test_a():\n    assert x == {'a': 1}\n",
        "def test_a():\n    assert x == {'a': 1, 'b': 2}\n",
    )
    assert res == [{"file": "test_k.py", "kind": "assert-removed", "lines": [2]}]

    # Dict literal shrunk: weakened
    res = _weaken_test(
        repo,
        "def test_a():\n    assert x == {'a': 1, 'b': 2}\n",
        "def test_a():\n    assert x == {'a': 1}\n",
    )
    assert res == [{"file": "test_k.py", "kind": "assert-removed", "lines": [2]}]


def test_weakened_direct_superset():
    base = gateguard._test_index(b"def test_foo():\n    assert set(x) == {'a', 'b'}\n")["test_foo"]
    new_grown = gateguard._test_index(b"def test_foo():\n    assert set(x) == {'a', 'b', 'c'}\n")["test_foo"]
    new_shrunk = gateguard._test_index(b"def test_foo():\n    assert set(x) == {'a'}\n")["test_foo"]
    assert not gateguard._weakened(base, new_grown)
    assert gateguard._weakened(base, new_shrunk)


def test_weakened_edge_cases():
    base = gateguard._test_index(b"def test_foo():\n    assert set(x) == {'a', 'b'}\n")["test_foo"]

    # Different literal type (set vs list)
    new_type = gateguard._test_index(b"def test_foo():\n    assert set(x) == ['a', 'b', 'c']\n")["test_foo"]
    assert gateguard._weakened(base, new_type)

    # Different left side
    new_left = gateguard._test_index(b"def test_foo():\n    assert set(y) == {'a', 'b', 'c'}\n")["test_foo"]
    assert gateguard._weakened(base, new_left)

    # Different place (under if)
    new_place = gateguard._test_index(b"def test_foo():\n    if False:\n        assert set(x) == {'a', 'b', 'c'}\n")[
        "test_foo"
    ]
    assert gateguard._weakened(base, new_place)

    # Non-container literal
    base_int = gateguard._test_index(b"def test_foo():\n    assert x == 5\n")["test_foo"]
    new_int = gateguard._test_index(b"def test_foo():\n    assert x == 6\n")["test_foo"]
    assert gateguard._weakened(base_int, new_int)

    # Different operator (!=)
    new_op = gateguard._test_index(b"def test_foo():\n    assert set(x) != {'a', 'b', 'c'}\n")["test_foo"]
    assert gateguard._weakened(base, new_op)


def test_and_chain_grown_literal_still_weakening(repo):
    res = _weaken_test(
        repo,
        "def test_a():\n    assert result == [1] and status == 0\n",
        "def test_a():\n    assert result == [1, 2]\n",
    )
    assert res == [{"file": "test_k.py", "kind": "assert-removed", "lines": [2]}]

    base = gateguard._test_index(b"def test_a():\n    assert result == [1] and status == 0\n")["test_a"]
    new = gateguard._test_index(b"def test_a():\n    assert result == [1, 2]\n")["test_a"]
    assert gateguard._weakened(base, new)


def test_empty_expected_literal_grown_is_weakening(repo):
    res = _weaken_test(
        repo,
        "def test_a():\n    assert errors == []\n",
        "def test_a():\n    assert errors == ['boom']\n",
    )
    assert res == [{"file": "test_k.py", "kind": "assert-removed", "lines": [2]}]

    base = gateguard._test_index(b"def test_a():\n    assert errors == []\n")["test_a"]
    new = gateguard._test_index(b"def test_a():\n    assert errors == ['boom']\n")["test_a"]
    assert gateguard._weakened(base, new)


def test_superset_does_not_steal_token_from_exact_direct_match():
    base_src = "def test_a():\n    if cond:\n        assert x == [1, 2]\n    assert x == [1]\n"
    new_src = "def test_a():\n    assert x == [1, 2]\n    assert x == [1, 4]\n"
    base = gateguard._test_index(base_src.encode("utf-8"))["test_a"]
    new = gateguard._test_index(new_src.encode("utf-8"))["test_a"]
    assert not gateguard._weakened(base, new)


def test_starred_growth_is_weakening(repo):
    res = _weaken_test(
        repo,
        "def test_a():\n    assert x == [1, 2]\n",
        "def test_a():\n    assert x == [1, 2, *x[2:]]\n",
    )
    assert res == [{"file": "test_k.py", "kind": "assert-removed", "lines": [2]}]

    base = gateguard._test_index(b"def test_a():\n    assert x == [1, 2]\n")["test_a"]
    new = gateguard._test_index(b"def test_a():\n    assert x == [1, 2, *x[2:]]\n")["test_a"]
    assert gateguard._weakened(base, new)

    res_set = _weaken_test(
        repo,
        "def test_a():\n    assert set(x) == {'a', 'b'}\n",
        "def test_a():\n    assert set(x) == {'a', 'b', *set(x)}\n",
    )
    assert res_set == [{"file": "test_k.py", "kind": "assert-removed", "lines": [2]}]

    base_set = gateguard._test_index(b"def test_a():\n    assert set(x) == {'a', 'b'}\n")["test_a"]
    new_set = gateguard._test_index(b"def test_a():\n    assert set(x) == {'a', 'b', *set(x)}\n")["test_a"]
    assert gateguard._weakened(base_set, new_set)


def test_dict_unpack_growth_is_weakening(repo):
    res = _weaken_test(
        repo,
        "def test_a():\n    assert x == {'a': 1}\n",
        "def test_a():\n    assert x == {'a': 1, **x}\n",
    )
    assert res == [{"file": "test_k.py", "kind": "assert-removed", "lines": [2]}]

    base = gateguard._test_index(b"def test_a():\n    assert x == {'a': 1}\n")["test_a"]
    new = gateguard._test_index(b"def test_a():\n    assert x == {'a': 1, **x}\n")["test_a"]
    assert gateguard._weakened(base, new)


def test_duplicate_dict_key_growth_is_weakening(repo):
    res = _weaken_test(
        repo,
        "def test_a():\n    assert x == {'status': 'ok'}\n",
        "def test_a():\n    assert x == {'status': 'ok', 'status': 'error'}\n",
    )
    assert res == [{"file": "test_k.py", "kind": "assert-removed", "lines": [2]}]

    base = gateguard._test_index(b"def test_a():\n    assert x == {'status': 'ok'}\n")["test_a"]
    new = gateguard._test_index(b"def test_a():\n    assert x == {'status': 'ok', 'status': 'error'}\n")["test_a"]
    assert gateguard._weakened(base, new)


def test_dict_growth_is_weakening(repo):
    res = _weaken_test(
        repo,
        "def test_a():\n    assert x == {'a': 1}\n",
        "def test_a():\n    assert x == {'a': 1, 'b': 2}\n",
    )
    assert res == [{"file": "test_k.py", "kind": "assert-removed", "lines": [2]}]

    base = gateguard._test_index(b"def test_a():\n    assert x == {'a': 1}\n")["test_a"]
    new = gateguard._test_index(b"def test_a():\n    assert x == {'a': 1, 'b': 2}\n")["test_a"]
    assert gateguard._weakened(base, new)


def test_non_constant_element_growth_is_weakening(repo):
    res = _weaken_test(
        repo,
        "def test_a():\n    assert x == [1]\n",
        "def test_a():\n    assert x == [1, x]\n",
    )
    assert res == [{"file": "test_k.py", "kind": "assert-removed", "lines": [2]}]

    base = gateguard._test_index(b"def test_a():\n    assert x == [1]\n")["test_a"]
    new = gateguard._test_index(b"def test_a():\n    assert x == [1, x]\n")["test_a"]
    assert gateguard._weakened(base, new)


def test_non_distinct_base_constant_growth_is_weakening(repo):
    res = _weaken_test(
        repo,
        "def test_a():\n    assert x == [1, 1]\n",
        "def test_a():\n    assert x == [1, 1, 2]\n",
    )
    assert res == [{"file": "test_k.py", "kind": "assert-removed", "lines": [2]}]

    base = gateguard._test_index(b"def test_a():\n    assert x == [1, 1]\n")["test_a"]
    new = gateguard._test_index(b"def test_a():\n    assert x == [1, 1, 2]\n")["test_a"]
    assert gateguard._weakened(base, new)
