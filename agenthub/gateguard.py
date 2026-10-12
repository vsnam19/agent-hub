import ast
import re
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from agenthub import guards

# Repo-relative `.agent-hub/` files that set how the gates run (repo gates, standing orders).
AGENT_HUB_GATE_CONFIG = {".agent-hub/config.toml", ".agent-hub/standing-orders.md"}

# Statements that end their statement list unconditionally (see _dead_ids).
_TERMINATORS = (ast.Return, ast.Raise, ast.Continue, ast.Break)


@dataclass
class Change:
    path: str
    kind: str
    line_num: int
    content: str


@dataclass
class TestDef:
    start: int
    end: int
    dump: str  # ast.dump of the whole function, decorators included: an identical test is never judged
    # (place, ast.dump, nested) of each assertion; see _assertion_places
    asserts: list[tuple[tuple[str, ...], str, bool]]
    kept: list[tuple]  # what the test keeps: ("direct", dump), ("placed", place, dump) or ("nested", place, dump)


def _parse_diff(diff_text: str) -> list[Change]:
    changes = []
    current_file = None
    old_line = 0
    new_line = 0
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:]
        elif line.startswith("--- a/") and current_file is None:
            pass
        elif line.startswith("--- a/"):
            current_file = line[6:]
        elif line.startswith("+++ /dev/null"):
            pass
        elif line.startswith("--- /dev/null"):
            pass
        elif line.startswith("@@ "):
            m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            if m:
                old_line = int(m.group(1))
                new_line = int(m.group(2))
        elif line.startswith("+") and not line.startswith("+++"):
            if current_file:
                changes.append(Change(current_file, "added", new_line, line[1:]))
                new_line += 1
        elif line.startswith("-") and not line.startswith("---"):
            if current_file:
                changes.append(Change(current_file, "removed", old_line, line[1:]))
                old_line += 1
    return changes


def _is_test_file(path_str: str) -> bool:
    p = Path(path_str)
    if p.name.startswith("test_") and p.name.endswith(".py"):
        return True
    if p.name.endswith("_test.py"):
        return True
    if p.name == "conftest.py":
        return True
    if "tests" in p.parts:
        return True
    return False


def _is_gate_config(path_str: str) -> bool:
    if path_str in AGENT_HUB_GATE_CONFIG:
        return True
    name = Path(path_str).name
    return name in {
        "justfile",
        "Makefile",
        "pyproject.toml",
        "setup.cfg",
        ".ruff.toml",
        "ruff.toml",
        "pytest.ini",
        "tox.ini",
        "conftest.py",
    }


def _is_assertion(node: ast.AST) -> bool:
    if isinstance(node, ast.Assert):
        return True
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute):
            name = func.attr
            dotted = ast.unparse(func)
        elif isinstance(func, ast.Name):
            name = func.id
            dotted = func.id
        else:
            return False
        return name.startswith("assert") or dotted.endswith(("raises", "warns", "fail"))
    return False


def _index_tests(body: list[ast.stmt], prefix: str, index: dict[str, ast.AST]) -> None:
    """Adds the tests of `body` to `index` under their dotted path of enclosing class and function names. Classes,
    non-test functions and module-level `if` blocks are entered; a test's own body is not."""
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test_"):
                index[prefix + node.name] = node
            else:
                _index_tests(node.body, f"{prefix}{node.name}.", index)
        elif isinstance(node, ast.ClassDef):
            _index_tests(node.body, f"{prefix}{node.name}.", index)
        elif isinstance(node, ast.If):
            _index_tests(node.body + node.orelse, prefix, index)


