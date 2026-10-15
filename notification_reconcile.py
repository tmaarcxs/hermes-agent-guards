"""Re-notify a card's subscribers when a terminal state event was never delivered.

The gateway notifier is at-least-once while a subscription lives: an event that
failed to send rewinds the claim and is retried next tick. Two holes remain.
After MAX_SEND_FAILURES consecutive failures the subscription is deleted and
everything it had not yet received is lost for good. And a card left with no
subscription row at all is invisible to every wake hook, which all query
``kanban_notify_subs`` for injection — no subs, no wake, forever.

On each dispatcher tick this reconciler compares each card's latest notifiable
state event (the kinds the gateway renders a text ping for) with the
subscriptions' ``last_ping_event_id`` checkpoints, the durable sent-ping
markers the gateway advances only after an adapter confirms a send. An event
no subscriber ever received triggers one terse notice through the same gateway
injection every other wake uses, naming the event kind and cause.

Two edges keep the notice honest and duplicate-free. A state younger than
the grace period is pending, not notified: the notifier may be delivering
it right now, and the reconciler must not race it. A card whose subscription
still exists never gets a notice either — the gateway's own retry loop
already re-sends to it until it delivers or drops it — but the lagging state
is recorded as pending so a subscription deleted later leaves a durable
record. The notice then belongs to the state that lost its delivery path
together with its subscription: a pending state whose subscription is gone
notifies exactly once through the remembered origins, and every later
uncovered transition of that card does too. First sight of an uncovered
card with no subscription row and no record is adopted silently — the
dropped row cannot say whether it ever delivered — and a card that never
had a subscriber in this gateway stays adopted, never noticed. An unchanged
state never re-notifies, a failed injection records nothing so the next
sweep retries, a delivered state drops out of the record, and a card that
left the reconciled statuses forgets its entry. Nothing here is an idle
reminder, replaces the gateway's own retry loop, or restarts or terminates
anything.

The sweep is event-driven (``board_watch.tick``): it runs on the first tick
after any notifiable event, a status change, a completed worker run, or a
successful board mutation on any process (``board_watch.poke``); on a quiet
board the tick skips the sweep entirely.

Fails open.
"""

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from . import board_watch
from . import stale_wake

logger = logging.getLogger(__name__)

NOTIFIABLE_KINDS = (
    "completed", "blocked", "gave_up", "crashed", "timed_out",
    "status", "review_requested", "changes_requested", "block_loop_detected",
)
STATUSES = ("todo", "ready", "review", "blocked", "triage", "scheduled", "done")
GRACE_SECONDS = 60
_ORIGINS = "subscribers"
_EVENT = "event_id"
_PENDING = "pending"
_CAUSE_FIELDS = ("reason", "error", "summary", "handoff", "result", "review")
_CAUSE_LIMIT = 200

_ctx: Any = None


def register(ctx) -> None:
    global _ctx
    _ctx = ctx
    ctx.register_hook("on_kanban_dispatch_tick", on_tick)


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
    return _home() / "state" / "kanban-notify-reconcile.json"


def uncovered_cards(conn: sqlite3.Connection, now: float) -> list[dict]:
    """Open cards whose latest notifiable event reached no subscriber.

    ``last_ping_event_id`` is the durable sent-ping checkpoint the gateway
    advances only after an adapter confirms a send, so an event id past every
    subscription's checkpoint is exactly "nobody got this", independent of
    delivery attempts and restarts. A card with no subscription row at all
    counts. Events inside the grace period are returned with ``pending``
    set: the notifier may still be delivering them.
    """
    placeholders = ",".join("?" * len(NOTIFIABLE_KINDS))
    rows = conn.execute(
        f"select t.id, t.status, t.title, e.id, e.kind, e.payload, e.created_at "
        f"from tasks t join task_events e on e.id = ("
        f"  select max(e2.id) from task_events e2"
        f"  where e2.task_id = t.id and e2.kind in ({placeholders})"
        f") where t.status in ({','.join('?' * len(STATUSES))})",
        (*NOTIFIABLE_KINDS, *STATUSES),
    )
    cards = []
    for tid, status, title, event_id, kind, payload, created_at in rows:
        subs = conn.execute(
            "select last_ping_event_id from kanban_notify_subs where task_id = ?",
            (tid,),
        ).fetchall()
        if subs and any(int(row[0] or 0) >= int(event_id) for row in subs):
            continue
        cards.append({
            "id": tid, "status": status, "title": title or "",
            "event_id": event_id, "kind": kind, "cause": _cause(payload),
            "pending": now - float(created_at or 0) < GRACE_SECONDS,
        })
    return cards


