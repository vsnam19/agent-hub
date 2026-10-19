from __future__ import annotations

import os.path
import re
import shlex
import urllib.parse
from typing import Any, NamedTuple

from agenthub.redact import redact_argv

_ENV_VAR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*")
_RSYNC_HOST_RE = re.compile(r"^(?:[a-zA-Z0-9_.-]+@)?[a-zA-Z0-9_.-]+(?:::|:)")
_ALLOWLISTED_EXACT_DOMAINS = {
    "api.anthropic.com",
    "api.openai.com",
    "chatgpt.com",
    "localhost",
    "127.0.0.1",  # loopback alias of localhost
}


class Classified(NamedTuple):
    kind: str
    target: str
    detail: dict[str, Any]


def _extract_domain(raw: str) -> str:
    if not raw:
        return ""
    if "://" not in raw and not raw.startswith("//"):
        raw = "//" + raw
    parsed = urllib.parse.urlparse(raw)
    return parsed.hostname or parsed.netloc.split(":")[0] or ""


def _is_allowlisted_domain(domain: str) -> bool:
    d = domain.lower().strip()
    if not d:
        return False
    if d in _ALLOWLISTED_EXACT_DOMAINS:
        return True
    if d == "googleapis.com" or d.endswith(".googleapis.com"):
        return True
    return False


def _split_command_segments(command: str) -> list[str]:
    segments: list[str] = []
    current: list[str] = []
    in_single = False
    in_double = False
    escaped = False
    i = 0
    n = len(command)

    while i < n:
        c = command[i]
        if escaped:
            current.append(c)
            escaped = False
            i += 1
            continue
        if c == "\\":
            escaped = True
            current.append(c)
            i += 1
            continue
        if c == "'" and not in_double:
            in_single = not in_single
            current.append(c)
            i += 1
            continue
        if c == '"' and not in_single:
            in_double = not in_double
            current.append(c)
            i += 1
            continue
        if not in_single and not in_double:
            if command[i : i + 2] in ("&&", "||"):
                seg = "".join(current).strip()
                if seg:
                    segments.append(seg)
                current = []
                i += 2
                continue
            if c in (";", "|", "\n"):
                seg = "".join(current).strip()
                if seg:
                    segments.append(seg)
                current = []
                i += 1
                continue
            if c == "&":
                is_redir = (i > 0 and command[i - 1] in (">", "<")) or (i + 1 < n and command[i + 1] == ">")
                if not is_redir:
                    seg = "".join(current).strip()
                    if seg:
                        segments.append(seg)
                    current = []
                    i += 1
                    continue
        current.append(c)
        i += 1

    if current:
        seg = "".join(current).strip()
        if seg:
            segments.append(seg)

    return segments or [command.strip()] if command.strip() else []


_WRAPPERS = {"command", "nohup", "time", "exec", "nice", "setsid"}


def _strip_leading_prefixes(tokens: list[str]) -> list[str]:
    tokens = list(tokens)
    while tokens and tokens[-1] == ")":
        tokens.pop()
    if tokens and tokens[-1].endswith(")") and "(" not in tokens[-1]:
        tokens[-1] = tokens[-1].rstrip(")")

    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if not tok:
            i += 1
            continue

        if tok.startswith("("):
            stripped_tok = tok.lstrip("(")
            if not stripped_tok:
                i += 1
                continue
            tokens[i] = stripped_tok
            continue

        base = os.path.basename(tok)
        if base == "sudo":
            i += 1
            while i < n and tokens[i].startswith("-"):
                flag = tokens[i]
                i += 1
                if (
                    flag in ("-u", "-g", "-C", "-p", "-c", "-a", "-r", "-t", "-T", "-D", "-U")
                    and i < n
                    and not tokens[i].startswith("-")
                ):
                    i += 1
            continue
        if base == "env":
            i += 1
            while i < n and tokens[i].startswith("-"):
                flag = tokens[i]
                i += 1
                if flag in ("-S", "--split-string") and i < n:
                    s_val = tokens[i]
                    try:
                        sub_tokens = shlex.split(s_val)
                    except ValueError:
                        sub_tokens = s_val.split()
                    tokens[i : i + 1] = sub_tokens
                    n = len(tokens)
                    continue
                if flag.startswith("--split-string="):
                    s_val = flag.split("=", 1)[1]
                    try:
                        sub_tokens = shlex.split(s_val)
                    except ValueError:
                        sub_tokens = s_val.split()
                    tokens[i:i] = sub_tokens
                    n = len(tokens)
                    continue
                if flag in ("-u", "--unset", "-C", "--chdir") and i < n and not tokens[i].startswith("-"):
                    i += 1
            continue
        if base == "timeout":
            i += 1
            while i < n and tokens[i].startswith("-"):
                flag = tokens[i]
                i += 1
                if flag in ("-k", "--kill-after", "-s", "--signal") and i < n and not tokens[i].startswith("-"):
                    i += 1
            if i < n and not tokens[i].startswith("-"):
                i += 1
            continue
        if base == "xargs":
            i += 1
            while i < n and tokens[i].startswith("-"):
                flag = tokens[i]
                i += 1
                if (
                    flag
                    in (
                        "-n",
                        "--max-args",
                        "-L",
                        "--max-lines",
                        "-I",
                        "--replace",
                        "-s",
                        "--max-chars",
                        "-P",
                        "--max-procs",
                        "-E",
                        "-d",
                        "--delimiter",
                        "-a",
                        "--arg-file",
                    )
                    and i < n
                    and not tokens[i].startswith("-")
                ):
                    i += 1
            continue
        if base in _WRAPPERS:
            i += 1
            while i < n and tokens[i].startswith("-"):
                flag = tokens[i]
                i += 1
                if flag in ("-n", "--adjustment", "-a", "-u") and i < n and not tokens[i].startswith("-"):
                    i += 1
            continue
        if _ENV_VAR_RE.match(tok):
            i += 1
            continue
        break
    return tokens[i:]


