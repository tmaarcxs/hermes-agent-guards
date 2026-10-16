"""One rule: a lead implementation card must declare its swarm lane.

A ``kanban_create`` issued outside a worker context (``HERMES_KANBAN_TASK`` unset)
must carry a body line ``Lane: <k> of <N>`` with integers ``1 <= k <= N`` and
``N >= 10``. Two exemptions, and only these: a worker-context call (the card is
a worker's own card) and a card created with a non-empty ``parents`` list
(reworks, reviews and follow-ups are child cards, not lane heads). Everything
else blocks with a single-line message naming the exact line to add.
Non-``kanban_create`` tools pass through untouched.
"""

import os
import re
from typing import Any, Optional

LANE_LINE = re.compile(r"(?m)^Lane:\s*(\d+)\s+of\s+(\d+)\b")
SWARM_MIN_LANES = 10

BLOCK_MESSAGE = (
    "agent-guards blocked kanban_create: implementation work must be split into at least "
    f"{SWARM_MIN_LANES} parallel cards, each created with its own kanban_create and the exact "
    "body line `Lane: k of N` (1 <= k <= N, N >= 10)."
)


def check(tool_name: str, args: Any) -> Optional[str]:
    """Return the one-line block message for an undeclared lead implementation card, else None."""
    if tool_name != "kanban_create" or os.environ.get("HERMES_KANBAN_TASK"):
        return None
    if isinstance(args, dict) and args.get("parents"):
        return None
    match = LANE_LINE.search(str(args.get("body") or "")) if isinstance(args, dict) else None
    if match is None:
        return BLOCK_MESSAGE
    lane, lanes = int(match.group(1)), int(match.group(2))
    if lanes < SWARM_MIN_LANES or not 1 <= lane <= lanes:
        return BLOCK_MESSAGE
    return None
