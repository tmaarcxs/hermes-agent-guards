"""Block Kanban workers from writing outside their own task workspace.

Active only in worker processes (``HERMES_KANBAN_TASK`` set); the lead is never
affected. Allowed write targets: the task's own workspace
(``HERMES_KANBAN_WORKSPACE``, its descendants and any ``mktemp -d`` test home
created inside it), the Hermes scratch dir (``$HERMES_HOME/cache/scratch`` and
``~/.hermes/cache/scratch``), ``TMPDIR``/tmp dirs and the board's kanban
attachments directory. Everything else under the real Hermes home (live plugin
install, config.yaml, .env, profiles, hermes-agent core, other tasks'
workspaces) is blocked from writes and deletions; git mutations of a repository
outside the allowed zones (``git -C <path> <verb>`` or ``cd <path> && git
<pull|checkout|reset|rebase|merge|commit|push|fetch|clone|...>``) are blocked
even when the repo is outside ``~/.hermes``. Read-only access everywhere stays
allowed. A worker with no workspace env keeps the scratch allowances but blocks
every other write target.

Covered mechanisms: ``write_file``/``patch`` by path; terminal redirects
``>``/``>>``, ``tee``, ``cp``/``mv``/``rsync``/``install``/``touch``/``mkdir``
with that destination, ``sed -i``, ``rm``/``rmdir``, piped ``xargs rm`` against
a protected path, ``python -c``/heredoc writes, removals and renames by path,
and ``git clone`` destinations. Unknown shapes fail open.
"""

import os
import re
import shlex
from pathlib import Path
from typing import Any, Optional

from . import kanban_policy

_MESSAGE = (
    "agent-guards blocked {tool}: this write targets a protected path outside the "
    "task workspace; workers write only inside their own task workspace "
    "(HERMES_KANBAN_WORKSPACE), the Hermes scratch dir (~/.hermes/cache/scratch and "
    "$HERMES_HOME/cache/scratch), TMPDIR/tmp and the board's kanban attachments "
    "directory."
)
_GIT_MESSAGE = (
    "agent-guards blocked {tool}: this git mutation targets a repository outside the "
    "task workspace; run git only against the task's own workspace checkout "
    "(HERMES_KANBAN_WORKSPACE)."
)
_GIT_VERBS = re.compile(
    r"\bgit\s+(?:-C\s+(?P<gitdir>\S+)\s+)?(?P<verb>"
    r"pull|fetch|push|checkout|reset|rebase|merge|commit|stash|clean|"
    r"cherry-pick|revert|am|apply|rm|mv|worktree|clone)\b"
    r"(?P<rest>[^\n;|&]*)"
)
_READ_ONLY_GIT = re.compile(r"^(?:worktree\s+(?:list|show)|stash\s+(?:list|show)|status|log|diff|show)\b")
_WRITE_HEADS = (
    "cp", "mv", "rsync", "install", "touch", "mkdir", "rm", "rmdir", "tee",
)
_SED_IN_PLACE = re.compile(r"(?:^|\s)-i(?:\s|$)|--in-place")
_PY_CALL = re.compile(
    r"\b(?:rmtree|unlink|remove|copytree|move|makedirs|mkdir|rename|replace)\s*\("
)
_PY_WRITE_CALL = re.compile(
    r"\bopen\s*\([^()]*,\s*['\"][waxu+]|"
    r"\b(?:write_text|write_bytes)\s*\(|"
    r"\brates?_file\(|\bto_csv\s*\(|\bto_json\s*\(|\bsavefig\s*\(|\bdump\s*\("
)
_PY_STRINGS = re.compile(r"(['\"])(.*?)\1")
_HEREDOC = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?")
_REDIRECT = re.compile(r"(?:>>|>)\s*(\S+)")
_FD_REDIRECT = re.compile(r"^\d?&[\d()]*$|^\d?&|^[&]|\)$")
_XARGS_RM = re.compile(r"\bxargs\s+(?:-\S+\s+)*(?:rm|rmdir)\b")