def _cause(payload: Optional[str]) -> str:
    try:
        data = json.loads(payload or "")
    except (ValueError, TypeError):
        data = {}
    for field in _CAUSE_FIELDS:
        text = str(data.get(field) or "").strip() if isinstance(data, dict) else ""
        if text:
            return " ".join(text.split())[:_CAUSE_LIMIT]
    return ""


def wake_text(card: dict) -> str:
    return (
        f"[Kanban unreconciled notice] {card['id']} reached {card['kind']} ({card['status']}) "
        f"but the gateway never confirmed delivery of that state to any subscriber: "
        f"{card['title'][:120]}"
        + (f"\nCause: {str(card['cause'])[:_CAUSE_LIMIT]}" if card["cause"] else "")
        + "\nThis is the one-time reconciled notice for this card state. Inspect the card and decide here: "
        "recover it, unblock it, or complete it with evidence. You will not be re-notified for this card "
        "until its state changes again."
    )


def _origins(card: dict) -> list[dict]:
    return [
        {k: v for k, v in origin.items() if v is not None}
        for origin in card.get(_ORIGINS) or ()
    ]


def sweep(conn: sqlite3.Connection, state: dict, inject, now: Optional[float] = None) -> dict:
    """Notify each lost-delivery state once per state; returns the new record.

    ``state`` maps task id to ``{"event_id": N, "subscribers": [...]}`` from
    earlier sweeps (``pending`` marks a state only ever seen while the card's
    own retry loop was alive). A delivered state or a card gone from the
    reconciled statuses drops its entry. Delivery targets are the live
    subscriptions, falling back to the remembered origins so a subscription
    deleted after the state was recorded still has somewhere to be delivered.
    """
    now = time.time() if now is None else now
    fresh: dict = {}
    for card in uncovered_cards(conn, now):
        tid = card["id"]
        previous = state.get(tid)
        previous = previous if isinstance(previous, dict) else {}
        seen = previous.get(_EVENT)
        live = stale_wake.subscribers(conn, tid)
        origins = live or list(previous.get(_ORIGINS) or [])
        entry = {_EVENT: card[_EVENT], _ORIGINS: origins}
        if card["pending"] or live:
            fresh[tid] = entry | {_PENDING: True}
            continue
        if seen is None:
            fresh[tid] = entry
            continue
        if seen == card[_EVENT] and not previous.get(_PENDING):
            fresh[tid] = entry
            continue
        if not origins:
            continue
        card[_ORIGINS] = origins
        if any(inject(wake_text(card), origin) for origin in _origins(card)):
            fresh[tid] = entry
    return fresh


def on_tick(board: Optional[str] = None, dry_run: bool = False, **_: Any) -> None:
    if dry_run:
        return
    try:
        from hermes_cli import kanban_db

        db = kanban_db.kanban_db_path(board=board)
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            path = _state_path()
            state = json.loads(path.read_text()) if path.exists() else {}
            new_state = sweep(conn, state, stale_wake._inject)
            if new_state != state:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(new_state))
        finally:
            conn.close()
    except Exception:
        logger.warning("agent-guards notify reconcile: sweep failed", exc_info=True)
