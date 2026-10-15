"""Wake the chat that created a Kanban card once when the card stops moving.

On each dispatcher tick it finds triage, todo, ready, review, blocked and
scheduled cards whose last state event is at least 1 h old; heartbeats, comments,
edits, links and attachments are not state changes. Running cards
are never checked. For each such card it starts one agent turn in every chat
subscribed to the card (the creating chat is auto-subscribed) via the gateway's
plugin injection, so that session can recover the card where its work lives.

The sweep is event-driven (``board_watch.tick``) with a due-time trigger: it
runs on the first tick after any state event this wake tracks (any kind except
the ignored ones), a successful board mutation on any process
(``board_watch.poke``), or the earliest moment an un-woken open card crosses
its 1 h age: each sweep returns the crossing time of the card that will go
stale next, stored as ``due_at`` in the cursor, and the tick sweeps when the
clock reaches it even on a fully quiet board (no fixed interval).

Each card is woken once per state: the event id of its last real change is
recorded, and the card is not woken again until a new state event changes it.
A genuinely blocked card therefore wakes its chat a single time, even after the
woken agent comments on it.

The same module also provides the stall wake, registered as its own unthrottled
hook (every tick): a todo or ready card whose last 30 task_events are all
``respawn_guarded`` events with ``reason=active_pr`` is being held out of the
ready queue by the dispatcher's 24 h PR-URL respawn guard despite the queue
draining — a silent stall the 1 h stale threshold is too slow to surface. It
wakes the subscribed chats once per guard event, keyed on the newest
respawn_guarded event id, naming the cause and the release levers.

Fails open: errors are logged and never affect the dispatcher.
"""

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from . import board_watch

logger = logging.getLogger(__name__)

STATUSES = ("triage", "todo", "ready", "review", "blocked", "scheduled")
STALE_AFTER = 3600
STALL_MIN_EVENTS = 30
STALL_STATUSES = ("todo", "ready")
STALL_REASON = "active_pr"
_IGNORED_KINDS = (
    "heartbeat", "skipped_nonspawnable", "respawn_guarded", "claim_extended",
    "commented", "edited", "attached", "attachment_removed", "linked", "unlinked",
)

_ctx: Any = None


def register(ctx) -> None:
    global _ctx
    _ctx = ctx
    ctx.register_hook("on_kanban_dispatch_tick", on_tick)
    ctx.register_hook("post_tool_call", _poke_post_tool_call)


def _home() -> Path:
    home = getattr(getattr(_ctx, "_manager", None), "home_path", None)
    if home:
        return Path(home)
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        return Path.home() / ".hermes"


def _state_path() -> Path:
    return _home() / "state" / "kanban-stale-wake.json"


def stale_cards(conn: sqlite3.Connection, now: float) -> list[dict]:
    placeholders = ",".join("?" * len(_IGNORED_KINDS))
    last = (
        f"(select max(e.id) from task_events e where e.task_id = t.id and e.kind not in ({placeholders}))"
    )
    rows = conn.execute(
        f"select t.id, t.status, t.title, t.block_kind, t.created_at, ev.id, ev.created_at "
        f"from tasks t left join task_events ev on ev.id = {last} "
        f"where t.status in ({','.join('?' * len(STATUSES))})",
        (*_IGNORED_KINDS, *STATUSES),
    )
    cards = []
    for tid, status, title, block_kind, created, event_id, event_at in rows:
        age = now - float(event_at or created)
        if age >= STALE_AFTER:
            cards.append({
                "id": tid, "status": status, "title": title or "", "block_kind": block_kind,
                "event_id": event_id or 0, "age_h": age / 3600,
            })
    return cards


def next_due(conn: sqlite3.Connection, now: float) -> Optional[float]:
    """Earliest future moment an open card crosses ``STALE_AFTER``.

    Mirrors ``stale_cards``' age key (last non-ignored event, falling back to
    creation) for every open card and returns the smallest crossing time still
    ahead of ``now``. Crossings already in the past belong to the current
    sweep (its wake set includes every crossed card), so nothing needs a later
    tick for them. None when nothing will ripen.
    """
    placeholders = ",".join("?" * len(_IGNORED_KINDS))
    last = (
        f"(select max(e.id) from task_events e where e.task_id = t.id and e.kind not in ({placeholders}))"
    )
    rows = conn.execute(
        f"select t.created_at, ev.created_at "
        f"from tasks t left join task_events ev on ev.id = {last} "
        f"where t.status in ({','.join('?' * len(STATUSES))})",
        (*_IGNORED_KINDS, *STATUSES),
    )
    due = None
    for created, event_at in rows:
        crossing = float(event_at or created) + STALE_AFTER
        if crossing <= now:
            continue
        if due is None or crossing < due:
            due = crossing
    return due


def subscribers(conn: sqlite3.Connection, task_id: str) -> list[dict]:
    rows = conn.execute(
        "select platform, chat_id, thread_id, chat_type, user_id, user_id_alt, notifier_profile "
        "from kanban_notify_subs where task_id = ?",
        (task_id,),
    )
    return [
        {
            "platform": platform, "chat_id": chat_id, "thread_id": thread_id or None,
            "chat_type": chat_type or "dm", "user_id": user_id, "user_id_alt": user_id_alt,
            "profile": profile or None,
        }
        for platform, chat_id, thread_id, chat_type, user_id, user_id_alt, profile in rows
    ]


