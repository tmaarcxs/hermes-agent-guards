"""Compare-and-set body writes for the card progress block.

``kanban_db.edit_task`` is a full-replace UPDATE with no compare-and-set: two
concurrent writers, last one wins, a human edit lost. This module instead does::

    UPDATE tasks SET body = ? WHERE id = ? AND body = ? AND status = 'running'

inside one ``BEGIN IMMEDIATE`` transaction, so a concurrent writer *or a status
transition* makes the CAS miss and the caller re-reads and retries rather than
clobbering or stranding a block on a terminal card.

The status predicate is part of the write itself, not a pre-read: the sweep
reads the card's status before it formats the block, and a completion between
that read and this write would otherwise leave a block on a ``done`` card that
nothing ever removes (``refresh_card`` refuses non-running cards and
``cleanup_stopped`` deliberately skips ``done``/``archived``).

The same transaction appends a ``progress`` ``task_events`` row, and only when
the UPDATE matched, so a CAS miss (stale body *or* a status transition) emits
nothing. The desktop board and the dashboard drawer both tail ``task_events``
over the ``/events`` WebSocket and invalidate the board plus each touched task
for *any* event kind carrying a ``task_id``
(``apps/desktop/src/plugins/kanban/api.ts``), so the custom kind is what makes
the card update live. ``edited`` is deliberately NOT emitted — that kind is
owned by the dashboard PATCH path — and ``notify_task_updated`` is never called,
so the ``on_kanban_task_updated`` hook does not fan out to other plugins.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Optional

# Statuses this plugin may never write to. ``running`` is the only status it
# touches; ``todo``/``blocked``/``review`` cards are left alone so the block is
# never written under a card that is not executing.
FROZEN_STATUSES = frozenset({"done", "archived"})

# SQL predicates spliced into the CAS UPDATE's WHERE clause.
# * The refresh path may write only while the card is still ``running``.
# * The cleanup path may strip a block only from a card that is no longer
#   running and is not terminal — i.e. todo/ready/blocked/review/triage.
RUNNING_ONLY = "status = 'running'"
NOT_RUNNING_TERMINAL = "status NOT IN ('running', 'done', 'archived')"

# Cards whose body exceeds this are skipped entirely (never truncated).
DEFAULT_MAX_BODY_BYTES = 256 * 1024


def _current_body(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    row = conn.execute("SELECT body FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return row["body"] if row else None


def cas_write_body(
    conn: sqlite3.Connection,
    task_id: str,
    old_body: Optional[str],
    new_body: str,
    *,
    emit_event: bool = True,
    status_predicate: Optional[str] = RUNNING_ONLY,
) -> bool:
    """Compare-and-set the body; return True iff exactly one row matched.

    The UPDATE carries both the body CAS *and* a status predicate, so a card
    that transitioned (completed, archived, reclaimed) between the caller's
    read and this write is a no-op rather than a stranded block. The ``progress``
    event is appended in the same transaction and only on a match, so a dashboard
    tailer can never observe the event without the body it describes.

    ``status_predicate`` defaults to :data:`RUNNING_ONLY`; the cleanup path
    passes :data:`NOT_RUNNING_TERMINAL`. ``None`` disables the status guard
    (used only by callers that have already proven the transition cannot race).
    """
    from hermes_cli.kanban_db_connect import write_txn

    with write_txn(conn):
        if old_body is None:
            sql = "UPDATE tasks SET body = ? WHERE id = ? AND body IS NULL"
            params = [new_body, task_id]
        else:
            sql = "UPDATE tasks SET body = ? WHERE id = ? AND body = ?"
            params = [new_body, task_id, old_body]
        if status_predicate:
            sql += f" AND {status_predicate}"
        cur = conn.execute(sql, params)
        matched = cur.rowcount == 1
        if matched and emit_event:
            conn.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                "VALUES (?, NULL, 'progress', NULL, ?)",
                (task_id, int(time.time())),
            )
        return matched


def status_of(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return row["status"] if row else None


def write_block(
    conn: sqlite3.Connection,
    task_id: str,
    new_body: str,
    *,
    max_retries: int = 5,
) -> bool:
    """CAS-write ``new_body`` onto a running card, retrying on a concurrent writer.

    Returns True when the body was written, False when every attempt missed
    (another writer kept winning, the card vanished, or the card left the
    ``running`` status before the write landed).
    """
    for _ in range(max_retries):
        old_body = _current_body(conn, task_id)
        if old_body is None and conn.execute(
            "SELECT 1 FROM tasks WHERE id = ?", (task_id,)
        ).fetchone() is None:
            return False
        if old_body == new_body:
            return False
        if cas_write_body(conn, task_id, old_body, new_body,
                          status_predicate=RUNNING_ONLY):
            return True
    return False


def strip_block(conn: sqlite3.Connection, task_id: str, *, max_retries: int = 5) -> bool:
    """Remove a leading block from a card that is no longer running.

    The CAS UPDATE itself refuses to touch a card that is ``running`` (it would
    strip a live block) or terminal (``done``/``archived``), so a card that
    re-started or completed between the cleanup query and this write is a no-op.
    Never touches anything else in the body; a card without a leading block is
    left alone (returns False).
    """
    from . import progress_block as pb

    for _ in range(max_retries):
        old_body = _current_body(conn, task_id)
        if not old_body:
            return False
        new_body = pb.strip_block(old_body)
        if new_body == old_body:
            return False
        if cas_write_body(conn, task_id, old_body, new_body,
                          status_predicate=NOT_RUNNING_TERMINAL):
            return True
    return False
