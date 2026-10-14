"""Wake the chat that created a Kanban card once when the card stops moving.

On each dispatcher tick (at most hourly) it finds triage, todo, ready, review,
blocked and scheduled cards whose last state event is at least 1 h old; heartbeats, comments,
edits, links and attachments are not state changes. Running cards
are never checked. For each such card it starts one agent turn in every chat
subscribed to the card (the creating chat is auto-subscribed) via the gateway's
plugin injection, so that session can recover the card where its work lives.

Each card is woken once per state: the event id of its last real change is
recorded, and the card is not woken again until a new state event changes it.
A genuinely blocked card therefore wakes its chat a single time, even after the
woken agent comments on it.

Fails open: errors are logged and never affect the dispatcher.
"""

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

_logger = logging.getLogger(__name__)

STATUSES = ("triage", "todo", "ready", "review", "blocked", "scheduled")
STALE_AFTER = 3600
CHECK_EVERY = 3600
_IGNORED_KINDS = (
    "heartbeat", "skipped_nonspawnable", "respawn_guarded", "claim_extended",
    "commented", "edited", "attached", "attachment_removed", "linked", "unlinked",
)

try:  # shared Jev client in this plugin package
    from .jev import decide as jev_decide
except Exception:  # pragma: no cover - the plugin must never fail on import
    def jev_decide(state: str, questions: dict, timeout: float = 2.0, caller: str = "") -> Optional[dict]:
        return None

# Jev wake filter: before injecting a wake, ask whether the card's recent context
# makes a wake useful. ``skip`` suppresses the wake; any Jev failure wakes anyway.
WAKE_QUESTIONS = {
    "wake": {
        "type": "choice",
        "instructions": (
            "A Kanban card has been idle for over an hour and an automatic wake message is about "
            "to be injected into the subscriber chat so an agent can recover it. From the card's "
            "recent activity, decide whether this wake would materially help."
        ),
        "criteria": {
            "wake": "The wake plausibly helps: the card needs attention, recovery, or new context.",
            "skip": "The wake would be noise: the card is waiting on a known external input, a scheduled time, or an in-flight dependency that activity alone cannot resolve.",
        },
    }
}

WAKE_SKIP_CONFIDENCE = 0.85

_ctx: Any = None
_last_check = 0.0


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


def _jev_wake_verdict(conn: sqlite3.Connection, task_id: str, card: dict, limit: int = 10) -> bool:
    """True when the wake should proceed. Jev judges the card's recent activity
    (from the DB if reachable, else the last known state) and a ``skip`` verdict
    with the seminar's confidence suppresses this one wake; any failure wakes anyway."""
    try:
        cursor = conn.execute(
            "select body from comments where task_id = ? order by id desc limit ?",
            (task_id, limit),
        )
        history = "\n".join(f"- {str(row[0] or '')[:300]}" for row in reversed(cursor.fetchall()))
    except Exception:
        history = ""
    state = (
        f"Kanban card {task_id} '{str(card.get('title') or '')[:120]}' is stale in status "
        f"'{card.get('status')}' for {card.get('age_h', 0):.0f}h and is about to auto-wake its subscriber chat.\n\n"
        f"Recent card activity (latest last):\n{history or '(no recorded comments)'}"
    )
    decision = jev_decide(state, WAKE_QUESTIONS, caller="stale_wake")
    if decision is None:
        return True
    answer = (decision.get("answers") or {}).get("wake")
    if not isinstance(answer, dict):
        return True
    choice = answer.get("choice")
    confidence = float(answer.get("confidence") or 0.0)
    if choice == "skip" and confidence >= WAKE_SKIP_CONFIDENCE:
        _logger.info("agent-guards stale wake: Jev suppressed wake for %s (confidence %.2f)", task_id, confidence)
        return False
    return True


def sweep(conn: sqlite3.Connection, state: dict, inject, now: float) -> dict:
    """Wake each stale card once per state, unless Jev suppresses it. Returns the new state."""
    stale = {card["id"]: card for card in stale_cards(conn, now)}
    kept = {tid: event_id for tid, event_id in state.items() if tid in stale}
    for tid, card in stale.items():
        if kept.get(tid) == card["event_id"]:
            continue
        # Jev filters the wake itself (not per subscriber): a suppressed card keeps its
        # old event id, so the next state change still wakes it.
        if not _jev_wake_verdict(conn, tid, card):
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
        _logger.warning("agent-guards stale wake: injection failed for %s", origin, exc_info=True)
        return False


def on_tick(board: Optional[str] = None, dry_run: bool = False, **_: Any) -> None:
    global _last_check
    now = time.time()
    if dry_run or now - _last_check < CHECK_EVERY:
        return
    _last_check = now
    try:
        from hermes_cli import kanban_db

        db = kanban_db.kanban_db_path(board=board)
        path = _state_path()
        state = json.loads(path.read_text()) if path.exists() else {}
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            new_state = sweep(conn, state, _inject, now)
        finally:
            conn.close()
        if new_state != state:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(new_state))
    except Exception:
        _logger.warning("agent-guards stale wake: sweep failed", exc_info=True)
