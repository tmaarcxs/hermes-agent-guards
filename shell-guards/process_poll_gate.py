"""Keep every wait on a background process event-driven.

A ``process_manage`` ``poll`` is a status snapshot that costs a full model turn and returns
nothing the model can act on: ``wait`` already returns the moment the process exits, ``log``
reads the output, and a ``notify=true`` job delivers its completion as a message. ``check``
blocks ``poll``. ``fix`` lifts a ``wait`` whose ``timeout`` is shorter than the configured
limit up to that limit, because a short timeout turns ``wait`` into a polling cadence.

Worker setups only (``kanban.default_assignee`` set). Fails open.
"""

import os
from typing import Any, Optional

from . import _policy

MESSAGE = (
    "agent-guards blocked process_manage: poll is not allowed - it costs a full turn and returns nothing "
    "to act on. For the same process now: process_manage(action=\"wait\", session_id=...) returns the "
    "moment it exits, process_manage(action=\"log\", session_id=...) reads its output, and a job was "
    "launched with terminal(background=true, notify=true) sends its completion as a message. If the wait "
    "comes back interrupted with no new output, end the turn and re-issue the wait in a later turn - "
    "never poll, never sleep."
)


def _max_wait() -> int:
    try:
        return int(os.getenv("TERMINAL_TIMEOUT", "180"))
    except (ValueError, TypeError):
        return 180


def check(tool_name: str, args: Any) -> Optional[str]:
    if tool_name != "process_manage" or not isinstance(args, dict) or args.get("action") != "poll":
        return None
    return MESSAGE if _policy.configured_worker() else None


def fix(tool_name: str, args: Any) -> Optional[dict]:
    """The args change that makes a ``wait`` run to the process exit instead of a short window."""
    if tool_name != "process_manage" or not isinstance(args, dict) or args.get("action") != "wait":
        return None
    timeout = args.get("timeout")
    limit = _max_wait()
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout >= limit:
        return None
    return {"timeout": limit} if _policy.configured_worker() else None
