"""Park a ``ready`` card whose PR is already merged instead of re-spawning it.

A worker that comments its PR URL and then dies — ``Gateway disconnected; turn
outcome is unknown`` is the dominant crash — leaves a ``ready`` card with a
``crashed`` run behind it. The dispatcher's own respawn guard holds such a card
only while no handoff follows the PR comment, and it holds it forever on an open
PR; once the PR merges, the retry re-runs delivered work.

This module runs on the ``on_kanban_dispatch_tick`` observer. For each ``ready``
card with an empty ``claim_lock``, no ``current_run_id`` and at least one
``crashed`` run, it reads the newest PR-URL comment (the same regex and 24 h
window as ``active_pr_unblock``, reused not copied) and asks GitHub for the PR
state. A deliberate handoff strictly newer than the comment — an ``assigned``
that moves the card to another profile, ``unblocked``, ``changes_requested``,
``review_reopened`` — keeps the card running. Only ``state == "MERGED"`` parks
the card ``blocked`` (``needs_input``) with the PR number and merge SHA in the
reason and posts exactly one comment; the card is never completed, closed or
merged.

Fails open: any read, auth or parse failure logs and does nothing. A per-task
cache (TTL 30 min) plus a global cap of 20 GitHub lookups per hour keep the tick
cheap, and the cap logs once per window. ``AGENT_GUARDS_DELIVERED_PR_PARK=off``
disables the park action, which is what the RED-on-base proof exercises.
"""

import json
import logging
import os
import subprocess
import time
from typing import Any, Optional

from . import active_pr_unblock as _active_pr

logger = logging.getLogger(__name__)

STATE_FILE = "delivered-pr-park.json"
CACHE_TTL = 1800.0
CALL_WINDOW = 3600.0
CALL_CAP = 20
TIMEOUT = 10.0
AUTHOR = "agent-guards"
MARKER = "delivered-pr-park"
KILL_ENV = "AGENT_GUARDS_DELIVERED_PR_PARK"
CALLS_KEY = "__calls__"
CAP_LOGGED_KEY = "__cap_logged__"


def _state_path():
    from plugins.plugin_storage import plugin_data_dir

    return plugin_data_dir("kanban-guards") / STATE_FILE


def _read_state() -> dict:
    try:
        data = json.loads(_state_path().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(state: dict) -> None:
    path = _state_path()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(path)


def _entry(state: dict, task_id: str) -> dict:
    entry = state.get(task_id)
    return entry if isinstance(entry, dict) else {}


def _park_enabled() -> bool:
    return (os.environ.get(KILL_ENV) or "").strip().lower() not in ("off", "0", "false", "no")


def _candidates(conn) -> list[str]:
    rows = conn.execute(
        "SELECT t.id FROM tasks t WHERE t.status = 'ready' "
        "AND (t.claim_lock IS NULL OR t.claim_lock = '') AND t.current_run_id IS NULL "
        "AND EXISTS (SELECT 1 FROM task_runs r WHERE r.task_id = t.id AND r.status = 'crashed')"
    ).fetchall()
    return [row[0] for row in rows]


def _cached(state: dict, task_id: str, now: float) -> bool:
    checked = _entry(state, task_id).get("checked_at")
    return isinstance(checked, (int, float)) and now - checked < CACHE_TTL


def _calls(state: dict, now: float) -> list:
    raw = state.get(CALLS_KEY)
    return [t for t in raw if isinstance(t, (int, float)) and now - t < CALL_WINDOW] if isinstance(raw, list) else []


def _cap_ok(state: dict, now: float) -> bool:
    if len(_calls(state, now)) < CALL_CAP:
        return True
    logged = state.get(CAP_LOGGED_KEY)
    if not (isinstance(logged, (int, float)) and now - logged < CALL_WINDOW):
        state[CAP_LOGGED_KEY] = now
        logger.info(
            "agent-guards delivered_pr_park: %d gh calls in the last hour, skipping quietly",
            CALL_CAP,
        )
    return False


def _record_call(state: dict, now: float) -> None:
    calls = _calls(state, now)
    calls.append(now)
    state[CALLS_KEY] = calls


def _run_gh(args: list, env: dict) -> tuple:
    """The one place a subprocess is spawned; tests inject their own runner."""
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=TIMEOUT, env=env)
        return proc.returncode, proc.stdout
    except Exception:
        return 1, ""


