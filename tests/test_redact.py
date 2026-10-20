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
    assert redact_argv(["tool", "--password", "-v"]) == ["tool", "--password", "<redacted>"]
    assert redact_argv(["tool", "--api-key", "-f", "val"]) == ["tool", "--api-key", "<redacted>", "val"]
    assert redact_argv(["tool", "--token", "--password", "secret2"]) == [
        "tool",
        "--token",
        "--password",
        "<redacted>",
    ]
    assert redact_argv(["tool", "--token"]) == ["tool", "--token"]
    assert redact_argv(["tool", "--client-secret"]) == ["tool", "--client-secret"]


def test_secret_flag_value_starting_with_dash():
    from agenthub.redact import redact_argv

    # Next item starts with single dash: treated as value and redacted
    assert redact_argv(["app", "--password", "-hunter2"]) == ["app", "--password", "<redacted>"]
    assert redact_argv(["deploy", "--token", "-Xk9a"]) == ["deploy", "--token", "<redacted>"]
    assert redact_argv(["cmd", "--api-key", "-secret"]) == ["cmd", "--api-key", "<redacted>"]

    # Password programs with -p followed by single dash value
    assert redact_argv(["mysql", "-p", "-hunter2"]) == ["mysql", "-p", "<redacted>"]
    assert redact_argv(["sudo", "mysql", "-p", "-hunter2"]) == ["sudo", "mysql", "-p", "<redacted>"]

    # Next item starts with -- (long option): not treated as value
    assert redact_argv(["app", "--password", "--verbose"]) == ["app", "--password", "--verbose"]
    assert redact_argv(["mysql", "-p", "--help"]) == ["mysql", "-p", "--help"]


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
    assert redact_argv(["echo", "mysql", "-p", "secret"]) == ["echo", "mysql", "-p", "<redacted>"]
    assert redact_argv(["ls", "mysqldump", "-p"]) == ["ls", "mysqldump", "-p"]


def test_compound_command_starting_with_echo_still_redacts_p():
    from agenthub.redact import redact_argv

    assert redact_argv(["echo", "start", "&&", "mysql", "-phunter2", "db"]) == [
        "echo",
        "start",
        "&&",
        "mysql",
        "-p<redacted>",
        "db",
    ]


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


def test_curl_user_password_redacted():
    from agenthub.redact import redact_argv

    assert redact_argv(["curl", "-u", "alice:hunter2", "http://x"]) == ["curl", "-u", "alice:<redacted>", "http://x"]
    assert redact_argv(["curl", "--user", "alice:hunter2"]) == ["curl", "--user", "alice:<redacted>"]
    assert redact_argv(["curl", "--user=alice:hunter2"]) == ["curl", "--user=alice:<redacted>"]
    assert redact_argv(["curl", "-U", "bob:pw:x"]) == ["curl", "-U", "bob:<redacted>"]
    proxy = redact_argv(["/usr/bin/curl", "--proxy-user", "bob:pw"])
    assert proxy == ["/usr/bin/curl", "--proxy-user", "bob:<redacted>"]
    assert redact_argv(["curl", "--proxy-user=bob:pw"]) == ["curl", "--proxy-user=bob:<redacted>"]
    assert redact_argv(["curl", "-ualice:hunter2"]) == ["curl", "-ualice:<redacted>"]
    assert redact_argv(["curl", "-u", "alice"]) == ["curl", "-u", "alice"]


def test_curl_bundled_short_flags_user():
    from agenthub.redact import redact_argv

    assert redact_argv(["curl", "-ku", "a:b", "http://x"]) == ["curl", "-ku", "a:<redacted>", "http://x"]
    assert redact_argv(["curl", "-su", "a:b", "http://x"]) == ["curl", "-su", "a:<redacted>", "http://x"]
    assert redact_argv(["curl", "-sSu", "a:b", "http://x"]) == ["curl", "-sSu", "a:<redacted>", "http://x"]
    assert redact_argv(["curl", "-kua:b", "http://x"]) == ["curl", "-kua:<redacted>", "http://x"]
    assert redact_argv(["curl", "-kU", "a:b", "http://x"]) == ["curl", "-kU", "a:<redacted>", "http://x"]
    assert redact_argv(["curl", "-kUa:b", "http://x"]) == ["curl", "-kUa:<redacted>", "http://x"]
    assert redact_argv(["curl", "-sSkua:b", "http://x"]) == ["curl", "-sSkua:<redacted>", "http://x"]


def test_wget_user_password_redacted():
    from agenthub.redact import redact_argv

    out = redact_argv(["wget", "--user=alice", "--password=hunter2", "http://x"])
    assert out[2] == "--password=<redacted>"
    assert "hunter2" not in " ".join(out)
    out = redact_argv(["wget", "--http-password", "hunter2", "http://x"])
    assert "hunter2" not in " ".join(out)


def test_curl_user_value_still_generic_redacted():
    from agenthub.redact import redact_argv

    key = "sk_live_51HxXk9pLmQ2vR7tY4wZq"
    out = redact_argv(["curl", "http://x", "-u", f"{key}:"])
    assert key not in " ".join(out)
    ghp = "ghp_" + "aB3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1xY3zA5"
    out = redact_argv(["curl", "-u", ghp, "http://x"])
    assert ghp not in " ".join(out)
    out = redact_argv(["curl", "-u", "alice@corp.com:pw"])
    assert out[:2] == ["curl", "-u"]
    assert "pw" not in out[2]
    assert out[2] in ("<email>:<redacted>", "alice@corp.com:<redacted>", "<email>")


