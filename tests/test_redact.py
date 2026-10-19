import ast
import sys
from pathlib import Path


def test_skillmine_redact_is_the_leaf_function():
    from agenthub import redact as redact_module
    from agenthub import skillmine

    assert skillmine.redact is redact_module.redact
    assert skillmine.HOME_PATH is redact_module.HOME_PATH


def test_redact_argv_token_flag_forms():
    from agenthub.redact import redact_argv

    flags = ["--token", "--password", "--passwd", "--secret", "--api-key"]
    for flag in flags:
        assert redact_argv(["tool", flag, "secret_val"]) == ["tool", flag, "<redacted>"]
        assert redact_argv(["tool", f"{flag}=secret_val"]) == ["tool", f"{flag}=<redacted>"]

    argv = ["tool", "--token", "tok123", "--api-key=key456", "--verbose", "--password", "pass789"]
    assert redact_argv(argv) == [
        "tool",
        "--token",
        "<redacted>",
        "--api-key=<redacted>",
        "--verbose",
        "--password",
        "<redacted>",
    ]

    assert redact_argv(["tool", "--token"]) == ["tool", "--token"]
    assert redact_argv(["tool", "--token="]) == ["tool", "--token=<redacted>"]


def test_redact_argv_env_prefix():
    from agenthub.redact import redact_argv

    secret_env_keys = [
        "TOKEN",
        "API_KEY",
        "AUTH_SECRET",
        "DB_PASSWORD",
        "APP_PASSWD",
        "SESSION_COOKIE",
        "SIGNATURE",
        "PRIVATE_KEY",
        "apiKey",
        "adminpass",
        "MY_SALT",
    ]
    for key in secret_env_keys:
        assert redact_argv([f"{key}=my_secret_val", "python", "app.py"]) == [
            f"{key}=<redacted>",
            "python",
            "app.py",
        ]

    assert redact_argv(["PORT=8080", "TOKEN=xyz", "DEBUG=1", "python", "main.py"]) == [
        "PORT=8080",
        "TOKEN=<redacted>",
        "DEBUG=1",
        "python",
        "main.py",
    ]

    assert redact_argv(["CONFIG_DIR=/home/user/cfg", "sh", "start.sh"]) == [
        "CONFIG_DIR=~/cfg",
        "sh",
        "start.sh",
    ]


def test_redact_argv_dash_p_only_for_password_programs():
    from agenthub.redact import redact_argv

    pw_progs = ["mysql", "mysqldump", "mysqladmin", "sshpass"]
    for prog in pw_progs:
        assert redact_argv([prog, "-p", "secret123", "mydb"]) == [prog, "-p", "<redacted>", "mydb"]
        assert redact_argv([prog, "-psecret123", "mydb"]) == [prog, "-p<redacted>", "mydb"]
        assert redact_argv([f"/usr/bin/{prog}", "-p", "secret123"]) == [f"/usr/bin/{prog}", "-p", "<redacted>"]
        assert redact_argv([f"/usr/local/bin/{prog}", "-psecret123"]) == [f"/usr/local/bin/{prog}", "-p<redacted>"]

    assert redact_argv(["mysql", "-u", "root", "-p"]) == ["mysql", "-u", "root", "-p"]

    assert redact_argv(["mkdir", "-p", "my/nested/dir"]) == ["mkdir", "-p", "my/nested/dir"]
    assert redact_argv(["mkdir", "-pdir"]) == ["mkdir", "-pdir"]
    assert redact_argv(["/bin/mkdir", "-p", "dir"]) == ["/bin/mkdir", "-p", "dir"]
    assert redact_argv(["grep", "-p", "pattern"]) == ["grep", "-p", "pattern"]


def test_redact_argv_plain_args_unchanged():
    from agenthub.redact import redact_argv

    assert redact_argv([]) == []
    assert redact_argv(["git", "status", "--short"]) == ["git", "status", "--short"]
    assert redact_argv(["echo", "hello", "world"]) == ["echo", "hello", "world"]
    assert redact_argv(["ls", "-la", "/home/alice/project"]) == ["ls", "-la", "~/project"]
    assert redact_argv(["curl", "-H", "Bearer secret-token-value"]) == [
        "curl",
        "-H",
        "Bearer <redacted>",
    ]
    assert redact_argv(["sendmail", "alice@example.com"]) == ["sendmail", "<email>"]


def test_redact_module_imports_stdlib_only():
    redact_path = Path(__file__).parent.parent / "agenthub" / "redact.py"
    assert redact_path.is_file(), f"Expected {redact_path} to exist"

    tree = ast.parse(redact_path.read_text())
    stdlib_names = sys.stdlib_module_names

    imported_modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_modules.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported_modules.append(node.module)

    assert imported_modules, "Expected agenthub/redact.py to have imports"
    for mod in imported_modules:
        root_pkg = mod.split(".")[0]
        assert not mod.startswith("agenthub"), f"agenthub/redact.py must not import from agenthub, found: {mod}"
        assert root_pkg in stdlib_names, f"agenthub/redact.py must only import stdlib, found: {mod}"


def test_redact_argv_secret_named_flags_space_separated():
    from agenthub.redact import redact_argv

    assert redact_argv(["az", "login", "--client-secret", "s3cr3tpw"]) == [
        "az",
        "login",
        "--client-secret",
        "<redacted>",
    ]
    assert redact_argv(["gh", "--auth-token", "abc123xyz"]) == [
        "gh",
        "--auth-token",
        "<redacted>",
    ]
    assert redact_argv(["az", "login", "--client-secret=x"]) == [
        "az",
        "login",
        "--client-secret=<redacted>",
    ]
    assert redact_argv(["tool", "--api-key", "secret-key-123"]) == [
        "tool",
        "--api-key",
        "<redacted>",
    ]
    assert redact_argv(["tool", "--db-password", "hunter2"]) == [
        "tool",
        "--db-password",
        "<redacted>",
    ]
    assert redact_argv(["ls", "--sort", "size"]) == [
        "ls",
        "--sort",
        "size",
    ]
    assert redact_argv(["tool", "--output", "dir"]) == [
        "tool",
        "--output",
        "dir",
    ]


def test_redact_argv_password_program_after_sudo():
    from agenthub.redact import redact_argv

    assert redact_argv(["sudo", "mysql", "-psecret123"]) == [
        "sudo",
        "mysql",
        "-p<redacted>",
    ]
    assert redact_argv(["sudo", "mysql", "-p", "secret123"]) == [
        "sudo",
        "mysql",
        "-p",
        "<redacted>",
    ]
    assert redact_argv(["mkdir", "-p", "dir"]) == [
        "mkdir",
        "-p",
        "dir",
    ]
