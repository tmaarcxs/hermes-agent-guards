"""Swarm-split audit variant V4: inventory-first injection at lead turn start.

At the start of a lead turn (``pre_llm_call``) injects one short instruction:
before delegating, write the inventory of the request's independent atomic
units, then create one narrow Kanban card per unit, because workers are slow
and need narrow tasks and parallel cards finish sooner. No blocking, no card
count, no fixed floor.

Contract facts (verified against the running Hermes source, hermes-agent):
- ``agent/turn_context.py`` ``_collect_pre_llm_call_context`` (line 798,
  invoked once per user turn at the turn prologue, line 1177) fires
  ``pre_llm_call`` with ``session_id, task_id, turn_id, user_message,
  conversation_history, is_first_turn, model, ...``. Tool-result iterations of
  the same turn never re-run the hook: they replay the prologue's
  ``api_content`` stamp (``build_api_messages`` lines 1291-1295), so one
  pre_llm_call call == one user turn == one injection. This module still keeps
  a per-``(session_id, turn_id)`` dedup so a defensive double fire of the same
  turn cannot double-inject.
- ``hermes_cli/plugins_dispatch.py`` ``invoke_hook`` (lines 211-218):
  ``pre_llm_call`` callbacks may return ``{"context": "..."}`` (or a str) to
  inject; the host joins it into the current user message only
  (``turn_context.py`` lines 833-849 and ``compose_user_api_content`` 120-131),
  never the system prompt (lines 1341-1344). Returning a context dict can
  never block anything — the mechanism is unblockable by construction.

Lead-process only: any call with ``HERMES_KANBAN_TASK`` set (worker context)
returns ``None`` — workers must never see the instruction.
"""

from collections import OrderedDict
from typing import Any, Optional

_MAX_TRACKED_TURNS = 200

INSTRUCTION = (
    "[System: swarm-split reminder. Workers are slow and need narrow tasks; more "
    "independent cards in parallel = faster delivery. Before delegating anything in this "
    "turn: first write the inventory of the request's independent atomic units — one "
    "narrow, independently verifiable objective each, real dependencies only — then "
    "create one Kanban card per unit, either in this turn or at the moment the plan "
    "covering that unit is approved, whichever the workflow prescribes, exactly one "
    "unit per card, as many as the work genuinely allows. Child cards (non-empty parents) are exempt; "
    "never let this block or delay delivery. Each acceptance item on a card states the "
    "input range it must survive and the final verification surface, and any premise "
    "the plan depends on is named with the cheap check that would falsify it. A running "
    "card is never widened: new work found mid-run becomes a new card with a dependency "
    "edge on the running one.]"
)

_injected_turns: "OrderedDict[tuple[str, str], bool]" = OrderedDict()


def reset() -> None:
    """Clear per-session turn dedup state (test helper)."""
    _injected_turns.clear()


def _turn_key(kwargs: dict[str, Any]) -> Optional[tuple[str, str]]:
    session = str(kwargs.get("session_id") or "")
    turn = str(kwargs.get("turn_id") or "")
    if turn:
        return (session, turn)
    message = kwargs.get("user_message")
    if isinstance(message, str) and message.strip():
        import hashlib

        return (session, "msg:" + hashlib.sha256(message.encode("utf-8")).hexdigest())
    return None


def hook(**kwargs: Any) -> Optional[dict[str, str]]:
    """``pre_llm_call``: inject the inventory-first rule once per user turn.

    Returns ``{"context": INSTRUCTION}`` for a fresh lead turn, else ``None``
    (worker process, already-injected turn, or empty turn payload). Never
    returns a block directive.
    """
    import os

    if (os.environ.get("HERMES_KANBAN_TASK") or "").strip():
        return None
    key = _turn_key(kwargs)
    if key is None:
        return None
    if _injected_turns.get(key):
        return None
    _injected_turns[key] = True
    _injected_turns.move_to_end(key)
    while len(_injected_turns) > _MAX_TRACKED_TURNS:
        _injected_turns.popitem(last=False)
    return {"context": INSTRUCTION}


__all__ = ["INSTRUCTION", "hook", "reset"]
