"""Terminal command policy for shell-guards: the sleep/merge/osascript/force-push/gh-auth/home-delete blocks, the inline-attach cap and the argument normalization.

Behaviour-identical move of the terminal block, ``normalized_args`` and the
inline-attach cap from the old monolithic ``agent-guards`` root ``__init__.py``,
plus the ``gh auth`` shared-account block (see ``SPLIT_EQUIVALENCE.md`` §10).
"""

import re
from typing import Any, Optional

from . import hermes_home_gate

_START = r"(?:^|[;&|\n(`{]|\$\(|\bthen\b|\bdo\b|\belse\b)\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*(?:sudo\s+|command\s+)?"
_SEGMENT_END = re.compile(r"[;\n]|&&|\|\|")
_QUOTED = re.compile(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"")
_LOOP = re.compile(r"\b(?:for|while|until)\b")
_SLEEP = re.compile(_START + r"sleep\s+(\d+(?:\.\d+)?)([smh]?)\b")
_MERGE = re.compile(_START + r"gh\s+pr\s+merge\b")
_OSASCRIPT = re.compile(_START + r"osascript\b")
_PUSH = re.compile(_START + r"git\s+push\b")
_BARE_FORCE = re.compile(r"\s(?:--force|-f)(?=\s|$)")
_GH_AUTH = re.compile(
    _START
    + r"(?:env\s+(?:[-\w]+(?:=\S*)?\s+)*)?(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*"
    + r"gh\s+auth\s+(switch|login|logout|refresh)\b"
)
_SHELL_C = re.compile(r"(?:bash|sh)\s+-c\s+")

_UNIT_SECONDS = {"": 1, "s": 1, "m": 60, "h": 3600}
_MAX_FOREGROUND_SLEEP = 30
_MAX_LOOP_SLEEP = 15
_EMPTY_TO_ABSENT = {"kanban_list": ("assignee", "status", "tenant"), "kanban_create": ("completion_contract",)}
_ENV_TASK_LITERAL = re.compile(r"^\s*\$?\{?HERMES_KANBAN_TASK\}?\s*$")
_MAX_INLINE_ATTACH = 256
_INLINE_ATTACH_MESSAGE = (
    "agent-guards blocked kanban_attach: inline base64 over 256 characters arrives "
    "corrupted; attach by path: artifacts=[absolute path], then sha256-compare the stored file."
)


def attach_cap_block(args: Optional[dict]) -> Optional[str]:
    """The inline-attach cap message for a ``kanban_attach`` call, or ``None``."""
    if args is not None and len(str(args.get("content_base64") or "")) > _MAX_INLINE_ATTACH:
        return _INLINE_ATTACH_MESSAGE
    return None


def _segments(command: str, pattern: re.Pattern) -> list[str]:
    found = []
    for match in pattern.finditer(command):
        end = _SEGMENT_END.search(command, match.end())
        found.append(command[match.start() : end.start() if end else len(command)])
    return found


_GH_AUTH_MESSAGE = (
    "gh auth switch (also login/logout/refresh) is not allowed: it changes the shared "
    "active account for every Hermes process. To act as another account, set it in the "
    "command itself: GH_TOKEN=$(gh auth token -u <user>) <command>. The default active "
    "account is acme-bot."
)


def _quoted_spans(text: str) -> list[tuple[int, int]]:
    return [(match.start(), match.end()) for match in _QUOTED.finditer(text)]


def _shell_c_argument(text: str, start: int) -> Optional[str]:
    """The quoted argument of a top-level ``bash -c``/``sh -c``, or ``None``.

    Only the quoted form is a real spelling: an unquoted ``bash -c gh auth switch``
    runs ``gh`` with ``auth``/``switch`` as ``$0``/``$1``, not the gh subcommand.
    """
    quote = text[start] if start < len(text) and text[start] in ("'", '"') else None
    if quote is None:
        return None
    end = text.find(quote, start + 1)
    while end > 0 and quote == '"' and text[end - 1] == "\\":
        end = text.find(quote, end + 1)
    return text[start + 1 : end] if end > 0 else text[start + 1 :]


