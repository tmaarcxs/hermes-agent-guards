"""One chassis for every Kanban wake (board_watch stays the shared engine).

Each wake is a condition — a board query, a text template, a dedup key — over
the shared pieces: subscriber lookup, gateway injection, board_watch cursors
and durable state files. The five conditions kept after the debloat census
(t_8b9e56aa) plus the unthrottled stall wake:

- ``stale_parks``: a parked card (blocked/triage with a block_kind record)
  whose last non-ignored event is at least ``STALE_AFTER`` old wakes once per
  parked stretch minus the parks whose own notification already carried the
  state (capability/triage). The plain open-card stale class is gone (0/17
  acted in the census).
- ``review_loop``: an open card reaching 3, 6, 9... ``changes_requested``
  rounds wakes once per threshold, durable-write-before-send (best
  follow-through, 8/10).
- ``churn_corner``: a non-archived, non-done card with at least 10 change
  requests warns once per ungated crossing; the runs family is gone.
- ``waiting_run``: a running card whose newest non-empty heartbeat note
  admits a wait on an external gate named inside the note's own clause wakes
  once per run when the streak is ``WAIT_AFTER`` old.
- ``reconcile``: an out-of-running card's latest notifiable state event that
  no subscription confirmed delivered (past the grace window, delivery path
  gone = recorded pending, sub then dropped) gets one terse notice through
  the remembered origins.
- ``stall``: a todo/ready card whose last ``STALL_MIN_EVENTS`` events are all
  ``active_pr`` respawn guards wakes once per guard episode and never twice
  within ``STALL_MIN_INTERVAL`` (the 10-16 storm woken 11 times in 15 minutes
  cannot recur).

Every kept wake's text is byte-identical to its pre-merge module's text.
Fails open: errors are logged and never affect the dispatcher.
"""

import json
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from . import board_watch

logger = logging.getLogger(__name__)

CTX: Any = None
STALE_AFTER = 300
STALL_MIN_EVENTS = 30
STALL_MIN_INTERVAL = 600.0
STALL_STATUSES = ("todo", "ready")
STALL_REASON = "active_pr"
LOOP_EVERY = 3
CHURN_MIN_CHANGE_REQUESTS = 10
WAIT_AFTER = 900.0
GRACE_SECONDS = 60
CAUSE_FIELDS = ("reason", "error", "summary", "handoff", "result", "review")
CAUSE_LIMIT = 200
PARKED_STATUSES = ("blocked", "triage")
_SUPPRESSED = frozenset({
    ("blocked", "capability"), ("triage", "capability"),
    ("triage", "needs_input"), ("triage", "transient"),
})
STALE_STATUSES = ("triage", "todo", "ready", "review", "blocked", "scheduled")
STALE_IGNORED_KINDS = (
    "heartbeat", "skipped_nonspawnable", "respawn_guarded", "claim_extended",
    "commented", "edited", "attached", "attachment_removed", "linked", "unlinked",
)
_WAITS_ON_PARENT = (
    "not (t.status = 'todo' and exists ("
    "select 1 from task_links l join tasks p on p.id = l.parent_id "
    "where l.child_id = t.id and p.status != 'done'))"
)
STALE_STATE = "kanban-stale-wake.json"
STALL_STATE = "kanban-stall-wake.json"
LOOP_STATE = "kanban-review-loop.json"
CHURN_STATE = "kanban-churn-wake.json"
WAITING_STATE = "kanban-waiting-wake.json"
RECONCILE_STATE = "kanban-notify-reconcile.json"
_LOOP_KINDS = ("changes_requested", "status", "promoted", "promoted_manual", "completed", "archived")
_CHURN_KINDS = ("changes_requested", "status", "promoted", "promoted_manual", "completed", "timed_out", "crashed", "reclaimed")
_NOTIFIABLE_KINDS = (
    "completed", "blocked", "gave_up", "crashed", "timed_out",
    "status", "review_requested", "changes_requested", "block_loop_detected",
)
_RECONCILE_STATUSES = ("todo", "ready", "review", "blocked", "triage", "scheduled", "done")
_LOOP_OPEN = ("todo", "ready", "running", "review", "blocked")
CHURN_OPEN = ("todo", "ready", "running", "review", "blocked", "triage", "scheduled")

