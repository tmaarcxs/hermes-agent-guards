"""Deterministic lead-only slimming of kanban_show results: drop worker_context
and text-free bookkeeping events; keep every text-bearing field byte-identical.

A lead ``kanban_show`` result stays in chat history and is re-sent on every later
request, but it carries its content twice: the structured ``task``/``comments``/
``parents`` fields and again the whole rendering inside ``worker_context``, plus
heartbeats and dispatcher bookkeeping events that carry no text the lead reads.
Measured over 7 days of lead sessions in the host state.db (494 distinct slimmed
payloads): ``worker_context`` holds 46.0% of the parsed bytes, the event kinds
below 86% of event rows and 64% of event bytes (heartbeat alone is 9458 of 12955
rows), and dropping ``worker_context`` and the listed event kinds while keeping
``runs`` whole saves 52.1% of the raw result bytes (14.28 -> 6.85 MB incl. 266
persisted-output wrapper rows replayed unchanged). Every run
summary, error, review verdict and kept event payload survives byte-identical.

The transform rides ``transform_tool_result`` and rewrites only a successful
``kanban_show`` JSON result of dict shape ``{"task": ...}``. It is
deterministic (same input, same output bytes) because the result becomes
persisted history; it keeps unknown fields rather than dropping them; and it is
lead-only: with ``HERMES_KANBAN_TASK`` set, a worker process gets the unchanged
result because it relies on ``worker_context``. Fails open on any parse error.
"""

import json
import os
from typing import Optional

EVENT_KINDS_DROPPED = frozenset({
    "heartbeat", "claimed", "spawned", "attached", "respawn_guarded",
    "promoted", "linked", "unlinked", "assigned", "archived",
})

_REMOVED_KEY = "removed_fields"
_DROPPED_KEY = "dropped_event_rows"


def transform(tool_name: str, args: object, result: object) -> Optional[str]:
    """Slim a lead ``kanban_show`` result; anything else passes through unchanged."""
    if os.environ.get("HERMES_KANBAN_TASK"):
        return None  # worker process: the intact worker_context is load-bearing
    if tool_name != "kanban_show" or not isinstance(result, str):
        return None
    try:
        payload = json.loads(result)
    except Exception:
        return None  # a malformed result passes through untouched
    return _slim(payload)


def _slim(payload) -> Optional[str]:
    if not isinstance(payload, dict) or not isinstance(payload.get("task"), dict):
        return None
    slim = {key: value for key, value in payload.items() if key != "worker_context"}
    if len(slim) == len(payload):
        return None  # no worker_context: nothing this transform targets
    events = slim.get("events")
    dropped = 0
    if isinstance(events, list):
        kept = []
        for row in events:
            if isinstance(row, dict) and isinstance(row.get("kind"), str) \
                    and row.get("kind") in EVENT_KINDS_DROPPED:
                dropped += 1
                continue
            kept.append(row)
        slim["events"] = kept
    slim[_REMOVED_KEY] = "worker_context"
    slim[_DROPPED_KEY] = dropped
    return json.dumps(slim)
