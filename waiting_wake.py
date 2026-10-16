"""Wake the chat that owns a running card when its worker keeps waiting on an outside actor.

On every dispatcher tick (unthrottled, its own hook so the cursored wakes'
heartbeats-are-noise cursors stay untouched) it reads each ``running`` card's
``heartbeat`` notes since the run's boundary — the newest ``spawned`` or
``claimed`` event. A note that admits waiting on something outside the worker's
control (a merge bot, an approval, a deploy or rollout, a CI run, a promotion)
is evidence only the card's owner can act on: the worker cannot merge itself,
approve itself or speed up a pipeline, and heartbeat notes reach nobody. The
first note may time-stamp the situation, so the wake fires when a run has
accumulated ``WAIT_MIN_NOTES`` such notes and quotes the latest one with its
age. Notes whose wait phrase has no external actor after it (a rate cooldown,
waiting for local tooling) and progress notes without a wait phrase never
count; a note merely discussing waits (an analysis of waiting buckets) can
still slip through, and the bound for that is one short injected turn per run
with the note quoted for the owner to judge.

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
WAIT_MIN_NOTES = 2
_BOUNDARY_KINDS = ("spawned", "claimed")
_LOOK_BACK = 60
_LOOK_AHEAD = 200

_WAIT = re.compile(r"(?is)\b(?:still\s+)?(?:waiting|awaiting)\b(?:\s+(?:on|for|out|to))?")
_ACTOR = re.compile(
    r"(?is)\bcdtool\b|\bauto-?merge\b|\bmerge\b|\bmerge\s+queue\b|\bcodeowners\b|\bapproval\b|\breview\b"
    r"|\bpromotion\b|\brollout\b|\bdeploy(?:ment|ing|ed)?\b|\brelease\b|\bci\b|\bpipeline\b"
    r"|\bworkflow\b|\brun\s+\d+\b|\bpr\s*#?\s*\d+\b|\bpr\b|\bbot\b|\bchecks?\b|\bdisposition\b"
    r"|\bverdicts?\b|\bcode/\w+|\bverify\b|\bintegration\b|\bsonar\b|\bsha\b"
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
    window = note[max(0, hit.start() - _LOOK_BACK): hit.end() + _LOOK_AHEAD]
    return bool(_ACTOR.search(window))


def waiting_cards(conn: sqlite3.Connection, now: float) -> list:
    """Running cards whose current run carries ``WAIT_MIN_NOTES`` wait notes."""
    cards = []
    for tid, title in conn.execute("select id, title from tasks where status = 'running'"):
        boundary = conn.execute(
            f"select max(id) from task_events where task_id = ? "
            f"and kind in ({','.join('?' * len(_BOUNDARY_KINDS))})",
            (tid, *_BOUNDARY_KINDS),
        ).fetchone()[0] or 0
        notes = []
        for event_id, payload, created_at in conn.execute(
            "select id, payload, created_at from task_events "
            "where task_id = ? and kind = 'heartbeat' and id > ? "
            "and payload is not null and payload not in ('', 'null') order by id",
            (tid, boundary),
        ):
            note = note_of(payload)
            if note and is_wait_note(note):
                notes.append((event_id, note, float(created_at)))
        if len(notes) >= WAIT_MIN_NOTES:
            cards.append({
                "id": tid, "title": title or "", "boundary": boundary,
                "count": len(notes), "latest": notes[-1][1], "latest_at": notes[-1][2],
            })
    return cards


def wake_text(card: dict, now: float) -> str:
    age = max(0.0, now - card["latest_at"]) / 60
    return (
        f"[Kanban waiting] {card['id']} has reported waiting on an external gate {card['count']} times "
        f"in this run: {card['title'][:120]}\n"
        f'Latest note, {age:.0f} min ago: "{card["latest"][:160]}".\n'
        "The worker cannot move its card while an outside actor holds the gate: decide here — intervene "
        "(merge, approve, re-run the pipeline), take the card over, or tell the worker to block the card "
        "with kind=needs_input if the wait will outlive the run. This wake fires once per run."
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
