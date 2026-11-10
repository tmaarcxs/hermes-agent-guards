"""Only Marcos's own chat turns may write the weekly Jot board.

Workers, cron jobs, one-shot runs and delegated children saw the board skill's standing
procedure and wrote rows themselves, racing the reviewer and adding rows for tasks Marcos
never asked about. The reviewer is the one writer; Marcos's attended turns keep direct access.
"""

import os
import re

JOT_WRITES = frozenset({"jot_update", "jot_create", "jot_set_task", "jot_delete"})
BOARD_SCRIPT = re.compile(r"(?:append-row|remove-row|set-rich-row|set_row_state)\b")
_TRUE = {"1", "true", "yes", "on"}
_UNATTENDED_SOURCES = frozenset({"tool", "cron", "oneshot", "kanban", "webhook", "msgraph_webhook", "api_server", "bot_room", "subagent"})

MESSAGE = (
    "agent-guards blocked {tool}: this run is not one of Marcos's own chat turns, so it must not "
    "write the weekly Jot board. A background reviewer updates the board from his conversations; "
    "put the outcome in your final reply or on the card instead and carry on without a Jot write."
)


def _env(name):
    try:
        from gateway.session_context import get_session_env
        return str(get_session_env(name, "") or os.environ.get(name, "") or "").strip()
    except Exception:
        return str(os.environ.get(name, "") or "").strip()


def unattended():
    """True for cron, Kanban, delegated-child, one-shot and tool-source runs; false in a chat, CLI or desktop turn."""
    if any(_env(name).lower() in _TRUE for name in ("HERMES_CRON_SESSION", "HERMES_DELEGATED_CHILD_CONTEXT")):
        return True
    if _env("HERMES_KANBAN_TASK") or _env("HERMES_SINGLE_QUERY_SESSION").lower() in _TRUE:
        return True
    return any(_env(name).lower() in _UNATTENDED_SOURCES for name in ("HERMES_SESSION_PLATFORM", "HERMES_SESSION_SOURCE"))


def writes_board(tool_name, args):
    """The Jot tool or board script this call would write with, or None."""
    if tool_name in JOT_WRITES:
        return tool_name
    command = (args or {}).get("command") if tool_name == "terminal" and isinstance(args, dict) else None
    if isinstance(command, str) and "jot-weekly-board" in command and BOARD_SCRIPT.search(command):
        return "the weekly-board script"
    return None


def check(tool_name, args=None, **_):
    """Block message for an unattended board write, else None."""
    tool = writes_board(tool_name, args)
    return MESSAGE.format(tool=tool) if tool and unattended() else None
