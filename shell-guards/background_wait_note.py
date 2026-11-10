"""Tell the model its foreground wait now runs in the background.

``pre_tool_call`` rewrites a foreground ``terminal`` wait (sleep, polling loop, ``read -t``,
``select``) to ``background=true, notify=true``. A rewrite carries no message, so the model
sees only a bare ``Background process started`` and never learns its wait moved. ``record``
remembers the rewritten call by session and ``tool_call_id``; ``transform`` adds one
``rewritten_to_background`` field to that call's started-process result, once. A call the model
made with ``background=true`` itself is never recorded. Fails open: any error leaves the
rewrite and the result untouched.
"""

import json
from collections import OrderedDict
from typing import Any, Optional

_MAX_PENDING = 8
_PENDING: "OrderedDict[str, bool]" = OrderedDict()

_HEAD = (
    "This command has NOT finished: a guard moved this foreground wait to the "
    "background, so this result holds no output from the command. Do NOT re-run it. "
)
NOTE = _HEAD + (
    "Its completion message (exit code + output tail) arrives when it exits; to read it sooner use "
    'process_manage(action="wait", session_id="{sid}") or process_manage(action="log", session_id="{sid}"). '
    "Next time start such a wait as terminal(command=<the same command>, background=true, notify=true)."
)
NOTE_QUIET = _HEAD + (
    "No completion message will reach you in this session: before you finish, collect the outcome with "
    'process_manage(action="wait", session_id="{sid}") or read it with '
    'process_manage(action="log", session_id="{sid}").'
)


def reset() -> None:
    """Clear the pending records (used by the test suite)."""
    _PENDING.clear()


def _key(session_id: str, tool_call_id: str) -> str:
    """Both hooks of one call receive the same ids; the session keeps two sessions' ids apart."""
    return f"{session_id}\0{tool_call_id}"


def record(tool_call_id: str = "", session_id: str = "") -> None:
    """``pre_tool_call``: remember a call whose foreground wait was rewritten to the background."""
    try:
        if not tool_call_id:
            return
        key = _key(session_id, tool_call_id)
        _PENDING[key] = True
        _PENDING.move_to_end(key)
        while len(_PENDING) > _MAX_PENDING:
            _PENDING.popitem(last=False)
    except Exception:
        return


def transform(tool_name: str = "", result: Any = None, tool_call_id: str = "", session_id: str = "") -> Optional[str]:
    """``transform_tool_result``: add the note to the started-process result of a recorded call.

    The record is consumed by the first ``terminal`` result carrying its session and
    ``tool_call_id``, so the note appears once. Only a ``Background process started`` result gets
    it: a failed spawn or a foreground result passes through untouched.
    """
    try:
        if tool_name != "terminal" or not tool_call_id or _PENDING.pop(_key(session_id, tool_call_id), None) is None:
            return None
        data = json.loads(result) if isinstance(result, str) else None
        if not isinstance(data, dict) or data.get("output") != "Background process started" or not data.get("session_id"):
            return None
        note = NOTE if data.get("notify_on_complete") is True else NOTE_QUIET
        data["rewritten_to_background"] = note.format(sid=data["session_id"])
        return json.dumps(data, ensure_ascii=False)
    except Exception:
        return None