def _strip_heredoc_bodies(command: str) -> str:
    """Blank heredoc bodies so shell write-target scanning never reads python
    code (a script's own ``>`` in python comparisons is not a shell redirect)."""
    delim = None
    kept: list[str] = []
    for line in command.split("\n"):
        if delim is not None:
            kept.append("")
            if line.strip() == delim:
                delim = None
            continue
        kept.append(line)
        match = _HEREDOC.search(line)
        if match:
            delim = match.group(1)
    return "\n".join(kept)


def _home() -> Path:
    real = os.environ.get("HERMES_REAL_HOME") or os.path.expanduser("~")
    return Path(real).expanduser().resolve()


def allowed_roots() -> list[Path]:
    roots: list[Path] = []
    workspace = (os.environ.get("HERMES_KANBAN_WORKSPACE") or "").strip()
    if workspace:
        roots.append(Path(workspace).expanduser().resolve(strict=False))
    for base in ((os.environ.get("HERMES_HOME") or "").strip(), str(_home() / ".hermes")):
        if base:
            roots.append((Path(base).expanduser() / "cache" / "scratch").resolve(strict=False))
    for env in ("TMPDIR", "TMP", "TEMP"):
        value = (os.environ.get(env) or "").strip()
        if value:
            roots.append(Path(value).expanduser().resolve(strict=False))
    for path in ("/tmp", "/var/tmp", "/private/tmp", "/private/var/tmp"):
        roots.append(Path(path).resolve(strict=False))
    attachments = (os.environ.get("HERMES_KANBAN_ATTACHMENTS_ROOT") or "").strip()
    if attachments:
        roots.append(Path(attachments).expanduser().resolve(strict=False))
    else:
        kanban_home = (os.environ.get("HERMES_KANBAN_HOME") or "").strip() or str(_home() / ".hermes")
        roots.append((Path(kanban_home) / "kanban" / "attachments").resolve(strict=False))
    return roots


def _normalize(token: str) -> str:
    token = token.strip().strip("\"'")
    if not token:
        return ""
    for name in ("HERMES_KANBAN_WORKSPACE", "HERMES_KANBAN_WORKSPACES_ROOT", "TMPDIR",
                 "HERMES_HOME", "HOME"):
        value = (os.environ.get(name) or "").strip()
        if value:
            token = re.sub(r"\$\{?%s\}?" % name, value, token)
    if token.startswith("~"):
        token = os.path.expanduser("~") + token[1:]
    return token


def _is_allowed(path: str) -> bool:
    try:
        resolved = Path(path).resolve(strict=False)
    except (OSError, ValueError):
        return False
    return any(resolved == root or root in resolved.parents for root in allowed_roots())


def _write_protected(token: str) -> bool:
    """A file-write target is blocked only when it lands under the real Hermes
    home; plain writes anywhere else (user repos, other projects) stay allowed."""
    candidate = _normalize(token)
    if not candidate:
        return False
    try:
        resolved = Path(candidate).resolve(strict=False)
    except (OSError, ValueError):
        return False
    if _is_allowed(candidate):
        return False
    home = _home()
    hermes_tree = home / ".hermes"
    under_hermes = resolved == hermes_tree or hermes_tree in resolved.parents
    workspaces_root = (os.environ.get("HERMES_KANBAN_WORKSPACES_ROOT") or "").strip()
    under_workspaces = False
    if workspaces_root:
        root = Path(workspaces_root).expanduser().resolve(strict=False)
        under_workspaces = resolved == root or root in resolved.parents
    return under_hermes or under_workspaces


def _protected(token: str) -> bool:
    return _write_protected(token)