def _parse(text: str) -> Optional[dict]:
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _pr_state(url: str) -> Optional[dict]:
    """PR JSON from GitHub: ambient ``gh`` first, then the fallback account."""
    args = ["gh", "pr", "view", url, "--json", "state,mergeCommit,number"]
    env = {k: v for k, v in os.environ.items() if k != "GH_TOKEN"}
    rc, out = _run_gh(args, env)
    data = _parse(out) if rc == 0 else None
    if data is not None:
        return data
    rc, out = _run_gh(["gh", "auth", "token", "-u", "tmaarcxs"], env)
    token = out.strip() if rc == 0 and out.strip() else None
    if not token:
        return None
    env = dict(env, GH_TOKEN=token)
    rc, out = _run_gh(args, env)
    return _parse(out) if rc == 0 else None


def _merge_sha(data: dict) -> Optional[str]:
    commit = data.get("mergeCommit")
    return commit.get("oid") if isinstance(commit, dict) else None


def _handoff_after(conn, task_id: str, since: int) -> bool:
    for finder in (_active_pr._newest_handoff, _active_pr._newest_unblock):
        if finder(conn, task_id, since) is not None:
            return True
    return False


def _commented(conn, task_id: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM task_comments WHERE task_id = ? AND body LIKE ? LIMIT 1",
        (task_id, "%" + MARKER + "%"),
    ).fetchone())


def _park(conn, kb, task_id: str, number: Any, sha: Optional[str]) -> None:
    reason = (
        f"PR #{number} already merged at {sha or 'unknown'}: parked instead of re-spawned "
        "(the work is delivered; a retry would redo it). Unblock with new instructions if more work is needed."
    )
    try:
        parked = kb.block_task(conn, task_id, reason=reason, kind="needs_input")
    except Exception:
        logger.warning("agent-guards delivered_pr_park: block failed for %s", task_id, exc_info=True)
        return
    if not parked:
        return
    if _commented(conn, task_id):
        return
    body = (
        f"{AUTHOR}: PR #{number} is already MERGED at {sha or 'unknown'}. "
        "This card was parked instead of re-spawned, because the work it was retrying is delivered. "
        f"No action is needed unless more work remains. ({MARKER})"
    )
    try:
        kb.add_comment(conn, task_id, AUTHOR, body)
    except Exception:
        logger.warning("agent-guards delivered_pr_park: comment failed for %s", task_id, exc_info=True)


def _handle(conn, kb, task_id: str, now: float, state: dict) -> None:
    found = _active_pr._newest_pr_comment(conn, task_id, now)
    if found is None:
        return
    pr_at, url = found
    if _handoff_after(conn, task_id, pr_at):
        logger.info("agent-guards delivered_pr_park: %s has a handoff after its PR comment; left running", task_id)
        return
    if _cached(state, task_id, now):
        return
    if not _cap_ok(state, now):
        return
    data = _pr_state(url)
    _record_call(state, now)
    if data is None:
        logger.info("agent-guards delivered_pr_park: PR state unreadable for %s; doing nothing", task_id)
        return
    number = data.get("number")
    state[task_id] = {"state": data.get("state"), "checked_at": now, "number": number}
    if data.get("state") != "MERGED":
        return
    if not _park_enabled():
        logger.info("agent-guards delivered_pr_park: park action disabled; %s left ready", task_id)
        return
    _park(conn, kb, task_id, number, _merge_sha(data))


def _sweep(conn) -> None:
    from hermes_cli import kanban_db as kb

    now = time.time()
    state = _read_state()
    candidates = _candidates(conn)
    for task_id in candidates:
        try:
            _handle(conn, kb, task_id, now, state)
        except Exception:
            logger.warning("agent-guards delivered_pr_park: %s failed", task_id, exc_info=True)
    if candidates:
        try:
            _write_state(state)
        except Exception:
            logger.warning("agent-guards delivered_pr_park: state write failed", exc_info=True)


def tick(result: Any = None, board: Optional[str] = None, dry_run: bool = False, **_: Any) -> None:
    """``on_kanban_dispatch_tick`` observer: park every ready card with a merged PR."""
    try:
        if dry_run or result is None or getattr(result, "skipped_locked", False):
            return
        from hermes_cli import kanban_db_connect

        conn = kanban_db_connect.connect(board=board)
        try:
            _sweep(conn)
        finally:
            conn.close()
    except Exception:
        logger.warning("agent-guards delivered_pr_park: tick failed", exc_info=True)


def register(ctx) -> None:
    ctx.register_hook("on_kanban_dispatch_tick", tick)