def test_single_dash_secret_flag_with_u_not_curl_bundle():
    from agenthub.redact import redact_argv

    assert redact_argv(["curl", "-auth-token=SECRETVALUE1", "http://x"]) == [
        "curl",
        "-auth-token=<redacted>",
        "http://x",
    ]
    assert redact_argv(["curl", "-auth-token", "SECRETVALUE1"]) == ["curl", "-auth-token", "<redacted>"]


def test_every_argv_item_gets_redact_pass():
    from agenthub.redact import redact, redact_argv

    key = "sk_live_51HxXk9pLmQ2vR7tY4wZq"
    assert redact(key) != key
    for argv in (["curl", "-u", key], ["curl", f"-u{key}"], ["curl", "--user", key], ["curl", f"--user={key}"]):
        assert key not in " ".join(redact_argv(argv))


def test_curl_cert_separate_redacts_password():
    from agenthub.redact import redact_argv

    out = redact_argv(["curl", "-E", "cert.pem:pw", "--cert", "cert.pem:pw", "http://x"])
    assert out == ["curl", "-E", "cert.pem:<redacted>", "--cert", "cert.pem:<redacted>", "http://x"]
    assert redact_argv(["curl", "-E", "cert.pem"]) == ["curl", "-E", "cert.pem"]


def test_curl_cert_equals_form():
    from agenthub.redact import redact_argv

    assert redact_argv(["curl", "--cert=cert.pem:pw"]) == ["curl", "--cert=cert.pem:<redacted>"]
    assert redact_argv(["curl", "--proxy-cert=cert.pem:pw"]) == ["curl", "--proxy-cert=cert.pem:<redacted>"]
    assert redact_argv(["curl", "--cert=cert.pem"]) == ["curl", "--cert=cert.pem"]


def test_curl_proxy_cert_redacts_password():
    from agenthub.redact import redact_argv

    out = redact_argv(["curl", "--proxy-cert", "cert.pem:pw", "http://x"])
    assert out == ["curl", "--proxy-cert", "cert.pem:<redacted>", "http://x"]


def test_curl_short_cert_attached():
    from agenthub.redact import redact_argv

    assert redact_argv(["curl", "-Ecert.pem:pw", "http://x"]) == ["curl", "-Ecert.pem:<redacted>", "http://x"]
    assert redact_argv(["curl", "-Ecert.pem"]) == ["curl", "-Ecert.pem"]


def test_curl_cert_windows_drive_kept():
    from agenthub.redact import redact_argv

    assert redact_argv(["curl", "-E", "C:\\certs\\a.pem"]) == ["curl", "-E", "C:\\certs\\a.pem"]
    assert redact_argv(["curl", "-E", "C:\\certs\\a.pem:pw"]) == ["curl", "-E", "C:\\certs\\a.pem:<redacted>"]


def test_curl_cert_password_with_colon():
    from agenthub.redact import redact_argv

    assert redact_argv(["curl", "-E", "cert.pem:pa:ss", "http://x"]) == [
        "curl",
        "-E",
        "cert.pem:<redacted>",
        "http://x",
    ]
    assert redact_argv(["curl", "--cert=cert.pem:pa:ss"]) == ["curl", "--cert=cert.pem:<redacted>"]
    assert redact_argv(["curl", "-Ecert.pem:pa:ss"]) == ["curl", "-Ecert.pem:<redacted>"]


def test_curl_cert_escaped_colon_in_cert():
    from agenthub.redact import redact_argv

    assert redact_argv(["curl", "-E", "my\\:cert.pem:pw"]) == ["curl", "-E", "my\\:cert.pem:<redacted>"]
    assert redact_argv(["curl", "-E", "my\\:cert.pem:pa:ss"]) == ["curl", "-E", "my\\:cert.pem:<redacted>"]


def test_curl_cert_windows_drive():
    from agenthub.redact import redact_argv

    assert redact_argv(["curl", "-E", "C:\\a\\b.pem:pa:ss"]) == ["curl", "-E", "C:\\a\\b.pem:<redacted>"]
    assert redact_argv(["curl", "-E", "C:\\a\\my\\:cert.pem:pw"]) == ["curl", "-E", "C:\\a\\my\\:cert.pem:<redacted>"]
    assert redact_argv(["curl", "-E", "C:\\a\\b.pem"]) == ["curl", "-E", "C:\\a\\b.pem"]


def test_curl_cert_bundled_flags():
    from agenthub.redact import redact_argv

    assert redact_argv(["curl", "-kEcert.pem:pw", "http://x"]) == ["curl", "-kEcert.pem:<redacted>", "http://x"]
    assert redact_argv(["curl", "-sE", "cert.pem:pw", "http://x"]) == ["curl", "-sE", "cert.pem:<redacted>", "http://x"]
    assert redact_argv(["curl", "-sSEcert.pem:pa:ss", "http://x"]) == ["curl", "-sSEcert.pem:<redacted>", "http://x"]
    assert redact_argv(["curl", "-sE", "cert.pem"]) == ["curl", "-sE", "cert.pem"]
    assert redact_argv(["curl", "-kEcert.pem"]) == ["curl", "-kEcert.pem"]


def test_non_curl_cert_flag_untouched():
    from agenthub.redact import redact_argv

    assert redact_argv(["ls", "-E", "cert.pem:pw"]) == ["ls", "-E", "cert.pem:pw"]
    assert redact_argv(["ls", "--cert=cert.pem:pw"]) == ["ls", "--cert=cert.pem:pw"]
