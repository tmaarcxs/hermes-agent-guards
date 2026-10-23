"""Auto-lift the ``active_pr`` respawn guard when a human unblocked the card.

``hermes_cli.kanban_db_dispatch.check_respawn_guard`` holds a ``ready`` card
whose newest comment (within 24 h) carries a GitHub PR URL, unless a handoff
event follows that comment. Its handoff kinds are ``assigned`` (only when the
payload's ``from`` differs from ``assignee``), ``changes_requested`` and
``review_reopened`` — ``unblocked`` is NOT one of them. A card a human unblocked
with new instructions therefore sits held forever: card ``t_4ba7dbfc`` commented
the PR URL at 16:03:13, was blocked at 16:03:33 and unblocked at 16:04:08, and
the dispatcher then held it for 66+ ticks with ``Last tick held back:
active_pr=1``. It had to be lifted by hand.

Core behaviour is not ours to change, so this module does automatically what the
operator did by hand: a real profile hop — assign the card to another profile,
then back to the original. Two committed ``assigned`` events with
``from != assignee`` are genuine handoff events, so core's own guard lifts on
the next tick and the card spawns. Nothing synthetic is written: no fabricated
``changes_requested`` and no hand-inserted event row.

Runs on the ``on_kanban_dispatch_tick`` observer, which core fires strictly
after the dispatch lock is released, so the write cannot stall a sibling
dispatcher. Fails open: every error is swallowed and logged, and a tick with no
``active_pr`` entry in ``respawn_guarded`` pays only a list scan.

Loop protection lives in ``plugin-data`` (survives a restart): one hop per task
per 10 minutes, at most 3 per hour. When the cap is hit the module stops hopping
and posts exactly one comment asking a human to decide.
"""

import json
import logging
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

REASON = "active_pr"
STATE_FILE = "active-pr-hops.json"
HOP_WINDOW_SHORT = 600.0
HOP_WINDOW_LONG = 3600.0
HOP_MAX_LONG = 3
DEFAULT_CANDIDATES = ("default",)
ALERT_AUTHOR = "agent-guards"


def _state_path():
    from plugins.plugin_storage import plugin_data_dir

    return plugin_data_dir("kanban-guards") / STATE_FILE


def _read_state() -> dict:
    try:
        return json.loads(_state_path().read_text())
    except (OSError, ValueError):
        return {}


def _write_state(state: dict) -> None:
    path = _state_path()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(path)


def _entry(state: dict, task_id: str) -> dict:
    entry = state.get(task_id)
    return entry if isinstance(entry, dict) else {}


def _allowed(state: dict, task_id: str, now: float) -> bool:
    """One hop per task per 10 minutes, at most 3 per hour."""
    hops = [h for h in _entry(state, task_id).get("hops") or [] if isinstance(h, (int, float))]
    recent = [h for h in hops if now - h < HOP_WINDOW_LONG]
    if any(now - h < HOP_WINDOW_SHORT for h in recent):
        return False
    return len(recent) < HOP_MAX_LONG


def _record_hop(state: dict, task_id: str, now: float) -> None:
    hops = [
        h for h in _entry(state, task_id).get("hops") or []
        if isinstance(h, (int, float)) and now - h < HOP_WINDOW_LONG
    ]
    hops.append(now)
    state[task_id] = {"hops": hops, "alerted": False}


def _core():
    """The read-only core helpers this module mirrors.

    Reusing ``_rows_since``/``_is_handoff_event`` and the guard's own regex and
    window keeps "same comment, same 24 h window" literal rather than a
    reimplementation that can drift from the guard it compensates for.
    """
    from hermes_cli import kanban_db
    from hermes_cli import kanban_db_dispatch

    return kanban_db_dispatch, kanban_db


def _newest_pr_comment(conn, task_id: str, now: float) -> Optional[tuple]:
    """``(epoch, pr_url)`` of the newest PR-URL comment inside the guard window."""
    dispatch, kb = _core()
    found = []
    for t, c in dispatch._rows_since(
            conn, "task_comments", task_id, int(now) - dispatch._RESPAWN_GUARD_PR_WINDOW,
            cols=", body", unreadable=int(now)):
        match = dispatch._RESPAWN_GUARD_PR_URL_RE.search(kb._lossy_text(c["body"]) or "")
        if match:
            found.append((t, match.group(0)))
    return max(found) if found else None


def _newest_handoff(conn, task_id: str, since: int) -> Optional[int]:
    dispatch, _ = _core()
    times = [
        t for t, e in dispatch._rows_since(
            conn, "task_events", task_id, since,
            "AND kind IN ('assigned', 'changes_requested', 'review_reopened')",
            cols=", kind, payload", strict=True,
        )
        if dispatch._is_handoff_event(e["kind"], e["payload"])
    ]
    return max(times) if times else None