_WAIT = re.compile(r"(?is)\b(?:still\s+)?(?:waiting|awaiting)\b(?:\s+(?:on|for|out|to))?")
_ACTOR = re.compile(
    r"(?is)\bcdtool\b|\bauto-?merge\b|\bmerge\b|\bmerge\s+queue\b|\bcodeowners\b|\bapproval\b|\breview\b"
    r"|\bpromotion\b|\brollout\b|\bdeploy(?:ment|ing|ed)?\b|\brelease\b|\bci\b|\bpipeline\b"
    r"|\bworkflow\b|\brun\s+\d+\b|\bpr\s*#?\s*\d+\b|\bpr\b|\bbot\b|\bchecks?\b|\bdisposition\b"
    r"|\bverdicts?\b|\bcode/\w+|\bverify\b|\bintegration\b|\bsonar\b|\bsha\b|\bwatcher\b"
)
_CLAUSE_BOUND = 80
_BOUNDARY_KINDS = ("spawned", "claimed")


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


def inject(text: str, origin: dict) -> bool:
    try:
        return bool(CTX.inject_message(text, origin=origin))
    except Exception:
        logger.warning("agent-guards wakes: injection failed for %s", origin, exc_info=True)
        return False


def _send(conn: sqlite3.Connection, tid: str, text: str, inject_fn=None) -> bool:
    """Inject to every subscribed chat; True when any one of them delivered."""
    fn = inject_fn if inject_fn is not None else inject
    delivered = [fn(text, {k: v for k, v in origin.items() if v is not None}) for origin in subscribers(conn, tid)]
    return any(delivered)


def _jsonish(payload: Any) -> dict:
    try:
        data = json.loads(payload or "")
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _cause(payload: Optional[str]) -> str:
    data = _jsonish(payload)
    for field in CAUSE_FIELDS:
        text = str(data.get(field) or "").strip()
        if text:
            return " ".join(text.split())[:CAUSE_LIMIT]
    return ""


def _home() -> Path:
    home = getattr(getattr(CTX, "_manager", None), "home_path", None) if CTX else None
    if home:
        return Path(home)
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        return Path.home() / ".hermes"


def _stall_state_path() -> Path:
    return _home() / "state" / STALL_STATE


def _reconcile_state_path() -> Path:
    return _home() / "state" / RECONCILE_STATE


def _wake_poke(tool_name: str = "", args: Optional[dict] = None, status: Optional[str] = None, **kwargs: object) -> None:
    board_watch.poke_post_tool_call(tool_name=tool_name, args=args, status=status, **kwargs)


def _ro_conn(board: Optional[str]):
    from hermes_cli import kanban_db

    db = kanban_db.kanban_db_path(board=board)
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)



def stale_cards(conn: sqlite3.Connection, now: float) -> list[dict]:
    placeholders = ",".join("?" * len(STALE_IGNORED_KINDS))
    last = (
        f"(select max(e.id) from task_events e where e.task_id = t.id and e.kind not in ({placeholders}))"
    )
    rows = conn.execute(
        f"select t.id, t.status, t.title, t.block_kind, t.created_at, ev.id, ev.created_at "
        f"from tasks t left join task_events ev on ev.id = {last} "
        f"where t.status in ({','.join('?' * len(STALE_STATUSES))}) and {_WAITS_ON_PARENT}",
        (*STALE_IGNORED_KINDS, *STALE_STATUSES),
    )
    cards = []
    for tid, status, title, block_kind, created, event_id, event_at in rows:
        age = now - float(event_at or created)
        if age >= STALE_AFTER and block_kind:
            cards.append({
                "id": tid, "status": status, "title": title or "", "block_kind": block_kind,
                "event_id": event_id or 0, "age_min": age / 60,
            })
    return cards


