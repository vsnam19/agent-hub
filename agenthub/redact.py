"""Stdlib-only leaf module for redacting secrets from text and argv."""

import math
import os
import re
import string
from collections import Counter

_PEM = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S)
_JWT = re.compile(r"\beyJ[\w-]+\.[\w-]+\.[\w-]+")
_BEARER = re.compile(r"(?i)\bBearer\s+[\w.~+/=-]+")
_BASIC = re.compile(r"\bBasic\s+([A-Za-z0-9+/_-]{8,}={0,2})(?![\w+/])")
# `scheme://user:` prefix; the scheme is capped at 32 chars so every start position scans a bounded window. The password
# is found by `_redact_url_passwords`, which is not limited in length
_URL_PREFIX = re.compile(r"(?<![a-z])[a-z][\w+.-]{0,31}://[^\s/:@]*:", re.I)
_NON_SPACE = re.compile(r"\S*")
_PREFIXED = re.compile(r"\b(?:sk-[\w-]{16,}|ghp_\w{20,}|xox[a-z]-[\w-]{10,}|AKIA[0-9A-Z]{16})")
# a secret name contains one of `_LONG_WORDS` (`PGPASSWORD`, `SECRETKEY`, `api_key`), or a part (split on `_`, `-` or a
# camelCase hump) equals or ends with one of `_PART_WORDS` (`DB_PASS`, `apiKey`, `adminpass`). A `key` ending needs four
# letters before it, so `monkey` stays a word; `Design`, `author`, `keyboard`, `signal` and `passage` do not match
_LONG_WORDS = "password|passwd|passphrase|secret|token|credential|apikey|api_key|privatekey|private_key"
_PART_WORDS = "pass|pwd|auth|authorization|cookie|session|salt|sig|signature"
_PART_START = r"(?:(?<![\w-])|(?<=[_-])|(?<=(?-i:[a-z0-9]))(?=(?-i:[A-Z])))"
_PART_END = r"(?=[_-]|(?![\w-])|(?<=(?-i:[a-z0-9]))(?=(?-i:[A-Z])))"
_SECRET_NAME = (
    rf"(?=[\w-]*?(?:{_LONG_WORDS})"
    rf"|[\w-]*?(?:{_PART_WORDS}){_PART_END}"
    rf"|[\w-]*?(?:[a-z]{{4}}|{_PART_START})key{_PART_END})"
)
_NAME = rf"{_SECRET_NAME}[\w-]+"
_VALUE = r"(?:\"[^\"]*\"|'[^']*'|[^\s\"',;]+)"
_KV = re.compile(rf"(?i)(?<![\w-])({_NAME})(\s*[=:]\s*){_VALUE}")
_KV_SPACE = re.compile(rf"(?i)(?<![\w-])({_NAME})(\s+)(?!-|<redacted>){_VALUE}")
_KV_JSON = re.compile(rf"(?i)(\"{_NAME}\"\s*:\s*)\"[^\"]*\"")
_BARE_SECRET_WORDS = {"password", "passwd", "pwd", "passphrase"}
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
HOME_PATH = re.compile(r"(?:^|(?<=[\s\"'`(=:/\[]))(?:/home/[^/\s]+|/Users/[^/\s]+|/root(?![\w.-]))")
_HOME = HOME_PATH

# Token rule. Text is split on whitespace, quotes, backticks and `=:,;()<>[]{}?&`; each token is then split on
# `. / @ # *` (Markdown, paths, extensions, `...`) and only a piece is redacted, when ALL hold:
#   - >= 16 chars: shorter strings are mostly words or short ids, and name rules cover short passwords;
#   - >= 2 of {lower, upper, digit}: a single-class string is a word or a number (`_-+` do not count);
#   - Shannon entropy >= 3.0 bits/char: random keys sit at 4+, repeated or patterned text well below;
#   - not an identifier: a piece split on `_`, `-` or `+` whose parts are all words (letters only, each upper-case or
#     camel/lower with >= 3 letter pieces, or a single letter) or digit groups of <= 8 digits (`FastAPI+SQLite+React`,
#     `2026-10-11-model-routing`, `phase-b`), and not a job id (`YYYYMMDD-HHMMSS-role-xxxx`, role = one or more
#     lower-case words joined by `-`): names the extract needs to keep.
_TOKEN = re.compile(r"[^\s\"'`=:,;()<>\[\]{}?&]+")
_PIECE_SPLIT = re.compile(r"([./@#*]+)")
_REDACTED_RUN = re.compile(r"<redacted>(?:[./@#*]+<redacted>)+")
_TOKEN_MIN_LEN = 16
_CONTINUATION_MIN_LEN = 8
_TOKEN_MIN_ENTROPY = 3.0
_DIGIT_GROUP_MAX = 8
_PART_SPLIT = re.compile(r"[_+-]+")
# a readable snake chunk: a word of >= 2 letters with a short digit run (`sha256`, `base64url`, `uuid4`), a letter and
# digits (`t1`), digits and a word (`24h`), or a plain number; random chunks like `k3f9x2m8q7z1` match none of these
_SNAKE_CHUNK = re.compile(r"[a-z]{2,}(?:\d{1,3}[a-z]*)?|[a-z]\d{1,3}|\d{1,3}[a-z]+|\d{1,8}")
_SNAKE_CHUNK_MAX = 12
_ASCII_RUN = re.compile(r"[\x00-\x7f]+")
_WORD_PIECE = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+")
_IDENT_WORD = re.compile(r"(?:[A-Z]+(?![a-z])|[A-Z][a-z]+|[a-z]{3,})+")
_JOB_ID = re.compile(r"\d{8}-\d{6}-[a-z]+(?:-[a-z]+)*-[0-9a-f]{4}(?:\.\w+)?")

