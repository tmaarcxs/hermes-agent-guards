"""Block a worker's ``kanban_complete`` that does not account for every Acceptance item.

Active only in worker setups (``kanban.default_assignee`` set; see ``kanban_policy``) and only
in a worker process (``HERMES_KANBAN_TASK`` set). When the card body has an ``Acceptance``
heading followed by a numbered list, the completion metadata must carry ``acceptance``: a
mapping from every item number to the evidence that it is met. Missing items block the call.
A worker that cannot meet an item hands off with ``kanban_block`` (real constraint) instead of
completing. Reviewer completions are exempt (``metadata.review_verdict`` set or the run was
claimed from the review column).
"""

import os
import re
from typing import Any, Optional

from . import kanban_policy

_ACCEPTANCE = re.compile(r"(?im)^[#*\s]*acceptance\b[^\n]*\n((?:[ \t]*\n|[ \t]*\d+[.)][^\n]*\n?|[ \t]+[^\n]*\n?)*)")
_ITEM = re.compile(r"(?m)^[ \t]*(\d+)[.)][ \t]+\S")
MESSAGE = (
    "agent-guards blocked kanban_complete: the card's Acceptance list has items {missing} without evidence in "
    "metadata.acceptance. Map every item number to its concrete evidence, e.g. metadata.acceptance = "
    '{{"1": "...", "2": "..."}}. If an item cannot be met, do not complete: finish it, or kanban_block with the '
    "exact external constraint."
)
_UNMET = re.compile(r"(?i)\b(?:not yet|not met|pending (?:merge|review|des|deploy)|remains open)\b")
UNMET_MESSAGE = (
    "agent-guards blocked kanban_complete: the evidence for Acceptance items {unmet} says they are not met yet. "
    "Finish those items (merge, deploy, verify, close), or kanban_block with the exact external constraint. "
    "Completing hands the card off as done and the remaining steps are lost."
)


def acceptance_items(body: str) -> list[str]:
    match = _ACCEPTANCE.search(body or "")
    return list(dict.fromkeys(_ITEM.findall(match.group(1)))) if match else []


def _task(task_id: str) -> Optional[tuple[str, bool]]:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    conn = kbc.connect(board=None)
    try:
        task = kb.get_task(conn, task_id)
        if task is None:
            return None
        return str(task.body or ""), kb._retry_status_for_run(conn, task_id) == "review"
    finally:
        conn.close()


def missing_items(body: str, metadata: Any) -> list[str]:
    covered = metadata.get("acceptance") if isinstance(metadata, dict) else None
    covered = covered if isinstance(covered, dict) else {}
    evidenced = {str(key).strip() for key, value in covered.items() if str(value or "").strip()}
    return [item for item in acceptance_items(body) if item not in evidenced]


def unmet_items(body: str, metadata: Any) -> list[str]:
    covered = metadata.get("acceptance") if isinstance(metadata, dict) else None
    covered = {str(key).strip(): str(value or "") for key, value in covered.items()} if isinstance(covered, dict) else {}
    return [item for item in acceptance_items(body) if _UNMET.search(covered.get(item, ""))]


def check(tool_name: str, args: Any, load_task=_task) -> Optional[str]:
    if tool_name != "kanban_complete" or not isinstance(args, dict) or not kanban_policy.configured_worker():
        return None
    task_id = str(args.get("task_id") or os.environ.get("HERMES_KANBAN_TASK") or "")
    if not os.environ.get("HERMES_KANBAN_TASK") or not task_id:
        return None
    metadata = args.get("metadata")
    if isinstance(metadata, dict) and metadata.get("review_verdict"):
        return None
    task = load_task(task_id)
    if task is None or task[1]:
        return None
    missing = missing_items(task[0], metadata)
    if missing:
        return MESSAGE.format(missing=", ".join(missing))
    unmet = unmet_items(task[0], metadata)
    return UNMET_MESSAGE.format(unmet=", ".join(unmet)) if unmet else None
