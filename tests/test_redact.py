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


def test_generic_secret_long_flags():
    from agenthub.redact import redact_argv

    flags = [
        "--secret",
        "--client-secret",
        "--token",
        "--auth-token",
        "--password",
        "--db-password",
        "--passwd",
        "--admin-passwd",
        "--api-key",
        "--service-api-key",
        "--apikey",
        "--google-apikey",
        "--credential",
        "--user-credential",
        "--private-key",
        "--ssl-private-key",
    ]
    for flag in flags:
        assert redact_argv(["cmd", flag, "secret_value"]) == ["cmd", flag, "<redacted>"]

    argv = [
        "tool",
        "--client-secret",
        "s3cr3t",
        "--output",
        "dir",
        "--auth-token",
        "tok123",
        "--sort",
        "name",
        "--private-key",
        "pkval",
    ]
    assert redact_argv(argv) == [
        "tool",
        "--client-secret",
        "<redacted>",
        "--output",
        "dir",
        "--auth-token",
        "<redacted>",
        "--sort",
        "name",
        "--private-key",
        "<redacted>",
    ]


def test_secret_flag_equals_form():
    from agenthub.redact import redact_argv

    flags = [
        "--secret",
        "--client-secret",
        "--token",
        "--auth-token",
        "--password",
        "--db-password",
        "--passwd",
        "--admin-passwd",
        "--api-key",
        "--service-api-key",
        "--apikey",
        "--google-apikey",
        "--credential",
        "--user-credential",
        "--private-key",
        "--ssl-private-key",
    ]
    for flag in flags:
        assert redact_argv(["cmd", f"{flag}=secret_value"]) == ["cmd", f"{flag}=<redacted>"]

    assert redact_argv(["tool", "--token="]) == ["tool", "--token=<redacted>"]
    assert redact_argv(["tool", "--client-secret=foo=bar"]) == ["tool", "--client-secret=<redacted>"]
    assert redact_argv(["tool", "--output=dir", "--sort=size"]) == ["tool", "--output=dir", "--sort=size"]


def test_secret_flag_next_item_is_flag():
    from agenthub.redact import redact_argv

    assert redact_argv(["tool", "--token", "--other-flag"]) == ["tool", "--token", "--other-flag"]
    assert redact_argv(["tool", "--client-secret", "--verbose", "arg"]) == [
        "tool",
        "--client-secret",
        "--verbose",
        "arg",
    ]
    assert redact_argv(["tool", "--password", "-v"]) == ["tool", "--password", "-v"]
    assert redact_argv(["tool", "--api-key", "-f", "val"]) == ["tool", "--api-key", "-f", "val"]
    assert redact_argv(["tool", "--token", "--password", "secret2"]) == [
        "tool",
        "--token",
        "--password",
        "<redacted>",
    ]
    assert redact_argv(["tool", "--token"]) == ["tool", "--token"]
    assert redact_argv(["tool", "--client-secret"]) == ["tool", "--client-secret"]


def test_mysql_p_behind_sudo_env_and_path():
    from agenthub.redact import redact_argv

    assert redact_argv(["sudo", "mysql", "-p", "secret123"]) == ["sudo", "mysql", "-p", "<redacted>"]
    assert redact_argv(["sudo", "mysql", "-psecret123"]) == ["sudo", "mysql", "-p<redacted>"]
    assert redact_argv(["sudo", "-E", "mysql", "-p", "secret123"]) == ["sudo", "-E", "mysql", "-p", "<redacted>"]
    assert redact_argv(["sudo", "-E", "-n", "mysqldump", "-psecret123"]) == [
        "sudo",
        "-E",
        "-n",
        "mysqldump",
        "-p<redacted>",
    ]
    assert redact_argv(["env", "A=1", "mysql", "-p", "secret123"]) == [
        "env",
        "A=1",
        "mysql",
        "-p",
        "<redacted>",
    ]
    assert redact_argv(["env", "mysqladmin", "-psecret123"]) == [
        "env",
        "mysqladmin",
        "-p<redacted>",
    ]
    assert redact_argv(["/usr/bin/mysql", "-p", "secret123"]) == ["/usr/bin/mysql", "-p", "<redacted>"]
    assert redact_argv(["/usr/local/bin/mysqldump", "-psecret123"]) == [
        "/usr/local/bin/mysqldump",
        "-p<redacted>",
    ]
    assert redact_argv(["sudo", "env", "FOO=bar", "/usr/bin/mysql", "-p", "secret123"]) == [
        "sudo",
        "env",
        "FOO=bar",
        "/usr/bin/mysql",
        "-p",
        "<redacted>",
    ]
    assert redact_argv(["sshpass", "-p", "secret123", "ssh", "host"]) == [
        "sshpass",
        "-p",
        "<redacted>",
        "ssh",
        "host",
    ]
    assert redact_argv(["sudo", "sshpass", "-psecret123", "ssh", "host"]) == [
        "sudo",
        "sshpass",
        "-p<redacted>",
        "ssh",
        "host",
    ]