def _newest_unblock(conn, task_id: str, since: int) -> Optional[int]:
    dispatch, _ = _core()
    times = [
        t for t, _ in dispatch._rows_since(
            conn, "task_events", task_id, since, "AND kind = 'unblocked'", strict=True,
        )
    ]
    return max(times) if times else None


def _candidate_profile(assignee: Optional[str]) -> Optional[str]:
    """An existing profile that is not the current assignee, else None.

    Source list is ``kanban.dispatch_profiles`` when set, else ``["default"]``.
    A non-existent candidate is never returned: assigning to one would park the
    card outside every dispatcher's spawnable set.
    """
    from hermes_cli.profiles import normalize_profile_name, profile_exists

    try:
        from hermes_cli.config_effective import load_user_config_effective

        kanban = (load_user_config_effective(fail_closed=True) or {}).get("kanban", {})
    except Exception:
        kanban = {}
    raw = kanban.get("dispatch_profiles") if isinstance(kanban, dict) else None
    if raw:
        names = [str(n) for n in raw] if isinstance(raw, (list, tuple)) else str(raw).split(",")
    else:
        names = list(DEFAULT_CANDIDATES)
    for name in names:
        try:
            canon = normalize_profile_name(name)
        except ValueError:
            continue
        if canon != assignee and profile_exists(canon):
            return canon
    return None


def _alert(conn, kb, task_id: str, state: dict, url: str, now: float) -> bool:
    """Post the one explanatory comment, at most once per guard episode."""
    entry = dict(_entry(state, task_id))
    if entry.get("alerted"):
        return False
    entry["alerted"] = True
    state[task_id] = entry
    hops = [h for h in _entry(state, task_id).get("hops") or [] if isinstance(h, (int, float))]
    recent = [h for h in hops if now - h < HOP_WINDOW_LONG]
    if len(recent) >= HOP_MAX_LONG:
        why = (
            f"keeps re-asserting on this card after {HOP_MAX_LONG} automatic lifts in the last hour, "
            "so the auto-lift has stopped."
        )
    else:
        why = (
            "is still holding this card because the automatic lift did not land after the PR comment "
            "(same clock second), so core does not count it as a handoff."
        )
    body = (
        f"{ALERT_AUTHOR}: the `active_pr` respawn guard {why} "
        f"The PR link in the newest comment ({url}) is what makes core hold the card; "
        "a human must decide. Reply with new instructions, or reassign the card yourself."
    )
    try:
        kb.add_comment(conn, task_id, ALERT_AUTHOR, body)
    except Exception:
        logger.warning("agent-guards active_pr unblock: alert comment failed for %s", task_id, exc_info=True)
        return False
    return True


def _hop_once(conn, task_id: str, now: float, state: dict) -> bool:
    row = conn.execute(
        "SELECT status, claim_lock, current_run_id, assignee FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None or row["status"] != "ready" or row["claim_lock"] or row["current_run_id"] is not None:
        return False
    found = _newest_pr_comment(conn, task_id, now)
    if found is None:
        return False
    pr_at, url = found
    if _newest_handoff(conn, task_id, pr_at) is not None:
        logger.info(
            "agent-guards active_pr unblock: %s has a handoff after its PR comment; core lifts it", task_id,
        )
        return False
    if _newest_unblock(conn, task_id, pr_at) is None:
        return False
    if not _allowed(state, task_id, now):
        return _alert(conn, _core()[1], task_id, state, url, now)
    other = _candidate_profile(row["assignee"])
    if other is None:
        logger.info(
            "agent-guards active_pr unblock: no hop profile for %s (assignee=%s)",
            task_id, row["assignee"],
        )
        return False
    kb = _core()[1]
    original = row["assignee"]
    kb.assign_task(conn, task_id, other)
    kb.assign_task(conn, task_id, original)
    _record_hop(state, task_id, now)
    return True


def _lift(conn, task_ids) -> None:
    now = time.time()
    state = _read_state()
    changed = False
    for task_id in task_ids:
        try:
            changed = _hop_once(conn, task_id, now, state) or changed
        except Exception:
            logger.warning("agent-guards active_pr unblock: %s failed", task_id, exc_info=True)
    if changed:
        try:
            _write_state(state)
        except Exception:
            logger.warning("agent-guards active_pr unblock: state write failed", exc_info=True)


def tick(result: Any = None, board: Optional[str] = None, dry_run: bool = False, **_: Any) -> None:
    """``on_kanban_dispatch_tick`` observer: lift every ``active_pr`` hold."""
    try:
        if dry_run or result is None:
            return
        guarded = {tid for tid, reason in (getattr(result, "respawn_guarded", None) or []) if reason == REASON}
        if not guarded:
            return
        from hermes_cli import kanban_db_connect

        conn = kanban_db_connect.connect(board=board)
        try:
            _lift(conn, guarded)
        finally:
            conn.close()
    except Exception:
        logger.warning("agent-guards active_pr unblock: tick failed", exc_info=True)


def register(ctx) -> None:
    ctx.register_hook("on_kanban_dispatch_tick", tick)
