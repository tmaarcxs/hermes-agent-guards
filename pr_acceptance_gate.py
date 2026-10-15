"""Inject the completion-contract duty where a worker reads its card, and cap the published_pr rejection loop.

Active only in worker setups (``kanban.default_assignee`` set; see ``kanban_policy``) and
only in a worker process (``HERMES_KANBAN_TASK`` set).

Six weeks of board receipts (933 ``PR acceptance missing`` rejections on 34 cards, two cards
accounting for 895) show the same two failures: a worker closes a card whose persisted
``completion_contract`` names a GitHub publication without ever learning that completion
requires ``metadata.published_pr``, then keeps retrying the same completion payload after
the rejection. Two levers:

- ``transform`` rides ``transform_tool_result``. A ``kanban_show`` of the worker's own card
  when its ``completion_contract`` names a publication gains one reminder line stating exactly
  what ``kanban_complete`` must carry. A rejected ``kanban_complete`` (``PR acceptance
  missing``) gains the precise fix on the rejection text the worker is already reading.
- ``record``/``check`` cap the loop: after ``CAP`` counted rejections, a retry that still
  lacks a ``published_pr`` matching the card's contract is blocked with the contract restated,
  the publication-binding hazard named, and the ``kanban_block`` escape required in the same
  response.

The binding hazard: under an OWNER/REPO contract the acceptance layer permanently rewrites
the card's contract to the first ``published_pr`` whose repository matches, so a guessed PR
number poisons the card for every later completion. Fails open; per-session state is bounded
like the other gates.
"""

import json
import os
import re
from collections import OrderedDict
from typing import Any, Callable, Optional

from . import completion_gate, kanban_policy

CAP = 2
_MAX_SESSIONS = 200

_REPO = re.compile(r"[\w.-]+/[\w.-]+")
_PR_URL = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/([1-9]\d*)")
_MISSING_REJECTION = re.compile(r"PR acceptance missing")

SHOW_REMINDER = (
    "\n\nagent-guards reminder: this card's completion contract is {contract!r}. kanban_complete on it "
    "must carry metadata.published_pr as {fix} — the PR's required checks on its exact head must be green "
    "before completion is accepted, and an attempt without published_pr is rejected as \"PR acceptance "
    "missing\"."
)
REJECTION_SUFFIX = (
    "\nagent-guards: this rejection is fixed by metadata, not by more work. Retry kanban_complete in this "
    "same response with metadata.published_pr set to {fix} exactly."
)
CAP_MESSAGE = (
    "agent-guards blocked kanban_complete: {count} PR acceptance rejections in this run and this retry still "
    "lacks metadata.published_pr matching the card's completion contract ({contract}). Set metadata.published_pr "
    "to {fix} exactly and retry kanban_complete in this same response. Under an OWNER/REPO contract the first "
    "published_pr whose repository matches is bound into the card's contract permanently, so never guess a PR "
    "number. If no PR for this contract exists yet or you cannot supply one, do not complete: call "
    'kanban_block(kind="needs_input", reason="published_pr missing for completion contract {contract}") in this '
    "same response — do not end on a text-only reply."
)

_rejections: "OrderedDict[str, int]" = OrderedDict()


def publication_contract(contract: str) -> tuple[str, str]:
    """``("url"|"repo", contract)`` for a GitHub publication contract, ``("none", "")`` otherwise."""
    text = str(contract or "").strip()
    if _PR_URL.fullmatch(text):
        return "url", text
    if _REPO.fullmatch(text):
        return "repo", text
    return "none", ""


def required_value(kind: str, contract: str) -> str:
    """The manifest value ``metadata.published_pr`` must carry for this contract."""
    return contract if kind == "url" else f"a GitHub PR URL in {contract}"


def _session(kwargs: dict) -> str:
    return str(kwargs.get("session_id") or kwargs.get("task_id") or "")


def _read_contract(task_id: str) -> Optional[str]:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    conn = kbc.connect(board=None)
    try:
        task = kb.get_task(conn, task_id)
        return str(task.completion_contract or "") if task is not None else None
    finally:
        conn.close()


def _worker_process() -> bool:
    return bool(kanban_policy.configured_worker()) and bool((os.environ.get("HERMES_KANBAN_TASK") or "").strip())


def _trim() -> None:
    while len(_rejections) > _MAX_SESSIONS:
        _rejections.popitem(last=False)


def record(tool_name: str = "", status: str = "", error_message: Optional[str] = None, **kwargs: Any) -> None:
    """``post_tool_call``: count acceptance rejections; a successful completion clears the session."""
    if tool_name != "kanban_complete":
        return
    session = _session(kwargs)
    if not session:
        return
    if status == "ok":
        _rejections.pop(session, None)
        _trim()
        return
    if status != "error" or not _MISSING_REJECTION.search(str(error_message or "")):
        return
    _rejections[session] = _rejections.get(session, 0) + 1
    _rejections.move_to_end(session)
    _trim()


def check(tool_name: str = "", args: Any = None, *, load_contract: Optional[Callable[[str], Optional[str]]] = None,
          **kwargs: Any) -> Optional[str]:
    """``pre_tool_call``: block an unfixed completion retry once the rejection cap is reached."""
    if tool_name != "kanban_complete" or not isinstance(args, dict) or not _worker_process():
        return None
    task_id = str(os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    count = _rejections.get(_session(kwargs), 0)
    if not task_id or count < CAP:
        return None
    try:
        contract = str((load_contract or _read_contract)(task_id) or "")
    except Exception:
        return None
    kind, text = publication_contract(contract)
    if kind == "none":
        return None
    published = str(completion_gate._metadata(args).get("published_pr") or "")
    match = _PR_URL.fullmatch(published)
    if match and ((kind == "url" and published == text) or (kind == "repo" and match.group(1) == text)):
        return None
    return CAP_MESSAGE.format(count=count, contract=text, fix=required_value(kind, text))


def transform(tool_name: str = "", args: Any = None, result: Any = None, status: str = "",
              error_message: Optional[str] = None,
              load_contract: Optional[Callable[[str], Optional[str]]] = None, **kwargs: Any) -> Optional[str]:
    """``transform_tool_result``: the show-time reminder and the on-rejection fix."""
    if not _worker_process() or not isinstance(result, str):
        return None
    if tool_name == "kanban_show" and str((args or {}).get("task_id") or "") in ("", os.environ.get("HERMES_KANBAN_TASK") or ""):
        try:
            contract = str(json.loads(result).get("task", {}).get("completion_contract") or "")
        except Exception:
            return None
        kind, text = publication_contract(contract)
        if kind == "none":
            return None
        return result + SHOW_REMINDER.format(contract=text, fix=required_value(kind, text))
    if tool_name == "kanban_complete" and status == "error" and _MISSING_REJECTION.search(str(error_message or "")):
        task_id = str(os.environ.get("HERMES_KANBAN_TASK") or "").strip()
        try:
            contract = str((load_contract or _read_contract)(task_id) or "")
        except Exception:
            return None
        kind, text = publication_contract(contract)
        if kind == "none":
            return None
        return result + REJECTION_SUFFIX.format(fix=required_value(kind, text))
    return None


def reset() -> None:
    """Clear per-session state (used by the test suite)."""
    _rejections.clear()


__all__ = ["CAP", "CAP_MESSAGE", "REJECTION_SUFFIX", "SHOW_REMINDER", "check", "publication_contract",
           "record", "required_value", "reset", "transform"]
