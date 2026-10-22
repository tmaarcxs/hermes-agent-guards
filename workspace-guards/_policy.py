"""Local copy of the two ``kanban_policy`` helpers this plugin needs.

The monorepo split made each plugin folder self-contained (Marcos, 2026-10-22):
no cross-plugin imports, so the small shared-library bits that
``workspace_write_gate`` and ``workspace_convention`` use travel with the plugin
instead of importing ``kanban-guards``. ``tests/test_helper_copies_agree.py`` at
the monorepo root fails when these copies drift from the original.
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


def _unused_note() -> None:
    """The rest of ``kanban_policy`` (``violations``, ``check``, ``merge_hint``,
    ``EXECUTION_OVERRIDES``) is delegation/lifecycle policy owned by kanban-guards
    and is not used by this plugin's modules."""
    return None
