"""Bind a serve/WebSocket Desktop review to its existing live turn, never a label alone."""

from contextlib import closing
import sqlite3
import sys
import time
from pathlib import Path


def origin():
    """Fail closed unless the loaded legacy server owns this attended Desktop turn."""
    try:
        from gateway.session_context import bound_session_key, get_session_env
        from hermes_constants import get_hermes_home

        if get_session_env("HERMES_SESSION_PLATFORM", ""):
            return None
        source = get_session_env("HERMES_SESSION_SOURCE", "")
        if source not in {"gui", "desktop"}:
            return None
        truthy = {"1", "true", "yes", "on"}
        if any(str(get_session_env(name, "") or "").strip().lower() in truthy for name in
               ("HERMES_CRON_SESSION", "HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_SINGLE_QUERY_SESSION")):
            return None
        if get_session_env("HERMES_KANBAN_TASK", ""):
            return None
        server = sys.modules.get("tui_gateway.server")  # never import/create another owner
        ui = get_session_env("HERMES_UI_SESSION_ID", "")
        sid = get_session_env("HERMES_SESSION_ID", "")
        key = bound_session_key()
        home = Path(get_hermes_home()).resolve()
        with server._sessions_lock:
            record = server._sessions.get(ui)
            if not ui or not sid or not key or record is None:
                return None
            agent = record.get("agent")
            if (record.get("session_key") != key or record.get("source") != source
                    or not record.get("running") or record.get("_hosted_room_task")
                    or getattr(agent, "session_id", None) != sid):
                return None
            if sum(r.get("agent") is agent for r in server._sessions.values()) != 1:
                return None
            record_home = Path(record.get("profile_home") or server._launch_home()).resolve()
            if record_home != home or not server._session_has_live_transport(record):
                return None
            callback = getattr(agent, "background_review_callback", None)
            if not callable(callback):
                return None

        def latest_user():
            with closing(sqlite3.connect((home / "state.db").as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
                return db.execute("SELECT MAX(id) FROM messages WHERE session_id IN (?, ?) AND role = ?",
                                  (key, sid, "user")).fetchone()[0]

        # Submit-time persistence includes queued inputs. Do not review across one already waiting.
        with record["history_lock"]:
            if record.get("queued_prompt") or record.get("queued_prompts") or record.get("_turn_cancel_requested"):
                return None
            started = (record.get("inflight_turn") or {}).get("started_at")
            user_row = latest_user()
        if user_row is None:
            return None

        def current():
            try:
                with server._sessions_lock:
                    if (server._sessions.get(ui) is not record or record.get("agent") is not agent
                            or record.get("session_key") != key or record.get("source") != source
                            or getattr(agent, "session_id", None) != sid
                            or getattr(agent, "background_review_callback", None) is not callback
                            or record.get("_turn_cancel_requested")
                            or record.get("queued_prompt") or record.get("queued_prompts")
                            or record.get("_hosted_room_task")
                            or Path(record.get("profile_home") or server._launch_home()).resolve() != home
                            or sum(r.get("agent") is agent for r in server._sessions.values()) != 1
                            or (record.get("inflight_turn") is not None
                                and record["inflight_turn"].get("started_at") != started)
                            or not server._session_has_live_transport(record)):
                        return False
                return latest_user() == user_row
            except Exception:
                return False

        def deliver(message):
            deadline = time.monotonic() + 120
            while current():
                # message.complete is emitted before the legacy server releases running.
                with record["history_lock"]:
                    settled = not record.get("running")
                if settled:
                    if current():
                        callback(message)
                    return
                if time.monotonic() >= deadline:
                    raise TimeoutError("Desktop response did not settle")
                time.sleep(0.05)
            raise RuntimeError("Desktop review superseded or detached")

        return {"platform": "local", "chat_id": ui, "chat_type": "dm", "thread_id": "",
                "user_id": "", "message_id": str(user_row),
                "profile": get_session_env("HERMES_SESSION_PROFILE", ""),
                "_desktop_sender": deliver, "_desktop_current": current}
    except Exception:
        return None
