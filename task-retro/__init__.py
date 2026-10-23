"""Auto-create a retrospective card when a worker card completes.

Subscribes to the ``kanban_task_completed`` lifecycle hook. That hook fires in
the WORKER process immediately AFTER the completion write transaction commits
(``hermes_cli/kanban_db.py``, ``_fire_task_hook("kanban_task_completed", ...)``
right after ``complete_task``'s txn), so the run row and its metadata are
already durable when this code runs.

Also subscribes to ``kanban_task_blocked`` (same ``_fire_task_hook`` path, in
the process that blocked the card) to archive a retro the moment it is blocked,
so it never gates its queued children.

Behaviour:
- one retro card per completed task, ``idempotency_key="retro:<task_id>"``;
- no notify subscription is created for the new card (no ``creator_task_id``,
  so neither the notify-sub inheritance paths nor the ``kanban_create``
  auto-subscribe path are touched);
- the retro card carries the worker session id so the retrospective worker can
  find the session in ``state.db``;
- loop guard: a card whose title starts with ``retro:`` or that already carries
  the ``task-retrospective`` skill never spawns another retro;
- trivial cards (the card's TOTAL run wall, summed over all its runs, below
  ``min_duration_seconds``) are skipped;
- probe/throwaway titles (``[probe]``, a ``probe:`` prefix, ``throwaway``) are
  skipped — a throwaway card must not earn a retrospective;
- **at most THREE retros are active at once.** Open retros (status
  ``todo``/``ready``/``running``) form lanes: a *lane tail* is an open retro with
  no open retro child, and each tail is one lane. A completion that arrives
  while fewer than ``_MAX_ACTIVE_RETROS`` lanes are open still creates its retro
  card, unparented, so it starts immediately. Once every lane is taken, the new
  retro becomes a CHILD of the OLDEST lane tail (``created_at`` asc, id
  tie-break), so retros round-robin across the lanes: A, B, C free; D behind A;
  E behind B; F behind C; G behind D. A parented retro waits in ``todo`` and
  auto-promotes when its parent finishes. Nothing is ever dropped. Tail
  selection and insertion share ONE ``write_txn`` (``BEGIN IMMEDIATE``) so two
  workers completing at the same instant cannot both read the same lane state
  and both insert a ready retro; the lock covers only that lookup+insert, never
  slow work.
- ``blocked``/``archived``/``done`` retros are NOT lane tails. A blocked retro
  would park its queued children in ``todo`` forever (``recompute_ready``
  releases a child only when EVERY parent is ``done``/``archived``), so the
  ``kanban_task_blocked`` hook archives a blocked retro and releases its
  children. Nobody ever unblocks a retro, so archiving is the honest terminal
  state. The dispatcher circuit-breaker parks cards with a raw ``UPDATE`` and
  never fires the hook, so every ``kanban_task_completed`` fire also sweeps any
  retro left in ``blocked``/``triage`` and archives it.
- a retro is created only once per completed task, even if a retro for that
  task was later archived (``create_task``'s own idempotency check ignores
  archived rows, so the plugin checks the key itself first).
- the completion sweep also retires OPEN retros (``todo``/``ready``) older than
  ``queue_max_age_seconds``: each is archived with a comment naming its parent
  card, which releases its queued children. A ``running`` retro is live work and
  is never aged out; ``0`` disables the sweep. Without it a busy board grows an
  unbounded ``todo`` backlog behind the three lanes.
- every failure is logged and swallowed — this hook never raises and never
  blocks a completion. In particular, when the transaction, the lane lookup,
  the link call or the archive fails, the retro card is still created
  (unparented) rather than lost.

Config (``plugins.entries.task-retro`` in the profile's config.yaml):
``enabled`` (default true), ``min_duration_seconds`` (default 900, measured on
the PARENT CARD'S TOTAL run wall — the sum of all its runs), ``queue_max_age_seconds``
(default 21600 = 6 h), ``debug``.
"""

from __future__ import annotations

import time
import traceback
from pathlib import Path
from typing import Any, Dict, Optional

