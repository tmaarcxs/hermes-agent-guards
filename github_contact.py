"""Block worker terminal calls that contact people on GitHub.

Active only in a worker process (``HERMES_KANBAN_TASK`` set; the delegation policy in
``kanban_policy`` is what puts repository work there). Review requests, issue assignees
and @mention comments notify a person, and contacting people needs the owner's explicit
approval; the authorized alternatives are the guarded admin merge
(``github-agent-ops/scripts/pr_merge_verified``) for a CODEOWNERS-only review gap, or
``kanban_block`` with ``kind=needs_input``.

Blocked at command-word position (segments split like the shell does, quotes respected;
a quoted string is never a command word, so grep patterns and commit messages pass):

- ``gh pr review ...``: every form posts a review;
- ``gh pr create --reviewer/-r`` and ``--assignee/-a``, ``gh issue create --assignee/-a``,
  ``gh pr edit --add-reviewer``, ``gh pr edit --add-assignee`` and
  ``gh issue edit --add-assignee``. Removal flags stay allowed (they reduce contact),
  and ``@me``/``@copilot`` are exempt (not people); for the assignee flags only, the
  authenticated login is also exempt (self-assignment contacts nobody — resolved once
  per process via ``gh api user --jq .login`` and cached; if resolution fails, a value
  that is not ``@me`` still blocks, fail closed);
- ``gh api`` POST to ``.../requested_reviewers`` (GET stays allowed);
- ``gh pr comment`` / ``gh issue comment`` and ``gh api`` POST to ``.../comments`` whose
  body — inline via ``-b/--body`` or ``-f/-F body=...``, or via ``--body-file`` /
  ``-F body=@file`` / ``--input`` (files are read) — contains an ``@user`` or
  ``@org/team`` mention. ``@`` inside code spans, URLs or email addresses is not a
  mention; mention-free comments stay allowed (evidence comments are part of delivery).

Not tracked (catches slips, not deliberate evasion): bodies written in the interactive
editor, stdin bodies (``--body-file -`` / ``--input -``) that cannot be read here, the
``.../reviews`` REST endpoint, GraphQL mutations and values arriving through command
substitution inside a body. Fails open on unreadable files.
"""

import os
import re
import subprocess
from pathlib import Path
from typing import Any, Optional

from . import kanban_policy

MESSAGE = (
    "Contacting people needs the owner's approval. For a CODEOWNERS/review-only gap use "
    "github-agent-ops/scripts/pr_merge_verified; otherwise block the card with kind=needs_input."
)
RETRY = (
    "Then fix the command and retry it in this same response — do not end on a text-only reply: "
    "a reply without the retry ends the run as a protocol violation."
)

_URL = re.compile(r"https?://\S+")
_CODE_SPAN = re.compile(r"`[^`\n]*`")
_EMAIL = re.compile(
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+"
)
_MENTION = re.compile(
    r"(?<![\w@./])@"
    r"(?=[A-Za-z])"                                     # logins start with a letter, not a digit
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?"   # user or org login (max 39, no edge hyphens)
    r"(?:/[A-Za-z0-9][\w.-]{0,98})?"                    # optional org/team slug
    r"(?!\w)"
)
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PREFIX = {"env", "sudo", "command", "nohup", "time"}
_NOT_PEOPLE = {"@me", "@copilot"}
_NOT_PEOPLE_TEXT = re.compile(r"(?<![\w@./])@(?:me|copilot)\b", re.I)
_CREATE_FLAGS = {"-r": "reviewer", "--reviewer": "reviewer", "-a": "assignee", "--assignee": "assignee"}
_EDIT_FLAGS = {"--add-reviewer": "reviewer", "--add-assignee": "assignee"}
# Flags whose values contact people only when the value is not the authenticated
# login itself: assigning work to yourself contacts nobody (self-assignment).
_ASSIGN_FLAGS = {"--assignee", "-a", "--add-assignee"}
_login_cache: Optional[str] = None
_login_resolved = False


def authenticated_login() -> Optional[str]:
    """The authenticated GitHub login, resolved once per process and cached.

    None when resolution fails (no gh, not authenticated, network error) — that
    outcome is cached too, and callers then fail closed for values that are not
    ``@me``.
    """
    global _login_cache, _login_resolved
    if not _login_resolved:
        _login_resolved = True
        try:
            proc = subprocess.run(
                ["gh", "api", "user", "--jq", ".login"],
                capture_output=True, text=True, timeout=10,
            )
            login = (proc.stdout or "").strip().lstrip("@") if proc.returncode == 0 else ""
        except (OSError, ValueError, subprocess.SubprocessError):
            # SubprocessError covers TimeoutExpired and CalledProcessError too:
            # any resolver failure must fail closed, not crash the guard chain
            # (a crashed guard swallows into pass-through = contact allowed).
            login = ""
        _login_cache = login or None
    return _login_cache


