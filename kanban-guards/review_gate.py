"""Block ``kanban_request_review`` until the pre-review checks are closed with receipts in the task list.

Active only in worker setups (``kanban.default_assignee`` set; see ``kanban_policy``).

``record`` runs on ``post_tool_call`` and keeps the latest ``todo_list`` result of each
session. ``check`` runs on ``pre_tool_call``: a review request passes only when that list
has one item per check from ``delivery-verification`` and each one is closed — completed
with a receipt (a receipt path, file reference, hash, exit code or count — anything that
survives the session, unlike a bare "done"), or cancelled with ``N/A: <reason>`` in its
text. A bare completed item like ``Contract coverage`` with no receipt does not pass.

Monotone-progress credit: while the session's open-check set is a proper subset of the
open-check set blocked at its last request, a request passes without recording a new
block (the baseline keeps the last blocked set); any set that is not a proper subset —
equal, larger, or disjoint, i.e. one that gains a check the baseline didn't have —
blocks again and re-arms the baseline. This mirrors handoff_gate's one-bounce escape —
the first request per state change still pays its block, but the gate stops charging
re-tries that demonstrably converge on the last blocked set instead of wandering.
"""

import json
import re
from collections import OrderedDict
from typing import Any, Optional

from . import kanban_policy

CHECKS = (
    "Contract coverage", "Failure lifecycle", "Model evidence", "Claims within evidence", "Real boundary", "Stale siblings",
    "Self reproduction", "Claims anchored",
)
RECEIPT = re.compile(
    r"\breceipts?\b"                                                   # the word itself
    r"|\b\w[\w./-]*:\d{1,6}\b"                                         # file:line references
    r"|\b[0-9a-fA-F]{7,64}\b"                                          # commit or content hashes
    r"|\b\d+[.,]\d+\b"                                                 # decimal results ("609.5")
    r"|\b\d+/\d+\b"                                                    # ratio counts ("47/47", "8/8")
    r"|\b\d+\s*\+\s*\d+\b"                                             # N+M test counts ("608+1")
    r"|\bPR\s*#\d+\b"                                                  # pull-request references
    r"|\bRED\s*(?:->|=>|\u2192|to)\s*GREEN\b"                          # red-to-green proof
    r"|\bexit\b|\brc\b|exit[=:]\s*\d+"                                 # exit codes / rc values
    r"|\b\d+\s+(?:passed|failed|error|errors)\b"                       # test-summary counts
    r"|\b[\w./-]+\.(?:md|txt|log|json|csv|py|yaml|yml|xml|html)\b",    # artifact paths
    re.I,
)
_MAX_SESSIONS = 200

_todos: "OrderedDict[str, list[dict[str, Any]]]" = OrderedDict()
_blocked: "OrderedDict[str, frozenset[str]]" = OrderedDict()


def _session(kwargs: dict) -> str:
    return str(kwargs.get("session_id") or kwargs.get("task_id") or "")


def record(tool_name: str = "", result: Any = None, **kwargs) -> None:
    if tool_name != "todo_list":
        return
    todos = json.loads(result).get("todos") if isinstance(result, str) else None
    if not isinstance(todos, list):
        return
    session = _session(kwargs)
    _todos[session] = todos
    _todos.move_to_end(session)
    while len(_todos) > _MAX_SESSIONS:
        _todos.popitem(last=False)


def _closed(item: dict) -> bool:
    """Completed items need a receipt in their text; cancelled items need an N/A reason."""
    status = item.get("status")
    if status == "completed":
        return bool(RECEIPT.search(str(item.get("content", ""))))
    return status == "cancelled" and "N/A:" in str(item.get("content", ""))


def check(tool_name: str, **kwargs) -> Optional[str]:
    if tool_name != "kanban_request_review" or not kanban_policy.configured_worker():
        return None
    session = _session(kwargs)
    todos = _todos.get(session, [])
    open_checks = [
        check_name
        for check_name in CHECKS
        if not any(check_name.lower() in str(item.get("content", "")).lower() and _closed(item) for item in todos)
    ]
    if not open_checks:
        _blocked.pop(session, None)
        return None
    blocked = _blocked.get(session)
    blocked_set = blocked if blocked is not None else frozenset()
    open_set = frozenset(open_checks)
    if open_set < blocked_set:
        return None
    _blocked[session] = open_set
    _blocked.move_to_end(session)
    while len(_blocked) > _MAX_SESSIONS:
        _blocked.popitem(last=False)
    return (
        "agent-guards blocked kanban_request_review: the pre-review checks are not closed in todo_list: "
        + ", ".join(open_checks)
        + ". Add one todo_list item per check from delivery-verification ('Before requesting review'), named "
        "exactly as there, and close each: status completed after the check passed with its receipt, or status "
        "cancelled with 'N/A: <reason>' in the item text. Then request review again — "
        + kanban_policy.RETRY_INSTRUCTION
    )


def reset() -> None:
    _todos.clear()
    _blocked.clear()
