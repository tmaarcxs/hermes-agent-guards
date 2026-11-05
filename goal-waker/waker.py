"""Find parked goals whose barrier lifted and inject their continuation prompt.

Source of truth: ``state_meta`` rows ``goal:<session_id>`` in the profile's state.db
(read-only) and ``sessions.session_key`` for the chat route. The barrier decision is
the goal manager's own ``is_waiting()``, which also clears a satisfied barrier and
persists that, so a second sweep never wakes the same goal twice.
"""

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

logger = logging.getLogger(__name__)

CTX: Any = None
MIN_INTERVAL = 15.0
RETRY_SECONDS = 30
MAX_IDLE = 3 * 86400
WAIT_FIELDS = ("waiting_on_pid", "waiting_on_session", "waiting_until")
_LOCK = threading.Lock()
_state = {"last": 0.0}


def _home() -> Path:
    home = getattr(getattr(CTX, "_manager", None), "home_path", None) if CTX else None
    if home:
        return Path(home)
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home())


def parked_session_ids(conn: sqlite3.Connection) -> Iterator[str]:
    for key, value in conn.execute("select key, value from state_meta where key like 'goal:%'").fetchall():
        try:
            state = json.loads(value)
        except (TypeError, ValueError):
            continue
        if state.get("status") == "active" and any(state.get(f) for f in WAIT_FIELDS):
            yield key[len("goal:"):]


def _route(conn: sqlite3.Connection, session_id: str, now: float, routes: dict[str, float]) -> Optional[str]:
    """The messaging-route filter plus the wake debounce.

    ``routes`` is a sweep-local map session id → the ``last_activity_at`` value the
    session had when it was last woken. After an injection the next sweep skips
    every session whose goal row has not advanced since that value — the turn a
    continuation prompt produces touches the row, so no wake again before
    something actually responded. A refused injection writes nothing: no wake
    happened, and the retry re-park still stands.
    """
    row = conn.execute(
        "select session_key, origin_json, last_activity_at from sessions where id = ?", (session_id,)
    ).fetchone()
    if not row:
        return None
    activity = float(row[2] or 0)
    if now - activity > MAX_IDLE:
        return None
    if not row[0] or not row[1]:
        return None
    try:
        platform = json.loads(row[1]).get("platform")
    except (TypeError, ValueError):
        return None
    if not platform or platform == "local":
        return None
    stamp = routes.get(session_id)
    if stamp is not None and activity <= stamp:
        return None
    return row[0]


def sweep(
    conn: sqlite3.Connection, inject_fn: Callable[[str, str], bool], manager_factory: Callable[[str], Any],
    now: Optional[float] = None,
    routes: Optional[dict[str, float]] = None,
) -> Iterator[str]:
    """Wake the lifted goals once, then stay quiet while their rows stay still.

    ``routes`` is a sweep-local snapshot only when the caller passes a dict: a
    fresh ``None`` value means a fresh sweep (no wake yet), so the debounce map
    never becomes state that must be reset between sweeps.
    """
    if routes is None:
        routes = {}
    for session_id in list(parked_session_ids(conn)):
        route = _route(conn, session_id, time.time() if now is None else now, routes)
        if route is None:
            continue
        mgr = manager_factory(session_id)
        if mgr.is_waiting():
            continue
        prompt = mgr.next_continuation_prompt()
        if not prompt:
            continue
        if inject_fn(prompt, route):
            routes[session_id] = float(
                conn.execute(
                    "select last_activity_at from sessions where id = ?", (session_id,)
                ).fetchone()[0]
                or 0
            )
            yield session_id
        else:
            mgr.wait_for_seconds(RETRY_SECONDS, reason="goal-waker: injection refused, retrying")


def _inject(text: str, session_key: str) -> bool:
    try:
        return bool(CTX.inject_message(text, session_key=session_key))
    except Exception:
        logger.warning("goal-waker: injection failed for %s", session_key, exc_info=True)
        return False


def _manager(session_id: str):
    from hermes_cli.goals import GoalManager

    return GoalManager(session_id=session_id)


def on_tick(dry_run: bool = False, **_: Any) -> None:
    if dry_run or not _LOCK.acquire(blocking=False):
        return
    try:
        now = time.monotonic()
        if now - _state["last"] < MIN_INTERVAL:
            return
        _state["last"] = now
        conn = sqlite3.connect(f"file:{_home() / 'state.db'}?mode=ro", uri=True, timeout=2)
        try:
            woken = list(sweep(conn, _inject, _manager))
        finally:
            conn.close()
        if woken:
            logger.info("goal-waker: resumed parked goals %s", woken)
    except Exception:
        logger.warning("goal-waker: sweep failed", exc_info=True)
    finally:
        _LOCK.release()