_COMMENT_BODY = {"-b": "text", "--body": "text", "-F": "file", "--body-file": "file"}
_API_VALUE_FLAGS = {
    "-X", "--method", "-H", "--header", "-f", "--raw-field", "-F", "--field", "--input",
    "--hostname", "--cache", "-q", "--jq", "-t", "--template", "-p", "--preview",
}


def _has_mention(text: str) -> bool:
    """True when the text @mentions a user or org/team outside code spans, URLs, emails
    and the not-people placeholders ``@me``/``@copilot``."""
    text = _URL.sub(" ", text)
    text = _CODE_SPAN.sub(" ", text)
    text = _EMAIL.sub(" ", text)
    text = _NOT_PEOPLE_TEXT.sub(" ", text)
    return bool(_MENTION.search(text))


def _scan(command: str) -> list[list[tuple[str, bool]]]:
    """Split a command into shell segments, respecting quotes.

    Each segment is a token list of ``(text, quoted)``. Separators are ``;``, ``&``,
    ``|``, newline and the characters that open a new command context (``(``, ``)``,
    ``{``, ``}``, backtick), so a subshell's command becomes its own segment.
    """
    segments: list[list[tuple[str, bool]]] = []
    tokens: list[tuple[str, bool]] = []
    cur: list[str] = []
    quoted = False
    mode = ""
    i, n = 0, len(command)

    def flush_token() -> None:
        nonlocal cur, quoted, mode
        if cur or mode:
            tokens.append(("".join(cur), bool(mode)))
        cur, quoted, mode = [], False, ""

    def flush_segment() -> None:
        nonlocal tokens
        flush_token()
        if tokens:
            segments.append(tokens)
        tokens = []

    while i < n:
        ch = command[i]
        if mode == "'":
            if ch == "'":
                mode = ""
            else:
                cur.append(ch)
        elif mode == '"':
            if ch == "\\" and i + 1 < n:
                cur.append(command[i + 1])
                i += 1
            elif ch == '"':
                mode = ""
            else:
                cur.append(ch)
        elif ch == "\\" and i + 1 < n:
            cur.append(command[i + 1])
            i += 1
        elif ch in "'\"":
            mode = ch
        elif ch in ";&|\n(){}":
            flush_segment()
        elif ch == "`":
            flush_segment()
        elif ch.isspace():
            flush_token()
        else:
            cur.append(ch)
        i += 1
    flush_segment()
    return segments


def _unprefixed(tokens: list[tuple[str, bool]]) -> list[tuple[str, bool]]:
    """Drop environment assignments and wrappers like env/sudo before the command word."""
    parts = list(tokens)
    while parts and not parts[0][1] and (_ENV_ASSIGN.match(parts[0][0]) or parts[0][0] in _PREFIX):
        parts = parts[1:]
    return parts


def _flags(parts: list[tuple[str, bool]], names: dict[str, str]) -> dict[str, list[str]]:
    """Values of the named flags: inline after ``=``, else the next token; repeats collected."""
    found: dict[str, list[str]] = {}
    i = 0
    while i < len(parts):
        text, quoted = parts[i]
        if not quoted and text.startswith("-") and len(text) > 1:
            name, eq, inline = text.partition("=")
            if name in names:
                if eq:
                    found.setdefault(name, []).append(inline)
                elif (
                    i + 1 < len(parts)
                    and parts[i + 1][1] is False or (not parts[i + 1][0].startswith("-") and len(parts[i + 1][0]) > 1)
                ):
                    found.setdefault(name, []).append(parts[i + 1][0])
                    i += 1
                else:
                    found.setdefault(name, []).append("")
        i += 1
    return found


def _split_users(value: str) -> list[str]:
    """Split one flag value into candidate logins.

    ``gh`` accepts both ``--assignee a,b`` and ``--assignee a -b``; comma-splitting
    keeps ``a,self`` from hiding behind the self-assignment exemption.
    """
    return [part.strip() for part in str(value).split(",")]


def _contacts(values: list[str], flag: str = "") -> bool:
    """True when any value contacts a person.

    ``@me``/``@copilot`` are never people. For assignee flags, the authenticated
    login is not a person either (self-assignment); when the login cannot be
    resolved, fail closed and keep blocking other values. Comma-splitting applies
    to assignee values only: reviewer flags keep their prior whole-value checks.
    """
    if flag in _ASSIGN_FLAGS:
        users = [u for value in values for u in _split_users(value) if u]
        if users:
            me = authenticated_login()
            if me:
                users = [u for u in users if u.lstrip("@").lower() != me.lower()]
            return any(u.strip().lower() not in _NOT_PEOPLE for u in users)
    return any(str(value).strip().lower() not in _NOT_PEOPLE for value in values)


