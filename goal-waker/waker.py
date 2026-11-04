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


def messaging_route(conn: sqlite3.Connection, session_id: str) -> Optional[str]:
    row = conn.execute("select session_key, origin_json from sessions where id = ?", (session_id,)).fetchone()
    if not row or not row[0] or not row[1]:
        return None
    try:
        platform = json.loads(row[1]).get("platform")
    except (TypeError, ValueError):
        return None
    return row[0] if platform and platform != "local" else None


def sweep(conn: sqlite3.Connection, inject_fn: Callable[[str, str], bool], manager_factory: Callable[[str], Any]) -> list:
    woken = []
    for session_id in list(parked_session_ids(conn)):
        route = messaging_route(conn, session_id)
        if route is None:
            continue
        mgr = manager_factory(session_id)
        if mgr.is_waiting():
            continue
        prompt = mgr.next_continuation_prompt()
        if not prompt:
            continue
        if inject_fn(prompt, route):
            woken.append(session_id)
        else:
            mgr.wait_for_seconds(RETRY_SECONDS, reason="goal-waker: injection refused, retrying")
    return woken


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
            woken = sweep(conn, _inject, _manager)
        finally:
            conn.close()
        if woken:
            logger.info("goal-waker: resumed parked goals %s", woken)
    except Exception:
        logger.warning("goal-waker: sweep failed", exc_info=True)
    finally:
        _LOCK.release()
