"""Wake the chat that created a Kanban card when its review keeps bouncing.

On each dispatcher tick it counts ``changes_requested`` events per
open card. When a card reaches 3, 6, 9, ... rounds it starts one agent turn in every chat
subscribed to the card, so the lead decides whether to narrow the scope, split the card,
fix the contract or take over, instead of letting implementer and reviewer loop.

Each threshold wakes once: the last woken round count is written to the durable
state before the injection runs, so a gateway restart between the two cannot replay a
threshold that may have already woken a lead, at the cost of dropping a threshold's
wake if the gateway dies inside the smaller marker-to-inject window (the next
threshold re-arms it). Shares the gateway injection of ``stale_wake``.

The sweep is event-driven (``board_watch.tick``): it runs on the first tick
after any new ``changes_requested`` event, a card status change, or a successful
board mutation on any process (``board_watch.poke``); on a quiet board the tick
skips the sweep entirely. Thresholds are unchanged.

Fails open.
"""

import json
import logging
import sqlite3
from typing import Any, Optional

from . import board_watch

from . import stale_wake

logger = logging.getLogger(__name__)

EVERY = 3
STATE_NAME = "kanban-review-loop.json"
_OPEN = ("todo", "ready", "running", "review", "blocked")


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
        "Implementer and reviewer are not converging. Decide it here with one of:\n"
        "(1) narrow the contract and send the card back — if the recent findings are sentence or number "
        "integrity only, record this convergence contract on the card: every numeric sentence names the "
        "script and output that print it, or is removed; the reviewer re-executes the scripts, checks "
        "that mapping and the named lines, and opens no new analyses (a new analysis goes to a new card);\n"
        "(2) continue over the real defects — check whether they are new or repeats of earlier ones and "
        "whether a successor card is needed once they are fixed;\n"
        "(3) split the card — close it and hand the remaining, still-tractable parts to fresh cards;\n"
        "(4) take it over — finish the remaining work yourself, or archive the card with a comment on "
        "where the outcome lives.\n"
        f"The wake repeats at the next threshold (every {EVERY} review rounds)."
    )


def sweep(conn: sqlite3.Connection, state: dict, inject, persist=None) -> dict:
    """Wake each looping card once per threshold. Returns the new state.

    Durable-write-before-send: the woken thresholds of this sweep are flushed to
    the state file *before* the first ``inject`` call, so a gateway restart that
    lands between record and send cannot replay the same threshold's wake into a
    lead's chat (the duplicate at t_23a8c303 after the 10-14/10-15 restarts).
    The trade is at-most-once: a death inside the smaller marker-to-inject
    window drops that wake, and the next threshold re-arms it. Each injection
    outcome rewrites the file with the live record: a delivery marks that
    card's threshold, a failed delivery drops only that card's entry — every
    other card's flushed marker stays, so the failed card retries on the next
    tick and a delivered sibling's marker is never erased.
    ``persist``
    replaces the state file for tests; production passes nothing and the write
    goes to ``board_watch.write_state``.
    """
    def _write(new_state: dict) -> None:
        if persist is not None:
            persist(new_state)
        else:
            board_watch.write_state(STATE_NAME, new_state)

    cards = {card["id"]: card for card in looping_cards(conn)}
    kept = {tid: n for tid, n in state.items() if tid in cards}
    flush = []
    for tid, card in cards.items():
        threshold = card["rounds"] // EVERY * EVERY
        if kept.get(tid, 0) >= threshold:
            continue
        flush.append((tid, threshold, card))
    durable = dict(kept)
    if flush:
        durable.update({tid: threshold for tid, threshold, _card in flush})
        _write(durable)
        for tid, threshold, card in flush:
            text = wake_text(card, last_reason(conn, tid))
            origins = stale_wake.subscribers(conn, tid)
            if any(inject(text, {k: v for k, v in origin.items() if v is not None}) for origin in origins):
                durable[tid] = threshold
            else:
                durable.pop(tid, None)
            _write(durable)
    return durable


_WAKE_KINDS = ("changes_requested", "status", "promoted", "promoted_manual", "completed", "archived")


def _sweep_for(board: Optional[str]):
    def run(state: dict) -> dict:
        from hermes_cli import kanban_db

        db = kanban_db.kanban_db_path(board=board)
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            return sweep(conn, state, stale_wake._inject)
        finally:
            conn.close()

    return run


def on_tick(board: Optional[str] = None, dry_run: bool = False, **_: Any) -> None:
    if dry_run:
        return
    try:
        from hermes_cli import kanban_db

        db = kanban_db.kanban_db_path(board=board)
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            board_watch.tick(conn, STATE_NAME, _sweep_for(board), kinds=_WAKE_KINDS)
        finally:
            conn.close()
    except Exception:
        logger.warning("agent-guards review loop: sweep failed", exc_info=True)
