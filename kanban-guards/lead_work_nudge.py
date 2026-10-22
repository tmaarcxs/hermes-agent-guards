"""One-time lead nudge to delegate after more than 5 work calls in a user turn.

When a lead process (``HERMES_KANBAN_TASK`` absent and a worker profile configured,
the plugin's established lead definition in ``lead_delegation.is_lead``) has made
more than ``WORK_CALL_THRESHOLD`` work calls in one user turn without calling
``kanban_create``, the call that crosses the threshold gets one appended nudge
telling it to stop doing the work itself and split the remaining work across as
many parallel cards as possible. The nudge fires once per turn, never for a turn
that already created a card, and never in a worker process.

Mechanism: ``transform_tool_result`` (non-blocking). Verified against the running
Hermes source:

- ``model_tools.py`` ``_apply_transform_tool_result_hook`` (lines 849-866): runs
  after ``post_tool_call`` and before the result enters context; the first
  non-``None`` string return replaces the result. ``agent/tool_executor.py``
  (lines 1808-1813) and ``agent/agent_runtime_helpers.py`` (line 2602) call the
  same helper for registry and inline-dispatched tools, so every executed tool
  call passes through exactly once. Appending to the real result is therefore a
  nudge, not a block: the call ran and the model reads both parts.
- ``hermes_cli/plugins_dispatch.py`` (lines 42-48): ``transform_tool_result`` is
  a bounded (fail-open) hook, so a slow callback cannot wedge the tool loop.
- Hook kwargs carry ``session_id``, ``turn_id``, ``tool_call_id`` and
  ``api_request_id`` (``model_tools.py`` ``_CallIds.hook_kwargs``, lines 643-654;
  filled from ``agent._current_turn_id`` in ``agent/inline_tool_executors.py``
  lines 20-28). ``turn_id`` is minted once per user turn in
  ``agent/turn_context.py`` ``_bind_turn_identity`` (lines 587-591), so a new
  ``turn_id`` is a new counter bucket and the counter resets with each user turn.
  Tool-result iterations inside one turn reuse the same ``turn_id``; a defensive
  double fire of one call is deduped by ``tool_call_id``.

Counting rule: every tool call counts except coordination and bookkeeping —
``kanban_*``, ``clarify``, ``memory``, ``skill_view``/``skills_list``/
``skill_manage``, ``todo``/``todo_list``, ``hindsight_*`` and ``process_manage``.
``kanban_create`` is both excluded from the count and recorded as delegation.
An unknown tool name counts (the rule is exclusion-based). Blocked calls never
reach this hook and are never counted.

Fails open: no turn identity, a worker process, or a non-string result leaves the
result untouched.
"""

import os
from collections import OrderedDict
from typing import Any, Optional

from . import kanban_policy

_MAX_TRACKED_TURNS = 200
WORK_CALL_THRESHOLD = 5

_EXCLUDED_TOOLS = frozenset({
    "clarify",
    "memory",
    "skill_view",
    "skills_list",
    "skill_manage",
    "todo",
    "todo_list",
    "process_manage",
})
_EXCLUDED_PREFIXES = ("kanban_", "hindsight_")

NUDGE = (
    "You have made {count} work calls this turn without delegating. Stop doing the work "
    "yourself: split the remaining work across as many parallel workers as possible to "
    "spread the load and maximize parallelism — list every independent part (repos, "
    "modules, questions, hypotheses, files, data slices, implementation/verification/review "
    "lanes) and create one kanban_create card per part in this same turn. Use a single card "
    "only if the work truly cannot be split."
)

_state: "OrderedDict[tuple[str, str], dict]" = OrderedDict()


def reset() -> None:
    """Clear per-turn counter state (test helper)."""
    _state.clear()


def is_lead() -> bool:
    """True in a lead process: no task pin and a Kanban worker profile configured."""
    if (os.environ.get("HERMES_KANBAN_TASK") or "").strip():
        return False
    return bool(kanban_policy.configured_worker())


def counts_as_work(tool_name: str) -> bool:
    """True when the tool call is direct work rather than coordination/bookkeeping."""
    name = str(tool_name or "")
    if not name or name in _EXCLUDED_TOOLS:
        return False
    return not name.startswith(_EXCLUDED_PREFIXES)


def _bucket(key: tuple[str, str]) -> dict:
    bucket = _state.get(key)
    if bucket is None:
        bucket = {"calls": 0, "delegated": False, "nudged": False}
        _state[key] = bucket
    _state.move_to_end(key)
    while len(_state) > _MAX_TRACKED_TURNS:
        _state.popitem(last=False)
    return bucket


def hook(tool_name: str = "", args: Any = None, result: Any = None,
         session_id: str = "", turn_id: str = "", **_: Any) -> Optional[str]:
    """``transform_tool_result``: append the one-time delegation nudge.

    Returns the result with the nudge appended when this call is the first work
    call past the threshold in a turn that has not created a card, else ``None``
    (the result stays byte-identical and every later transform still runs).
    """
    if not is_lead():
        return None
    turn = str(turn_id or "")
    if not turn:
        return None
    key = (str(session_id or ""), turn)
    if str(tool_name or "") == "kanban_create":
        _bucket(key)["delegated"] = True
        return None
    if not counts_as_work(tool_name):
        return None
    bucket = _bucket(key)
    bucket["calls"] += 1
    if bucket["delegated"] or bucket["nudged"] or bucket["calls"] <= WORK_CALL_THRESHOLD:
        return None
    if not isinstance(result, str):
        return None
    bucket["nudged"] = True
    return result + "\n\n" + NUDGE.format(count=bucket["calls"])


__all__ = ["NUDGE", "WORK_CALL_THRESHOLD", "counts_as_work", "hook", "is_lead", "reset"]
