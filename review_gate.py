"""Block ``kanban_request_review`` until the six pre-review checks are closed in the task list.

Active only in worker setups (``kanban.default_assignee`` set; see ``kanban_policy``).

``record`` runs on ``post_tool_call`` and keeps the latest ``todo_list`` result of each
session. ``check`` runs on ``pre_tool_call``: a review request passes only when that list
has one item per check from ``delivery-verification`` and each one is completed, or
cancelled with ``N/A: <reason>`` in its text.
"""

import json
from collections import OrderedDict
from typing import Any, Optional

from . import kanban_policy

CHECKS = (
    "Contract coverage", "Failure lifecycle", "Model evidence", "Claims within evidence", "Real boundary", "Stale siblings",
)
_MAX_SESSIONS = 200

_todos: "OrderedDict[str, list[dict[str, Any]]]" = OrderedDict()


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
    status = item.get("status")
    return status == "completed" or (status == "cancelled" and "N/A:" in str(item.get("content", "")))


def check(tool_name: str, **kwargs) -> Optional[str]:
    if tool_name != "kanban_request_review" or not kanban_policy.configured_worker():
        return None
    todos = _todos.get(_session(kwargs), [])
    open_checks = [
        check_name
        for check_name in CHECKS
        if not any(check_name.lower() in str(item.get("content", "")).lower() and _closed(item) for item in todos)
    ]
    if not open_checks:
        return None
    return (
        "agent-guards blocked kanban_request_review: the pre-review checks are not closed in todo_list: "
        + ", ".join(open_checks)
        + ". Add one todo_list item per check from delivery-verification ('Before requesting review'), named "
        "exactly as there, and close each: status completed after the check passed with its receipt, or status "
        "cancelled with 'N/A: <reason>' in the item text. Then request review again."
    )


def reset() -> None:
    _todos.clear()
