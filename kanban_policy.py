"""Kanban delegation policy for setups that route delegated cards to one worker profile.

Active only when ``kanban.default_assignee`` is set in the profile config; that value is
the worker profile every delegated card must use. Then:
- ``kanban_create`` must use that assignee, must not land in triage (reserved for
  automatic escalation) and must not override execution defaults;
- ``kanban_request_review`` may only name that worker as reviewer.
With no default assignee configured, nothing is enforced.
"""

from typing import Any, Optional

EXECUTION_OVERRIDES = ("model", "provider", "goal_mode", "goal_max_turns", "max_runtime_seconds")


def configured_worker() -> str:
    try:
        from hermes_cli.config import load_config_readonly

        kanban = (load_config_readonly() or {}).get("kanban") or {}
        return str(kanban.get("default_assignee") or "").strip()
    except Exception:
        return ""


def violations(tool_name: str, args: dict[str, Any], worker: str) -> list[str]:
    if tool_name == "kanban_create":
        found = []
        if args.get("assignee") != worker:
            found.append(f"assignee must be '{worker}', got {args.get('assignee')!r}")
        if args.get("triage"):
            found.append("triage is reserved for automatic escalation; create the card in todo")
        found += [f"remove execution override '{key}'" for key in EXECUTION_OVERRIDES if key in args]
        return found
    if tool_name == "kanban_request_review" and args.get("reviewer") not in (None, worker):
        return [f"reviewer must be '{worker}' or omitted, got {args['reviewer']!r}"]
    return []


def check(tool_name: str, args: Any) -> Optional[str]:
    if tool_name not in ("kanban_create", "kanban_request_review") or not isinstance(args, dict):
        return None
    worker = configured_worker()
    if not worker:
        return None
    found = violations(tool_name, args, worker)
    if not found:
        return None
    return f"agent-guards blocked {tool_name}: " + "; ".join(found) + ". Fix the call and retry."