def _test_index(source: str) -> dict[str, TestDef] | None:
    """Each test in `source` by identity: its dotted path of enclosing class and function names (see _index_tests),
    e.g. `test_name` at module level, `Class.Inner.test_name` nested in classes. None when the source does not parse."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    index: dict[str, ast.AST] = {}
    _index_tests(tree.body, "", index)
    tests = {}
    for ident, fn in index.items():
        statements = _statement_ids(fn)
        asserts = [(place, _assertion_key(node), id(node) not in statements) for place, node in _assertion_places(fn)]
        tests[ident] = TestDef(
            fn.lineno, fn.end_lineno, ast.dump(fn, include_attributes=False), asserts, _kept_tokens(fn)
        )
    return tests


def _weakened(base: TestDef, new: TestDef) -> bool:
    """A base assertion the new test does not keep. A statement assertion is kept by the same assertion in the same
    place (same enclosing statement kinds), or as a direct statement of the new test anywhere (see _kept_tokens). A
    nested assertion is kept only by the identical nested one in the same place, never by a statement. Containment in
    a larger expression (`x == 1 or True`, `not (x == 1)`) keeps nothing. An identical test is never weakened."""
    if base.dump == new.dump:
        return False
    pool = Counter(new.kept)
    unmatched = []
    for place, key, nested in base.asserts:
        token = ("nested", place, key) if nested else ("placed", place, key)
        if pool[token]:
            pool[token] -= 1
        else:
            unmatched.append((nested, key))
    for nested, key in unmatched:
        if nested or not pool[("direct", key)]:
            return True
        pool[("direct", key)] -= 1
    return False


def _is_raises(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and ast.unparse(node.func).endswith(("raises", "warns"))


def _assertion_key(node: ast.AST) -> str:
    """The key of an assertion: an `assert`'s test, or the whole call such as `self.assertEqual(...)`."""
    return ast.dump(node.test) if isinstance(node, ast.Assert) else ast.dump(node)


def _assertion_places(fn: ast.AST) -> list[tuple[tuple[str, ...], ast.AST]]:
    """Each assertion in the function with its place: the enclosing compound statements, each as `Kind.field`
    (`Try.body`, `If.body`). Statements directly in the function body add no label."""
    places = []
    dead = _dead_ids(fn)

    def visit(node: ast.AST, place: tuple[str, ...]) -> None:
        for field, value in ast.iter_fields(node):
            for item in value if isinstance(value, list) else [value]:
                if not isinstance(item, ast.AST):
                    continue
                child = place
                if node is not fn and isinstance(item, (ast.stmt, ast.excepthandler)):
                    child = (*place, _place_label(node, field))
                if _is_assertion(item) and id(item) not in dead:
                    places.append((child, item))
                else:
                    visit(item, child)

    visit(fn, ())
    return places


def _kept_tokens(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[tuple]:
    """What a test keeps, one token per form of each assertion. A direct assertion keeps its form wherever it moved
    (`("direct", dump)`); any other keeps it only in the same place (`("placed", place, dump)`). Direct: a top-level
    `assert` or assertion call and a `with` header call. A `raises/warns` body is a place of its own, keyed by its
    with-items, so an assertion there keeps only in a block with the same exception. An assert's forms are its test
    and, for an `and`, each direct operand. An assertion nested in another expression keeps only the identical nested
    assertion in the same place (`("nested", place, dump)`), never a statement."""
    direct = _direct_ids(fn.body)
    statements = _statement_ids(fn)
    tokens = []
    for place, node in _assertion_places(fn):
        if id(node) not in statements:
            tokens.append(("nested", place, _assertion_key(node)))
            continue
        forms = [_assertion_key(node)]
        if isinstance(node, ast.Assert) and isinstance(node.test, ast.BoolOp) and isinstance(node.test.op, ast.And):
            forms.extend(ast.dump(v) for v in node.test.values)
        for form in forms:
            tokens.append(("direct", form) if id(node) in direct else ("placed", place, form))
    return tokens


def _statement_ids(fn: ast.AST) -> set[int]:
    """Ids of the assertions that are statements: an `assert`, an assertion call that is an expression statement,
    or an assertion heading a `with`. Any other expression that contains an assertion does not make it a statement."""
    ids = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Assert):
            ids.add(id(node))
        elif isinstance(node, ast.Expr):
            ids.add(id(node.value))
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            ids.update(id(item.context_expr) for item in node.items)
    return ids


def _direct_ids(body: list[ast.stmt]) -> set[int]:
    """Ids of the assertion nodes a test body states directly (see _kept_tokens)."""
    ids = set()
    for stmt in body:
        if isinstance(stmt, ast.Assert):
            ids.add(id(stmt))
        elif isinstance(stmt, ast.Expr) and _is_assertion(stmt.value):
            ids.add(id(stmt.value))
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            for item in stmt.items:
                if _is_assertion(item.context_expr):
                    ids.add(id(item.context_expr))
    return ids


def _dead_ids(fn: ast.AST) -> set[int]:
    """Ids of the nodes in a statement list that follow an unconditional `return`, `raise`, `continue` or `break` in
    that same list: they never run, so an assertion there keeps nothing."""
    dead = set()
    for node in ast.walk(fn):
        for _, value in ast.iter_fields(node):
            if not isinstance(value, list):
                continue
            end = next((i for i, s in enumerate(value) if isinstance(s, _TERMINATORS)), None)
            if end is not None:
                for stmt in value[end + 1 :]:
                    dead.update(id(inner) for inner in ast.walk(stmt))
    return dead


