"""``repeat_block_gate``: escalate a call the same guard already blocked twice.

The block text exists; models re-issue the identical call anyway and burn turns
re-reading an identical verdict. Measured over 30 days in the worker profile
state.db: 364 second-identical-block events in 207 sessions (worst: 8 identical
blocks of one call in a single session), source reports attesting the shape.

The gate rides ``transform_tool_result`` and acts only on the OUTCOME. It never
blocks in advance: every call reaches the real guards, and a veto can flip
between two identical attempts, so a pre-emptive block would be a false
positive. When the result that comes back IS a guard block of a call this
session already had blocked by the same guard with the same arguments and no
suspect that changed the state in between, the gate REWRITES the result into an
escalation that says repetition cannot pass and names the two permitted ways
out, keeping the original block text (with the exact corrected call inside)
below it. Any other result — a pass, a different guard, a different guard's
seed, a non-string result — records state or resets and passes untouched.
Every internal error fails open: ``transform`` returns None and the original
block text is delivered to the model unchanged.
"""

import hashlib
import json
import os
import re
from typing import Any, Optional

_BLOCK_RE = re.compile(
    r'\{"error": "agent-guards (?:blocked|bounced) (?P<tool>[A-Za-z0-9_-]+)')
"""Head of a guard-block result: the JSON the core hands the model, tool first."""

_GUARD_RE = re.compile(r"\[(?P<guard>[a-z_]+)\]")
"""Guard names ride the ``[name]`` stamp every blocking plugin adds."""

_MAX_KEY = 64_000
"""Longest serialized call hashed (a JSON dump larger than this is truncated)."""

_WORKER_ESCALATION = (
    "agent-guards repeat_block_gate: this exact call was rejected {count} times by "
    "{guard} in this session. Repeating it cannot pass; the original block below says "
    "what to change. If the guard is refusing work your task legitimately needs, "
    "call kanban_block(reason=\"{guard}: <what the guard keeps rejecting>\"), keep the "
    "original corrected call from the block text below, and end on that report.\n\n"
    "{original}"
)

_PLAIN_ESCALATION = (
    "agent-guards repeat_block_gate: this exact call was rejected {count} times by "
    "{guard} in this session. Repeating it cannot pass; the original block below "
    "states the exact change that would let it through — make that one change to "
    "the call (or switch to a different approach) instead of resending it.\n\n"
    "{original}"
)


def _key(tool_name: str, guard: str, args: Any) -> str:
    """Stable per-session key: tool + guard + canonical argument hash."""
    try:
        blob = json.dumps(args, sort_keys=True, default=str)
    except Exception:
        blob = repr(args)
    digest = hashlib.sha256(blob[:_MAX_KEY].encode("utf-8", "replace")).hexdigest()[:16]
    return f"{tool_name}|{guard}|{digest}"


def _guard_name(result: str) -> str:
    """The LAST ``[name]`` stamp on the block's first line, else ``agent-guards``."""
    head = result.split("\n", 1)[0]
    stamps = _GUARD_RE.findall(head)
    return stamps[-1] if stamps else "agent-guards"


def _is_escape_hatch(tool_name: str) -> bool:
    """A call whose whole job is to change state or hand off; escalating a repeat of
    it with kanban_block advice would be absurd."""
    return tool_name in ("kanban_block", "kanban_heartbeat")


def _worker_session() -> bool:
    """True in a kanban worker process (the escalation offers ``kanban_block``)."""
    return bool(os.environ.get("HERMES_KANBAN_TASK"))


class _State:
    """Per-session memory of the last blocked call; bounded resets keep it small."""

    __slots__ = ("count", "key")

    def __init__(self) -> None:
        self.key: Optional[str] = None
        self.count = 0


_sessions: dict = {}
"""session_id -> _State; one entry per live session, trimmed to _MAX_SESSIONS."""


_MAX_SESSIONS = 4096
"""Live sessions tracked (one tiny state object each); cleared wholesale beyond it."""


def _state_for(session_id: str) -> _State:
    if len(_sessions) >= _MAX_SESSIONS:
        _sessions.clear()
    state = _sessions.get(session_id)
    if state is None:
        state = _State()
        _sessions[session_id] = state
    return state


def reset(session_id: str = "") -> None:
    """Forget everything (tests, teardown)."""
    if session_id:
        _sessions.pop(session_id, None)
    else:
        _sessions.clear()


def transform(tool_name: str, args: Any, result: object, session_id: str = "") -> Optional[str]:
    """``transform_tool_result``: rewrite a REPEATED guard block, else hand back None.

    Any result for a different call resets the counter (the model tried something
    else); only the second-and-later identical block is rewritten. A PASS of the
    same call answers different from every block, so the string compare below
    never sees it as a repeat.
    """
    if not isinstance(result, str):
        return None
    key = _key(tool_name or "", _guard_name(result) if result.startswith('{"error": "agent-guards ') else "", args)
    state = _state_for(session_id or "default")
    try:
        if not result.startswith('{"error": "agent-guards '):
            state.key, state.count = None, 0
            return None
        head = _BLOCK_RE.match(result)
        if not head or head.group("tool") != (tool_name or ""):
            return None
        guard = _guard_name(result)
        if _is_escape_hatch(tool_name):
            return None
        true_key = _key(tool_name, guard, args)
        if state.key != true_key:
            state.key, state.count = true_key, 1
            return None
        state.count += 1
        template = _WORKER_ESCALATION if _worker_session() else _PLAIN_ESCALATION
        return template.format(count=state.count, guard=guard, original=result)
    except Exception:
        return None
