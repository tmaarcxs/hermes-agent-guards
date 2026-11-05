"""Block recursive searches over the whole Hermes home or its bulk directories.

``~/.hermes`` holds about 2.5 million files (``installs`` and ``benchmarks`` alone hold more than
1.4 million). A recursive ``find``/``grep -r``/``rg`` over the root, ``installs`` or
``benchmarks`` runs for minutes and often hits the 420 s tool limit (spa#578 lost 7 minutes to one
``find ~/.hermes -name "*sonar*"``; 14 days of worker sessions lost about 10.8 h). Searches inside
``skills``, ``memories``, ``plugins``, ``profiles/<name>``, ``kanban`` and so on stay allowed, as does
``find`` with ``-maxdepth`` of 2 or less.

Every profile. Matches at command-word position outside quoted strings and heredocs. Fails open.
"""

import os
import re
from typing import Any, Optional

from . import hermes_home_gate, terminal_policy

_SEARCH = re.compile(terminal_policy._START + r"(?P<tool>find|grep|egrep|fgrep|rg|ag|fd)\b")
_SEGMENT_END = re.compile(r"[;|&\n)]")
_MAXDEPTH = re.compile(r"-maxdepth\s+[012]\b")
_HOME_PREFIX = re.compile(r"^(?:~|\$HOME|\$\{HOME\})(?=/|$)")

MESSAGE = (
    "agent-guards blocked terminal: a recursive search over the whole Hermes home, installs or "
    "benchmarks takes minutes (about 2.5 million files) and usually hits the tool time limit. "
    "Search the directory that holds what you need (skills, memories, plugins, profiles/<name>, "
    "kanban, hermes-agent), use search_files, or recall with hindsight_recall."
)


def _heavy() -> set:
    home = os.path.expanduser("~")
    root = os.path.join(home, ".hermes")
    return {root, os.path.join(root, "installs"), os.path.join(root, "benchmarks")}


def _recursive(tool: str, flags: list) -> bool:
    if tool in ("find", "rg", "ag", "fd"):
        return True
    return any(f == "--recursive" or (not f.startswith("--") and re.search(r"[rR]", f)) for f in flags)


def check(tool_name: str, args: Any) -> Optional[str]:
    if tool_name != "terminal" or not isinstance(args, dict):
        return None
    command = args.get("command")
    if not isinstance(command, str) or ".hermes" not in command and "$HOME" not in command and "~" not in command:
        return None
    command = hermes_home_gate._strip_heredocs(command)
    masked = terminal_policy._QUOTED.sub(lambda m: m.group()[0] + "x" * (len(m.group()) - 2) + m.group()[-1], command)
    heavy = _heavy()
    home = os.path.expanduser("~")
    for match in _SEARCH.finditer(masked):
        end = _SEGMENT_END.search(masked, match.end())
        segment = command[match.end(): end.start() if end else len(command)]
        tokens = [t.strip("'\"") for t in segment.split()]
        tool = match.group("tool")
        if not _recursive(tool, [t for t in tokens if t.startswith("-")]):
            continue
        if tool == "find" and _MAXDEPTH.search(segment):
            continue
        for token in tokens:
            if token.startswith("-"):
                continue
            path = _HOME_PREFIX.sub(home, token).rstrip("/")
            if path in heavy:
                return MESSAGE
    return None
