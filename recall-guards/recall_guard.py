"""Keep the agent from asking the operator for what its own memory already answers.

Two gates over the same seam, ``pre_tool_call``:

- ``clarify``: asking the operator a question through the form tool is the main
  way the agent returns work to him. When the current turn has not read memory,
  the call is blocked once, with the instruction to read memory first.
- ``nudge``: an advisory reminder, injected on the first tool call of every Nth
  user turn, telling the agent to consult Hindsight.

What counts as "has read memory" is a read in THE SAME USER TURN. A read from
half an hour ago, or from an earlier turn, does not count: the point is that the
agent consults memory as part of the work it is about to hand back, not that it
read something once in this session. ``post_tool_call`` records the
``(session_id, turn_id)`` of every memory call, and ``pre_tool_call`` compares.

A second, narrow check reads the profile's ``state.db`` for a memory read in the
last minute. That is a safety net for one case only: the gateway reloads a plugin
mid-turn, which empties the in-process turn record, and a read made seconds
before must still count. It is a fallback, never the primary test.

Every path fails open — a missing database, a locked file or a schema change
leaves the call untouched.

Both gates apply to the lead surfaces only. A worker process answers to its card,
and its memory reads happen through the dispatcher's own prompt, so gating it
would block legitimate work.
"""

import os
import sqlite3
import time
from collections import OrderedDict
from typing import Any, Optional

from . import policy

CLARIFY_TOOL = "clarify"
MEMORY_TOOL_PREFIX = "hindsight_"

FALLBACK_WINDOW_SECONDS = 60
"""Only for the reload case: a memory read this recent still counts even when the
in-process turn record was lost. Never widen this to cover a whole session — a
stale read is exactly what the gate exists to catch."""

_NUDGE_EVERY_TURNS = 5
"""Advisory nudge cadence, in user turns of the session."""

_MAX_TRACKED = 400

NUDGE = (
    "Memory check: {count} user turns have passed in this session. Before the next "
    "non-trivial step, run `hindsight_recall` on the topic at hand — the operator has "
    "standing decisions on file, and re-deciding or re-asking what memory already "
    "answers is the failure this reminder exists to prevent."
)

BLOCK_MESSAGE = (
    "agent-guards blocked clarify [recall_guard]: this turn has not read memory. Before "
    "asking the operator, run `hindsight_recall` on the question (and `hindsight_reflect` "
    "if a synthesis is needed) and check whether the answer is already on file — a "
    "standing decision, a credential, an authorization or a prior correction. If memory "
    "answers it, act on the answer instead of asking. If memory does not answer it, ask "
    "again in this same turn and say what memory returned. Do not ask the operator to "
    "re-authorize work he has already authorized."
)

_state: "OrderedDict[str, dict]" = OrderedDict()


def reset() -> None:
    """Clear recorded turns and cached reads (test helper)."""
    _state.clear()


def state_db_path() -> Optional[str]:
    """The active profile's session store, or None when it cannot be resolved."""
    try:
        from hermes_constants import get_hermes_home

        return str(get_hermes_home() / "state.db")
    except Exception:
        home = os.environ.get("HERMES_HOME") or os.path.join(os.path.expanduser("~"), ".hermes")
        path = os.path.join(home, "state.db")
        return path if os.path.exists(path) else None


def record(tool_name: str = "", session_id: str = "", turn_id: str = "", **kwargs: Any) -> None:
    """``post_tool_call``: remember that this turn read memory."""
    try:
        if not str(tool_name or "").startswith(MEMORY_TOOL_PREFIX):
            return
        session = str(session_id or "")
        turn = str(turn_id or "")
        if not session or not turn:
            return
        entry = _state.setdefault(session, {})
        turns = entry.setdefault("read_turns", [])
        if turn not in turns:
            turns.append(turn)
            del turns[:-100]
        entry["read_at"] = time.monotonic()
        entry.pop("db_read_at", None)
        _state.move_to_end(session)
        while len(_state) > _MAX_TRACKED:
            _state.popitem(last=False)
    except Exception:
        return


