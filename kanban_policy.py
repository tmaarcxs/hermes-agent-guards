"""Kanban delegation policy for setups that route delegated cards to one worker profile.

Active only when ``kanban.default_assignee`` is set in the profile config; that value is
the worker profile every delegated card must use. Then:
- ``kanban_create`` must use that assignee, must not land in triage (reserved for
  automatic escalation), must not override execution defaults and must carry an
  ``Acceptance`` heading followed by a numbered list of checkable criteria;
- ``kanban_request_review`` may only name that worker as reviewer.
With no default assignee configured, nothing is enforced.
"""

import re
from typing import Any, Optional

EXECUTION_OVERRIDES = ("model", "provider", "goal_mode", "goal_max_turns", "max_runtime_seconds")
ACCEPTANCE_LIST = re.compile(r"(?im)^[#*\s]*acceptance\b[^\n]*\n(?:[ \t]*\n)*[ \t]*1[.)][ \t]+\S")
DES_MEASUREMENT = re.compile(
    r"(?i)\b(?:fresh|new)\b[^.\n]{0,40}\bexecutions?\b[^.\n]{0,60}\bat least\s+\d+\s+audits?"
    r"|\bin DES\b[^.\n]{0,60}\bat least\s+\d+\s+audits?"
    r"|\bat least\s+\d+\s+audits?\b[^.\n]{0,60}\bin DES\b"
)
LOCAL_REPLAY = re.compile(r"(?i)\blocal(?:ly)?\b[^.\n]{0,80}\b(?:replay|replicat\w*|snapshots?)\b")

# Appended to every guard block of a Kanban lifecycle call. A blocked call does not satisfy
# the terminal-call protocol: Hermes' core stop-guard (`agent/kanban_stop.py`
# `session_called_kanban_terminal`) scans transcript TOOL NAMES, not outcomes, so a reply
# that ends on text after the block exits rc=0 as a protocol violation with no nudge. The
# only reliable lever a plugin block message has is the message itself: demand the fixed
# retry in the SAME response, because a text-only reply is a run-ending violation.
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


def violations(tool_name: str, args: dict[str, Any], worker: str) -> list[str]:
    if tool_name == "kanban_create":
        found = []
        if args.get("assignee") != worker:
            found.append(f"assignee must be '{worker}', got {args.get('assignee')!r}")
        if args.get("triage"):
            found.append("triage is reserved for automatic escalation; create the card in todo")
        found += [f"remove execution override '{key}'" for key in EXECUTION_OVERRIDES if key in args]
        if not ACCEPTANCE_LIST.search(str(args.get("body") or "")):
            found.append(
                "body needs an 'Acceptance' heading followed by a numbered list (1., 2., ...) with one checkable "
                "criterion per item, ending at the last step the worker can execute"
            )
        body = str(args.get("body") or "")
        if DES_MEASUREMENT.search(body) and not LOCAL_REPLAY.search(body):
            found.append(
                "the body measures analysis behavior with DES executions on several audits; analysis tests run "
                "locally on replicated DES snapshots (real LUMEN, task-owned Postgres), before the PR. Say so in the "
                "body and keep DES to the deployed-identity readback plus at most one rendered execution"
            )
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
    return (
        f"agent-guards blocked {tool_name}: " + "; ".join(found)
        + ". Fix the call and retry — " + RETRY_INSTRUCTION
    )