def _extract_sh_c_command(tokens: list[str]) -> str | None:
    stripped = _strip_leading_prefixes(tokens)
    if not stripped:
        return None
    prog = os.path.basename(stripped[0])
    if prog not in ("sh", "bash", "zsh"):
        return None
    args = stripped[1:]
    i = 0
    n = len(args)
    while i < n:
        arg = args[i]
        if arg == "-c":
            if i + 1 < n:
                return args[i + 1]
            return None
        if arg.startswith("-") and not arg.startswith("--"):
            if "c" in arg[1:]:
                if i + 1 < n:
                    return args[i + 1]
                return None
            if arg in ("-o", "+o") and i + 1 < n:
                i += 1
            i += 1
            continue
        if arg.startswith("--"):
            if arg in ("--rcfile",):
                if i + 1 < n:
                    i += 1
            i += 1
            continue
        break
    return None


def _is_rsync_host(arg: str) -> bool:
    if arg.startswith(("-", "/", "./", "../")):
        return False
    return bool(_RSYNC_HOST_RE.search(arg))


_CURL_OPT_WITH_ARG = {
    "-X",
    "--request",
    "-H",
    "--header",
    "-d",
    "--data",
    "--data-raw",
    "--data-binary",
    "--data-urlencode",
    "--data-ascii",
    "-u",
    "--user",
    "-o",
    "--output",
    "-A",
    "--user-agent",
    "-e",
    "--referer",
    "-m",
    "--max-time",
    "--connect-timeout",
    "-b",
    "--cookie",
    "-c",
    "--cookie-jar",
    "-F",
    "--form",
    "--form-string",
    "-T",
    "--upload-file",
    "--cacert",
    "--cert",
    "--key",
    "--capath",
    "-w",
    "--write-out",
    "--resolve",
    "-K",
    "--config",
    "--proxy",
    "-x",
    "--retry",
    "--retry-delay",
    "--retry-max-time",
    "-r",
    "--range",
    "-C",
    "--continue-at",
    "--proto",
    "--proto-default",
    "--proto-redir",
    "--ciphers",
    "--tls13-ciphers",
    "--interface",
    "--limit-rate",
    "--local-port",
    "--unix-socket",
    "--abstract-unix-socket",
    "--pinnedpubkey",
    "--oauth2-bearer",
    "--trace",
    "--trace-ascii",
}

_WGET_OPT_WITH_ARG = {
    "-O",
    "--output-document",
    "-o",
    "--output-file",
    "-a",
    "--append-output",
    "-e",
    "--execute",
    "-P",
    "--directory-prefix",
    "-U",
    "--user-agent",
    "--header",
    "--post-data",
    "--post-file",
    "--body-data",
    "--body-file",
    "-t",
    "--tries",
    "-T",
    "--timeout",
    "-w",
    "--wait",
    "--waitretry",
    "-i",
    "--input-file",
    "-B",
    "--base",
    "--bind-address",
    "--limit-rate",
    "--dns-timeout",
    "--connect-timeout",
    "--read-timeout",
    "--http-user",
    "--http-password",
    "--proxy-user",
    "--proxy-password",
    "--ca-certificate",
    "--ca-directory",
    "--certificate",
    "--certificate-type",
    "--private-key",
    "--private-key-type",
    "--secure-protocol",
    "--ciphers",
    "--user",
    "--password",
    "--load-cookies",
    "--save-cookies",
    "--progress",
    "--regex-type",
    "-D",
    "--domains",
    "--exclude-domains",
    "-I",
    "--include-directories",
    "-X",
    "--exclude-directories",
    "-A",
    "--accept",
    "-R",
    "--reject",
    "--accept-regex",
    "--reject-regex",
    "-l",
    "--level",
    "-Q",
    "--quota",
    "--cut-dirs",
    "--default-page",
}


