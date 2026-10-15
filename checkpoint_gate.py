"""Force a durable progress checkpoint before a worker run is cut at the iteration cap.

Active only in worker setups (``kanban.default_assignee`` set; see ``kanban_policy``) and
only in a worker process (``HERMES_KANBAN_TASK`` set, dispatcher-owned formula of
``agent/turn_iteration_prep._maybe_inject_iteration_budget_warning``).

The failure this closes (14-day board forensics, 14 runs cut at the 500-iteration cap):
the core one-shot warning injects at 90% of ``iteration_budget.used``, but ``used`` lags
the true call count because ``execute_code``-only turns refund their iteration, and it
resets relative to the turn counter on session rotation. Five of the twelve real 500-cap
runs never saw a warning row at all, and two more saw it and spent the last 50 calls
grinding the same blocked actions. Whatever the model opportunely keeps in hand dies with
the process: the next worker on the card starts from the board prose alone.

Hermes-side accounting is out of a plugin's reach (source), so this gate guarantees the
durable checkpoint instead: ``pre_tool_call`` reads the true iteration count from the
``api_request_id`` tail (``<turn_id>:api:<n>``, set per call in ``agent/conversation_loop``).
Once a run passes ``FIRST_CHECKPOINT_CALLS`` calls without a successful post-first checkpoint
Kanban comment (or review/complete/block, which carry the state too), every non-checkpoint
tool call is blocked with the instruction to write the checkpoint comment now — kanban
lifecycle tools stay allowed so the instruction is one call away. A successful checkpoint
re-arms the duty, and the gate re-blocks ``RECHECK_INTERVAL_CALLS`` calls later, so a run
that keeps working past its checkpoints still checkpoints every ~25 calls until it ends.
Per-session state is bounded like the other gates; fails open.

Known gap this cannot close (reported, not fixed here): a worker that exhausts its own
budget a few turns after handing off closes whatever run is current on the card —
``_record_kanban_budget_exhausted`` targets the task, not the worker's own run id — so a
fast successor run can be booked as ``timed_out (500/500)`` in under a minute despite doing
almost nothing. Fixing that needs a run-scoped CAS in Hermes source.
"""

import json
import os
import re
from collections import OrderedDict
from typing import Any, Optional

from . import kanban_policy

# First duty lands at 400/500 calls: after it, the model still has 100 calls — enough to
# checkpoint AND finish or hand off — and p99 of healthy worker sessions sits at ~430, so
# normal short and medium runs never meet the gate. Gap continues mid-run gaps of 50 that
# left the core 450-warning too late to matter.
FIRST_CHECKPOINT_CALLS = 400
RECHECK_INTERVAL_CALLS = 25

# Tools that ARE the checkpoint (or end the run with durable state): a block must never
# trap the model away from its own escape hatches, and a successful one discharges the duty.
_CHECKPOINT_TOOLS = frozenset({
    "kanban_comment", "kanban_request_review", "kanban_complete", "kanban_block", "kanban_heartbeat",
})

_CALLS = re.compile(r":api:(\d+)$")
_MAX_SESSIONS = 200

# session -> state: calls at the last discharged checkpoint
_last_checkpoint: "OrderedDict[str, int]" = OrderedDict()

MESSAGE = (
    "agent-guards blocked {tool}: this worker run is at {calls} API calls and past its budget-checkpoint duty. "
    "Durable, board-visible progress first, now, in ONE kanban_comment on your task: what is done and verified, "
    "what is mid-flight (paths, exact next step), and what the next worker must know if this run is cut. "
    "Then continue working. The checkpoint is not a stop."
)

REPEAT_MESSAGE = (
    "agent-guards blocked {tool}: {calls} API calls since your last budget checkpoint. Write the follow-up "
    "kanban_comment now (new verified facts, mid-flight state, next step) and continue. "
    "The checkpoint is not a stop."
)


def _session(kwargs: dict) -> str:
    return str(kwargs.get("session_id") or kwargs.get("task_id") or "")


def _call_count(kwargs: dict) -> Optional[int]:
    """True iteration count for this run, from the request id ``<turn_id>:api:<n>``."""
    raw = str(kwargs.get("api_request_id") or "")
    match = _CALLS.search(raw)
    return int(match.group(1)) if match else None


def _inner_calls_checkpoint(args: Any) -> bool:
    """``tool_call`` batches carry the real tool names inside ``calls``; a batch is a
    checkpoint when ANY inner call is one (the batch then runs the escape hatch)."""
    try:
        calls = json.loads(args).get("calls") if isinstance(args, str) else (args or {}).get("calls")
    except Exception:
        return False
    if not isinstance(calls, list):
        return False
    for call in calls:
        if isinstance(call, dict) and str(call.get("name") or "") in _CHECKPOINT_TOOLS:
            return True
        inner_args = call.get("arguments") if isinstance(call, dict) else None
        if inner_args is not None and _inner_calls_checkpoint(inner_args):
            return True
    return False


def _trim() -> None:
    while len(_last_checkpoint) > _MAX_SESSIONS:
        _last_checkpoint.popitem(last=False)


def record(tool_name: str = "", status: str = "", **kwargs: Any) -> None:
    """``post_tool_call``: a successful checkpoint tool discharges the duty at this call count."""
    if tool_name not in _CHECKPOINT_TOOLS or status != "ok":
        return
    session = _session(kwargs)
    if not session:
        return
    calls = _call_count(kwargs)
    _last_checkpoint[session] = calls if calls is not None else 0
    _last_checkpoint.move_to_end(session)
    _trim()


def check(tool_name: str = "", args: Any = None, **kwargs: Any) -> Optional[str]:
    """``pre_tool_call``: block non-checkpoint tools until the duty is discharged.

    Without a parsable counter (unknown harness), no decision: the core warning stays
    responsible. Any lifecycle call (including a ``tool_call`` batch containing one) passes.
    """
    if not kanban_policy.configured_worker():
        return None
    if not (os.environ.get("HERMES_KANBAN_TASK") or "").strip():
        return None
    if tool_name in _CHECKPOINT_TOOLS:
        return None
    if tool_name == "tool_call" and _inner_calls_checkpoint(args):
        return None
    session = _session(kwargs)
    if not session:
        return None
    calls = _call_count(kwargs)
    if calls is None or calls < FIRST_CHECKPOINT_CALLS:
        return None
    discharged_at = _last_checkpoint.get(session, 0)
    if discharged_at > calls - RECHECK_INTERVAL_CALLS:
        return None
    message = REPEAT_MESSAGE if discharged_at else MESSAGE
    return message.format(tool=tool_name, calls=calls)


def reset() -> None:
    """Clear per-session state (used by the test suite)."""
    _last_checkpoint.clear()


__all__ = [
    "FIRST_CHECKPOINT_CALLS", "RECHECK_INTERVAL_CALLS", "MESSAGE", "REPEAT_MESSAGE",
    "check", "record", "reset",
]