def _git_protected(token: str) -> bool:
    """A git mutation is blocked for every repository outside the allowed
    zones — including user repos outside ``~/.hermes`` — and allowed inside
    the own workspace clone, scratch or test homes."""
    candidate = _normalize(token)
    if not candidate:
        return False
    return not _is_allowed(candidate)


def is_worker() -> bool:
    if not (os.environ.get("HERMES_KANBAN_TASK") or "").strip():
        return False
    try:
        return bool(kanban_policy.configured_worker())
    except Exception:
        return True


def _segments(command: str, base_cwd: Optional[str] = None) -> list[tuple[str, str]]:
    """(cwd, segment) pairs; a bare ``cd <path>`` segment retargets the cwd the
    following segments resolve relative paths against. Newlines inside quotes
    stay inside the segment: a multi-line quoted payload (commit message,
    PR body) is prose, not pipeline syntax.

    *base_cwd* is the cwd the command actually starts in (the terminal tool's
    resolved ``workdir`` or recorded session cwd); when omitted, the task
    workspace stands in, as before.
    """
    cwd = base_cwd or "${HERMES_KANBAN_WORKSPACE}"
    pairs: list[tuple[str, str]] = []
    chunks = _outside_quote_split(command, r"\n|&&|;|\|\||\|(?!\|)")
    for raw in chunks:
        segment = raw.strip()
        if not segment:
            continue
        change = re.match(r"cd\s+(\S+)\s*$", segment)
        if change:
            resolved = _join(cwd, change.group(1))
            cwd = resolved or cwd
            continue
        pairs.append((cwd, segment))
    return pairs


def _outside_quote_split(command: str, pattern: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    quote = None
    index = 0
    while index < len(command):
        char = command[index]
        if quote:
            if char == "\\" and quote == '"':
                current.append(command[index:index + 2])
                index += 2
                continue
            if char == quote:
                quote = None
            current.append(char)
            index += 1
            continue
        if char in ("'", '"'):
            quote = char
            current.append(char)
            index += 1
            continue
        match = re.match(pattern, command[index:])
        if match:
            parts.append("".join(current))
            current = []
            index += len(match.group(0))
            continue
        current.append(char)
        index += 1
    parts.append("".join(current))
    return parts


def _tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment)
    except ValueError:
        return segment.split()


def _join(cwd: str, dest: str) -> str:
    dest = _normalize(dest)
    if not dest or dest.startswith("/"):
        return dest
    if dest.startswith("$") and not dest.startswith("$HERMES_KANBAN_WORKSPACE"):
        return dest
    base = _normalize(cwd)
    if base:
        return base.rstrip("/") + "/" + dest
    return dest


def _write_targets(cwd: str, segment: str) -> list[str]:
    targets: list[str] = []
    unquoted = re.sub(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"", "''", segment)
    for match in _REDIRECT.finditer(unquoted):
        if _FD_REDIRECT.match(match.group(1)):
            continue
        targets.append(_join(cwd, match.group(1)))
    for match in re.finditer(r"\btee\b(?:\s+-\S+)*\s+(\S+)", unquoted):
        targets.append(_join(cwd, match.group(1)))
    tokens = _tokens(segment)
    for index, head in enumerate(tokens):
        if head.lower() in _WRITE_HEADS:
            operands = [dest for dest in tokens[index + 1:] if not dest.startswith("-")]
            if head.lower() in ("rm", "rmdir", "mkdir", "touch"):
                for dest in operands:
                    targets.append(_join(cwd, dest))
            elif operands:
                targets.append(_join(cwd, operands[-1]))
            break
        if head == "sed":
            if not _SED_IN_PLACE.search(" ".join(tokens)):
                break
            for dest in tokens[index + 2:]:
                if not dest.startswith("-"):
                    targets.append(_join(cwd, dest))
            break
    return targets


def _pipe_targets(segment: str) -> list[str]:
    return []