def _extract_domains_with_opts(tokens: list[str], opt_with_arg: set[str]) -> list[str]:
    domains: list[str] = []
    args = tokens[1:]

    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg in opt_with_arg:
            skip_next = True
            continue
        if arg.startswith("--") and "=" in arg:
            opt_name = arg.split("=", 1)[0]
            if opt_name in opt_with_arg:
                continue
        if "://" in arg:
            host = _extract_domain(arg)
            if host:
                domains.append(host)

    if not domains:
        skip_next = False
        for arg in args:
            if skip_next:
                skip_next = False
                continue
            if arg in opt_with_arg:
                skip_next = True
                continue
            if arg.startswith("--") and "=" in arg:
                opt_name = arg.split("=", 1)[0]
                if opt_name in opt_with_arg:
                    continue
            if arg.startswith("-"):
                continue
            host = _extract_domain(arg)
            if host:
                domains.append(host)

    return domains


def _extract_curl_domains(tokens: list[str]) -> list[str]:
    return _extract_domains_with_opts(tokens, _CURL_OPT_WITH_ARG)


def _extract_wget_domains(tokens: list[str]) -> list[str]:
    return _extract_domains_with_opts(tokens, _WGET_OPT_WITH_ARG)


def _extract_curl_wget_domains(tokens: list[str]) -> list[str]:
    if tokens:
        prog = os.path.basename(tokens[0])
        if prog == "wget":
            return _extract_wget_domains(tokens)
    return _extract_curl_domains(tokens)


def _is_outward_segment(tokens: list[str]) -> bool:
    stripped = _strip_leading_prefixes(tokens)
    if not stripped:
        return False

    prog = os.path.basename(stripped[0])
    args = stripped[1:]

    if prog == "git":
        # Skip git flags like -C, etc.
        j = 0
        while j < len(args) and args[j].startswith("-"):
            flag = args[j]
            j += 1
            if flag in ("-C", "-c", "--git-dir", "--work-tree") and j < len(args):
                j += 1
        non_flag_args = args[j:]
        if non_flag_args:
            sub = non_flag_args[0]
            if sub == "push":
                return True
            if sub == "remote" and len(non_flag_args) > 1:
                if non_flag_args[1] in ("add", "set-url"):
                    return True
        return False

    if prog == "gh":
        j = 0
        while j < len(args) and args[j].startswith("-"):
            flag = args[j]
            j += 1
            if flag in ("-R", "--repo") and j < len(args):
                j += 1
        sub_args = args[j:]
        if len(sub_args) >= 2:
            if sub_args[0] in ("pr", "repo", "release") and sub_args[1] == "create":
                return True
        return False

    if prog == "scp":
        for a in args:
            if _is_rsync_host(a):
                return True
        return False

    if prog == "rsync":
        for a in args:
            if _is_rsync_host(a):
                return True
        return False

    if prog == "curl":
        domains = _extract_curl_domains(stripped)
        return any(not _is_allowlisted_domain(d) for d in domains)

    if prog == "wget":
        domains = _extract_wget_domains(stripped)
        return any(not _is_allowlisted_domain(d) for d in domains)

    return False


def _parse_git_detail(tokens: list[str]) -> dict[str, Any]:
    # tokens starts with 'git'
    detail: dict[str, Any] = {}
    i = 1
    n = len(tokens)

    while i < n and tokens[i].startswith("-"):
        flag = tokens[i]
        i += 1
        if flag in ("-C", "-c", "--git-dir", "--work-tree") and i < n:
            i += 1

    if i >= n:
        return detail

    op = tokens[i]
    detail["op"] = op
    args = tokens[i + 1 :]

    pos_args: list[str] = []
    flags: dict[str, str] = {}
    j = 0
    while j < len(args):
        a = args[j]
        if a == "--":
            break
        if a.startswith("-"):
            if a in ("-b", "-B", "-c", "-C", "-m", "-M", "-d", "-D", "--onto"):
                if j + 1 < len(args):
                    flags[a] = args[j + 1]
                    j += 2
                    continue
            j += 1
            continue
        pos_args.append(a)
        j += 1

    if op in ("checkout", "switch"):
        for f in ("-b", "-B", "-c", "-C"):
            if f in flags:
                detail["ref"] = flags[f]
                break
        else:
            if pos_args:
                detail["ref"] = pos_args[0]

    elif op == "branch":
        for f in ("-d", "-D", "-m", "-M"):
            if f in flags:
                detail["ref"] = flags[f]
                break
        else:
            if pos_args:
                detail["ref"] = pos_args[0]

    elif op in ("merge", "rebase", "reset", "tag", "show", "diff"):
        if pos_args:
            detail["ref"] = pos_args[0]

    elif op in ("fetch", "pull"):
        if len(pos_args) >= 2:
            detail["remote"] = pos_args[0]
            detail["ref"] = pos_args[1]
        elif len(pos_args) == 1:
            detail["remote"] = pos_args[0]

    elif op == "remote":
        if pos_args and pos_args[0] in ("show", "remove", "rm", "prune") and len(pos_args) > 1:
            detail["remote"] = pos_args[1]

    return detail