def _place_label(node: ast.AST, field: str) -> str:
    """`Kind.field`; a `raises/warns` body adds its with-items, so the exception is part of the place."""
    label = f"{type(node).__name__}.{field}"
    if isinstance(node, (ast.With, ast.AsyncWith)) and field == "body":
        raises = [ast.dump(i) for i in node.items if _is_raises(i.context_expr)]
        if raises:
            label += f"[{' '.join(raises)}]"
    return label


def _compare_tests(path: str, base: dict[str, TestDef], new: dict[str, TestDef], removed: list[Change]) -> list[dict]:
    """assert-removed and test-deleted for a Python test file, judged per test identity: a test that is weakened, or
    whose identity disappears, is flagged. A removed assert outside every base test is flagged as before."""

    def owner(line: int) -> str | None:
        return next((ident for ident, t in base.items() if t.start <= line <= t.end), None)

    weakened = {ident for ident, t in base.items() if ident in new and _weakened(t, new[ident])}
    findings = []
    owned = set()
    for c in removed:
        ident = owner(c.line_num)
        owned.add(ident)
        if (ident is None and "assert" in c.content) or ident in weakened:
            findings.append({"file": path, "kind": "assert-removed", "line": c.line_num})
    for ident in weakened - owned:
        findings.append({"file": path, "kind": "assert-removed", "line": base[ident].start})
    for ident, t in base.items():
        if ident not in new:
            findings.append({"file": path, "kind": "test-deleted", "line": t.start})
    return findings


def _assertion_free_test_lines(source: str, added_lines: set[int]) -> list[int]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test_") or node.lineno not in added_lines:
            continue
        if not any(_is_assertion(inner) for stmt in node.body for inner in ast.walk(stmt)):
            lines.append(node.lineno)
    return lines


def _test_def(content: str) -> str | None:
    """The test function a diff line defines; a commented-out or quoted `def test_x` defines none."""
    m = re.match(r"\s*(?:async\s+)?def\s+(test_\w*)", content)
    return m.group(1) if m else None


def _get_sections(content: str) -> dict[int, str]:
    mapping = {}
    current_section = None
    for i, line in enumerate(content.splitlines(), start=1):
        line_s = line.strip()
        if line_s.startswith("[") and line_s.endswith("]"):
            current_section = line_s[1:-1].strip()
        mapping[i] = current_section
    return mapping


def _git_show(repo: guards.Repo, since: str, path: str) -> str | None:
    res = subprocess.run(
        guards._git_argv("show", f"{since}:{path}"),
        cwd=repo.root,
        capture_output=True,
        text=True,
        env=guards._git_env(),
    )
    return res.stdout if res.returncode == 0 else None


def _read_or_empty(path: Path) -> str | None:
    """The file's text; empty when it is gone (a deleted test file), None when it cannot be read as text."""
    if not path.is_file():
        return ""
    try:
        return path.read_text()
    except UnicodeDecodeError:
        return None