def read_this_turn(session_id: str, turn_id: str) -> bool:
    """True when ``(session_id, turn_id)`` already read memory in this process."""
    session = str(session_id or "")
    turn = str(turn_id or "")
    if not session or not turn:
        return False
    entry = _state.get(session) or {}
    return turn in (entry.get("read_turns") or [])


def _db_memory_read(session_id: str) -> Optional[bool]:
    """True when the session read memory within the fallback window; None when unknown.

    ``None`` is the fail-open answer: no session id, no database, or any error.
    """
    if not session_id:
        return None
    path = state_db_path()
    if not path or not os.path.exists(path):
        return None
    cutoff = time.time() - FALLBACK_WINDOW_SECONDS
    try:
        connection = sqlite3.connect("file:" + path + "?mode=ro", uri=True, timeout=1.0)
        try:
            row = connection.execute(
                "SELECT 1 FROM messages WHERE session_id = ? AND tool_name LIKE 'hindsight\\_%' ESCAPE '\\' "
                "AND timestamp >= ? LIMIT 1",
                (session_id, cutoff),
            ).fetchone()
        finally:
            connection.close()
        return row is not None
    except Exception:
        return None


def recent_read(session_id: str) -> Optional[bool]:
    """Cached wrapper around :func:`_db_memory_read`."""
    session = str(session_id or "")
    if not session:
        return None
    entry = _state.setdefault(session, {})
    now = time.monotonic()
    stamp = entry.get("db_read_at")
    if stamp is not None and now - stamp < 5.0:
        return entry.get("db_read")
    read = _db_memory_read(session)
    entry["db_read_at"] = now
    entry["db_read"] = read
    return read


def turn_count(session_id: str, turn_id: str) -> int:
    """Distinct user turns seen in this session, counting the current one."""
    session = str(session_id or "")
    if not session:
        return 0
    entry = _state.setdefault(session, {})
    turns = entry.setdefault("turns", [])
    turn = str(turn_id or "")
    if turn and turn not in turns:
        turns.append(turn)
        del turns[:-200]
    return len(turns)


def clarify_block(session_id: str, turn_id: str, tool_name: str) -> Optional[str]:
    """The block message for a ``clarify`` call that has not read memory this turn."""
    if str(tool_name or "") != CLARIFY_TOOL:
        return None
    if read_this_turn(session_id, turn_id):
        return None
    if recent_read(session_id) is True:
        return None
    return BLOCK_MESSAGE


def nudge_text(session_id: str, turn_id: str, tool_name: str) -> Optional[str]:
    """The advisory nudge on the first call of every Nth user turn."""
    if not str(tool_name or ""):
        return None
    count = turn_count(session_id, turn_id)
    if count == 0 or count % _NUDGE_EVERY_TURNS != 0:
        return None
    session = str(session_id or "")
    entry = _state.setdefault(session, {})
    if entry.get("nudged_turn") == count:
        return None
    entry["nudged_turn"] = count
    return NUDGE.format(count=count)


def check(tool_name: str = "", args: Any = None, session_id: str = "", turn_id: str = "",
          **kwargs: Any) -> Optional[dict]:
    """``pre_tool_call``: block ``clarify`` when the current turn read no memory."""
    try:
        if not policy.is_lead():
            return None
        blocked = clarify_block(session_id, turn_id, tool_name)
        if blocked:
            return {"action": "block", "message": blocked}
    except Exception:
        return None
    return None


def transform(tool_name: str = "", result: Any = None, session_id: str = "", turn_id: str = "",
              **kwargs: Any) -> Optional[str]:
    """``transform_tool_result``: append the periodic memory reminder."""
    try:
        if not policy.is_lead() or not isinstance(result, str):
            return None
        text = nudge_text(session_id, turn_id, tool_name)
        if text:
            return result + "\n\n" + text
    except Exception:
        return None
    return None


__all__ = [
    "BLOCK_MESSAGE", "FALLBACK_WINDOW_SECONDS", "NUDGE", "check", "clarify_block",
    "nudge_text", "read_this_turn", "recent_read", "record", "reset", "state_db_path",
    "transform", "turn_count",
]