_DEFAULT_MIN_DURATION_SECONDS = 900
_DEFAULT_QUEUE_MAX_AGE_SECONDS = 6 * 3600
_RETRO_SKILL = "task-retrospective"
# The skill ships inside this plugin and resolves only as the qualified name
# ``task-retro:task-retrospective``; the bare name is kept for the loop guard so
# cards written before this change (or by hand) still suppress a second retro.
_RETRO_SKILL_QUALIFIED = "task-retro:task-retrospective"
_TITLE_PREFIX = "retro:"
_SKILL_NAME = "task-retrospective"
_SKILL_DIR = Path(__file__).resolve().parent / "skills" / _SKILL_NAME
_LOG_PATH = Path(__file__).resolve().parent / "debug.log"

# A completion whose original title carries one of these markers is a
# probe/throwaway and earns no retrospective.
_PROBE_MARKERS = ("[probe]", "throwaway")
_PROBE_PREFIX = "probe:"

# Statuses that make a retro card an OPEN lane. Only an open retro can gate the
# next one; ``blocked``/``done``/``archived`` never do.
_ACTIVE_RETRO_STATUSES = ("todo", "ready", "running")

# How many retros may be active at once. Open retros form this many lanes; each
# lane is a chain of queued retros behind one lane tail.
_MAX_ACTIVE_RETROS = 3

# A retro in one of these statuses is parked awaiting a human decision and will
# not progress on its own. Nobody ever unblocks a retro, so the blocked hook (and
# the completed-hook sweep covering blocks the dispatcher makes without firing
# the hook) archives it, which releases its queued children. ``review`` is NOT
# swept: a review handoff is a live lane, not a park.
_ARCHIVE_RETRO_STATUSES = ("blocked", "triage")

# A retro in one of these statuses is WAITING in the queue: it has not started,
# so the age cap may retire it. ``running`` is live work and is never aged out.
_OPEN_QUEUE_STATUSES = ("todo", "ready")

# config.yaml does not change mid-run and load_config does file I/O — resolve once.
_config_cache: Optional[Dict[str, Any]] = None


def register(ctx) -> None:
    ctx.register_hook("kanban_task_completed", _on_kanban_task_completed)
    ctx.register_hook("kanban_task_blocked", _on_kanban_task_blocked)
    # Ship the retrospective skill inside the plugin so installing the plugin is
    # enough: it is served read-only as ``task-retro:task-retrospective`` and is
    # NOT copied into ``~/.hermes/skills/``. A missing tree must not stop the
    # hook from registering.
    try:
        if (_SKILL_DIR / "SKILL.md").is_file():
            ctx.register_skill(
                _SKILL_NAME, _SKILL_DIR / "SKILL.md",
                description="Mine a finished worker session and fix its governing rule.",
            )
    except Exception:
        _log(f"skill registration failed:\n{traceback.format_exc()}")


# --- config -----------------------------------------------------------------


def _config() -> Dict[str, Any]:
    global _config_cache
    if _config_cache is None:
        try:
            from hermes_cli.config import load_config_readonly

            cfg = load_config_readonly() or {}
            entries = ((cfg.get("plugins") or {}).get("entries") or {})
            _config_cache = {
                "plugin": entries.get("task-retro") or {},
                "default_assignee": str(
                    ((cfg.get("kanban") or {}).get("default_assignee")) or ""
                ).strip(),
            }
        except Exception:
            _config_cache = {"plugin": {}, "default_assignee": ""}
    return _config_cache