def next_due(conn: sqlite3.Connection, now: float) -> Optional[float]:
    """Earliest future moment a park card crosses ``STALE_AFTER`` (None = nothing pending)."""
    placeholders = ",".join("?" * len(STALE_IGNORED_KINDS))
    last = (
        f"(select max(e.id) from task_events e where e.task_id = t.id and e.kind not in ({placeholders}))"
    )
    rows = conn.execute(
        f"select t.created_at, ev.created_at "
        f"from tasks t left join task_events ev on ev.id = {last} "
        f"where t.status in ({','.join('?' * len(STALE_STATUSES))}) and {_WAITS_ON_PARENT}",
        (*STALE_IGNORED_KINDS, *STALE_STATUSES),
    )
    due = None
    for created, event_at in rows:
        crossing = float(event_at or created) + STALE_AFTER
        if crossing <= now:
            continue
        if due is None or crossing < due:
            due = crossing
    return due


def stale_text(card: dict) -> str:
    status = card["status"] + (f"/{card['block_kind']}" if card["block_kind"] else "")
    return (
        f"[Kanban stale card] {card['id']} has had no movement for {card['age_min']:.0f} min ({status}): "
        f"{card['title'][:120]}\n"
        "This is a one-time automatic wake for this card state. Inspect the card and recover it here: "
        "resume, unblock, complete with evidence, or confirm a genuine block with its exact release "
        "condition. If it is genuinely blocked on something only the user can provide, say so briefly. "
        "You will not be woken again for this card until its state changes."
    )


def stale_sweep(conn: sqlite3.Connection, state: dict, inject_fn, now: float) -> dict:
    """Wake each parked stale card once per state/episode. Returns the new state."""
    stale = {card["id"]: card for card in stale_cards(conn, now)
             if (card["status"], card["block_kind"]) not in _SUPPRESSED}
    open_status = dict(conn.execute(
        f"select id, status from tasks where status in ({','.join('?' * len(STALE_STATUSES))})", STALE_STATUSES,
    ).fetchall())
    kept = {tid: key for tid, key in state.items() if tid in open_status}
    for tid, card in stale.items():
        park = card["status"] in PARKED_STATUSES
        key = "parked" if park else card["event_id"]
        if kept.get(tid) in (key, card["event_id"]):
            kept[tid] = key
            continue
        if _send(conn, tid, stale_text(card), inject_fn):
            kept[tid] = key
    return kept


def _stale_sweep_for(board: Optional[str], now: float):
    def run(state: dict) -> tuple:
        conn = _ro_conn(board)
        try:
            new_state = stale_sweep(conn, state, inject, now)
            try:
                due = next_due(conn, now)
            except Exception:
                logger.warning("agent-guards stale wake: due-time computation failed", exc_info=True)
                due = None
        finally:
            conn.close()
        return new_state, due

    return run


def stale_on_tick(board: Optional[str] = None, dry_run: bool = False, now: Optional[float] = None, **_: Any) -> None:
    if dry_run:
        return
    try:
        now = time.time() if now is None else now
        conn = _ro_conn(board)
        try:
            board_watch.tick(
                conn, STALE_STATE, _stale_sweep_for(board, now),
                exclude=STALE_IGNORED_KINDS, now=now, due_tracked=True,
            )
        finally:
            conn.close()
    except Exception:
        logger.warning("agent-guards stale wake: sweep failed", exc_info=True)



def looping_cards(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "select t.id, t.status, t.title, count(e.id) from tasks t "
        "join task_events e on e.task_id = t.id and e.kind = 'changes_requested' "
        f"where t.status in ({','.join('?' * len(_LOOP_OPEN))}) group by t.id having count(e.id) >= ?",
        (*_LOOP_OPEN, LOOP_EVERY),
    )
    return [{"id": tid, "status": status, "title": title or "", "rounds": n} for tid, status, title, n in rows]


