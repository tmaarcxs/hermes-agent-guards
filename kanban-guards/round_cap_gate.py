"""Cap the correction rounds a card can burn, then hand the decision back to the lead.

The failure this closes (measured over 30 days of this board): a card can be reopened
indefinitely and nothing stops it. 429 ``changes_requested`` verdicts, 244 cards, and the
tail is brutal — 93 cards took 2+ rounds, 16 took 4+, and one card burned **108 runs and
27.8 hours** across 29 review rounds before it was archived. Nothing in the lifecycle
counted the rounds, so a card that kept failing review simply kept going.

The measurement also says the rounds themselves converge (17 of the deep cards ask for
*less* each round, 13 for more): the defect is not a reviewer asking for the same thing
forever, it is that no ceiling exists. So this gate adds the ceiling, not a stricter
review.

``check`` runs on ``pre_tool_call`` for ``kanban_request_review``: once the card already
carries ``MAX_ROUNDS`` ``changes_requested`` runs, the handoff is blocked and the message
tells the worker to stop and escalate. The decision that follows — accept as-is, narrow
the scope, or fix the root cause — is the lead's, not another worker run's.

Escape hatch, deliberately human: a comment on the card by any profile OTHER than the
card's assignee containing ``rounds-override`` and posted AFTER the latest
``changes_requested`` lifts the cap. That is how the lead says "yes, this one really needs
another pass" without editing plugin config. The token is greppable and auditable.

Threshold override: ``AGENT_GUARDS_MAX_REVIEW_ROUNDS`` (default 4). Read per call, so it
can be changed without a reload. ``0`` disables the gate.

Only a worker process on a card (``HERMES_KANBAN_TASK`` set) is gated; a lead chat is not.
Reads the board read-only, short-lived connection, fails open on any error.
"""

import os
import sqlite3
from typing import Any, Optional

from . import kanban_policy

MAX_ROUNDS_DEFAULT = 4
OVERRIDE_TOKEN = "rounds-override"
_MAX_SESSIONS = 200

# Cards already reported in this session, so a worker that retries the same blocked call
# gets the same answer without the gate re-reading the board every time.
_reported: "dict[str, int]" = {}


def _max_rounds() -> int:
    raw = os.environ.get("AGENT_GUARDS_MAX_REVIEW_ROUNDS", "")
    try:
        value = int(str(raw).strip()) if str(raw).strip() else MAX_ROUNDS_DEFAULT
    except (TypeError, ValueError):
        return MAX_ROUNDS_DEFAULT
    return max(0, value)


def _board_path():
    """The board the calling process is working against."""
    try:
        from hermes_cli import kanban_db as kb

        return kb.kanban_db_path()
    except Exception:
        return None


def _connect(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)


def _rounds_and_escape(conn: sqlite3.Connection, task_id: str) -> tuple[int, Optional[int]]:
    """``(changes_requested rounds, epoch of the latest one)`` for this card."""
    row = conn.execute(
        "SELECT count(*), max(coalesce(ended_at, started_at)) FROM task_runs "
        "WHERE task_id = ? AND outcome = 'changes_requested'",
        (task_id,),
    ).fetchone()
    rounds = int(row[0] or 0)
    latest = int(row[1]) if row[1] is not None else None
    return rounds, latest


def _assignee(conn: sqlite3.Connection, task_id: str) -> str:
    row = conn.execute("SELECT assignee FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return str(row[0] or "") if row else ""


def _has_override(conn: sqlite3.Connection, task_id: str, assignee: str, since: Optional[int]) -> bool:
    """A non-assignee profile posted ``rounds-override`` after the latest round."""
    floor = since if since is not None else 0
    rows = conn.execute(
        "SELECT author, body, created_at FROM task_comments WHERE task_id = ?", (task_id,)
    ).fetchall()
    for author, body, created_at in rows:
        if OVERRIDE_TOKEN not in str(body or ""):
            continue
        if str(author or "").strip().lower() == assignee.strip().lower():
            continue  # the implementer cannot lift its own ceiling
        try:
            if int(created_at or 0) > floor:
                return True
        except (TypeError, ValueError):
            continue
    return False


def check(tool_name: str = "", args: Any = None, **kwargs: Any) -> Optional[str]:
    """``pre_tool_call``: block ``kanban_request_review`` past the round ceiling."""
    if tool_name != "kanban_request_review":
        return None
    max_rounds = _max_rounds()
    if max_rounds <= 0:
        return None
    if not kanban_policy.configured_worker():
        return None
    task_id = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    if not task_id:
        return None
    path = _board_path()
    if path is None or not getattr(path, "exists", lambda: False)():
        return None  # no board to read: fail open
    conn = None
    try:
        conn = _connect(path)
        rounds, latest = _rounds_and_escape(conn, task_id)
        if rounds < max_rounds:
            return None
        if _has_override(conn, task_id, _assignee(conn, task_id), latest):
            return None
    except Exception:
        return None  # any read failure leaves the call untouched
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    _reported[task_id] = rounds
    if len(_reported) > _MAX_SESSIONS:
        for key in list(_reported)[: len(_reported) - _MAX_SESSIONS]:
            _reported.pop(key, None)
    return (
        f"agent-guards blocked kanban_request_review: card {task_id} has already been through "
        f"{rounds} rounds of changes_requested (ceiling {max_rounds}). Another review pass is not the fix — "
        "the card is not converging on its own and a further round only spends another run. Stop here and "
        "escalate to the lead in ONE kanban_comment on this card: what each round asked for, what the current "
        "candidate does, and the ONE decision needed (accept as-is, narrow the acceptance to what is proven, "
        "or fix a root cause that is outside this card's scope). Then end the run — do not request review "
        "again. The lead can lift this ceiling for a genuine extra pass by posting a comment on this card "
        f"from another profile containing '{OVERRIDE_TOKEN}'."
    )


def reset() -> None:
    """Clear per-session state (used by the test suite)."""
    _reported.clear()


__all__ = ["MAX_ROUNDS_DEFAULT", "OVERRIDE_TOKEN", "check", "reset"]