_SECRET_FLAG_WORDS = (
    "secret",
    "token",
    "password",
    "passwd",
    "api-key",
    "apikey",
    "credential",
    "private-key",
)
_PASSWORD_PROGRAMS = {"mysql", "mysqldump", "mysqladmin", "sshpass"}
_CURL_USER_FLAGS = {"-u", "--user", "-U", "--proxy-user"}
# bundle letters must be curl short flags that never take a value
_CURL_SHORT_USER = re.compile(r"^-([#0123456ABCGIJLMNOREVZfgijklnpqsSv]*)([uU])(.*)$")
_SECRET_KEY = re.compile(rf"(?i)^{_NAME}$")
_ENV_VAR = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")


def _is_word(text: str) -> bool:
    """Whether `text` is a camel-case word run: capital runs and lowercase runs of 3+ letters, nothing else.

    Splitting into pieces in one linear pass keeps it fast; a nested `(?:...)+` pattern backtracks 2^(n-1) ways over
    a run of n capitals.
    """
    pieces = _WORD_PIECE.findall(text)
    return (
        bool(pieces)
        and "".join(pieces) == text
        and all(p.isupper() or len(p.lstrip(string.ascii_uppercase)) >= 3 for p in pieces)
    )


def _redact_url_passwords(text: str) -> str:
    """Redact `scheme://user:password@` up to the last `@` of the non-space run, however long the password is.

    The end of each run and its last `@` are computed once per run, so a long run of `scheme://` prefixes stays linear.
    """
    out = []
    done = 0  # text before `done` is already in `out`
    run_end = run_at = -1  # end of the current non-space run, and the last `@` before it
    match = _URL_PREFIX.search(text)
    while match:
        start = match.end()
        if start >= run_end:
            run_end = _NON_SPACE.match(text, start).end()
            run_at = text.rfind("@", start, run_end)
        if run_at > start:
            out.append(text[done:start])
            out.append("<redacted>@")
            done = run_at + 1
            match = _URL_PREFIX.search(text, done)
        else:
            match = _URL_PREFIX.search(text, start)
    out.append(text[done:])
    return "".join(out)


def redact(text: str) -> str:
    text = _PEM.sub("<redacted-key>", text)
    text = _JWT.sub("<redacted>", text)
    text = _BEARER.sub("Bearer <redacted>", text)
    text = _BASIC.sub(lambda m: m.group(0) if _is_word(m.group(1)) else "Basic <redacted>", text)
    text = _redact_url_passwords(text)
    text = _PREFIXED.sub("<redacted>", text)
    text = _KV.sub(r"\1\2<redacted>", text)
    text = _KV_SPACE.sub(_redact_space_value, text)
    text = _KV_JSON.sub(r'\1"<redacted>"', text)
    text = _EMAIL.sub("<email>", text)
    text = _TOKEN.sub(lambda m: _redact_token(m.group(0)), text)
    return HOME_PATH.sub("~", text)


def _redact_space_value(match: re.Match) -> str:
    """`name value`: only for flags, identifier-like names and bare password words, so prose stays readable."""
    name = match.group(1)
    value = match.group(0)[len(name) + len(match.group(2)) :]
    flag = name.startswith("--") or name.lower() in _BARE_SECRET_WORDS
    identifier = ("_" in name or re.search(r"[a-z][A-Z]", name)) and not value.isalpha()
    if flag or identifier:
        return f"{name}{match.group(2)}<redacted>"
    return match.group(0)


def _entropy(token: str) -> float:
    counts = Counter(token)
    return -sum(n / len(token) * math.log2(n / len(token)) for n in counts.values())


def _redact_token(token: str) -> str:
    parts = _PIECE_SPLIT.split(token)
    secret = [i % 2 == 0 and _looks_secret(part) for i, part in enumerate(parts)]
    # a base64 blob split by `/`: a mixed, non-word piece right next to a secret piece is part of it
    for i in range(0, len(parts), 2):
        near = (i >= 2 and secret[i - 2] and parts[i - 1] == "/") or (
            i + 2 < len(parts) and secret[i + 2] and parts[i + 1] == "/"
        )
        if near and not secret[i] and _looks_secret(parts[i], _CONTINUATION_MIN_LEN):
            secret[i] = True
    joined = "".join("<redacted>" if secret[i] else part for i, part in enumerate(parts))
    return _REDACTED_RUN.sub("<redacted>", joined)