def last_reason(conn: sqlite3.Connection, task_id: str) -> str:
    row = conn.execute(
        "select payload from task_events where task_id = ? and kind = 'changes_requested' order by id desc limit 1",
        (task_id,),
    ).fetchone()
    if not row or not row[0]:
        return ""
    return str(_jsonish(row[0]).get("reason") or "")[:300]


def loop_text(card: dict, reason: str) -> str:
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
        f"The wake repeats at the next threshold (every {LOOP_EVERY} review rounds)."
    )


def loop_sweep(conn: sqlite3.Connection, state: dict, inject_fn, persist=None, load=None) -> dict:
    """Wake each looping card once per threshold, durable-write-before-send."""

    def _write(new_state: dict) -> None:
        if persist is not None:
            persist(new_state)
        else:
            board_watch.write_state(LOOP_STATE, new_state)

    fresh = load() if load else {}
    cards = {card["id"]: card for card in looping_cards(conn)}
    durable = {tid: n for tid, n in {**fresh, **state}.items() if tid in cards}
    flush = []
    for tid, card in cards.items():
        threshold = card["rounds"] // LOOP_EVERY * LOOP_EVERY
        if durable.get(tid, 0) >= threshold:
            continue
        flush.append((tid, threshold, card))
    if flush:
        durable.update({tid: threshold for tid, threshold, _card in flush})
        _write(durable)
        for tid, threshold, card in flush:
            text = loop_text(card, last_reason(conn, tid))
            if _send(conn, tid, text, inject_fn):
                durable[tid] = threshold
            else:
                durable.pop(tid, None)
            _write(durable)
    return durable


def _file_load() -> dict:
    """A fresh read of the production marker file (what other writers see)."""
    latest = board_watch.read_state(LOOP_STATE)
    return latest if isinstance(latest, dict) else {}


def _loop_sweep_for(board: Optional[str]):
    def run(state: dict) -> dict:
        conn = _ro_conn(board)
        try:
            return loop_sweep(conn, state, inject, load=_file_load)
        finally:
            conn.close()

    return run


def loop_on_tick(board: Optional[str] = None, dry_run: bool = False, **_: Any) -> None:
    if dry_run:
        return
    try:
        conn = _ro_conn(board)
        try:
            board_watch.tick(conn, LOOP_STATE, _loop_sweep_for(board), kinds=_LOOP_KINDS)
        finally:
            conn.close()
    except Exception:
        logger.warning("agent-guards review loop: sweep failed", exc_info=True)



def churn_cards(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "select t.id, t.status, t.title, "
        "(select count(*) from task_events e where e.task_id = t.id and e.kind = 'changes_requested') "
        f"from tasks t where t.status in ({','.join('?' * len(CHURN_OPEN))})",
        CHURN_OPEN,
    )
    by_id = {}
    order = []
    for tid, status, title, crs in rows:
        card = {"id": tid, "status": status, "title": title or "", "crs": crs}
        by_id[tid] = card
        order.append(card)
    return [card for card in order if card["crs"] >= CHURN_MIN_CHANGE_REQUESTS]


def churn_reasons(conn: sqlite3.Connection, task_id: str, limit: int = 3) -> list[str]:
    rows = conn.execute(
        "select payload from task_events where task_id = ? and kind = 'changes_requested' "
        "order by id desc limit ?",
        (task_id, limit),
    ).fetchall()
    reasons = []
    for (payload,) in rows:
        reason = str(_jsonish(payload).get("reason") or "") or str(payload or "")
        reasons.append(reason.strip()[:160])
    return [r for r in reasons if r]


