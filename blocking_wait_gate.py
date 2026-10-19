"""Cap a blocking ``process_manage`` wait inside a Kanban worker session.

A Kanban worker is a one-shot ``hermes chat -q`` subprocess (``HERMES_KANBAN_TASK``
set) whose dispatcher observes only the card's own terminal transition. Hermes
therefore disables async completion delivery on exactly these sessions:
``gateway.session_context.async_delivery_supported`` returns False whenever
``HERMES_KANBAN_TASK`` is set, so ``notify_on_complete`` / ``watch_patterns`` are
stripped at spawn (the live worker log records ``async delivery unsupported on this
session; notify_on_complete/watch_patterns disabled``) and no completion event is
ever queued for the worker. No mechanism resumes a Kanban worker when a background
child exits; ``kanban_schedule`` sets no timer either, and ``schedule_gate`` blocks
it. A long ``process_manage`` wait is consequently pure idle time: the worker holds
its own turn for the whole window and learns nothing a non-blocking ``poll`` would
not have told it sooner.

Measured over the 2026-10-19 DeepSeek worker logs, under the reporting card's
00:00-21:00 cutoff (the first DeepSeek session started 14:27, so that cutoff is
exactly its afternoon batch): 126 ``process_manage`` calls totalling 23,022 s, of
which 75 ran 60 s or longer (22,662 s) and 45 ran 300 s or longer — 39 % of that
model's tool time for the day (23,022 of 59,001 s), and the driver of its two
longest runs (24 calls / 6,281 s and 35 calls / 8,042 s of 420 s blocking polls).
Across the whole worker profile, deduplicated by provider ``call_id``: 344 ``wait``
calls, 341 of them with a timeout over the cap.

The cap is one short blocking wait per call. A longer wait — including a bare
``wait``, whose default is the worker-scoped ``TERMINAL_TIMEOUT`` and can reach the
whole run budget — is blocked with the options that actually exist: re-issue under
the cap, finish the remaining work first and check with a non-blocking ``poll``, or
``kanban_block`` the card when the wait is an external dependency only a human can
release.

A ``wait`` that hits the tool's own 420 s ceiling is recorded in the log as
``sequential tool process_manage timed out after 420.0s`` with no
``tool process_manage completed`` line at all (53 such lines in this profile), so the
census above counts only the calls that returned; the real total is higher.

Exempt: any process without ``HERMES_KANBAN_TASK`` (the lead is untouched), setups
with no configured worker, waits at or under the cap, and every action other than
``wait`` (``poll``, ``log``, ``kill``, ``write``, ``submit``, ``close``, ``handoff``,
``list``). Fails open.
"""

import os
from typing import Any, Optional

from . import kanban_policy

MAX_WAIT_SECONDS = 60

MESSAGE = (
    "agent-guards blocked process_manage: a blocking wait over {cap}s in a Kanban worker is idle time "
    "nothing can wake you out of — Hermes disables async completion delivery for these sessions "
    "(HERMES_KANBAN_TASK), so notify_on_complete is stripped at spawn and no completion ever reaches you. "
    "Re-issue with timeout<={cap}; do the remaining work first and check with action='poll' "
    "(non-blocking); or kanban_block the card when this is an external dependency only a human can release. "
    "Fix the call and retry it in this same response."
)


def _over_cap(args: dict[str, Any]) -> bool:
    """True when this wait would block past the cap.

    An absent or non-numeric timeout uses the worker-scoped ``TERMINAL_TIMEOUT``
    default, which is always over the cap; a ``bool`` is not a timeout (``True``
    would otherwise read as 1)."""
    timeout = args.get("timeout")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        return True
    return float(timeout) > MAX_WAIT_SECONDS


def check(tool_name: str, args: Any) -> Optional[str]:
    """``pre_tool_call``: block a worker's over-cap blocking wait; fail open otherwise."""
    if tool_name != "process_manage" or not isinstance(args, dict):
        return None
    if str(args.get("action") or "") != "wait":
        return None
    if not (os.environ.get("HERMES_KANBAN_TASK") or "").strip():
        return None
    if not kanban_policy.configured_worker():
        return None
    if not _over_cap(args):
        return None
    return MESSAGE.format(cap=MAX_WAIT_SECONDS)
