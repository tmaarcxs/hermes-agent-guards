"""Scope a lead chat's ``kanban_list`` result to cards its own chat thread owns.

Active only in a lead process (``kanban.default_assignee`` set; ``HERMES_KANBAN_TASK``
absent; see ``kanban_policy``/``lead_delegation``) and only when the session origin is a
human messaging channel (``HERMES_SESSION_PLATFORM`` + ``HERMES_SESSION_CHAT_ID`` session
context; CLI, cron, TUI and workers leave the result unchanged).

Owner rule: a card belongs to the chat thread subscribed to it (``kanban_notify_subs``
rows keyed by platform/chat_id/thread_id). Without the filter a lead chat reporting its
status and Tasklist pulls in cards owned by other chats. Enforcement lives here because a
prose rule in the skills failed in practice.

``transform`` rides ``transform_tool_result`` and rewrites only a successful
``kanban_list`` JSON result. The tool handler applies ``limit`` (and truncation)
at the SQL layer *before* the transform runs, so a foreign chat's cards can fill
the whole returned window and a naive post-filter would hide this chat's own
later cards. Instead the transform re-queries the board with the call's own
filters (``assignee``/``status``/``tenant``/``include_archived``, same defaults as
the handler), keeps only the cards this chat subscribes to, applies the call's
``limit`` to that entirely-owned set, and rebuilds the result with the handler's
own summary helper: ``tasks`` is that owned slice, ``count`` follows it,
``truncated``/``next_limit`` are recomputed from the owned set (same
``next_limit`` doubling rule as the handler), ``hidden_foreign``
reports how many filter-matching cards on the board belong to other chats, and
``promoted`` (already applied by the handler) is preserved. When the incoming
window already shows exactly the owned view (same ids, same truncation), the
original result string is returned untouched. ``kanban_show`` of a foreign card
stays allowed — reading a card that constrains own work is legitimate. Fails
open: any read, parse or rebuild error returns ``None`` (the result is
untouched).
"""

import json
import os
import sqlite3
from typing import Optional

from . import kanban_policy

_HIDDEN_FIELD = "hidden_foreign"


def _origin() -> Optional[tuple[str, str, Optional[str]]]:
    """``(platform, chat_id, thread_id)`` of the calling session, or ``None``.

    Mirrors the gateway binding that ``tools/kanban_tools._resolve_notify_target`` reads:
    bindings live in per-turn session ContextVars (``gateway.session_context``), which are
    empty for CLI, cron and worker processes. A platform without a chat id is not a
    messaging origin and leaves the result unchanged.
    """
    if os.environ.get("HERMES_KANBAN_TASK"):
        return None  # worker process: orchestrator listing is untouched
    try:
        from gateway.session_context import get_session_env
    except Exception:
        return None
    platform = get_session_env("HERMES_SESSION_PLATFORM", "")
    chat_id = get_session_env("HERMES_SESSION_CHAT_ID", "")
    if not platform or not chat_id:
        return None
    thread_id = get_session_env("HERMES_SESSION_THREAD_ID", "") or None
    return platform, chat_id, thread_id


def subscribed_task_ids(conn: sqlite3.Connection, platform: str, chat_id: str,
                        thread_id: Optional[str]) -> set[str]:
    """Task ids with a subscription row for this origin (platform case-insensitive,
    chat id exact, thread id or none)."""
    keys = conn.execute(
        "SELECT task_id, LOWER(platform), chat_id, thread_id FROM kanban_notify_subs").fetchall()
    wanted_platform, wanted_thread = platform.lower(), (thread_id or "")
    return {
        task_id for task_id, sub_platform, sub_chat, sub_thread in keys
        if sub_platform == wanted_platform and sub_chat == chat_id
        and (sub_thread or "") == wanted_thread
    }


