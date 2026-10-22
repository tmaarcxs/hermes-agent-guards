"""Keep workers from parking their own card in Scheduled.

Active only in worker setups (``kanban.default_assignee`` set; see ``kanban_policy``) and only
in a worker process (``HERMES_KANBAN_TASK`` set). ``kanban_schedule`` ends the run and creates
no timer: the card stays parked until someone unblocks it, so a worker that schedules itself
"until tomorrow" silently stops the delivery. Workers wait for CI, deploys or executions with
background processes and notifications; a real external dependency is a ``kanban_block``
with its kind and release condition, which reaches the lead.
"""

import os
from typing import Any, Optional

from . import kanban_policy

MESSAGE = (
    "agent-guards blocked kanban_schedule: scheduling ends your run and sets no timer, so the card stops "
    "until someone notices. Keep working: wait for CI, deployments or executions with a background process "
    "and notify=true, and continue with the remaining steps. If something outside your control genuinely "
    "prevents progress, call kanban_block with kind and the exact release condition instead."
)


def check(tool_name: str, args: Any) -> Optional[str]:
    if tool_name != "kanban_schedule" or not os.environ.get("HERMES_KANBAN_TASK"):
        return None
    return (
        MESSAGE + " " + kanban_policy.RETRY_INSTRUCTION
    ) if kanban_policy.configured_worker() else None
