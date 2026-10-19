from __future__ import annotations

import re
import shlex
import urllib.parse
from typing import Any, NamedTuple

_ENV_VAR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*")
_RSYNC_HOST_RE = re.compile(r"^(?:[a-zA-Z0-9_.-]+@)?[a-zA-Z0-9_.-]+(?:::|:)")
_ALLOWLISTED_EXACT_DOMAINS = {
    "api.anthropic.com",
    "api.openai.com",
    "chatgpt.com",
    "localhost",
    "127.0.0.1",
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
    if d.endswith(".chatgpt.com"):
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
            if c in (";", "|"):
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


def _strip_leading_prefixes(tokens: list[str]) -> list[str]:
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if tok == "sudo":
            i += 1
            while i < n and tokens[i].startswith("-"):
                flag = tokens[i]
                i += 1
                if flag in ("-u", "-g", "-C") and i < n:
                    i += 1
            continue
        if tok == "env":
            i += 1
            while i < n and tokens[i].startswith("-"):
                flag = tokens[i]
                i += 1
                if flag in ("-u", "-S") and i < n:
                    i += 1
            continue
        if _ENV_VAR_RE.match(tok):
            i += 1
            continue
        break
    return tokens[i:]


def _is_rsync_host(arg: str) -> bool:
    if arg.startswith(("-", "/", "./", "../")):
        return False
    return bool(_RSYNC_HOST_RE.search(arg))


def _extract_curl_wget_domains(tokens: list[str]) -> list[str]:
    domains: list[str] = []
    args = tokens[1:]

    for arg in args:
        if "://" in arg:
            host = _extract_domain(arg)
            if host:
                domains.append(host)

    if not domains:
        skip_next = False
        curl_opt_with_arg = {
            "-X",
            "--request",
            "-H",
            "--header",
            "-d",
            "--data",
            "--data-raw",
            "--data-binary",
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
            "-T",
            "--upload-file",
            "--cacert",
            "--cert",
            "--key",
        }
        for arg in args:
            if skip_next:
                skip_next = False
                continue
            if arg in curl_opt_with_arg:
                skip_next = True
                continue
            if arg.startswith("-"):
                continue
            host = _extract_domain(arg)
            if host:
                domains.append(host)

    return domains


def _is_outward_segment(tokens: list[str]) -> bool:
    stripped = _strip_leading_prefixes(tokens)
    if not stripped:
        return False

    prog = stripped[0]
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
        return True

    if prog == "rsync":
        for a in args:
            if _is_rsync_host(a):
                return True
        return False

    if prog in ("curl", "wget"):
        domains = _extract_curl_wget_domains(stripped)
        if domains:
            return any(not _is_allowlisted_domain(d) for d in domains)
        for a in args:
            if not a.startswith("-"):
                host = _extract_domain(a)
                if host and not _is_allowlisted_domain(host):
                    return True
        return False

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


def classify_command(command: str, cwd: str | None = None) -> list[Classified]:
    try:
        argv = shlex.split(command)
    except ValueError:
        argv = [command]

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
        if stripped_first and stripped_first[0] == "git":
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

    # Check each segment for outward items
    for seg in segments:
        try:
            seg_tokens = shlex.split(seg)
        except ValueError:
            seg_tokens = [seg]

        if _is_outward_segment(seg_tokens):
            events.append(Classified(kind="outward", target=seg, detail=primary_detail))

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
                events.append(Classified(kind="outward", target=domain, detail=detail))
            return events

        if tool_name == "WebSearch":
            return [Classified(kind="net", target="web-search", detail={})]

    if host_norm == "agy" and tool_name == "run_command":
        cmd = inp.get("CommandLine", "")
        cwd = inp.get("Cwd") or inp.get("cwd")
        return classify_command(cmd, cwd=cwd)

    return [Classified(kind="tool", target=tool_name, detail={"tool": tool_name})]
