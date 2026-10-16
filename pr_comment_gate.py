"""Stop the lead's own chat from planting a dispatcher stall on a ready card.

When ``kanban.default_assignee`` is set and the lead process (``HERMES_KANBAN_TASK``
absent — reuse the lead/worker scoping of ``lead_delegation``) posts a
``kanban_comment`` whose body contains a full GitHub PR URL
(``https://github.com/<owner>/<repo>/pull/<n>``) on a card in ``todo``/``ready``/``blocked``,
the dispatcher's respawn guard (``check_respawn_guard`` in
``hermes_cli/kanban_db_dispatch.py``) reads that URL from ANY comment in the last 24 h
as "a prior worker already opened a PR" and holds the card out of the ready queue
every tick (``respawn_guarded reason=active_pr``) until someone assigns or
the comment ages out. A card once caught this way sat in ready for
the full queue-drain with no signal.

The comment is rejected before it is written and the lead is told to cite the PR as
``owner/repo#N`` instead — the dispatcher guard only matches full URLs, so a ``#N``
mention carries the same information without the stall. Workers are untouched:
their own PR URL is the guard's legitimate input, and worker processes carry
``HERMES_KANBAN_TASK``. Cards ALREADY blocked are also checked, because a lead
comment on a blocked card can silently arm the stall for the moment it is unblocked.
Fails open on any lookup error.
"""

import re
import sqlite3
from typing import Any, Optional

from . import lead_delegation

PR_URL = re.compile(r"https?://github\.com/[^/\s]+/[^/\s]+/pull/\d+", re.IGNORECASE)

STALLABLE = ("todo", "ready", "blocked")

MESSAGE = (
    "agent-guards blocked kanban_comment: that comment contains a full GitHub PR URL, and on a "
    "{status} card the dispatcher respawn guard reads any full PR URL posted in the last 24 h as "
    "'a worker already opened a PR' and holds the card out of the ready queue every tick "
    "(respawn_guarded reason=active_pr). Cite the pull request as {suggestion} instead — "
    "the guard only matches full URLs, so the same reference lands without stalling the card. "
    "Full URLs are fine on running/done cards and in worker comments."
)


def _board_conn() -> Optional[sqlite3.Connection]:
    """Read-only connection to the active board's kanban.db, or None when unavailable."""
    try:
        from hermes_cli import kanban_db

        path = kanban_db.kanban_db_path()
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        table = conn.execute(
            "select 1 from sqlite_master where type='table' and name='tasks'"
        ).fetchone()
        if not table:
            conn.close()
            return None
        return conn
    except Exception:
        return None


def _card_status(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    row = conn.execute("select status from tasks where id = ?", (task_id,)).fetchone()
    return row[0] if row else None


def _suggested_citation(body: str) -> str:
    match = PR_URL.search(body)
    if match:
        tail = match.group(0).split("github.com/", 1)[1]  # "owner/repo/pull/N"
        owner, repo, _pull, number = [p.strip(").,;") for p in tail.split("/")]
        return f"{owner}/{repo}#{number}"
    return "owner/repo#N"


def check(tool_name: str, args: Any) -> Optional[str]:
    if tool_name != "kanban_comment" or not isinstance(args, dict) or not lead_delegation.is_lead():
        return None
    body = str(args.get("body") or "")
    if not PR_URL.search(body):
        return None
    task_id = str(args.get("task_id") or "")
    if not task_id:
        return None
    conn = _board_conn()
    if conn is None:
        return None
    try:
        status = _card_status(conn, task_id)
    except Exception:
        status = None
    finally:
        conn.close()
    if status not in STALLABLE:
        return None
    return MESSAGE.format(status=status, suggestion=_suggested_citation(body))