def _py_paths(code: str) -> list[str]:
    starts: list[int] = []
    for pattern in (_PY_CALL, _PY_WRITE_CALL):
        for match in pattern.finditer(code):
            starts.append(match.start())
    if not starts:
        return []
    return _py_literals(code, starts)


def _py_literals(code: str, starts: list[int]) -> list[str]:
    literals = []
    for start in starts:
        depth = 1
        body: list[str] = []
        for char in code[start:start + 200]:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    break
            body.append(char)
        text = "".join(body)
        if "expanduser(" in text:
            text = text[text.index("expanduser(") + len("expanduser("):]
        for _, literal in _PY_STRINGS.findall(text):
            literals.append(literal)
    return literals


def _heredoc_bodies(command: str) -> list[str]:
    bodies: list[str] = []
    lines = command.split("\n")
    delim = None
    body: list[str] = []
    for line in lines:
        if delim is not None:
            if line.strip() == delim:
                if body:
                    bodies.append("\n".join(body))
                delim = None
                body = []
            else:
                body.append(line)
            continue
        match = _HEREDOC.search(line)
        if match:
            delim = match.group(1)
    return bodies


def _script_bodies(command: str) -> list[str]:
    bodies = _heredoc_bodies(command)
    bodies.extend(body for _, body in re.findall(r"-c\s+(['\"])(.*?)\1", command, re.S))
    return bodies


def _git_message_for(cwd: str, segment: str) -> Optional[str]:
    for match in _GIT_VERBS.finditer(segment):
        if _READ_ONLY_GIT.match((match.group("verb") + " " + match.group("rest")).strip()):
            continue
        verb = match.group("verb")
        gitdir = match.group("gitdir")
        if gitdir:
            repo = _join(cwd, gitdir)
        elif verb == "clone":
            operands = [token for token in _tokens(segment) if not token.startswith("-")]
            positions = [i for i, token in enumerate(operands) if token == "clone"]
            index = positions[0] if positions else 1
            rest = operands[index + 1:]
            repo = _join(cwd, rest[-1]) if len(rest) >= 2 else _join(cwd, ".")
        else:
            repo = _normalize(cwd)
        if repo and _git_protected(repo):
            return _GIT_MESSAGE.format(tool="terminal")
    return None


def terminal_violations(command: str) -> list[str]:
    blanked = _strip_heredoc_bodies(command)
    if _XARGS_RM.search(blanked):
        for match in _XARGS_RM.finditer(blanked):
            pipe_stages = re.split(r"\||&&|;|\n", blanked[: match.start()])
            tail_string = (pipe_stages[-2] if len(pipe_stages) > 1 else pipe_stages[-1]).strip()
            for token in _tokens(tail_string):
                if token in ("echo", "printf") or token.startswith("-"):
                    continue
                if _protected(_join(" ", token)):
                    return [_MESSAGE.format(tool="terminal")]
    for cwd, segment in _segments(_strip_heredoc_bodies(command)):
        for target in _write_targets(cwd, segment) + _pipe_targets(segment):
            if target and _protected(target):
                return [_MESSAGE.format(tool="terminal")]
        message = _git_message_for(cwd, segment)
        if message:
            return [message]
    for code in _script_bodies(command):
        for literal in _py_paths(code):
            if literal and _protected(literal):
                return [_MESSAGE.format(tool="terminal")]
    return []


def check(tool_name: str, args: Any) -> Optional[str]:
    if not is_worker() or not isinstance(args, dict):
        return None
    if tool_name in ("write_file", "patch"):
        path = str(args.get("path") or "")
        workspace = (os.environ.get("HERMES_KANBAN_WORKSPACE") or "").strip()
        if path and workspace and not path.startswith(("/", "~", "$")):
            path = workspace.rstrip("/") + "/" + path
        if _protected(path):
            return _MESSAGE.format(tool=tool_name)
        return None
    if tool_name != "terminal":
        return None
    found = terminal_violations(str(args.get("command") or ""))
    return found[0] if found else None
