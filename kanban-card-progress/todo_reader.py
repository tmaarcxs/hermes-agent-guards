"""Resolve a running card's worker Tasklist from the worker profile's ``state.db``.

The join key is the card's latest ``owner_admitted`` ``task_events`` row, written
once per run by the owner (``gateway/session_kanban.py``)::

    {"db": "<worker profile>/state.db",
     "session_id": "local-…", "request_id": "kanban:[\"board\",\"task\",run]",
     "pid": …, "birth": …}

A retry/reclaim mints a new run id and therefore a new ``session_id``, so the
mapping is re-resolved on every sweep — never cached across cycles.

The read is one indexed SELECT against the worker ``state.db`` opened with
``mode=ro``. ``immutable=1`` is NEVER used: it makes SQLite ignore the WAL, so a
live writer's newest rows are invisible (measured: 5 rows behind on a live
board).
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Optional

# Same shape the native resume reader uses (hermes_state_todo.py).
TODO_SQL = (
    "SELECT content FROM messages INDEXED BY idx_messages_session_id "
    "WHERE session_id = ? AND role = 'tool' "
    "AND tool_name IN ('todo_list','todo') "
    "AND (active = 1 OR compacted = 1) "
    "AND length(content) <= ? "
    "ORDER BY id DESC LIMIT 1"
)
MAX_TODO_RESULT_CHARS = 512_000

OWNER_SQL = (
    "SELECT payload FROM task_events "
    "WHERE kind = 'owner_admitted' AND task_id = ? "
    "ORDER BY id DESC LIMIT 1"
)


def owner_ref(conn: sqlite3.Connection, task_id: str) -> Optional[tuple[str, str]]:
    """``(state_db_path, session_id)`` from the card's newest ``owner_admitted``.

    ``None`` when the card has never been admitted (queued, or never ran).
    """
    row = conn.execute(OWNER_SQL, (task_id,)).fetchone()
    if not row or not row["payload"]:
        return None
    try:
        payload = json.loads(row["payload"])
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    db, session_id = payload.get("db"), payload.get("session_id")
    if not isinstance(db, str) or not db.strip():
        return None
    if not isinstance(session_id, str) or not session_id.strip():
        return None
    return db, session_id


def read_todo_content(
    state_db_path: str,
    session_id: str,
    *,
    timeout: float = 5.0,
) -> Optional[str]:
    """Newest visible ``todo_list`` result content for ``session_id``, or ``None``.

    ``mode=ro`` only. A missing DB, a locked DB, or a malformed path returns
    ``None`` — this function never raises.
    """
    if not state_db_path or not session_id:
        return None
    path = os.path.expanduser(state_db_path)
    if not os.path.isfile(path):
        return None
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=timeout)
        conn.row_factory = sqlite3.Row
        row = conn.execute(TODO_SQL, (session_id, MAX_TODO_RESULT_CHARS)).fetchone()
        return row["content"] if row else None
    except sqlite3.Error:
        return None
    except OSError:
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