def churn_text(card: dict, reasons: list[str]) -> str:
    lines = [
        f"[Kanban churn warning] {card['id']} is at {card['crs']} change-requests "
        f"({card['status']}): {card['title'][:120]}"
    ]
    for reason in reasons:
        lines.append(f"Requested change: {reason}")
    lines.append(
        "The card is grinding on. Decide here: split it into smaller cards, narrow the acceptance criteria, "
        "or confirm continue. Record the decision as a comment on the card."
    )
    return "\n".join(lines)


def churn_sweep(conn: sqlite3.Connection, state: dict, inject_fn) -> dict:
    """Warn each corner-case card once per ungated crossing. Returns the new state.

    A warned card at 12, 20 or 40 change-requests does not warn again: the
    corner case is one episode above the point, and a fresh crossing after the
    count fell back below it re-arms. Equality only looks equal when the count
    stood still at the point itself; a climb is a suppress, not a crossing."""
    new_state = {}
    for card in churn_cards(conn):
        tid = card["id"]
        seen = int(state.get(tid) or 0)
        if seen >= CHURN_MIN_CHANGE_REQUESTS:
            new_state[tid] = seen
            continue
        if _send(conn, tid, churn_text(card, churn_reasons(conn, tid)), inject_fn):
            new_state[tid] = card["crs"]
    return new_state


def _churn_sweep_for(board: Optional[str]):
    def run(state: dict) -> dict:
        conn = _ro_conn(board)
        try:
            return churn_sweep(conn, state, inject)
        finally:
            conn.close()

    return run


def churn_on_tick(board: Optional[str] = None, dry_run: bool = False, **_: Any) -> None:
    if dry_run:
        return
    try:
        conn = _ro_conn(board)
        try:
            board_watch.tick(conn, CHURN_STATE, _churn_sweep_for(board), kinds=_CHURN_KINDS)
        finally:
            conn.close()
    except Exception:
        logger.warning("agent-guards churn wake: sweep failed", exc_info=True)



def note_of(payload: Any) -> str:
    return str(_jsonish(payload).get("note") or "")


def is_wait_note(note: str) -> bool:
    """True when the note admits waiting with an external actor named after the wait phrase."""
    hit = _WAIT.search(note)
    if not hit:
        return False
    return bool(_ACTOR.search(note[hit.end(): hit.end() + _CLAUSE_BOUND]))


def waiting_cards(conn: sqlite3.Connection, now: float) -> list:
    """Running cards still waiting: the newest non-empty heartbeat note admits a
    wait and the first note of that wait streak is at least ``WAIT_AFTER`` old."""
    cards = []
    for tid, title in conn.execute("select id, title from tasks where status = 'running'"):
        boundary = conn.execute(
            f"select max(id) from task_events where task_id = ? "
            f"and kind in ({','.join('?' * len(_BOUNDARY_KINDS))})",
            (tid, *_BOUNDARY_KINDS),
        ).fetchone()[0] or 0
        newest_nonempty = None
        streak_start = None
        streak_len = 0
        rows = [
            (event_id, note_of(payload), float(created_at))
            for event_id, payload, created_at in conn.execute(
                "select id, payload, created_at from task_events "
                "where task_id = ? and kind = 'heartbeat' and id > ? "
                "and payload is not null and payload not in ('', 'null') order by id",
                (tid, boundary),
            )
        ]
        for event_id, note, created_at in reversed(rows):
            if not note:
                continue
            if newest_nonempty is None:
                newest_nonempty = (event_id, note, created_at)
            if not is_wait_note(note):
                break
            streak_start = (event_id, note, created_at)
            streak_len += 1
        if streak_start is not None and now - streak_start[2] >= WAIT_AFTER:
            cards.append({
                "id": tid, "title": title or "", "boundary": boundary,
                "count": streak_len, "first_at": streak_start[2],
                "latest": newest_nonempty[1], "latest_at": newest_nonempty[2],
            })
    return cards


