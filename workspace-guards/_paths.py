"""Path and command-segment helpers shared by ``home_litter_gate``.

Extracted from the removed ``workspace_write_gate`` so the home-directory gate keeps working.
"""

import os
import re
import shlex
from pathlib import Path
from typing import Optional

from . import _policy

_HEREDOC = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?")


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


def is_worker() -> bool:
    if not (os.environ.get("HERMES_KANBAN_TASK") or "").strip():
        return False
    try:
        return bool(_policy.configured_worker())
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