def test_p_flag_untouched_for_other_programs():
    from agenthub.redact import redact_argv

    assert redact_argv(["mkdir", "-p", "my/dir"]) == ["mkdir", "-p", "my/dir"]
    assert redact_argv(["mkdir", "-pdir"]) == ["mkdir", "-pdir"]
    assert redact_argv(["/bin/mkdir", "-p", "dir"]) == ["/bin/mkdir", "-p", "dir"]
    assert redact_argv(["sudo", "mkdir", "-p", "my/dir"]) == ["sudo", "mkdir", "-p", "my/dir"]
    assert redact_argv(["sudo", "-E", "mkdir", "-p", "my/dir"]) == ["sudo", "-E", "mkdir", "-p", "my/dir"]
    assert redact_argv(["env", "A=1", "mkdir", "-p", "my/dir"]) == ["env", "A=1", "mkdir", "-p", "my/dir"]
    assert redact_argv(["grep", "-p", "pattern"]) == ["grep", "-p", "pattern"]
    assert redact_argv(["python", "app.py", "-p", "arg"]) == ["python", "app.py", "-p", "arg"]
    assert redact_argv(["echo", "mysql", "-p", "secret"]) == ["echo", "mysql", "-p", "secret"]
    assert redact_argv(["ls", "mysqldump", "-p"]) == ["ls", "mysqldump", "-p"]


def test_secret_flag_name_variants():
    from agenthub.redact import redact_argv

    assert redact_argv(["tool", "--pass", "pw"]) == ["tool", "--pass", "<redacted>"]
    assert redact_argv(["gpg", "--passphrase", "pw"]) == ["gpg", "--passphrase", "<redacted>"]
    assert redact_argv(["tool", "--key", "mykey"]) == ["tool", "--key", "<redacted>"]
    assert redact_argv(["tool", "--access-key", "ak"]) == ["tool", "--access-key", "<redacted>"]
    assert redact_argv(["tool", "--auth", "x"]) == ["tool", "--auth", "<redacted>"]
    assert redact_argv(["tool", "--session", "s3ss"]) == ["tool", "--session", "<redacted>"]
    assert redact_argv(["tool", "--pass=pw"]) == ["tool", "--pass=<redacted>"]
    assert redact_argv(["tool", "--passphrase=pw"]) == ["tool", "--passphrase=<redacted>"]
    assert redact_argv(["tool", "--key=k"]) == ["tool", "--key=<redacted>"]
    assert redact_argv(["tool", "--access-key=ak"]) == ["tool", "--access-key=<redacted>"]
    assert redact_argv(["tool", "--auth=x"]) == ["tool", "--auth=<redacted>"]


def test_single_dash_secret_flags():
    from agenthub.redact import redact_argv

    assert redact_argv(["vault-tool", "-token", "x"]) == ["vault-tool", "-token", "<redacted>"]
    assert redact_argv(["app", "-password", "x"]) == ["app", "-password", "<redacted>"]
    assert redact_argv(["keytool", "-storepass", "changeit"]) == ["keytool", "-storepass", "<redacted>"]
    assert redact_argv(["tool", "-token=x"]) == ["tool", "-token=<redacted>"]
    assert redact_argv(["tool", "-password=x"]) == ["tool", "-password=<redacted>"]
    assert redact_argv(["tool", "-v"]) == ["tool", "-v"]
    assert redact_argv(["tool", "-o", "out"]) == ["tool", "-o", "out"]


def test_p_behind_wrapper_with_option_values():
    from agenthub.redact import redact_argv

    assert redact_argv(["sudo", "-u", "root", "mysql", "-psecret123"]) == [
        "sudo",
        "-u",
        "root",
        "mysql",
        "-p<redacted>",
    ]
    assert redact_argv(["docker", "exec", "db", "mysql", "-p", "s3cr3t"]) == [
        "docker",
        "exec",
        "db",
        "mysql",
        "-p",
        "<redacted>",
    ]
    assert redact_argv(["kubectl", "exec", "pod", "--", "mysql", "-p", "s3cr3t"]) == [
        "kubectl",
        "exec",
        "pod",
        "--",
        "mysql",
        "-p",
        "<redacted>",
    ]
    assert redact_argv(["timeout", "10", "sshpass", "-p", "pw", "ssh", "host"]) == [
        "timeout",
        "10",
        "sshpass",
        "-p",
        "<redacted>",
        "ssh",
        "host",
    ]
    assert redact_argv(["env", "-u", "FOO", "mysql", "-ppw"]) == [
        "env",
        "-u",
        "FOO",
        "mysql",
        "-p<redacted>",
    ]