def waiting_text(card: dict, now: float) -> str:
    age = max(0.0, now - card["first_at"]) / 60
    note_age = max(0.0, now - card["latest_at"]) / 60
    return (
        f"[Kanban waiting] {card['id']} has been waiting for {age:.0f} min "
        f"({card['count']} wait note{'s' if card['count'] != 1 else ''} this run): {card['title'][:120]}\n"
        f'Latest note, {note_age:.0f} min ago: "{card["latest"][:160]}".\n'
        "The worker cannot move its card while an outside actor holds the gate: act on the thing waited on "
        "directly here — intervene (merge, approve, re-run the pipeline), take the card over, or tell the "
        "worker to block the card with kind=needs_input if the wait will outlive the run. Do not just wait "
        "longer. This wake fires once per run."
    )


def waiting_sweep(conn: sqlite3.Connection, state: dict, inject_fn, now: float) -> dict:
    """Wake each waiting card once per run, in every subscribed chat. Returns the new state."""
    cards = {card["id"]: card for card in waiting_cards(conn, now)}
    kept = {tid: boundary for tid, boundary in state.items() if tid in cards}
    for tid, card in cards.items():
        if kept.get(tid) == card["boundary"]:
            continue
        if _send(conn, tid, waiting_text(card, now), inject_fn):
            kept[tid] = card["boundary"]
    return kept


def waiting_on_tick(board: Any = None, dry_run: bool = False, **_: Any) -> None:
    if dry_run:
        return
    try:
        state = board_watch.read_state(WAITING_STATE)
        conn = _ro_conn(board)
        try:
            new_state = waiting_sweep(conn, state, inject, time.time())
        finally:
            conn.close()
        if new_state != state:
            board_watch.write_state(WAITING_STATE, new_state)
    except Exception:
        logger.warning("agent-guards waiting wake: sweep failed", exc_info=True)



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
        first_id = min(int(i) for i, _k, _p in events)
        anchor = conn.execute(
            "select coalesce(max(id), 0) from task_events where task_id = ? and id < ? "
            "and (kind != 'respawn_guarded' or instr(coalesce(payload, ''), ?) = 0)",
            (tid, first_id, STALL_REASON),
        ).fetchone()[0]
        cards.append({
            "id": tid, "status": status, "title": title or "",
            "event_id": events[0][0], "episode_id": int(anchor),
        })
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


def stall_sweep(conn: sqlite3.Connection, state: dict, inject_fn, now: float) -> dict:
    """Wake each actively stalling card once per guard episode and at most once
    per ``STALL_MIN_INTERVAL``. Returns the new state (task id -> anchor)."""
    stalled = {card["id"]: card for card in stalled_cards(conn)}
    kept = {tid: key for tid, key in state.items() if tid in stalled}
    for tid, card in stalled.items():
        previous = kept.get(tid, -1)
        previous_at = 0.0
        if isinstance(previous, dict):
            previous_at = float(previous.get("at") or 0)
            previous = int(previous.get("anchor") or -1)
        if card["episode_id"] <= previous:
            continue
        if previous_at and now - previous_at < STALL_MIN_INTERVAL:
            continue
        if _send(conn, tid, stall_text(card), inject_fn):
            kept[tid] = {"anchor": card["event_id"], "at": now}
    return kept


def stall_on_tick(board: Optional[str] = None, dry_run: bool = False, **_: Any) -> None:
    """Unthrottled stall wake, separate hook so the event-driven wakes stay cursor-gated."""
    try:
        path = _stall_state_path()
        state = json.loads(path.read_text()) if path.exists() else {}
        conn = _ro_conn(board)
        try:
            new_state = stall_sweep(conn, state, inject, time.time())
        finally:
            conn.close()
        if new_state != state:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(new_state))
    except Exception:
        logger.warning("agent-guards stall wake: sweep failed", exc_info=True)



