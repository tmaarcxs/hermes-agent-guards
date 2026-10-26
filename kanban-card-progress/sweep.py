"""One bounded sweep: running cards get a refreshed block, stopped cards get cleaned.

Bounded by design — every step is wrapped, per-card failures are isolated, the
cycle honours a wall-clock budget and a card cap, and the whole sweep is
best-effort. It runs on a plugin-owned daemon thread, never on the event loop.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any, Callable, Optional

from . import card_write, progress_block, todo_progress, todo_reader

DEFAULT_INTERVAL_SECONDS = 30.0
DEFAULT_MAX_CARDS = 40
DEFAULT_SQLITE_TIMEOUT = 5.0
DEFAULT_CYCLE_BUDGET = 20.0
DEFAULT_MAX_BODY_BYTES = 256 * 1024

RUNNING_SQL = "SELECT id FROM tasks WHERE status = 'running' ORDER BY id"
"""A card that is not running any more but still carries a leading block.
``done``/``archived`` are excluded IN SQL, not after the fetch: they are
terminal and their block is left in place, so letting them consume the LIMIT
would starve cleanup on every live board (measured: 95 archived cards held all
20 slots, so a stopped card was never reached)."""
STOPPED_SQL = (
    "SELECT id, body, status FROM tasks WHERE status NOT IN ('running', 'done', 'archived') "
    "AND body LIKE '▶%' ORDER BY id LIMIT ?"
)


class SweepStats:
    __slots__ = ("running", "written", "stripped", "skipped", "errors")

    def __init__(self) -> None:
        self.running = 0
        self.written = 0
        self.stripped = 0
        self.skipped = 0
        self.errors = 0

    def __repr__(self) -> str:  # pragma: no cover - logging aid
        return (
            f"SweepStats(running={self.running}, written={self.written}, "
            f"stripped={self.stripped}, skipped={self.skipped}, errors={self.errors})"
        )


def _too_big(body: Optional[str], max_body_bytes: int) -> bool:
    if not body:
        return False
    try:
        return len(body.encode("utf-8")) > max_body_bytes
    except (UnicodeError, AttributeError):
        return True


def board_db_path(conn: sqlite3.Connection) -> Optional[str]:
    """Filesystem path of the board this connection points at, or None for ``:memory:``."""
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
    except sqlite3.Error:
        return None
    return (row[2] or None) if row else None


def acquire_sweep_lease(conn: sqlite3.Connection) -> Optional[int]:
    """Take the board-wide sweep lease a plugin instance holds for its whole life.

    Close the returned fd to release it; the OS frees it when the process dies.
    Two sweepers on one board rewrite each other's block shape (a strip from
    before v3.2 drops the two-line block but keeps its NBSP pad, so every one of
    its sweeps pushed the pad down a line and the description drifted away), so
    exactly one process may sweep. Returns None when another process holds it.
    """
    import fcntl
    import os

    path = board_db_path(conn)
    if not path:
        return None
    try:
        fd = os.open(path + ".progress.lock", os.O_CREAT | os.O_RDWR, 0o644)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def refresh_card(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    sqlite_timeout: float = DEFAULT_SQLITE_TIMEOUT,
    log: Optional[Callable[[str], None]] = None,
) -> bool:
    """Refresh one running card's block. Returns True when the body changed.

    Writes nothing when the card has no Tasklist, when M == 0, or when the body
    is already correct — an existing block is left untouched in the no-data case.
    """
    row = conn.execute(
        "SELECT body, status FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if row is None:
        return False
    if row["status"] != "running":
        return False
    old_body = row["body"]
    if _too_big(old_body, max_body_bytes):
        _emit(log, f"{task_id}: body over {max_body_bytes} bytes, skipped")
        return False

    ref = todo_reader.owner_ref(conn, task_id)
    if ref is None:
        _emit(log, f"{task_id}: no owner_admitted event, skipped")
        return False
    state_db, session_id = ref
    content = todo_reader.read_todo_content(state_db, session_id, timeout=sqlite_timeout)
    progress = todo_progress.progress_from_content(content)
    if progress is None:
        _emit(log, f"{task_id}: no Tasklist (or M==0), left as-is")
        return False

    block = progress_block.format_block(
        progress.done, progress.total, progress.label, progress.percent,
        todos=todo_progress.parse_todos(content),
    )
    new_body = progress_block.apply_block(old_body or "", block)
    if new_body == old_body:
        return False
    return card_write.write_block(conn, task_id, new_body)


def cleanup_stopped(
    conn: sqlite3.Connection,
    *,
    limit: int = 20,
    log: Optional[Callable[[str], None]] = None,
) -> int:
    """Strip a leading block from cards that stopped running. Returns the count.

    Requirement 6 ("never write to a card in done/archived") is stronger than
    requirement 3's "a card that is not running any more", so ``done`` and
    ``archived`` are excluded in ``STOPPED_SQL`` itself: they are terminal and
    their block is left in place (it is shadowed by ``latest_summary`` on the
    card anyway). Cleanup covers the non-terminal stopped states —
    todo/ready/blocked/review/triage.

    Two independent guards: the SQL excludes terminal statuses (so they cannot
    consume the ``LIMIT`` and starve a real stopped card), and the per-row
    :data:`card_write.FROZEN_STATUSES` check plus the ``strip_block`` CAS status
    predicate (defence in depth) mean a card that *transitions* to done/archived
    between the query and the write is still left alone.
    """
    stripped = 0
    try:
        rows = conn.execute(STOPPED_SQL, (limit,)).fetchall()
    except sqlite3.Error:
        return 0
    for row in rows:
        if row["status"] in card_write.FROZEN_STATUSES:
            _emit(log, f"{row['id']}: {row['status']} — block left in place")
            continue
        try:
            if card_write.strip_block(conn, row["id"]):
                stripped += 1
                _emit(log, f"{row['id']}: stripped stale block")
        except sqlite3.Error:
            continue
    return stripped


def sweep_once(
    conn: sqlite3.Connection,
    *,
    max_cards: int = DEFAULT_MAX_CARDS,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    sqlite_timeout: float = DEFAULT_SQLITE_TIMEOUT,
    budget_seconds: float = DEFAULT_CYCLE_BUDGET,
    log: Optional[Callable[[str], None]] = None,
    now: Callable[[], float] = time.monotonic,
) -> SweepStats:
    """One bounded sweep over the board. Never raises; per-card errors are isolated."""
    stats = SweepStats()
    deadline = now() + max(0.0, budget_seconds)
    try:
        rows = conn.execute(RUNNING_SQL).fetchall()
    except sqlite3.Error as exc:
        _emit(log, f"sweep: running query failed: {exc}")
        return stats

    stats.running = len(rows)
    for row in rows[:max_cards]:
        if now() > deadline:
            stats.skipped += len(rows) - stats.running
            _emit(log, "sweep: time budget exhausted, deferring remaining cards")
            break
        try:
            if refresh_card(
                conn,
                row["id"],
                max_body_bytes=max_body_bytes,
                sqlite_timeout=sqlite_timeout,
                log=log,
            ):
                stats.written += 1
        except Exception as exc:  # isolation: one bad card must not stop the sweep
            stats.errors += 1
            _emit(log, f"{row['id']}: refresh failed: {exc!r}")

    try:
        stats.stripped = cleanup_stopped(conn, log=log)
    except Exception as exc:  # defensive
        stats.errors += 1
        _emit(log, f"sweep: cleanup failed: {exc!r}")
    return stats


def _emit(log: Optional[Callable[[str], None]], message: str) -> None:
    if log is not None:
        try:
            log(message)
        except Exception:
            pass