def _read(ref: Optional[str], workdir: str) -> Optional[str]:
    """Read a body file; ``-`` (stdin) and unreadable paths yield None (fail open)."""
    if not ref or ref == "-":
        return None
    path = Path(ref).expanduser()
    if not path.is_absolute() and not path.is_file():
        for base in (workdir, os.getcwd()):
            if base and (Path(base) / path).is_file():
                path = Path(base) / path
                break
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _comment_body(parts: list[tuple[str, bool]], workdir: str) -> Optional[str]:
    """The body of a ``gh pr|issue comment`` call: file content beats inline text.

    None when no body flag carries a readable value (editor prompt, stdin) — fail open.
    """
    inline: Optional[str] = None
    file_ref: Optional[str] = None
    i = 0
    while i < len(parts):
        text, quoted = parts[i]
        if not quoted and text.startswith("-") and len(text) > 1:
            name, eq, inline_val = text.partition("=")
            if name in _COMMENT_BODY:
                value, consumed = inline_val, bool(eq)
                if not consumed:
                    if i + 1 < len(parts) and (parts[i + 1][1] or not parts[i + 1][0].startswith("-")):
                        value, consumed = parts[i + 1][0], True
                if consumed:
                    if _COMMENT_BODY[name] == "file":
                        file_ref = value
                    else:
                        inline = value
                    if consumed and value is not None and not eq:
                        i += 1
        i += 1
    if file_ref is not None:
        content = _read(file_ref, workdir)
        if content is not None:
            return content
    return inline


def _api_violation(parts: list[tuple[str, bool]], workdir: str) -> str:
    """One ``gh api`` call: blocked review request, or @mention in a POSTed comment body."""
    endpoint, method, hint = "", "", False
    body: Optional[str] = None
    file_ref: Optional[str] = None
    i = 0
    while i < len(parts):
        text, quoted = parts[i]
        if not quoted and text.startswith("-") and len(text) > 1:
            name, eq, inline = text.partition("=")
            value: Optional[str] = None
            consumed = False
            if name in _API_VALUE_FLAGS:
                if eq:
                    value = inline
                elif i + 1 < len(parts):
                    value = parts[i + 1][0]
                    consumed = True
            if name in ("-X", "--method"):
                method = str(value or "").strip().upper()
            elif name in ("-f", "--raw-field", "-F", "--field"):
                hint = True
                if value is not None:
                    key, _, val = value.partition("=")
                    if key == "body":
                        if name in ("-f", "--raw-field") or not val.startswith("@"):
                            body = val
                        else:
                            file_ref = val[1:]
            elif name == "--input":
                hint = True
                file_ref = value
            i += 1 + (1 if consumed else 0)
            continue
        if not endpoint:
            endpoint = text.lower().rstrip("/")
            if endpoint == "graphql":
                hint = True
        i += 1
    if not method:
        method = "POST" if hint else "GET"
    if method not in ("POST", "PUT", "PATCH"):
        return ""
    if endpoint.endswith("/requested_reviewers"):
        return "gh api " + method + " .../requested_reviewers requests a review from people"
    if endpoint.endswith("/comments"):
        if file_ref is not None:
            text = _read(file_ref, workdir)
            if text is None:
                return ""  # stdin or unreadable: cannot verify, fail open
        else:
            text = body
        if text is not None and _has_mention(text):
            return "gh api " + method + " .../comments comment body @mentions a person"
    return ""


def violations(command: str, workdir: str = "") -> list[str]:
    found: list[str] = []
    for tokens in _scan(command):
        parts = _unprefixed(tokens)
        if not parts or parts[0][1] or parts[0][0] != "gh":
            continue
        sub = parts[1][0] if len(parts) > 1 and not parts[1][1] else ""
        kind = parts[2][0] if len(parts) > 2 and not parts[2][1] else ""
        if sub == "pr" and kind == "review":
            found.append("gh pr review posts a review, which contacts people")
        elif sub in ("pr", "issue") and kind == "comment":
            body = _comment_body(parts[3:], workdir)
            if body is not None and _has_mention(body):
                found.append("comment body @mentions a person")
        elif sub in ("pr", "issue") and kind == "create":
            for flag in _CREATE_FLAGS:
                values = _flags(parts[3:], {flag: _CREATE_FLAGS[flag]})
                if _contacts(values.get(flag, []), flag=flag):
                    found.append(f"gh {sub} create {flag} assigns to or requests review from a person")
                    break
        elif sub in ("pr", "issue") and kind == "edit":
            for flag, values in _flags(parts[3:], _EDIT_FLAGS).items():
                if _contacts(values, flag=flag):
                    found.append(f"gh {sub} edit {flag} requests review or assignment from a person")
                    break
        elif sub == "api":
            hit = _api_violation(parts[2:], workdir)
            if hit:
                found.append(hit)
    return found


def check(tool_name: str, args: Any) -> Optional[str]:
    if tool_name != "terminal" or not isinstance(args, dict):
        return None
    if not os.environ.get("HERMES_KANBAN_TASK"):
        return None  # worker process only; the lead's own calls are out of scope
    command = str(args.get("command") or "")
    found = violations(command, str(args.get("workdir") or ""))
    if not found:
        return None
    return (
        "agent-guards blocked terminal: " + "; ".join(found) + ". " + MESSAGE + " " + RETRY
    )