def transform(tool_name: str, args: object, result: object) -> Optional[str]:
    """``transform_tool_result``: scope a lead ``kanban_list`` to the calling chat thread.

    The board resolution follows the tool's own: the args' board slug, else the
    env-pinned/default board (``HERMES_KANBAN_DB`` and back-compat paths)."""
    if tool_name != "kanban_list" or not isinstance(result, str):
        return None
    origin = _origin()
    if origin is None or not kanban_policy.configured_worker():
        return None
    try:
        payload = json.loads(result)
        if not isinstance(payload, dict) or not isinstance(payload.get("tasks"), list):
            return None
        from hermes_cli import kanban_db_connect as kbc
        from hermes_cli import kanban_db as kb

        args_dict = args if isinstance(args, dict) else {}
        board_slug = str(args_dict.get("board")) if args_dict.get("board") else None
        db_path = kb.kanban_db_path(board=board_slug)
        if not db_path.exists():
            return None  # no board to read: fail open rather than hide everything
        board_view = _board_view(board_slug, kb, kbc, origin, args_dict)
        if board_view is None:
            return None  # board read or rebuild failed: fail open
        kept_ids, summary_dicts, hidden_foreign, owned_beyond = board_view

        window_ids = [str(task.get("id") or "") for task in payload["tasks"]
                      if isinstance(task, dict)]
        if window_ids == kept_ids and bool(payload.get("truncated")) == owned_beyond:
            return None  # the incoming window already is the owned view
        out = dict(payload)
        out["tasks"] = summary_dicts
        out["count"] = len(summary_dicts)
        out["truncated"] = owned_beyond
        limit = _limit(args_dict)
        out["next_limit"] = min(limit * 2, 200) if owned_beyond and limit < 200 else None
        out[_HIDDEN_FIELD] = hidden_foreign
        return json.dumps(out)
    except Exception:
        return None  # any read, parse or rebuild failure leaves the result untouched


def _board_view(board_slug, kb, kbc, origin, args):
    """Re-derive the owned view ``(kept_ids, summaries, hidden_foreign, owned_beyond)``.

    Mirrors ``tools.kanban_tools._handle_list``'s arg handling and output shape but
    filters the board to this origin's subscriptions before applying ``limit``, so
    own cards beyond the handler's window are still visible. ``hidden_foreign``
    counts filter-matching cards the chat does not subscribe to — board-wide, the
    count the handler's window could never have shown. Returns ``None`` when any
    read or summary build fails."""
    try:
        from tools.kanban_tools import _task_summary_dict
    except Exception:
        return None
    conn = None
    try:
        conn = kbc.connect(board=board_slug)
        owned = subscribed_task_ids(conn, *origin)
        rows = kb.list_tasks(conn, assignee=args.get("assignee"), status=args.get("status"),
                             tenant=args.get("tenant"),
                             include_archived=_parse_bool(args.get("include_archived")))
        kept_rows = [row for row in rows if row.id in owned]
        limit = _limit(args)
        kept_rows, owned_beyond = kept_rows[:limit], len(kept_rows) > limit
        hidden_foreign = sum(1 for row in rows if row.id not in owned)
        return ([row.id for row in kept_rows],
                [_task_summary_dict(kb, conn, task) for task in kept_rows],
                hidden_foreign, owned_beyond)
    except Exception:
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _limit(args: dict) -> int:
    """The handler's ``limit`` handling: default 50, bounds-checked 1..200. An
    out-of-bounds or non-integer limit never reaches the transform (the handler
    rejects it first); anything unexpected here fails open via ValueError."""
    value = args.get("limit")
    limit = 50 if value is None else int(value)
    if not 1 <= limit <= 200:
        raise ValueError(limit)
    return limit


def _parse_bool(value) -> bool:
    """The handler's ``_parse_bool_arg`` semantics without the reject path: absent
    or ``None`` is ``False``, real booleans pass through, string forms the handler
    accepts are mapped, unknown strings raise (caller fails open)."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    parsed = {"true": True, "1": True, "yes": True,
              "false": False, "0": False, "no": False}.get(str(value).strip().lower())
    if parsed is None:
        raise ValueError(value)
    return parsed
