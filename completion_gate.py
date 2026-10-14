"""Block a worker's ``kanban_complete`` that does not account for every Acceptance item.

Active only in worker setups (``kanban.default_assignee`` set; see ``kanban_policy``) and only
in a worker process (``HERMES_KANBAN_TASK`` set). When the card body has an ``Acceptance``
heading followed by a numbered list, the completion metadata must carry ``acceptance``: a
mapping from every item number to the evidence that it is met. Missing items block the call.
A worker that cannot meet an item hands off with ``kanban_block`` (real constraint) instead of
completing.

Reviewer completions (``metadata.review_verdict`` set, or the run was claimed from the review
column) are blocked, not exempt, when the remaining unevidenced Acceptance items name
post-review steps (merge, deploy, DES, close): completing would close the card with those
steps never run. The approval must instead hand the card back to the implementer with the
verdict recorded, through ``kanban_request_changes`` — the supported return path that closes
the reviewer run and restores the implementer. Cards whose Acceptance is only a review
verdict, or whose post-review items are already evidenced, complete normally.
"""

import os
import re
from typing import Any, Optional

from . import kanban_policy

_ACCEPTANCE = re.compile(r"(?im)^[#*\s]*acceptance\b[^\n]*\n((?:[ \t]*\n|[ \t]*\d+[.)][^\n]*\n?|[ \t]+[^\n]*\n?)*)")
_ITEM = re.compile(r"(?m)^[ \t]*(\d+)[.)][ \t]+(\S.*)$")
_POST_REVIEW = re.compile(
    r"(?i)\b(?:merge[ds]?|merging|deploy(?:ment|ed)?s?|\bdes\b|clos(?:e|ed|es|ing|ure))\b"
)
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
REVIEWER_MESSAGE = (
    "agent-guards blocked kanban_complete: this reviewer approval would close the card, but Acceptance items "
    "{pending} are post-review steps (merge, deploy, DES, close) with no evidence, so completing now loses them "
    "(a completion from review marks the card done — there is no approved-and-returned state). Record the verdict "
    "as APPROVED and hand the card back to the implementer instead: call kanban_request_changes with reason "
    "'APPROVED — <verdict summary>; returning the card to the implementer for the remaining Acceptance items'. "
    "If those items are genuinely already evidenced, retry kanban_complete with the full metadata.acceptance map. "
)


def _metadata(args: Any) -> dict:
    """The runtime delivers ``metadata`` to pre_tool_call hooks as a JSON string
    (see ``tools/kanban_tools.py`` handler contracts); accept both shapes."""
    metadata = args.get("metadata") if isinstance(args, dict) else None
    if isinstance(metadata, str):
        import json

        try:
            metadata = json.loads(metadata)
        except Exception:
            metadata = None
    return metadata if isinstance(metadata, dict) else {}


def acceptance_item_texts(body: str) -> list[tuple[str, str]]:
    match = _ACCEPTANCE.search(body or "")
    if not match:
        return []
    found: dict[str, str] = {}
    for number, text in _ITEM.findall(match.group(1)):
        found.setdefault(number, text)
    return list(found.items())


def acceptance_items(body: str) -> list[str]:
    return [number for number, _ in acceptance_item_texts(body)]


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


def pending_post_review_items(body: str, metadata: Any) -> list[str]:
    """Unevidenced Acceptance items whose text names a post-review step."""
    covered = metadata.get("acceptance") if isinstance(metadata, dict) else None
    covered = {str(key).strip() for key, value in covered.items() if str(value or "").strip()} if isinstance(covered, dict) else set()
    return [
        number for number, text in acceptance_item_texts(body)
        if number not in covered and _POST_REVIEW.search(text)
    ]


def check(tool_name: str, args: Any, load_task=_task) -> Optional[str]:
    if tool_name != "kanban_complete" or not isinstance(args, dict) or not kanban_policy.configured_worker():
        return None
    task_id = str(args.get("task_id") or os.environ.get("HERMES_KANBAN_TASK") or "")
    if not os.environ.get("HERMES_KANBAN_TASK") or not task_id:
        return None
    metadata = _metadata(args)
    task = load_task(task_id)
    if task is None:
        return None
    body, from_review = task
    reviewer = bool(metadata.get("review_verdict")) or from_review
    missing = missing_items(body, metadata)
    unmet = unmet_items(body, metadata)
    if reviewer:
        pending = pending_post_review_items(body, metadata)
        if pending:
            return REVIEWER_MESSAGE.format(pending=", ".join(pending)) + " " + kanban_policy.RETRY_INSTRUCTION
        if unmet:
            return UNMET_MESSAGE.format(unmet=", ".join(unmet)) + " " + kanban_policy.RETRY_INSTRUCTION
        return None
    if missing:
        return MESSAGE.format(missing=", ".join(missing)) + " " + kanban_policy.RETRY_INSTRUCTION
    if not unmet:
        return None
    return UNMET_MESSAGE.format(unmet=", ".join(unmet)) + " " + kanban_policy.RETRY_INSTRUCTION
