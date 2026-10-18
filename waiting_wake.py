"""Wake the chat that owns a running card when its worker keeps waiting on an outside actor.

On every dispatcher tick (unthrottled, its own hook so the cursored wakes'
heartbeats-are-noise cursors stay untouched) it reads each ``running`` card's
``heartbeat`` notes since the run's boundary — the newest ``spawned`` or
``claimed`` event. A note that admits waiting on something outside the worker's
control (a merge bot, an approval, a deploy or rollout, a CI run, a promotion)
is evidence only the card's owner can act on: the worker cannot merge itself,
approve itself or speed up a pipeline, and heartbeat notes reach nobody. The
actor match is kept inside the wait phrase's own clause (``_LOOK_AHEAD``), so
an independent later sentence ("While waiting, drafting ...") is not read as
the gate.

The wake keys on the run's wait STREAK: the consecutive wait-admitting
heartbeat notes counted back from the newest non-empty heartbeat note (empty
notes neither start nor break a streak; a progress note ends it). It fires
when the newest non-empty note admits a wait and the FIRST note of that
streak is ``WAIT_AFTER`` seconds old — a run that already moved past its wait
(progress since) stays silent — and the wake quotes the latest note with its
age. The wait wording is matched deterministically in one table (``_WAIT`` +
``_ACTOR``): a wait phrase must be followed by the external actor being waited
on, so notes whose wait phrase has no external actor after it (a rate cooldown,
waiting for local tooling) and progress notes without a wait phrase never
count; a note merely discussing waits (an analysis of waiting buckets) can
still slip through, and the bound for that is one short injected turn per run
with the note quoted for the owner to judge. Upgrading the wording table to a
learned classifier is the path when the bound stops being acceptable.
``_LOOK_AHEAD`` bounds the actor search to the wait phrase's own clause: real
false positives matched an actor only across a clause boundary ("... While
waiting, drafting ..." or "... fix-merge-date verification ..."), while the
grounded wait notes in the incident record name their actor 20-40 chars out
and on 14 days of real heartbeat notes the only two matches beyond 80 chars
were cross-clause.

It wakes once per run: the recorded marker is the newest spawned/claimed event
id at wake time, so the same run is never woken twice while a new run re-arms
the wake. Closed or archived cards drop out of the state, a failed injection
records nothing so the next tick retries, and only the card's subscribed chats
(the creating chat is auto-subscribed) are woken via the gateway's plugin
injection, quoting at most the truncated heartbeat excerpt the card's owner
chat would have received as a comment anyway. Shares the gateway injection of
``stale_wake``. Fails open.
"""

import json
import logging
import sqlite3
import re
import time
from typing import Any

from . import board_watch
from . import stale_wake

logger = logging.getLogger(__name__)

STATE_NAME = "kanban-waiting-wake.json"
WAIT_AFTER = 900.0
_BOUNDARY_KINDS = ("spawned", "claimed")
_LOOK_AHEAD = 80

_WAIT = re.compile(r"(?is)\b(?:still\s+)?(?:waiting|awaiting)\b(?:\s+(?:on|for|out|to))?")
_ACTOR = re.compile(
    r"(?is)\bcdtool\b|\bauto-?merge\b|\bmerge\b|\bmerge\s+queue\b|\bcodeowners\b|\bapproval\b|\breview\b"
    r"|\bpromotion\b|\brollout\b|\bdeploy(?:ment|ing|ed)?\b|\brelease\b|\bci\b|\bpipeline\b"
    r"|\bworkflow\b|\brun\s+\d+\b|\bpr\s*#?\s*\d+\b|\bpr\b|\bbot\b|\bchecks?\b|\bdisposition\b"
    r"|\bverdicts?\b|\bcode/\w+|\bverify\b|\bintegration\b|\bsonar\b|\bsha\b|\bwatcher\b"
)


def note_of(payload: Any) -> str:
    """The heartbeat note text, or an empty string for an empty/broken payload."""
    try:
        return str(json.loads(payload).get("note") or "")
    except (ValueError, AttributeError, TypeError):
        return ""


def is_wait_note(note: str) -> bool:
    """True when the note admits waiting with an external actor named after the wait phrase."""
    hit = _WAIT.search(note)
    if not hit:
        return False
    return bool(_ACTOR.search(note[hit.end(): hit.end() + _LOOK_AHEAD]))


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


def wake_text(card: dict, now: float) -> str:
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


def sweep(conn: sqlite3.Connection, state: dict, inject, now: float) -> dict:
    """Wake each waiting card once per run, in every subscribed chat. Returns the new state."""
    cards = {card["id"]: card for card in waiting_cards(conn, now)}
    kept = {tid: boundary for tid, boundary in state.items() if tid in cards}
    for tid, card in cards.items():
        if kept.get(tid) == card["boundary"]:
            continue
        text = wake_text(card, now)
        delivered = [
            inject(text, {k: v for k, v in origin.items() if v is not None})
            for origin in stale_wake.subscribers(conn, tid)
        ]
        if any(delivered):
            kept[tid] = card["boundary"]
    return kept


def on_tick(board: Any = None, dry_run: bool = False, **_: Any) -> None:
    if dry_run:
        return
    try:
        from hermes_cli import kanban_db

        db = kanban_db.kanban_db_path(board=board)
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            state = board_watch.read_state(STATE_NAME)
            new_state = sweep(conn, state, stale_wake._inject, time.time())
        finally:
            conn.close()
        if new_state != state:
            board_watch.write_state(STATE_NAME, new_state)
    except Exception:
        logger.warning("agent-guards waiting wake: sweep failed", exc_info=True)
