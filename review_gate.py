"""Block ``kanban_request_review`` until the pre-review checks are closed with receipts in the task list.

Active only in worker setups (``kanban.default_assignee`` set; see ``kanban_policy``).

``record`` runs on ``post_tool_call`` and keeps the latest ``todo_list`` result of each
session. ``check`` runs on ``pre_tool_call``: a review request passes only when that list
has one item per check from ``delivery-verification`` and each one is closed — completed
with a receipt (a receipt path, file reference, hash, exit code or count — anything that
survives the session, unlike a bare "done"), or cancelled with ``N/A: <reason>`` in its
text. A bare completed item like ``Contract coverage`` with no receipt does not pass.

Monotone-progress credit: while the session's open-check set is strictly smaller than at
its last blocked request, a request passes without recording a new block (the baseline
stays at the last blocked size); an equal or larger open set blocks again and re-arms the
baseline. This mirrors handoff_gate's one-bounce escape — the first request per state
change still pays its block, but the gate stops charging re-tries that are visibly
converging instead of looping on the same set.
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
    r"|\bexit\b|\brc\b|exit[=:]\s*\d+"                                 # exit codes / rc values
    r"|\b\d+\s+(?:passed|failed|error|errors)\b"                       # test-summary counts
    r"|\b[\w./-]+\.(?:md|txt|log|json|csv|py|yaml|yml|xml|html)\b",    # artifact paths
    re.I,
)
_MAX_SESSIONS = 200

_todos: "OrderedDict[str, list[dict[str, Any]]]" = OrderedDict()
_blocked: "OrderedDict[str, tuple[int, int]]" = OrderedDict()


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
        blocked = _blocked.get(session)
        if blocked is not None and blocked[1]:
            _blocked[session] = (blocked[0], 0)
        return None
    blocked = _blocked.get(session)
    if blocked is not None:
        blocked_at, last_seen = blocked
        if len(open_checks) < blocked_at and len(open_checks) <= last_seen:
            _blocked[session] = (blocked_at, len(open_checks))
            return None
    _blocked[session] = (len(open_checks), len(open_checks))
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
