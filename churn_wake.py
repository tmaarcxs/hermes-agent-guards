"""Warn the owning chat when a Kanban card grinds on: too many runs or change-requests.

On each dispatcher tick it counts, per non-archived, non-done
card, the worker runs in ``task_runs`` and the ``changes_requested`` events in
``task_events``. The first crossing of 10 runs or 5 change-requests — then again
at double each (20/40 runs, 10/20 change-requests) — starts one agent turn in
every chat subscribed to the card with the counts and the last change-request
reasons, so the lead decides: split the card into smaller ones, narrow the
acceptance, or confirm continue. It is a warning only: nothing is blocked,
reassigned or cancelled.

Each threshold fires once per card while it stays past that level (a card whose
counts drop below a threshold re-arms it). Shares the gateway injection of
``stale_wake``.

The sweep is event-driven (``board_watch.tick``): it runs on the first tick
after any new ``changes_requested`` event, a status change, a completed worker
run, or a successful board mutation on any process (``board_watch.poke``); on
a quiet board the tick skips the sweep entirely. Thresholds are unchanged.

Fails open.
"""

import json
import logging
import sqlite3
from typing import Any, Optional

from . import board_watch
from . import stale_wake

logger = logging.getLogger(__name__)

RUN_STEPS = (10, 20, 40)
CR_STEPS = (5, 10, 20)
_OPEN = ("todo", "ready", "running", "review", "blocked", "triage", "scheduled")


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
    """Render the churn wake, naming the next threshold strictly above the encoded
    step this wake fired at — the sweep stores card["step"], so dedup and the hint
    must agree on the same arithmetic (defeats the old bare-arithmetic hint that
    promised a threshold the dedup can never fire)."""
    steps = [s for s in RUN_STEPS if s > card["step"]] + [100 + s for s in CR_STEPS if 100 + s > card["step"]]
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
        + (
            f" You will be warned again only if it reaches {next_step} runs."
            if next_step is not None and next_step < 100
            else ""
        )
        + (
            f" You will be warned again only if it reaches {next_step - 100} change-requests."
            if next_step is not None and 100 <= next_step < 200
            else ""
        )
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


_WAKE_KINDS = ("changes_requested", "status", "promoted", "promoted_manual", "completed", "timed_out", "crashed", "reclaimed")


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
            board_watch.tick(conn, "kanban-churn-wake.json", _sweep_for(board), kinds=_WAKE_KINDS)
        finally:
            conn.close()
    except Exception:
        logger.warning("agent-guards churn wake: sweep failed", exc_info=True)
