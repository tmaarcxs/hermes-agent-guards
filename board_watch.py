"""Shared event-driven plumbing for the Kanban wake hooks (stale, review loop, churn).

Every wake keeps a durable watermark in ``~/.hermes/state/<name>.json`` mapping
task id to the marker it was last woken for (unchanged from the sweep-gated
design). A companion cursor file ``<name>-cursor.json`` stores the id of the
last wake-relevant task_event the board had produced when the wake last swept.

``last_seen_event_id`` reads the board's ``task_events`` log directly, so the
cursor means "no relevant activity has happened since the last sweep". A wake
that sees the same relevant-event id twice skips the sweep entirely; any new
relevant event re-runs it. Because heartbeats and comments dominate the event
log, ``kinds``/``exclude`` let each wake track only the kinds that can change
its wake set — otherwise the loudest event kind would defeat the cursor for
every hook.

``poke``/``poke_post_tool_call`` are the belt-and-braces liveness path: a
successful board mutation pokes a timestamp file, and a poke newer than the
last sweep forces the next tick's sweep even when the cursor has not moved.
This covers a dispatcher crash between writing the cursor and the wake state
(re-evaluating is always safe: sweeps are idempotent once-per-watermark).

A wake whose condition ripens with time instead of events opts in with
``due_tracked``: its sweep returns ``(state, due_at)`` and each sweep stores
the next due time with the cursor (``None`` when nothing is pending). ``tick``
also sweeps when that time arrives even though the event log has not moved —
the stale wake's 1 h age gate on an otherwise quiet board. A cursor predating
the key makes the first tick sweep once to learn the due time, so an upgrade
never leaves a quiet-board crossing unswept. Count-based hooks are unchanged:
no key in the cursor, event/poke semantics as before.

The tick hook remains the driver — this module runs nowhere else — and every
writer fails open.
"""

import json
import logging
import time
from typing import Callable, Optional, Tuple, Union

logger = logging.getLogger(__name__)

POKE_FILE = "kanban-wake-poke.json"
_GRACE = 900.0

_LOGIN_TOOLS = frozenset({"kanban_show", "kanban_list", "kanban_attachments", "kanban_heartbeat"})


def state_path(name: str):
    """Path of a wake state/cursor file under the Kanban home's state dir."""
    from hermes_cli import kanban_db

    return __import__("pathlib").Path(kanban_db.kanban_db_path()).expanduser().parent / "state" / name


def read_json(path) -> dict:
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except (OSError, ValueError):
        return {}


def read_state(name: str) -> dict:
    return read_json(state_path(name))


def write_state(name: str, state: dict) -> None:
    path = state_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(path)


def poke() -> None:
    """Record that a board mutation just happened (best effort)."""
    try:
        write_state(POKE_FILE, {"at": time.time()})
    except Exception:
        pass


def poked_since(moment: float, now: Optional[float] = None) -> bool:
    """True when a poke landed after ``moment`` (0/absent cursor → False)."""
    at = float(read_json(state_path(POKE_FILE)).get("at") or 0)
    return bool(at) and bool(moment) and at > moment


def poke_post_tool_call(tool_name: str = "", args: Optional[dict] = None, status: Optional[str] = None, **_: object) -> None:
    """``post_tool_call`` hook: poke after successful Kanban board mutations.

    Read-only lookups and non-kanban calls do not poke; blocked calls never
    reach this hook (pre_tool_call stops them). Must be registered as its own
    entry so other post_tool_call guards cannot slow or fail the poke.
    """
    if not str(tool_name or "").startswith("kanban_") or str(tool_name) in _LOGIN_TOOLS:
        return
    if status not in (None, "success", "ok"):
        return
    poke()


def last_seen_event_id(conn, kinds: Optional[tuple] = None, exclude: tuple = ()) -> int:
    """Largest ``task_events`` id, optionally restricted to event kinds.

    ``kinds`` limits to exactly these kinds; ``exclude`` drops the given kinds
    (ignored when ``kinds`` is set). Empty log or empty kind set → 0.
    """
    if kinds:
        marks = ",".join("?" * len(kinds))
        row = conn.execute(
            f"select max(id) from task_events where kind in ({marks})", tuple(kinds)
        ).fetchone()
    elif exclude:
        marks = ",".join("?" * len(exclude))
        row = conn.execute(
            f"select max(id) from task_events where kind not in ({marks})", tuple(exclude)
        ).fetchone()
    else:
        row = conn.execute("select max(id) from task_events").fetchone()
    return int(row[0] or 0)


