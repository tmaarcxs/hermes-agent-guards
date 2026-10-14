"""Turn a worker's narrated stop into the promised Kanban lifecycle call.

Active only in worker setups (``kanban.default_assignee`` set; see ``kanban_policy``)
and only in a worker process (``HERMES_KANBAN_TASK`` set; the lead's own explanatory
turns must finish without the gate answering them).

The failure this closes: a worker whose terminal Kanban call was blocked (by any
guard or tool error) keeps gathering evidence across later turns, then ends with a
final text-only reply like "Building the todo list, then requesting review:".
Hermes treats that as a clean exit; because the BLOCKED ``kanban_request_review``
call is still in the transcript, the core stop-guard (`agent/kanban_stop.py`
``session_called_kanban_terminal``, which scans tool names, not outcomes) considers
the protocol satisfied and no nudge fires — the run ends rc=0 without ever handing
off, and the dispatcher records ``protocol_violation``.

This module rides the ``pre_verify`` hook (``hermes_cli/plugins.py``
``get_pre_verify_continue_message``: once per turn-end after code edits, bounded by
``agent.max_verify_nudges``). ``record`` runs on ``post_tool_call`` and keeps, per
session, whether a terminal Kanban call SUCCEEDED in this process. ``check`` runs on
``pre_verify``: when no terminal call succeeded, it returns a continue directive
telling the model to make the lifecycle call now instead of narrating it. The nudge
is also bounded by ``agent.max_verify_nudges`` (default 3 per turn), so a worker
cannot be held forever if it ignores it; after that the honest exit returns.

Hermes-side gap this cannot close (reported, not fixed here): runs that die on
"Response still truncated after 4 continuation attempts" exit non-zero only at the
Hermes transport/recovery layer; no plugin hook sees them.
"""

from collections import OrderedDict
from typing import Any, Optional

from . import kanban_policy

_TERMINAL_TOOLS = frozenset({
    "kanban_complete",
    "kanban_block",
    "kanban_schedule",
    "kanban_request_review",
    "kanban_request_changes",
})
_MAX_SESSIONS = 200

MESSAGE = (
    "[System: agent-guards. Your reply ended the turn with text only — that is not a terminal "
    "state for a Kanban worker; the core stop-guard counts the earlier BLOCKED lifecycle call as "
    "done, so no nudge will fire and the run exits cleanly as a protocol violation.]\n\n"
    "Do this now, in this same response, do not narrate it:\n"
    "1. Close the pre-review checklist requirement first if the review gate reported it "
    "(todo_list, one item per check, closed with its receipt).\n"
    "2. Then call the lifecycle tool for real: `kanban_complete(summary=..., artifacts=[...])` "
    "when the work is done, `kanban_request_review(summary=...)` when a code change needs "
    "independent review, or `kanban_block(reason=...)` when you are blocked.\n"
    "If the tool is blocked again, read the error, fix the named cause, and retry the call — "
    "never end the turn on a promise to call it later.]"
)

_handed_off: "OrderedDict[str, bool]" = OrderedDict()


def reset() -> None:
    """Clear per-session state (used by the test suite)."""
    _handed_off.clear()


def record(tool_name: str = "", status: str = "", error_message: Optional[str] = None, **kwargs) -> None:
    """``post_tool_call``: mark the session handed off only when a terminal Kanban call succeeded."""
    if tool_name not in _TERMINAL_TOOLS:
        return
    if status != "ok":
        return
    session = str(kwargs.get("session_id") or kwargs.get("task_id") or "")
    if not session:
        return
    _handed_off[session] = True
    _handed_off.move_to_end(session)
    while len(_handed_off) > _MAX_SESSIONS:
        _handed_off.popitem(last=False)


def check(**kwargs) -> Optional[dict[str, str]]:
    """``pre_verify``: keep the turn going when no lifecycle call has succeeded in this process.

    Fires only in a worker setup AND a worker process; the lead's chat replies must finish.
    The observed failure shape is a reply PROMISING a lifecycle call ("Building the todo list,
    then requesting review:"); any final reply while the handoff is still owed gets the nudge.
    Scope keeps this safe: worker process only, and the reply becomes an interim row on the
    way to the real call.
    """
    from os import getenv

    if not kanban_policy.configured_worker():
        return None
    if not (getenv("HERMES_KANBAN_TASK") or "").strip():
        return None
    session = str(kwargs.get("session_id") or kwargs.get("task_id") or "")
    if session and _handed_off.get(session):
        return None
    return {"action": "continue", "message": MESSAGE}


__all__ = ["MESSAGE", "check", "record", "reset"]