def inspect_diff(repo: guards.Repo, since: str) -> list[dict]:
    # 1. Tracked changes
    diff_res = subprocess.run(
        # --no-renames: a renamed file's removed lines stay under their old path, so its base can be read and compared.
        guards._git_argv("diff", "-U0", "--no-ext-diff", "--no-textconv", "--no-renames", since),
        cwd=repo.root,
        capture_output=True,
        text=True,
        env=guards._git_env(),
    )
    changes = _parse_diff(diff_res.stdout)

    # 2. Untracked changes
    untracked_res = subprocess.run(
        guards._git_argv("ls-files", "--others", "--exclude-standard"),
        cwd=repo.root,
        capture_output=True,
        text=True,
        env=guards._git_env(),
    )
    for path in untracked_res.stdout.strip().splitlines():
        file_path = repo.root / path
        if file_path.is_file():
            try:
                content = file_path.read_text()
                for i, line in enumerate(content.splitlines(), start=1):
                    changes.append(Change(path, "added", i, line))
            except UnicodeDecodeError:
                pass

    findings = []

    # Python test files that parse on both sides are judged by test identity below; the others keep the line rules.
    compared: dict[str, tuple[dict[str, TestDef], dict[str, TestDef]]] = {}
    for path in {c.path for c in changes if c.path.endswith(".py") and _is_test_file(c.path)}:
        # An unreadable base (git show fails) is not compared: its removed lines go through the line rules.
        base_src = _git_show(repo, since, path)
        base = None if base_src is None else _test_index(base_src)
        new = _test_index(_read_or_empty(repo.root / path))
        if base is not None and new is not None:
            compared[path] = (base, new)
    # Test names added per file: a removed test whose name is re-added (e.g. a signature change) was not deleted.
    # Known limit: this is per file, so a test moved to another file is still reported as deleted.
    added_test_names: dict[str, set[str]] = {}
    for c in changes:
        if c.kind == "added" and _is_test_file(c.path):
            name = _test_def(c.content)
            if name:
                added_test_names.setdefault(c.path, set()).add(name)

    # Process changes
    for c in changes:
        if _is_test_file(c.path):
            if c.path not in compared and c.kind == "removed" and "assert" in c.content:
                findings.append({"file": c.path, "kind": "assert-removed", "line": c.line_num})
            if c.kind == "added" and re.search(r"\b(?:skip|xfail)\b|--ignore|--deselect", c.content):
                findings.append({"file": c.path, "kind": "skip-added", "line": c.line_num})
            name = _test_def(c.content) if c.kind == "removed" else None
            # A compared file's removed def that an index entry owns is judged by identity (_compare_tests); any other
            # removed def falls back to the line rule.
            unowned = c.path not in compared or not any(
                t.start <= c.line_num <= t.end for t in compared[c.path][0].values()
            )
            if unowned and name and name not in added_test_names.get(c.path, set()):
                findings.append({"file": c.path, "kind": "test-deleted", "line": c.line_num})

        if _is_gate_config(c.path):
            if c.path == "pyproject.toml":
                pass  # Handled separately below
            else:
                findings.append({"file": c.path, "kind": "gate-config-changed", "line": c.line_num})

    for path, (base, new) in compared.items():
        removed = [c for c in changes if c.path == path and c.kind == "removed"]
        findings.extend(_compare_tests(path, base, new, removed))

    # New test functions (def line added in this diff) whose body asserts nothing
    added_by_file: dict[str, set[int]] = {}
    for c in changes:
        if c.kind == "added" and _is_test_file(c.path) and c.path.endswith(".py"):
            added_by_file.setdefault(c.path, set()).add(c.line_num)
    for path, added_lines in added_by_file.items():
        file_path = repo.root / path
        if not file_path.is_file():
            continue
        try:
            source = file_path.read_text()
        except UnicodeDecodeError:
            continue
        for def_line in _assertion_free_test_lines(source, added_lines):
            findings.append({"file": path, "kind": "assertion-free-test", "line": def_line})

    # Process pyproject.toml separately
    pyproject_changes = [c for c in changes if c.path == "pyproject.toml"]
    if pyproject_changes:
        added_changes = [c for c in pyproject_changes if c.kind == "added"]
        removed_changes = [c for c in pyproject_changes if c.kind == "removed"]

        file_path = repo.root / "pyproject.toml"
        if added_changes and file_path.exists():
            content = file_path.read_text()
            mapping = _get_sections(content)
            for c in added_changes:
                sec = mapping.get(c.line_num)
                if sec and (sec.startswith("tool.ruff") or sec.startswith("tool.pytest")):
                    findings.append({"file": "pyproject.toml", "kind": "gate-config-changed", "line": c.line_num})

        if removed_changes:
            res = subprocess.run(
                guards._git_argv("show", f"{since}:pyproject.toml"),
                cwd=repo.root,
                capture_output=True,
                text=True,
                env=guards._git_env(),
            )
            if res.returncode == 0:
                mapping = _get_sections(res.stdout)
                for c in removed_changes:
                    sec = mapping.get(c.line_num)
                    if sec and (sec.startswith("tool.ruff") or sec.startswith("tool.pytest")):
                        findings.append({"file": "pyproject.toml", "kind": "gate-config-changed", "line": c.line_num})

    # Group findings: {"file", "kind", "lines": [sorted line numbers]}
    grouped = {}
    for f in findings:
        k = (f["file"], f["kind"])
        if k not in grouped:
            grouped[k] = set()
        grouped[k].add(f["line"])

    unique_findings = []
    for (file, kind), lines in grouped.items():
        unique_findings.append({"file": file, "kind": kind, "lines": sorted(list(lines))})

    return unique_findings