def _gh_auth_blocked(blanked: str, source: str) -> bool:
    """A ``gh auth`` account-mutating verb at command-word position, or inside a
    quoted ``bash -c``/``sh -c`` argument.

    ``blanked`` has quoted content removed, so prose in ``echo``/``grep`` and a
    ``gh auth`` mention inside any string stay exempt; a ``bash -c``/``sh -c``
    argument is re-scanned from the original text because the blanking removes it.
    ``python -c`` bodies and heredoc bodies stay exempt, exactly as for the other
    terminal blocks.
    """
    if _GH_AUTH.search(blanked):
        return True
    spans = _quoted_spans(source)
    for match in _SHELL_C.finditer(source):
        if any(start <= match.start() < end for start, end in spans):
            continue
        inner = _shell_c_argument(source, match.end())
        if inner is not None and _GH_AUTH.search(_QUOTED.sub("''", inner)):
            return True
    return False


def terminal_violations(command: str, background: bool) -> list[str]:
    found = []
    home = hermes_home_gate.hermes_home_violations(command)
    if home:
        found.extend(home)
    source = hermes_home_gate._strip_heredocs(command)
    command = _QUOTED.sub("''", source)
    if not background:
        for match in _SLEEP.finditer(command):
            seconds = float(match.group(1)) * _UNIT_SECONDS[match.group(2)]
            if seconds >= _MAX_FOREGROUND_SLEEP:
                found.append(
                    "foreground sleep >= 30s: run the wait with background=true and notify=true, "
                    "or poll once now and re-issue without sleeping"
                )
                break
        if not found:
            loop_starts = [m.start() for m in _LOOP.finditer(command)]
            if loop_starts:
                for match in _SLEEP.finditer(command):
                    seconds = float(match.group(1)) * _UNIT_SECONDS[match.group(2)]
                    if seconds >= _MAX_LOOP_SLEEP and any(start < match.start() for start in loop_starts):
                        found.append(
                            "polling loop with sleep >= 15s: run the whole wait with background=true "
                            "and notify=[<ready pattern>], or poll once without sleeping"
                        )
                        break
    if any("--match-head-commit" not in segment for segment in _segments(command, _MERGE)):
        found.append("gh pr merge needs --match-head-commit <verified head sha>")
    if _OSASCRIPT.search(command):
        found.append("osascript is not allowed: use ego-browser for browser work")
    if any(_BARE_FORCE.search(segment) for segment in _segments(command, _PUSH)):
        found.append("git push --force/-f is not allowed: use --force-with-lease")
    if _gh_auth_blocked(command, source):
        found.append(_GH_AUTH_MESSAGE)
    return found


def _tool_deadline() -> Optional[float]:
    """Current per-call deadline, resolved the way the host resolves it (config, then env, then 420)."""
    try:
        from agent.deadline import resolve_timeout
    except Exception:
        return None
    try:
        return resolve_timeout("tools.sequential_call", default=None) or resolve_timeout(
            "tools.concurrent_batch", default=420.0, env_var="HERMES_CONCURRENT_TOOL_TIMEOUT_S"
        )
    except Exception:
        return None


def _wait_cap() -> Optional[int]:
    """A ``process_manage wait`` at or above the call deadline races it and dies without partial output.

    Measured over 7 days: ``timeout=420`` errored 649 times out of 1,275 reads, ``timeout=400`` never
    (787 reads). Keep a 5 s margin under the deadline so the tool's own timeout returns partial output.
    """
    deadline = _tool_deadline()
    if not deadline or deadline <= 6:
        return None
    return int(deadline) - 5


def normalized_args(tool_name: str, args: dict[str, Any]) -> Optional[dict[str, Any]]:
    changes: dict[str, Any] = {key: None for key in _EMPTY_TO_ABSENT.get(tool_name, ()) if args.get(key) == ""}
    if tool_name.startswith("kanban_") and _ENV_TASK_LITERAL.match(str(args.get("task_id") or "")):
        changes["task_id"] = None
    if tool_name == "process_manage" and args.get("action") == "wait":
        timeout = args.get("timeout")
        cap = _wait_cap()
        if cap and isinstance(timeout, int) and not isinstance(timeout, bool) and timeout > cap:
            changes["timeout"] = cap
    if tool_name == "skill_view":
        name = args.get("name")
        if isinstance(name, str) and ":" in name:
            prefix, _, rest = name.partition(":")
            if prefix == rest:
                changes["name"] = rest
    return changes or None