def wake_text(card: dict) -> str:
    status = card["status"] + (f"/{card['block_kind']}" if card["block_kind"] else "")
    return (
        f"[Kanban stale card] {card['id']} has had no movement for {card['age_h']:.0f} h ({status}): "
        f"{card['title'][:120]}\n"
        "This is a one-time automatic wake for this card state. Inspect the card and recover it here: "
        "resume, unblock, complete with evidence, or confirm a genuine block with its exact release "
        "condition. If it is genuinely blocked on something only the user can provide, say so briefly. "
        "You will not be woken again for this card until its state changes."
    )


def stalled_cards(conn: sqlite3.Connection) -> list[dict]:
    """Todo/ready cards whose last STALL_MIN_EVENTS events are all active_pr respawn guards."""
    rows = conn.execute(
        f"select t.id, t.status, t.title from tasks t where t.status in ({','.join('?' * len(STALL_STATUSES))})",
        STALL_STATUSES,
    )
    cards = []
    for tid, status, title in rows:
        events = conn.execute(
            "select id, kind, payload from task_events where task_id = ? order by id desc limit ?",
            (tid, STALL_MIN_EVENTS),
        ).fetchall()
        if len(events) < STALL_MIN_EVENTS or any(
            kind != "respawn_guarded" or STALL_REASON not in str(payload or "")
            for _id, kind, payload in events
        ):
            continue
        cards.append({"id": tid, "status": status, "title": title or "", "event_id": events[0][0]})
    return cards


def stall_text(card: dict) -> str:
    return (
        f"[Kanban stall] {card['id']} ({card['status']}) has been refused by the dispatcher respawn guard "
        f"reason={STALL_REASON} for the last {STALL_MIN_EVENTS} ticks: {card['title'][:120]}\n"
        "A full GitHub PR URL in one of this card's comments from the last 24 h makes the dispatcher treat "
        "the card as 'a worker already opened a PR' and hold it out of the ready queue every tick. Fix here: "
        "remove or reword that comment (cite the PR as owner/repo#N — the guard only matches full URLs), "
        "assign the card explicitly, or archive it if the outcome was delivered outside the card."
    )


def stall_sweep(conn: sqlite3.Connection, state: dict, inject, now: float) -> dict:
    """Wake each actively stalling card once per guard event. Returns the new state."""
    stalled = {card["id"]: card for card in stalled_cards(conn)}
    kept = {tid: event_id for tid, event_id in state.items() if tid in stalled}
    for tid, card in stalled.items():
        if kept.get(tid) == card["event_id"]:
            continue
        text = stall_text(card)
        if any(inject(text, {k: v for k, v in origin.items() if v is not None}) for origin in subscribers(conn, tid)):
            kept[tid] = card["event_id"]
    return kept


def sweep(conn: sqlite3.Connection, state: dict, inject, now: float) -> dict:
    """Wake each stale card once per state. Returns the new state."""
    stale = {card["id"]: card for card in stale_cards(conn, now)}
    kept = {tid: event_id for tid, event_id in state.items() if tid in stale}
    for tid, card in stale.items():
        if kept.get(tid) == card["event_id"]:
            continue
        origins = subscribers(conn, tid)
        text = wake_text(card)
        if any(inject(text, {k: v for k, v in origin.items() if v is not None}) for origin in origins):
            kept[tid] = card["event_id"]
    return kept


def _inject(text: str, origin: dict) -> bool:
    try:
        return bool(_ctx.inject_message(text, origin=origin))
    except Exception:
        logger.warning("agent-guards stale wake: injection failed for %s", origin, exc_info=True)
        return False


def _poke_post_tool_call(**kwargs: Any) -> None:
    board_watch.poke_post_tool_call(**kwargs)


def _sweep_for(board: Optional[str], now: float):
    def run(state: dict) -> tuple:
        from hermes_cli import kanban_db

        db = kanban_db.kanban_db_path(board=board)
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            new_state = sweep(conn, state, _inject, now)
            try:
                due = next_due(conn, now)
            except Exception:
                logger.warning("agent-guards stale wake: due-time computation failed", exc_info=True)
                due = None
        finally:
            conn.close()
        return new_state, due

    return run


def on_tick(board: Optional[str] = None, dry_run: bool = False, now: Optional[float] = None, **_: Any) -> None:
    if dry_run:
        return
    try:
        from hermes_cli import kanban_db

        now = time.time() if now is None else now
        db = kanban_db.kanban_db_path(board=board)
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            board_watch.tick(
                conn, "kanban-stale-wake.json", _sweep_for(board, now),
                exclude=_IGNORED_KINDS, now=now, due_tracked=True,
            )
        finally:
            conn.close()
    except Exception:
        logger.warning("agent-guards stale wake: sweep failed", exc_info=True)


def _stall_state_path() -> Path:
    return _home() / "state" / "kanban-stall-wake.json"


def stall_wake_on_tick(board: Optional[str] = None, dry_run: bool = False, **_: Any) -> None:
    """Unthrottled stall wake, separate hook so the 1 h stale sweep stays untouched."""
    try:
        from hermes_cli import kanban_db

        db = kanban_db.kanban_db_path(board=board)
        path = _stall_state_path()
        state = json.loads(path.read_text()) if path.exists() else {}
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            new_state = stall_sweep(conn, state, _inject, time.time())
        finally:
            conn.close()
        if new_state != state:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(new_state))
    except Exception:
        logger.warning("agent-guards stall wake: sweep failed", exc_info=True)