def _looks_secret(piece: str, min_len: int = _TOKEN_MIN_LEN) -> bool:
    if not piece.isascii():
        # only the ASCII runs are judged: a symbol or accented letter must not hide an identifier, nor a secret
        return any(_looks_secret(run, min_len) for run in _ASCII_RUN.findall(piece))
    if len(piece) < min_len or _snake_identifier(piece):
        return False
    classes = (any(c.islower() for c in piece), any(c.isupper() for c in piece), any(c.isdigit() for c in piece))
    if sum(classes) < 2 or _entropy(piece) < _TOKEN_MIN_ENTROPY or _JOB_ID.fullmatch(piece):
        return False
    parts = [part for part in _PART_SPLIT.split(piece) if part]
    return not all(_identifier_part(part) for part in parts)


def _snake_identifier(piece: str) -> bool:
    parts = piece.split("_")
    return len(parts) >= 3 and all(len(part) <= _SNAKE_CHUNK_MAX and _SNAKE_CHUNK.fullmatch(part) for part in parts)


def _identifier_part(part: str) -> bool:
    if part.isdigit():
        return len(part) <= _DIGIT_GROUP_MAX
    return part.isalpha() and (len(part) == 1 or _IDENT_WORD.fullmatch(part) is not None)


def _is_secret_flag_name(name: str) -> bool:
    """Whether a flag name contains secret keywords or matches the secret name grammar."""
    name_lower = name.lower()
    name_norm = name_lower.replace("_", "-")
    return (
        any(w in name_lower or w in name_norm for w in _SECRET_FLAG_WORDS)
        or bool(_SECRET_KEY.fullmatch(name_lower))
        or bool(_SECRET_KEY.fullmatch(name_norm))
    )


def _is_password_program(argv: list[str]) -> bool:
    """Return True if any argv item's basename is in _PASSWORD_PROGRAMS."""
    return any(os.path.basename(arg) in _PASSWORD_PROGRAMS for arg in argv)


def _is_curl(argv: list[str]) -> bool:
    return any(os.path.basename(arg) == "curl" for arg in argv)


def _redact_userpass(value: str) -> str:
    """`user:password` -> `user:<redacted>`; a value without `:` is left as is."""
    user, sep, _ = value.partition(":")
    return f"{user}:<redacted>" if sep else value


def redact_argv(argv: list[str]) -> list[str]:
    """Redact secret flags, env prefixes and embedded secrets in an argv list.

    Every item goes through `redact()` as a last step, so no branch bypasses generic redaction.
    """
    return [_final_pass(item) for item in _redact_argv_items(argv)]


def _final_pass(item: str) -> str:
    """`redact()` an item; a trailing `:<redacted>` / `=<redacted>` marker is kept, its prefix is still redacted."""
    if item == "<redacted>":
        return item
    for marker in (":<redacted>", "=<redacted>"):
        if item.endswith(marker):
            return redact(item[: -len(marker)]) + marker
    return redact(item)


def _redact_argv_items(argv: list[str]) -> list[str]:
    if not argv:
        return []

    is_pw_prog = _is_password_program(argv)
    is_curl = _is_curl(argv)
    res: list[str] = []
    i = 0

    while i < len(argv):
        arg = argv[i]

        if _ENV_VAR.match(arg):
            k, _ = arg.split("=", 1)
            if _SECRET_KEY.fullmatch(k):
                res.append(f"{k}=<redacted>")
            else:
                res.append(redact(arg))
            i += 1
            continue

        if is_pw_prog:
            if arg == "-p":
                res.append("-p")
                if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                    res.append("<redacted>")
                    i += 2
                else:
                    i += 1
                continue
            if arg.startswith("-p") and not arg.startswith("--") and len(arg) > 2:
                res.append("-p<redacted>")
                i += 1
                continue

        if is_curl:
            if arg in _CURL_USER_FLAGS:
                res.append(arg)
                if i + 1 < len(argv):
                    res.append(_redact_userpass(argv[i + 1]))
                    i += 2
                else:
                    i += 1
                continue
            flag, eq, value = arg.partition("=")
            if eq and flag in _CURL_USER_FLAGS and flag.startswith("--"):
                res.append(f"{flag}={_redact_userpass(value)}")
                i += 1
                continue
            m = _CURL_SHORT_USER.match(arg)
            if m:
                rest = m.group(3)
                if not rest:
                    res.append(arg)
                    if i + 1 < len(argv):
                        res.append(_redact_userpass(argv[i + 1]))
                        i += 2
                    else:
                        i += 1
                else:
                    res.append(f"-{m.group(1)}{m.group(2)}{_redact_userpass(rest)}")
                    i += 1
                continue

        if arg.startswith("-"):
            if "=" in arg:
                flag_name, _ = arg.split("=", 1)
                clean_name = flag_name.lstrip("-")
                if clean_name and _is_secret_flag_name(clean_name):
                    res.append(f"{flag_name}=<redacted>")
                    i += 1
                    continue
            else:
                clean_name = arg.lstrip("-")
                if clean_name and _is_secret_flag_name(clean_name):
                    res.append(arg)
                    if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                        res.append("<redacted>")
                        i += 2
                    else:
                        i += 1
                    continue

        res.append(redact(arg))
        i += 1

    return res
