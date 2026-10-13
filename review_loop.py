"""Wake the chat that created a Kanban card when its review keeps bouncing.

On each dispatcher tick (at most every 5 min) it counts ``changes_requested`` events per
open card. When a card reaches 3, 6, 9, ... rounds it starts one agent turn in every chat
subscribed to the card, so the lead decides whether to narrow the scope, split the card,
fix the contract or take over, instead of letting implementer and reviewer loop.

Each threshold wakes once: the last woken round count is recorded per card. Shares the
gateway injection of ``stale_wake``. Fails open.
"""

import json
import logging
import sqlite3
import time
from typing import Any, Optional

from . import stale_wake

logger = logging.getLogger(__name__)

EVERY = 3
CHECK_EVERY = 300
_OPEN = ("todo", "ready", "running", "review", "blocked")

_last_check = 0.0


def looping_cards(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "select t.id, t.status, t.title, count(e.id) from tasks t "
        "join task_events e on e.task_id = t.id and e.kind = 'changes_requested' "
        f"where t.status in ({','.join('?' * len(_OPEN))}) group by t.id having count(e.id) >= ?",
        (*_OPEN, EVERY),
    )
    return [{"id": tid, "status": status, "title": title or "", "rounds": n} for tid, status, title, n in rows]


def last_reason(conn: sqlite3.Connection, task_id: str) -> str:
    row = conn.execute(
        "select payload from task_events where task_id = ? and kind = 'changes_requested' order by id desc limit 1",
        (task_id,),
    ).fetchone()
    try:
        return str(json.loads(row[0]).get("reason") or "")[:300] if row and row[0] else ""
    except (ValueError, AttributeError):
        return ""


def wake_text(card: dict, reason: str) -> str:
    return (
        f"[Kanban review loop] {card['id']} has had {card['rounds']} review rounds with changes requested "
        f"({card['status']}): {card['title'][:120]}\n"
        f"Latest requested change: {reason or '(none recorded)'}\n"
        "Implementer and reviewer are not converging. Decide here instead of letting it loop: narrow the "
        "acceptance criteria, split the card, correct the contract or inputs, or take the remaining work "
        "over. Record the decision as a comment on the card. "
        f"You will be woken again only if it reaches {card['rounds'] // EVERY * EVERY + EVERY} rounds."
    )


def sweep(conn: sqlite3.Connection, state: dict, inject) -> dict:
    """Wake each looping card once per threshold. Returns the new state."""
    cards = {card["id"]: card for card in looping_cards(conn)}
    kept = {tid: n for tid, n in state.items() if tid in cards}
    for tid, card in cards.items():
        threshold = card["rounds"] // EVERY * EVERY
        if kept.get(tid, 0) >= threshold:
            continue
        text = wake_text(card, last_reason(conn, tid))
        origins = stale_wake.subscribers(conn, tid)
        if any(inject(text, {k: v for k, v in origin.items() if v is not None}) for origin in origins):
            kept[tid] = threshold
    return kept


def on_tick(board: Optional[str] = None, dry_run: bool = False, **_: Any) -> None:
    global _last_check
    now = time.time()
    if dry_run or now - _last_check < CHECK_EVERY:
        return
    _last_check = now
    try:
        from hermes_cli import kanban_db

        db = kanban_db.kanban_db_path(board=board)
        path = stale_wake._home() / "state" / "kanban-review-loop.json"
        state = json.loads(path.read_text()) if path.exists() else {}
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            new_state = sweep(conn, state, stale_wake._inject)
        finally:
            conn.close()
        if new_state != state:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(new_state))
    except Exception:
        logger.warning("agent-guards review loop: sweep failed", exc_info=True)
