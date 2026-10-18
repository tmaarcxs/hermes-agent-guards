"""Warn the owning chat when a Kanban card grinds on: too many runs or change-requests.

On each dispatcher tick it counts, per non-archived, non-done
card, the worker runs in ``task_runs`` and the ``changes_requested`` events in
``task_events``. Each threshold — 10/20/40 runs, 5/10/20 change-requests —
fires one agent turn in every chat subscribed to the card with the counts and
the last change-request reasons, so the lead decides: split the card into
smaller ones, narrow the acceptance, or confirm continue. It is a warning
only: nothing is blocked, reassigned or cancelled.

Per family (runs vs change-requests) the sweep bookkeeping stores the encoded
top threshold that family already fired — run thresholds raw (10/20/40),
change-request thresholds +100 (105/110/120) — as a two-slot state, so the
families fire independently: a card whose change-requests crossed 5 still
warns at 10, 20 and 40 runs when it reaches them, and a family that goes
below its fired threshold re-arms (a stepwise drop back past the level, then
a fresh crossing, fires that family again). One wake per sweep at the highest
firing family. The wake names the next threshold of the family that will
fire next. Runs older than this format stored a single scalar step; a raw
value is read as a runs-family slot, 100+n as a change-request slot.

The sweep is event-driven (``board_watch.tick``): it runs on the first tick
after any new ``changes_requested`` event, a status change, a completed worker
run, or a successful board mutation on any process (``board_watch.poke``); on
a quiet board the tick skips the sweep entirely.

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
OPEN_STATUSES = ("todo", "ready", "running", "review", "blocked", "triage", "scheduled")


def _raw(value: int) -> int:
    """Change-request slots are stored +100; run slots stay raw."""
    return value - 100 if value >= 100 else value


def _family(value: int) -> int:
    return 1 if value >= 100 else 0


def hot_cards(conn: sqlite3.Connection) -> list[dict]:
    """Open cards past at least one threshold, with each family's crossed top. Duplicate
    task rows for one id (the tests exercise this) are grouped: same id, same counts."""
    rows = conn.execute(
        "select t.id, t.status, t.title, "
        "(select count(*) from task_runs r where r.task_id = t.id), "
        "(select count(*) from task_events e where e.task_id = t.id and e.kind = 'changes_requested') "
        f"from tasks t where t.status in ({','.join('?' * len(OPEN_STATUSES))})",
        OPEN_STATUSES,
    )
    by_id = {}
    order = []
    for tid, status, title, runs, crs in rows:
        run_top = max((s for s in RUN_STEPS if runs >= s), default=0)
        cr_top = max((s for s in CR_STEPS if crs >= s), default=0)
        card = by_id.get(tid)
        if card is None:
            card = {"id": tid, "status": status, "title": title or "", "runs": runs, "crs": crs, "steps": [run_top, 100 + cr_top if cr_top else 0]}
            by_id[tid] = card
            order.append(card)
        else:
            card["runs"] = max(card["runs"], runs)
            card["crs"] = max(card["crs"], crs)
            card["steps"] = [max(card["steps"][0], run_top), max(card["steps"][1], 100 + cr_top if cr_top else 0)]
    return [card for card in order if any(card["steps"])]


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
    """Render the wake. The hint names the next threshold of the family that will fire
    next — the smallest not-yet-consumed threshold across both families, family-explicit —
    so it never promises a threshold the bookkeeping has already consumed."""
    consumed = card["steps"]
    assert len(consumed) == 2
    candidates = []
    for family, steps in ((0, RUN_STEPS), (1, CR_STEPS)):
        if consumed[family] == 0:
            next_step = steps[0]
        else:
            later = [s for s in steps if s > _raw(consumed[family])]
            if not later:
                continue
            next_step = later[0]
        candidates.append((100 + next_step if family else next_step, family, next_step))
    hint = ""
    if candidates:
        encoded, family, next_step = min(candidates)
        unit = "change-requests" if family else "runs"
        hint = f" You will be warned again only if it reaches {next_step} {unit}."
    lines = [
        f"[Kanban churn warning] {card['id']} is at {card['runs']} runs and {card['crs']} change-requests "
        f"({card['status']}): {card['title'][:120]}"
    ]
    for reason in reasons:
        lines.append(f"Requested change: {reason}")
    lines.append(
        "The card is grinding on. Decide here: split it into smaller cards, narrow the acceptance criteria, "
        "or confirm continue. Record the decision as a comment on the card." + hint
    )
    return "\n".join(lines)


def _fired_slots(conn: sqlite3.Connection, tid: str, merged: list[int], card: dict, inject) -> list[int]:
    """Send one wake reporting the merged bookkeeping; return the delivered flag."""
    text = wake_text(card, last_reasons(conn, tid))
    card["steps"] = merged
    delivered = [
        inject(text, {k: v for k, v in origin.items() if v is not None})
        for origin in stale_wake.subscribers(conn, tid)
    ]
    return any(delivered)


def sweep(conn: sqlite3.Connection, state: dict, inject) -> dict:
    """Warn each hot card whose crossed top exceeds its stored family top once per
    sweep, in every subscribed chat. Returns the new per-family bookkeeping."""
    new_state = {}
    for card in hot_cards(conn):
        tid = card["id"]
        kept = state.get(tid, [])
        if isinstance(kept, int):
            kept = [0, 0] if kept == 0 else ([kept, 0] if kept < 100 else [0, kept])
        if len(kept) != 2:
            kept = [0, 0]
        current = card["steps"]
        fired = [0, 0]
        for family in (0, 1):
            raw_current = _raw(current[family])
            raw_kept = _raw(kept[family])
            if raw_current > raw_kept:
                fired[family] = current[family]
        if fired == [0, 0]:
            if any(kept):
                new_state[tid] = list(kept)
            continue
        raw_fire = max(_raw(s) for s in fired if s)
        fire_family = _family([s for s in fired if s and _raw(s) == raw_fire][0])
        merged = [max(kept[f], fired[f]) for f in (0, 1)]
        if not _fired_slots(conn, tid, merged, card, inject):
            continue
        new_state[tid] = merged
    return new_state


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
