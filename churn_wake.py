"""Warn the owning chat when a Kanban card grinds on: too many runs or change-requests.

On each dispatcher tick (at most hourly) it counts, per non-archived, non-done
card, the worker runs in ``task_runs`` and the ``changes_requested`` events in
``task_events``. The first crossing of 10 runs or 5 change-requests — then again
at double each (20/40 runs, 10/20 change-requests) — starts one agent turn in
every chat subscribed to the card with the counts and the last change-request
reasons, so the lead decides: split the card into smaller ones, narrow the
acceptance, or confirm continue. It is a warning only: nothing is blocked,
reassigned or cancelled.

Each threshold fires once per card while it stays past that level (a card whose
counts drop below a threshold re-arms it). Shares the gateway injection of
``stale_wake``. Fails open.
"""

import json
import logging
import sqlite3
import time
from typing import Any, Optional

from . import stale_wake

logger = logging.getLogger(__name__)

RUN_STEPS = (10, 20, 40)
CR_STEPS = (5, 10, 20)
CHECK_EVERY = 3600
_OPEN = ("todo", "ready", "running", "review", "blocked", "triage", "scheduled")

_last_check = 0.0


def hot_cards(conn: sqlite3.Connection) -> list[dict]:
    """Cards past a threshold, with the lowest step each count has crossed."""
    rows = conn.execute(
        "select t.id, t.status, t.title, "
        "(select count(*) from task_runs r where r.task_id = t.id), "
        "(select count(*) from task_events e where e.task_id = t.id and e.kind = 'changes_requested') "
        f"from tasks t where t.status in ({','.join('?' * len(_OPEN))})",
        _OPEN,
    )
    cards = []
    for tid, status, title, runs, crs in rows:
        step = max(
            [s for s in RUN_STEPS if runs >= s] + [100 + s for s in CR_STEPS if crs >= s] + [0]
        )
        if step:
            cards.append({"id": tid, "status": status, "title": title or "", "runs": runs, "crs": crs, "step": step})
    return cards


def last_reasons(conn: sqlite3.Connection, task_id: str, limit: int = 3) -> list[str]:
    rows = conn.execute(
        "select payload from task_events where task_id = ? and kind = 'changes_requested' "
        "order by id desc limit ?",
        (task_id, limit),
    ).fetchall()
    reasons = []
    for (payload,) in rows:
        try:
            reason = str(json.loads(payload).get("reason") or "")
        except (ValueError, AttributeError):
            reason = str(payload or "")
        reasons.append(reason.strip()[:160])
    return [r for r in reasons if r]


def wake_text(card: dict, reasons: list[str]) -> str:
    steps = [s for s in RUN_STEPS + CR_STEPS if s > (card["step"] if card["step"] < 100 else card["step"] - 100)]
    next_step = min(steps) if steps else None
    lines = [
        f"[Kanban churn warning] {card['id']} is at {card['runs']} runs and {card['crs']} change-requests "
        f"({card['status']}): {card['title'][:120]}"
    ]
    for reason in reasons:
        lines.append(f"Requested change: {reason}")
    lines.append(
        "The card is grinding on. Decide here: split it into smaller cards, narrow the acceptance criteria, "
        "or confirm continue. Record the decision as a comment on the card."
        + (f" You will be warned again only if it reaches {next_step}." if next_step else "")
    )
    return "\n".join(lines)


def sweep(conn: sqlite3.Connection, state: dict, inject) -> dict:
    """Warn each hot card once per threshold, in every subscribed chat. Returns the new state."""
    cards = {card["id"]: card for card in hot_cards(conn)}
    kept = {tid: step for tid, step in state.items() if tid in cards}
    for tid, card in cards.items():
        if kept.get(tid, 0) >= card["step"]:
            continue
        text = wake_text(card, last_reasons(conn, tid))
        delivered = [
            inject(text, {k: v for k, v in origin.items() if v is not None})
            for origin in stale_wake.subscribers(conn, tid)
        ]
        if any(delivered):
            kept[tid] = card["step"]
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
        path = stale_wake._home() / "state" / "kanban-churn-wake.json"
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
        logger.warning("agent-guards churn wake: sweep failed", exc_info=True)
