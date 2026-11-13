"""Resolve only an attended Desktop turns existing owner-bound review sender."""


def turn_sender(runner):
    """Fail closed outside the bound Desktop owner; never send through a local adapter."""
    try:
        from gateway.session_context import get_session_env
        from gateway.session_authorities import active_authority
        from hermes_cli.plugin_session import session_surface
        if get_session_env("HERMES_SESSION_PLATFORM", "") != "local" or session_surface() != "desktop":
            return None
        truthy = {"1", "true", "yes", "on"}
        if any(str(get_session_env(name, "") or "").strip().lower() in truthy for name in
               ("HERMES_CRON_SESSION", "HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_SINGLE_QUERY_SESSION")):
            return None
        if get_session_env("HERMES_KANBAN_TASK", ""):
            return None
        if get_session_env("HERMES_SESSION_SOURCE", "") in {"tool", "oneshot", "api_server", "cron", "kanban", "subagent", "webhook", "bot_room"}:
            return None
        sid = get_session_env("HERMES_SESSION_ID", "")
        key = get_session_env("HERMES_SESSION_KEY", "")
        chat = get_session_env("HERMES_SESSION_CHAT_ID", "")
        authority = active_authority(runner)
        live = getattr(authority, "sessions", {}).get(sid)
        if not sid or not key or not chat or live is None or live.route != key:
            return None
        source = live.source
        if getattr(source.platform, "value", source.platform) != "local" or source.chat_id != chat:
            return None
        senders = []
        for worker, holder in list(getattr(authority, "_turn_workers", {}).values()):
            agent = holder[0] if holder else None
            if worker.worker_done.is_set() or getattr(agent, "session_id", None) != sid:
                continue
            callback = getattr(agent, "background_review_callback", None)
            if callable(callback):
                senders.append(callback)
        return senders[0] if len(senders) == 1 else None
    except Exception:
        return None