def _copy_detail(detail: dict[str, Any]) -> dict[str, Any]:
    copied = dict(detail)
    if "argv" in copied and isinstance(copied["argv"], list):
        copied["argv"] = list(copied["argv"])
    return copied


def classify_command(command: str, cwd: str | None = None) -> list[Classified]:
    try:
        argv = shlex.split(command)
    except ValueError:
        # unbalanced quote, heredoc, trailing backslash: whitespace tokens, so the argv rules still fire
        fallback = redact_argv(command.split())
        argv = fallback if fallback != command.split() else [command]

    segments = _split_command_segments(command)

    # Check if first segment is "Other git <op>"
    is_git_kind = False
    git_detail: dict[str, Any] = {}

    if segments:
        try:
            first_seg_tokens = shlex.split(segments[0])
        except ValueError:
            first_seg_tokens = [segments[0]]

        stripped_first = _strip_leading_prefixes(first_seg_tokens)
        if stripped_first and os.path.basename(stripped_first[0]) == "git":
            if not _is_outward_segment(first_seg_tokens):
                is_git_kind = True
                git_detail = _parse_git_detail(stripped_first)

    if is_git_kind:
        primary_kind = "git"
        primary_detail = git_detail
    else:
        primary_kind = "command"
        primary_detail = {"argv": argv}
        if cwd is not None:
            primary_detail["cwd"] = cwd

    events: list[Classified] = [Classified(kind=primary_kind, target=command, detail=primary_detail)]

    # Collect segments to scan for outward items
    segments_to_scan: list[str] = []
    for seg in segments:
        segments_to_scan.append(seg)
        try:
            seg_tokens = shlex.split(seg)
        except ValueError:
            seg_tokens = [seg]
        inner_cmd = _extract_sh_c_command(seg_tokens)
        if inner_cmd:
            inner_segs = _split_command_segments(inner_cmd)
            segments_to_scan.extend(inner_segs)

    # Check each segment for outward items
    for seg in segments_to_scan:
        try:
            seg_tokens = shlex.split(seg)
        except ValueError:
            seg_tokens = [seg]

        if _is_outward_segment(seg_tokens):
            events.append(Classified(kind="outward", target=seg, detail=_copy_detail(primary_detail)))

    return events


def classify(host: str, tool_name: str, tool_input: dict[str, Any] | None) -> list[Classified]:
    inp = tool_input or {}
    host_norm = host.lower()

    if host_norm == "claude":
        if tool_name == "Bash":
            cmd = inp.get("command", "")
            cwd = inp.get("cwd")
            return classify_command(cmd, cwd=cwd)

        if tool_name == "Read":
            path = inp.get("file_path") or inp.get("notebook_path") or inp.get("path") or ""
            return [Classified(kind="file_read", target=path, detail={"path": path, "op": "read"})]

        if tool_name == "Write":
            path = inp.get("file_path") or inp.get("notebook_path") or inp.get("path") or ""
            return [Classified(kind="file_write", target=path, detail={"path": path, "op": "write"})]

        if tool_name in ("Edit", "MultiEdit", "NotebookEdit"):
            path = inp.get("file_path") or inp.get("notebook_path") or inp.get("path") or ""
            return [Classified(kind="file_write", target=path, detail={"path": path, "op": "edit"})]

        if tool_name == "WebFetch":
            raw_url = inp.get("url") or inp.get("Url") or ""
            domain = inp.get("domain") or _extract_domain(raw_url)
            detail = {"domain": domain, "method": "GET"}
            events = [Classified(kind="net", target=domain, detail=detail)]
            if not _is_allowlisted_domain(domain):
                events.append(Classified(kind="outward", target=domain, detail=_copy_detail(detail)))
            return events

        if tool_name == "WebSearch":
            return [Classified(kind="net", target="web-search", detail={})]

    if host_norm == "agy" and tool_name == "run_command":
        cmd = inp.get("CommandLine", "")
        cwd = inp.get("Cwd") or inp.get("cwd")
        return classify_command(cmd, cwd=cwd)

    return [Classified(kind="tool", target=tool_name, detail={"tool": tool_name})]
