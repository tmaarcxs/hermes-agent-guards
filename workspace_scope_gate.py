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
``kanban_list`` JSON result: tasks are filtered to cards with a matching subscription,
``count`` follows the filtered list, ``hidden_foreign`` reports how many other-chat cards
were hidden, and every other key is preserved. ``kanban_show`` of a foreign card stays
allowed — reading a card that constrains own work is legitimate. Fails open: any read or
parse error returns ``None`` (the result is untouched).
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
    except Exception:
        return None
    tasks = payload.get("tasks") if isinstance(payload, dict) else None
    if not isinstance(tasks, list):
        return None
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db as kb

    try:
        board_slug = str(args.get("board")) if isinstance(args, dict) and args.get("board") else None
        db_path = kb.kanban_db_path(board=board_slug)
        if not db_path.exists():
            return None  # no board to read: fail open rather than hide everything
        conn = kbc.connect(board=board_slug)
    except Exception:
        return None
    try:
        owned = subscribed_task_ids(conn, *origin)
    except Exception:
        return None
    finally:
        conn.close()
    kept = [task for task in tasks
            if isinstance(task, dict) and str(task.get("id") or "") in owned]
    hidden = len(tasks) - len(kept)
    if hidden == 0:
        return None  # nothing foreign: keep the exact original result string
    payload = dict(payload)
    payload["tasks"] = kept
    payload["count"] = len(kept)
    payload[_HIDDEN_FIELD] = hidden
    return json.dumps(payload)