def _cursor_name(state_name: str) -> str:
    """Cursor file for a wake: ``kanban-stale-wake.json`` -> ``kanban-stale-wake-cursor.json``."""
    stem, dot, ext = state_name.rpartition(".")
    if not dot:
        return state_name + "-cursor"
    return stem + "-cursor." + ext


def _cursor(state_name: str) -> dict:
    return read_json(state_path(_cursor_name(state_name)))


SweepResult = Union[dict, Tuple[dict, Optional[float]], None]


def tick(conn, state_name: str, run_sweep: Callable[[dict], "SweepResult"],
         kinds: Optional[tuple] = None, exclude: tuple = (), now: Optional[float] = None,
         due_tracked: bool = False) -> None:
    """Event-driven driver: sweep when relevant board activity or a stored due time arrives.

    Skips when the largest wake-relevant event id equals the cursor, no poke
    landed after the last sweep and (with ``due_tracked``) no stored due time
    has passed. Otherwise runs ``run_sweep(state)`` — a sweep may return either
    the state alone or ``(state, due_at)`` (``due_tracked`` hooks), where
    ``due_at`` is the earliest epoch second at which the wake must sweep again
    even on a fully quiet board, or None when nothing ripens with time.
    Returning None leaves the wake state untouched (e.g. dry runs or failed
    sweeps); the cursor is recorded afterwards either way. A cursor older than
    a due-tracked upgrade lacks the ``due_at`` key, which makes the first tick
    sweep once to learn it. If the dispatcher dies between state and cursor
    writes, the poke-liveness path re-sweeps on the next tick.
    """
    cursor = _cursor(state_name)
    seen = _cursor_event_id(cursor)
    current = last_seen_event_id(conn, kinds, exclude)
    swept = float(cursor.get("swept_at") or 0)
    if due_tracked and "due_at" in cursor:
        stored = _cursor_due_at(cursor)
        due_at = float("inf") if stored is None else stored  # None: nothing pending
    elif due_tracked:
        due_at = 0.0  # cursor predates due tracking: sweep once to learn the due time
    else:
        due_at = float("inf")  # count-based wake: only events and pokes trigger
    if cursor and current == seen and not poked_since(swept, now) and _now(now) < due_at:
        return
    state = read_state(state_name)
    retry_due = None
    try:
        raw = run_sweep(state)
    except Exception:
        logger.warning("agent-guards board watch: %s sweep failed", state_name, exc_info=True)
        raw = None
        # A failed sweep must not consume a due trigger: keep the stored due
        # time so the next tick retries. The upgrade sentinel (0.0) and the
        # non-due sentinel (inf) are not real due times and are not kept.
        retry_due = due_at if isinstance(due_at, (int, float)) and due_at > 0 and due_at != float("inf") else None
    if due_tracked and isinstance(raw, tuple) and len(raw) == 2:
        new_state, sweep_due = raw
        if sweep_due is not None and not isinstance(sweep_due, (int, float)):
            logger.warning("agent-guards board watch: %s sweep returned a non-numeric due_at", state_name)
            sweep_due = None
    else:
        new_state, sweep_due = raw, None
    new_state = new_state if isinstance(new_state, dict) else None
    if new_state is not None and new_state != state:
        write_state(state_name, new_state)
    marker = {
        "event_id": max(current, seen),
        "swept_at": now if now is not None else time.time(),
    }
    if due_tracked:
        marker["due_at"] = sweep_due if retry_due is None else retry_due
    write_state(_cursor_name(state_name), marker)


def _cursor_event_id(cursor: dict) -> int:
    try:
        return int(cursor.get("event_id") or 0)
    except (TypeError, ValueError):
        return 0


def _cursor_due_at(cursor: dict) -> Optional[float]:
    try:
        value = cursor.get("due_at")
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


def _now(now: Optional[float]) -> float:
    return now if now is not None else time.time()
