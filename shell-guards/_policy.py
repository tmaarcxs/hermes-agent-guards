"""Local copy of the small Kanban-delegation helpers this plugin needs.

``shell-guards`` is a self-contained plugin: it imports nothing from sibling
plugins, so the two ``kanban_policy`` helpers its gates call — the worker probe
behind ``background_notify_gate`` and the retry instruction the block message
quotes — are carried here verbatim. Behaviour-identical copy of the
monolithic plugin's module; the full delegation policy lives with the
kanban-guard plugins.
"""

RETRY_INSTRUCTION = (
    "Then retry the lifecycle call in this same response — do not end on a text-only reply: "
    "a reply without the lifecycle call ends the run as a protocol violation."
)


def configured_worker() -> str:
    try:
        from hermes_cli.config import load_config_readonly

        kanban = (load_config_readonly() or {}).get("kanban") or {}
        return str(kanban.get("default_assignee") or "").strip()
    except Exception:
        return ""