def _log(msg: str) -> None:
    if not (_config().get("plugin") or {}).get("debug"):
        return
    try:
        with open(_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass  # a broken log path must never break a completion


def _min_duration_seconds(cfg: Dict[str, Any]) -> float:
    try:
        value = float(cfg.get("min_duration_seconds", _DEFAULT_MIN_DURATION_SECONDS))
    except (TypeError, ValueError):
        return float(_DEFAULT_MIN_DURATION_SECONDS)
    return value if value >= 0 else float(_DEFAULT_MIN_DURATION_SECONDS)


def _queue_max_age_seconds(cfg: Dict[str, Any]) -> float:
    try:
        value = float(cfg.get("queue_max_age_seconds", _DEFAULT_QUEUE_MAX_AGE_SECONDS))
    except (TypeError, ValueError):
        return float(_DEFAULT_QUEUE_MAX_AGE_SECONDS)
    return value if value >= 0 else float(_DEFAULT_QUEUE_MAX_AGE_SECONDS)


# --- data helpers -----------------------------------------------------------


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _title_of(task: Any) -> str:
    if task is None:
        return ""
    if isinstance(task, dict):
        return str(task.get("title") or "")
    return str(getattr(task, "title", "") or "")


def _skills_of(task: Any) -> list:
    raw = task.get("skills") if isinstance(task, dict) else getattr(task, "skills", None)
    if isinstance(raw, str):
        try:
            import json

            parsed = json.loads(raw)
        except Exception:
            return []
        return list(parsed) if isinstance(parsed, list) else []
    return list(raw or [])


def _field(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _is_retro_card(task: Any) -> bool:
    """Loop guard: never spawn a retro for a retro.

    Matches the qualified name the plugin writes today and the bare name older
    cards (or hand-written ones) may still carry, so neither can spawn a retro.
    """
    title = _title_of(task).strip().lower()
    if title.startswith(_TITLE_PREFIX):
        return True
    retro_skills = {_RETRO_SKILL, _RETRO_SKILL_QUALIFIED}
    return any(str(s).strip() in retro_skills for s in _skills_of(task))


def _is_probe_title(title: str) -> bool:
    """True for probe/throwaway titles, which must earn no retrospective."""
    text = (title or "").strip().lower()
    if not text:
        return False
    if text.startswith(_PROBE_PREFIX):
        return True
    return any(marker in text for marker in _PROBE_MARKERS)


def _session_id_from_run(run: Any) -> str:
    """``worker_session_id`` recorded on the run by the kanban tool layer."""
    metadata = _field(run, "metadata")
    if isinstance(metadata, dict):
        value = metadata.get("worker_session_id")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _session_id_from_env() -> str:
    import os

    return (os.environ.get("HERMES_SESSION_ID") or "").strip()


def _parent_total_wall_seconds(conn: Any, kb: Any, task_id: str, run: Any) -> float:
    """Sum of every run's wall on the PARENT CARD (the gate's unit).

    The completing run alone under-measures a card retried after a crash: two
    400 s attempts are an 800 s card. Falls back to the completing run's own
    duration when the run list is unavailable, so an unreadable history keeps
    the previous single-run behaviour instead of silently admitting everything.
    """
    try:
        runs = kb.list_runs(conn, task_id, include_active=False)
    except Exception:
        _log(f"runs read failed for {task_id}:\n{traceback.format_exc()}")
        runs = None
    if runs is None:
        return float(max(0, (_as_int(_field(run, "ended_at")) or 0)
                         - (_as_int(_field(run, "started_at")) or 0)))
    total = 0
    for item in runs:
        started = _as_int(_field(item, "started_at")) or 0
        ended = _as_int(_field(item, "ended_at")) or 0
        total += max(0, ended - started)
    return float(total)


def _build_body(
    task_id: str, run_id: Optional[int], board: Optional[str], title: str,
    summary: Optional[str], session_id: str,
) -> str:
    lines = [
        f"Retrospective for completed card `{task_id}`.",
        "",
        f"- task_id: `{task_id}`",
        f"- run_id: `{run_id if run_id is not None else 'unknown'}`",
        f"- board: `{board or 'default'}`",
        f"- original title: {title}",
    ]
    if session_id:
        lines.append(f"- worker session id: `{session_id}`")
    else:
        lines.append(
            "- worker session id: NOT AVAILABLE at hook time (no "
            "`task_runs.metadata.worker_session_id`, no `HERMES_SESSION_ID`); "
            f"locate the session for `{task_id}` in "
            "`~/.hermes/profiles/worker/state.db` with `source='kanban'`."
        )
    lines += ["", "## Handoff summary", "", (summary or "(no summary recorded)"), ""]
    lines += [
        "## How to run the retrospective",
        "",
        f"This card carries the `{_RETRO_SKILL_QUALIFIED}` skill (shipped inside the",
        "`task-retro` plugin, not copied under `~/.hermes/skills/`). Follow it for the",
        "session above: mine the worker session for model failures, then apply the",
        "smallest durable correction to the governing guidance.",
        "",
        f"If it was not force-loaded, resolve it first with",
        f"`skill_view(name=\"{_RETRO_SKILL_QUALIFIED}\")`.",
        "",
    ]
    return "\n".join(lines)


# --- lanes ------------------------------------------------------------------


def _open_retro_tails(conn: Any, kb: Any) -> list:
    """``[(created_at, id)]`` oldest-first, one entry per open lane tail.

    A lane tail is an open retro (``todo``/``ready``/``running``) with no open
    retro child; each tail is one lane. ``created_at`` (id tie-break) lets the
    caller pick the oldest lane deterministically. Read-only and fail-open — an
    unreadable board yields ``[]``, so the new retro is created unparented.
    """
    try:
        rows = kb.list_tasks(conn, include_archived=False)
    except Exception:
        _log("lanes: unreadable board:\n" + traceback.format_exc())
        return []
    status: dict = {}
    created: dict = {}
    for task in rows:
        if not _title_of(task).strip().lower().startswith(_TITLE_PREFIX):
            continue
        task_id = str(_field(task, "id") or "")
        status[task_id] = str(_field(task, "status") or "")
        created[task_id] = _as_int(_field(task, "created_at")) or 0
    open_ids = {tid for tid, st in status.items() if st in _ACTIVE_RETRO_STATUSES}
    tails = []
    for task_id in open_ids:
        try:
            children = {str(c) for c in kb.child_ids(conn, task_id)}
        except Exception:
            _log(f"lanes: child read failed for {task_id}:\n{traceback.format_exc()}")
            continue
        if children & open_ids:
            continue
        tails.append((created.get(task_id, 0), task_id))
    tails.sort()
    return tails


def _active_retro_parent(conn: Any, kb: Any) -> str:
    """Parent id for a new retro, or ``""`` when a lane is free.

    Fewer than ``_MAX_ACTIVE_RETROS`` open lane tails -> ``""`` (the new retro
    starts now). Otherwise the OLDEST tail (``created_at`` asc, id tie-break), so
    retros round-robin across the lanes: A/B/C free; D behind A; E behind B;
    F behind C; G behind D. Callers that must not race another completion run
    this INSIDE the same ``write_txn`` as the insert.
    """
    tails = _open_retro_tails(conn, kb)
    if len(tails) < _MAX_ACTIVE_RETROS:
        return ""
    return tails[0][1]


# --- archiving blocked retros -----------------------------------------------


def _archive_retro(conn: Any, kb: Any, task_id: str) -> bool:
    """Archive one retro card; never raises. Returns True when it was archived.

    ``kanban_db.archive_task`` opens its own write txn and calls
    ``recompute_ready`` after commit, which releases any child queued behind the
    archived parent.
    """
    try:
        return bool(kb.archive_task(conn, task_id))
    except Exception:
        _log(f"archive {task_id} failed:\n{traceback.format_exc()}")
        return False


def _archive_parked_retros(conn: Any, kb: Any) -> int:
    """Archive every retro left ``blocked``/``triage``; never raises.

    Safety net for blocks that do not fire ``kanban_task_blocked``: the
    dispatcher circuit-breaker parks cards with a raw ``UPDATE`` and fires no
    hook, so such a retro would gate its queued children forever. ``review`` is
    deliberately not swept — a review handoff is a live lane.
    """
    try:
        rows = kb.list_tasks(conn, include_archived=False)
    except Exception:
        _log("sweep: unreadable board:\n" + traceback.format_exc())
        return 0
    parked = [
        str(_field(task, "id") or "") for task in rows
        if str(_field(task, "status") or "") in _ARCHIVE_RETRO_STATUSES
        and _title_of(task).strip().lower().startswith(_TITLE_PREFIX)
    ]
    return sum(1 for task_id in parked if task_id and _archive_retro(conn, kb, task_id))


def _retro_parent_label(conn: Any, kb: Any, task: Any) -> str:
    """The completed card this retro belongs to, for an archive reason.

    The retro's own parent is the completed work card recorded in
    ``idempotency_key`` (``retro:<task_id>``); a lane link names the retro it
    was queued behind. Both are reported when present.
    """
    task_id = str(_field(task, "id") or "")
    key = str(_field(task, "idempotency_key") or "")
    card = key[len(_TITLE_PREFIX):] if key.startswith(_TITLE_PREFIX) else ""
    try:
        behind = [str(p) for p in kb.parent_ids(conn, task_id) if p]
    except Exception:
        behind = []
    if card and behind:
        return f"{card} (queued behind {behind[0]})"
    if card:
        return card
    return behind[0] if behind else ""


def _archive_stale_retros(conn: Any, kb: Any, max_age_seconds: float) -> int:
    """Archive OPEN retros (``todo``/``ready``) older than the age cap; never raises.

    A ``running`` retro is live work and is never touched. Archiving releases the
    queued children (``archive_task`` calls ``recompute_ready``), and a comment on
    the card records WHY, naming its parent, so the skip is never silent.
    """
    if max_age_seconds <= 0:
        return 0
    try:
        rows = kb.list_tasks(conn, include_archived=False)
    except Exception:
        _log("age sweep: unreadable board:\n" + traceback.format_exc())
        return 0
    cutoff = int(time.time()) - int(max_age_seconds)
    stale = [
        task for task in rows
        if str(_field(task, "status") or "") in _OPEN_QUEUE_STATUSES
        and _title_of(task).strip().lower().startswith(_TITLE_PREFIX)
        and (_as_int(_field(task, "created_at")) or 0) < cutoff
    ]
    archived = 0
    for task in stale:
        task_id = str(_field(task, "id") or "")
        if not task_id:
            continue
        parent = _retro_parent_label(conn, kb, task)
        reason = (
            f"retro skipped for age: queued longer than the "
            f"{int(max_age_seconds)}s cap (parent {parent or 'unknown'}); "
            "archived so its queued children are released"
        )
        try:
            kb.add_comment(conn, task_id, "task-retro", reason)
        except Exception:
            _log(f"age sweep: comment failed for {task_id}:\n{traceback.format_exc()}")
        if _archive_retro(conn, kb, task_id):
            _log(f"age sweep: archived {task_id} (parent {parent or 'unknown'})")
            archived += 1
    return archived



# --- hook -------------------------------------------------------------------


def _on_kanban_task_completed(
    task_id: Optional[str] = None,
    board: Optional[str] = None,
    assignee: Optional[str] = None,
    run_id: Optional[int] = None,
    profile_name: Optional[str] = None,
    summary: Optional[str] = None,
    **_ignored: Any,
) -> None:
    """Create one retro card. Never raises; never blocks the completion."""
    try:
        _create_retro_card(task_id, board, run_id, summary)
    except Exception:
        _log(f"FAILED task={task_id} run={run_id}:\n{traceback.format_exc()}")
        return None
    return None


def _on_kanban_task_blocked(
    task_id: Optional[str] = None,
    board: Optional[str] = None,
    assignee: Optional[str] = None,
    run_id: Optional[int] = None,
    profile_name: Optional[str] = None,
    reason: Optional[str] = None,
    **_ignored: Any,
) -> None:
    """Archive a blocked retro so it stops gating its children. Never raises."""
    try:
        _archive_blocked_retro(task_id, board)
    except Exception:
        _log(f"blocked FAILED task={task_id}:\n{traceback.format_exc()}")
    return None


def _archive_blocked_retro(task_id: Optional[str], board: Optional[str]) -> None:
    cfg = _config()
    if (cfg.get("plugin") or {}).get("enabled", True) is False:
        return
    if not task_id:
        return

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    conn = kbc.connect(board=_resolved_board(board))
    try:
        task = kb.get_task(conn, task_id)
        if task is None or not _is_retro_card(task):
            return
        _log(f"blocked {task_id}: archiving retro")
        _archive_retro(conn, kb, task_id)
    finally:
        conn.close()


def _resolved_board(board: Optional[str]) -> Optional[str]:
    """Board slug to resolve through, or ``None`` to honour the process pin.

    ``kanban_db._board_path`` lets an explicit slug outrank the
    ``HERMES_KANBAN_DB`` pin, so passing the literal ``"default"`` back in would
    ignore the pinned database and open ``kanban_home()/kanban.db`` instead.
    ``None`` keeps the pin/env/symlink resolution chain, which IS the board this
    process is pinned to; a named board still resolves through its own slug.
    """
    slug = (board or "").strip()
    return None if slug in ("", "default") else slug


def _existing_retro_id(conn: Any, task_id: str) -> str:
    """Id of an existing retro card for ``task_id`` in ANY status, else ``""``.

    ``kanban_db.create_task``'s own idempotency check filters
    ``status != 'archived'``, so an archived retro would stop protecting the key
    and a re-fired completion could mint a second retro for the same task. This
    check keeps ``retro:<task_id>`` authoritative for every status. It runs
    INSIDE the caller's write transaction, so it also closes the
    check-then-insert race ``create_task`` leaves open.
    """
    row = conn.execute(
        "SELECT id FROM tasks WHERE idempotency_key = ? "
        "ORDER BY created_at DESC LIMIT 1",
        (f"{_TITLE_PREFIX}{task_id}",),
    ).fetchone()
    if row is None:
        return ""
    try:
        return str(row["id"] or "")
    except (TypeError, IndexError, KeyError):
        return str(row[0] or "")


def _create_retro_card(
    task_id: Optional[str], board: Optional[str], run_id: Any, summary: Optional[str],
) -> None:
    cfg = _config()
    plugin_cfg = cfg.get("plugin") or {}
    if plugin_cfg.get("enabled", True) is False:
        _log("skip: plugin disabled")
        return
    if not task_id:
        _log("skip: no task_id in hook payload")
        return

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    board_slug = _resolved_board(board)
    conn = kbc.connect(board=board_slug)
    try:
        task = kb.get_task(conn, task_id)
        if task is None:
            _log(f"skip {task_id}: no such task row")
            return
        if _is_retro_card(task):
            _log(f"skip {task_id}: loop guard (retro card)")
            return
        original_title = _title_of(task).strip()
        if _is_probe_title(original_title):
            _log(f"skip {task_id}: probe/throwaway title {original_title!r}")
            return

        run = kb.get_run(conn, int(run_id)) if run_id is not None else None
        # A missing run and a non-completed run are the same decision; the
        # outcome gate below subsumes ``run is None`` (``_field`` on None yields
        # None, which is != "completed"), so there is no separate branch to keep
        # in sync.
        if str(_field(run, "outcome") or "") != "completed":
            _log(f"skip {task_id}: outcome={_field(run, 'outcome')!r}")
            return

        started = _as_int(_field(run, "started_at")) or 0
        ended = _as_int(_field(run, "ended_at")) or 0
        duration = max(0, ended - started)
        total_wall = _parent_total_wall_seconds(conn, kb, task_id, run)
        threshold = _min_duration_seconds(plugin_cfg)
        if total_wall < threshold:
            _log(f"skip {task_id}: trivial card ({total_wall:.0f}s total "
                 f"< {threshold:.0f}s; completing run {duration}s)")
            return

        session_id = _session_id_from_run(run) or _session_id_from_env()
        _log(f"create {task_id}: run={run_id} run_duration={duration}s "
             f"card_total={total_wall:.0f}s session={session_id or '<none>'}")

        # Safety net FIRST, in its own transactions: the dispatcher's
        # circuit-breaker parks cards with a raw UPDATE and fires no
        # ``kanban_task_blocked``, so a parked retro would gate its queued
        # children forever. Archiving it releases them before the tail read.
        _archive_parked_retros(conn, kb)
        # Then retire OPEN retros that have waited past the age cap, so a busy
        # board cannot grow its queue without bound. ``running`` retros are live.
        _archive_stale_retros(conn, kb, _queue_max_age_seconds(plugin_cfg))

        retro_assignee = cfg.get("default_assignee") or _field(task, "assignee")
        title = f"{_TITLE_PREFIX} {original_title}".strip()
        body = _build_body(
            task_id, _as_int(run_id), board, original_title, summary, session_id,
        )
        _create_card(
            conn, kb, kbc, title=title, body=body, assignee=retro_assignee,
            task_id=task_id, board=board_slug,
        )
    finally:
        conn.close()


def _create_card(
    conn: Any, kb: Any, kbc: Any, *, title: str, body: str, assignee: Any,
    task_id: str, board: Optional[str],
) -> str:
    """Select the lane tail and insert the retro card ATOMICALLY.

    ``kanban_db_connect.write_txn`` opens ONE ``BEGIN IMMEDIATE`` around the tail
    lookup AND the insert (``create_task`` opts into the nested savepoint via
    ``allow_nested=True``), so two workers completing in the same instant cannot
    both read the same lane state and both insert a ready retro — the invariant
    that at most ``_MAX_ACTIVE_RETROS`` retros are active. The lock is held only
    for that lookup+insert, never across slow work, and SQLite's own busy timeout
    governs contention.

    ``parents=[parent_id]`` makes ``create_task`` compute status ``todo`` (a
    not-yet-``done`` parent parks the child) and ``recompute_ready`` promotes it
    the moment the parent finishes.

    No ``creator_task_id``: it would copy the completed card's notify
    subscriptions onto this card (and the ``kanban_create`` tool layer would
    auto-subscribe the calling chat). ``create_task`` alone never subscribes
    anyone.

    Fail-open: a transaction, lookup or link failure must not lose the retro.
    The atomic path is retried WITHOUT the transaction and WITHOUT a parent, so
    the card is still created (it may then run concurrently, which is strictly
    better than dropping it). Returns the new id, or ``""`` when a retro for
    ``task_id`` already exists.
    """
    kwargs = dict(
        title=title,
        body=body,
        assignee=assignee,
        skills=[_RETRO_SKILL_QUALIFIED],
        idempotency_key=f"{_TITLE_PREFIX}{task_id}",
        workspace_kind="scratch",
        board=board,
    )
    txn = getattr(kbc, "write_txn", None)
    if txn is not None:
        try:
            with txn(conn):
                existing = _existing_retro_id(conn, task_id)
                if existing:
                    _log(f"skip {task_id}: retro {existing} already exists")
                    return ""
                parent_id = _active_retro_parent(conn, kb)
                if parent_id:
                    new_id = kb.create_task(conn, parents=[parent_id], **kwargs)
                    _log(f"created {new_id} for {task_id} (child of {parent_id})")
                else:
                    new_id = kb.create_task(conn, **kwargs)
                    _log(f"created {new_id} for {task_id}")
                return str(new_id or "")
        except Exception:
            _log(
                f"atomic create failed for {task_id}; retrying unparented "
                f"(fail-open):\n{traceback.format_exc()}"
            )

    # Degraded / fail-open path: no outer transaction. Still dedupe, still never
    # drop the retro, never raise into the completion path.
    try:
        existing = _existing_retro_id(conn, task_id)
        if existing:
            _log(f"skip {task_id}: retro {existing} already exists")
            return ""
    except Exception:
        _log(f"idempotency read failed for {task_id}:\n{traceback.format_exc()}")
    new_id = kb.create_task(conn, **kwargs)
    _log(f"created {new_id} for {task_id}")
    return str(new_id or "")
