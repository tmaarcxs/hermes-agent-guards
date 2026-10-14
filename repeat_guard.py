"""Block an identical tool call after it has already failed twice with nothing changed.

``record`` runs on ``post_tool_call`` and ``check`` on ``pre_tool_call``. A call is
identified by session, tool name and canonical JSON arguments. After two
non-transient failures of the same call, the next identical call is blocked
until something can have changed:
- the same call succeeds;
- any state-changing tool succeeds (it clears every recorded failure of the session).

Not tracked: terminal, execute_code and process_manage, whose failing output is
often normal work (red tests, empty greps, finished processes). Transient
failures (timeouts, rate limits, 5xx, ``No such process``) are never counted, so
they can always be retried.
"""

import json
import re
from collections import OrderedDict
from typing import Any, Optional

from . import kanban_policy

FAILURES_BEFORE_BLOCK = 2
_MAX_SESSIONS = 200
_UNTRACKED = frozenset({"terminal", "execute_code", "process_manage"})
_STATE_CHANGING = frozenset({
    "terminal", "execute_code", "write_file", "patch", "skill_manage", "memory", "todo_list",
    "kanban_create", "kanban_complete", "kanban_block", "kanban_unblock", "kanban_comment",
    "kanban_request_review", "kanban_link", "process_manage", "tool_call",
})
_TRANSIENT = re.compile(
    r"timed? ?out|timeout|rate.?limit|\b429\b|\b5\d\d\b|temporar|unavailable|connection (?:reset|refused|error)"
    r"|No such process",
    re.IGNORECASE,
)

# Lifecycle calls the ledger may block: when one of these is blocked, the run must not end
# on the block text (the core stop-guard counts the mere presence of the call as terminal).
_TERMINAL_TOOLS = frozenset({
    "kanban_complete", "kanban_block", "kanban_schedule", "kanban_request_review", "kanban_request_changes",
})

_failures: "OrderedDict[str, dict[str, tuple[int, str]]]" = OrderedDict()


def _session(kwargs: dict) -> str:
    return str(kwargs.get("session_id") or kwargs.get("task_id") or "")


def _call_key(tool_name: str, args: Any) -> str:
    try:
        return tool_name + " " + json.dumps(args or {}, sort_keys=True, default=str)
    except Exception:
        return tool_name + " " + repr(args)


def _session_failures(session: str) -> dict[str, tuple[int, str]]:
    failures = _failures.setdefault(session, {})
    _failures.move_to_end(session)
    while len(_failures) > _MAX_SESSIONS:
        _failures.popitem(last=False)
    return failures


def record(tool_name: str = "", args: Any = None, result: Any = None, status: str = "",
           error_message: Optional[str] = None, **kwargs: Any) -> None:
    failures = _session_failures(_session(kwargs))
    key = _call_key(tool_name, args)
    if status != "error":
        failures.pop(key, None)
        if tool_name in _STATE_CHANGING:
            failures.clear()
        return
    if tool_name in _UNTRACKED:
        return
    error = str(error_message or "") or str(result or "")[:400]
    if _TRANSIENT.search(error):
        return
    count, _ = failures.get(key, (0, ""))
    failures[key] = (count + 1, error[:200])


def check(tool_name: str = "", args: Any = None, **kwargs: Any) -> Optional[str]:
    session = _session(kwargs)
    if tool_name in _UNTRACKED or session not in _failures:
        return None
    count, error = _failures[session].get(_call_key(tool_name, args), (0, ""))
    if count < FAILURES_BEFORE_BLOCK:
        return None
    message = (
        f"agent-guards blocked {tool_name}: this exact call already failed {count} times in this session "
        f"with: {error}. Repeating it unchanged will fail again. Change the arguments, fix the cause first, "
        "or take a different approach."
    )
    if tool_name in _TERMINAL_TOOLS:
        # A ledger-blocked lifecycle call counts as terminal for the core stop-guard
        # (transcript tool-name scan), so the run must not end on the block text alone.
        message += " " + kanban_policy.RETRY_INSTRUCTION
    return message


def reset() -> None:
    _failures.clear()