def uncovered_cards(conn: sqlite3.Connection, now: float) -> list[dict]:
    """Open cards whose latest notifiable event reached no subscriber."""
    placeholders = ",".join("?" * len(_NOTIFIABLE_KINDS))
    rows = conn.execute(
        f"select t.id, t.status, t.title, e.id, e.kind, e.payload, e.created_at "
        f"from tasks t join task_events e on e.id = ("
        f"  select max(e2.id) from task_events e2"
        f"  where e2.task_id = t.id and e2.kind in ({placeholders})"
        f") where t.status in ({','.join('?' * len(_RECONCILE_STATUSES))})",
        (*_NOTIFIABLE_KINDS, *_RECONCILE_STATUSES),
    )
    cards = []
    for tid, status, title, event_id, kind, payload, created_at in rows:
        subs = conn.execute(
            "select last_ping_event_id, last_event_id from kanban_notify_subs where task_id = ?",
            (tid,),
        ).fetchall()
        if subs and any(
            int(ping or 0) >= int(event_id) or int(claim or 0) >= int(event_id)
            for ping, claim in subs
        ):
            continue
        cards.append({
            "id": tid, "status": status, "title": title or "",
            "event_id": event_id, "kind": kind, "cause": _cause(payload),
            "pending": now - float(created_at or 0) < GRACE_SECONDS,
        })
    return cards


def reconcile_text(card: dict) -> str:
    return (
        f"[Kanban unreconciled notice] {card['id']} reached {card['kind']} ({card['status']}) "
        f"but the gateway never confirmed delivery of that state to any subscriber: "
        f"{card['title'][:120]}"
        + (f"\nCause: {str(card['cause'])[:CAUSE_LIMIT]}" if card["cause"] else "")
        + "\nThis is the one-time reconciled notice for this card state. Inspect the card and decide here: "
        "recover it, unblock it, or complete it with evidence. You will not be re-notified for this card "
        "until its state changes again."
    )


def reconcile_sweep(conn: sqlite3.Connection, state: dict, inject_fn, now: Optional[float] = None) -> dict:
    """Notify each lost-delivery state once per state; returns the new record."""
    now = time.time() if now is None else now
    fresh: dict = {}
    for card in uncovered_cards(conn, now):
        tid = card["id"]
        previous = state.get(tid)
        previous = previous if isinstance(previous, dict) else {}
        seen = previous.get("event_id")
        live = subscribers(conn, tid)
        origins = live or list(previous.get("subscribers") or [])
        origins = [{k: v for k, v in origin.items() if v is not None} for origin in origins]
        entry = {"event_id": card["event_id"], "subscribers": origins}
        if card["pending"] or live:
            fresh[tid] = entry | {"pending": True}
            continue
        if seen is None:
            fresh[tid] = entry
            continue
        if seen == card["event_id"] and not previous.get("pending"):
            fresh[tid] = entry
            continue
        if not origins:
            continue
        if any(inject_fn(reconcile_text(card), origin) for origin in origins):
            fresh[tid] = entry
    return fresh


def reconcile_on_tick(board: Optional[str] = None, dry_run: bool = False, **_: Any) -> None:
    if dry_run:
        return
    try:
        path = _reconcile_state_path()
        state = json.loads(path.read_text()) if path.exists() else {}
        conn = _ro_conn(board)
        try:
            new_state = reconcile_sweep(conn, state, inject)
        finally:
            conn.close()
        if new_state != state:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(new_state))
    except Exception:
        logger.warning("agent-guards notify reconcile: sweep failed", exc_info=True)



EVENT_TICKS = (stale_on_tick, loop_on_tick, churn_on_tick)
UNTHROTTLED_TICKS = (waiting_on_tick, stall_on_tick, reconcile_on_tick)


def register(ctx) -> None:
    global CTX
    CTX = ctx
    ctx.register_hook("post_tool_call", _wake_poke)
    for hook in EVENT_TICKS + UNTHROTTLED_TICKS:
        ctx.register_hook("on_kanban_dispatch_tick", hook)
