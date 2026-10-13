"""Turn a worker's "finished, the rest is the lead's" block into a completion.

Active only in worker setups (``kanban.default_assignee`` set; see ``kanban_policy``).

A worker that has finished its own part and is waiting only on lead-owned steps (merge,
deployment, DES acceptance, publication, approval) should hand off with ``kanban_complete``
naming the next owner, not park the card in blocked. The first such ``kanban_block`` in a
session is bounced with that advice; repeating the call lets it through, so a genuine
decision block costs one extra call.
"""

import re
from collections import OrderedDict
from typing import Any, Optional

from . import kanban_policy

_FINISHED = re.compile(
    r"(?i)\b(?:implementation|implemented|tests?|evidence|review|verification|phase|work|deliverable|candidate|"
    r"corrections?|fix(?:es)?|publication|acceptance|rework|delivery[- ]prep|gates?)\b[^.;]{0,60}?"
    r"\b(?:is |are |was |were |now )?(?:complete|completed|done|finished|green|verified|frozen|recorded)\b"
)
_LEAD_OWNED = re.compile(
    r"(?i)\b(?:merge|merging|deploy(?:ment)?|DES (?:acceptance|deploy|admission|rollout)|publication|publish|approval)\b"
    r"[^.;]{0,80}\b(?:lead|operator|the owner|human)\b"
    r"|\b(?:lead|operator|the owner)\b[^.;]{0,60}\b(?:merge|deploy|DES|publish|approv)"
)
_MAX_SESSIONS = 200
MESSAGE = (
    "agent-guards bounced kanban_block: the reason says your part is finished and what remains is lead-owned "
    "(merge, deployment, DES, publication or approval). That is a handoff, not a blocker: call kanban_complete "
    "(or kanban_request_review if a code change awaits independent review) with the evidence and the exact next "
    "owner and action in the summary. If a decision you cannot make really prevents your own remaining work, "
    "repeat the same kanban_block call and it will go through."
)

_bounced: "OrderedDict[str, bool]" = OrderedDict()


def finished_with_lead_remainder(reason: str) -> bool:
    return bool(_FINISHED.search(reason) and _LEAD_OWNED.search(reason))


def reset() -> None:
    _bounced.clear()


def check(tool_name: str, args: Any, **kwargs) -> Optional[str]:
    if tool_name != "kanban_block" or not isinstance(args, dict) or not kanban_policy.configured_worker():
        return None
    if not finished_with_lead_remainder(str(args.get("reason") or "")):
        return None
    session = str(kwargs.get("session_id") or kwargs.get("task_id") or "")
    if session in _bounced:
        return None
    _bounced[session] = True
    while len(_bounced) > _MAX_SESSIONS:
        _bounced.popitem(last=False)
    return MESSAGE
