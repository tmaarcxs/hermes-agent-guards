"""Kanban guards plugin: worker/lead lifecycle gates, delegation policy and dispatcher wakes.

Worker-process gates:
- ``checkpoint_gate`` forces the budget checkpoint: once a dispatcher-owned worker run
  passes 400 API calls (read from the ``api_request_id`` tail) without a successful
  post-call-1 Kanban comment (or review/complete/block), non-checkpoint tool calls are
  blocked until one is written, and the duty re-arms every 25 calls so a long run keeps
  checkpointing instead of dying at the iteration cap holding everything in hand;
- ``kanban_policy`` (only with ``kanban.default_assignee`` set) keeps delegated cards on
  the worker profile, out of triage, without execution overrides and with a numbered
  Acceptance list;
- ``completion_gate`` blocks ``kanban_complete`` until ``metadata.acceptance`` maps every
  numbered Acceptance item of the card to its evidence, and keeps the published-PR
  completion duty visible (a ``kanban_show`` of a card with a GitHub ``completion_contract``
  gains the ``metadata.published_pr`` reminder, and a rejected ``kanban_complete``
  ("PR acceptance missing") gains the exact fix on the rejection text);
- ``github_contact`` blocks terminal calls that contact people on GitHub: review requests,
  issue assignees and comments whose body ``@mentions`` a user or org/team; ``@me``/
  ``@copilot``, removal flags and, on assignee flags, the authenticated login itself are
  exempt (self-assignment contacts nobody; unresolvable login fails closed); for a
  CODEOWNERS-only gap the message names the setup's documented merge command when one
  exists (``AGENT_GUARDS_MERGE_HINT``), and always offers ``kanban_block`` with
  ``kind=needs_input``;
- ``pr_link_gate`` blocks ``gh pr merge`` (and the merge API) on a PR whose
  ``closingIssuesReferences`` is empty or unreadable, and ``gh pr create|edit`` whose body
  lacks exactly one closing keyword (``Closes #N``);
- ``review_gate`` blocks ``kanban_request_review`` until the eight pre-review checks are
  closed in ``todo_list``, each completed item carrying a receipt's anchor;
- ``handoff_gate`` bounces, once per session, a ``kanban_block`` whose reason says the
  worker's part is finished and only lead-owned steps remain; that is a completion;
- ``schedule_gate`` blocks ``kanban_schedule``: it ends the run without a timer, so a
  self-scheduled card silently stops;
- ``backup_copy_gate`` blocks a ``kanban_complete`` (or block/comment) handoff whose
  summary or metadata keeps or preserves a backup/rollback copy of the delivered work;
- ``active_pr_unblock`` runs on ``on_kanban_dispatch_tick`` and lifts the ``active_pr``
  respawn guard automatically when the newest event is a committed ``unblocked`` strictly
  newer than the newest PR-URL comment: a real profile hop (other existing profile, then
  back) gives core's own guard the handoff event it wants, so the card spawns without a
  human lifting it by hand. One hop per task per 10 min, 3 per hour, persisted in
  ``plugin-data``; at the cap it posts one comment asking a human to decide.

Lead-process gates:
- ``lead_work_nudge`` appends one nudge to the tool result of the call that crosses
  ``lead_work_nudge.WORK_CALL_THRESHOLD`` (8) work calls in a user turn when the lead has not
  called ``kanban_create`` in that turn: split
  the remaining work across as many parallel cards as possible instead of continuing itself;
- ``workspace_scope_gate`` scopes a lead ``kanban_list`` result to the cards the calling
  chat thread is subscribed to (``kanban_notify_subs`` platform/chat_id/thread_id): other
  chats' cards are hidden, ``count`` follows and ``hidden_foreign`` reports how many were
  hidden; worker processes and origin-less calls (CLI, cron, TUI) pass through unchanged;
- ``kanban_show_slim_gate`` deterministically slims a lead ``kanban_show`` result:
  ``worker_context`` (a byte-duplicate of the structured fields) is removed and fixed
  text-free event kinds (heartbeat and dispatcher bookkeeping) are dropped; ``runs`` and
  every other text-bearing field stay byte-identical, so summaries, review verdicts and
  errors are preserved; the result adverts ``removed_fields``/``dropped_event_rows``;
  worker processes and non-``kanban_show`` results pass through unchanged.

Wakes (needs ``plugins.entries.kanban-guards.allow_gateway_injection: true`` after the
split cutover):
- ``wakes`` runs every Kanban wake on one shared chassis (board_watch cursors, subscriber
  lookup, gateway injection): the stale-park wake (a parked card with a block_kind record
  whose last non-ignored event is at least 5 min old, once per parked stretch, minus the
  capability/triage parks whose own park notification already reached the chats), the
  review-loop threshold wake (3, 6, 9... change requests, once per threshold,
  durable-write-before-send), the churn corner warning (a non-archived, non-done card with
  at least 10 change requests, once per ungated crossing), the waiting-on-external-gate
  wake (a running card whose heartbeat wait streak is at least 15 min old, once per run),
  the dispatcher-stall wake (last 30 events all active_pr respawn guards on a todo/ready
  card, once per guard episode, at least 10 minutes apart), and the unreconciled-notice
  delivery net (a terminal state event no subscription confirmed delivered). The plain
  open-card stale wake is gone: its 17 wakes in 14 days acted zero times.
- ``board_watch`` makes the wake sweeps event-driven: each sweeps on the first dispatcher
  tick after wake-relevant board activity (or any successful Kanban mutation, via a shared
  poke file) instead of on fixed ceilings. The stale-park wake, whose trigger ripens with
  time rather than events, also stores the next card's 5 min crossing in its cursor and
  sweeps on the first tick at or past it, so a quiet board cannot hold a wake past its age
  gate. Watermark and dedup semantics are unchanged.

Registration order (``register``) reproduces the Kanban part of the pre-split root
``agent-guards`` wiring so intra-plugin hook precedence is unchanged: ``checkpoint_gate``
stays first in the ``pre_tool_call`` check tuple, the ``transform_tool_result`` chain keeps
the completion/scope/show-slim/nudge order, and ``stop_gate.check`` stays the ``pre_verify``
hook. Wakes register the shared ``post_tool_call`` poke and the six dispatcher tick sweeps.

Split seam (for the integration pass): the root ``agent-guards`` ``_pre_tool_call`` mixed
this plugin's checks with shell-side logic. The terminal-policy block (sleep/polling,
PR-merge pin, osascript, force push, home deletion), the inline-attach cap and
``normalized_args`` (empty Kanban arguments, ``skill_view`` name, the
``$HERMES_KANBAN_TASK`` task-id literal) belong to ``shell-guards`` and are NOT
re-declared here; ``skill_size.transform`` in the root transform chain belongs to
``shell-guards`` too. This plugin runs only its own nine ``pre_tool_call`` checks and its
own four transforms, in the same relative order.
"""

try:
    from typing import Optional


    from . import active_pr_unblock, backup_copy_gate, board_watch, checkpoint_gate, completion_gate, github_contact, handoff_gate, kanban_policy, kanban_show_slim_gate, lead_work_nudge, pr_link_gate, review_gate, schedule_gate, stop_gate, wakes, workspace_scope_gate
except ImportError:
    if __name__ != "__init__":
        raise
    """Bare ``pytest`` (no test path, at the repo root) forms Package nodes from
    ``__init__.py`` files and imports this file as the top-level module ``__init__``, where
    the relative import above has no parent package. In that synthetic shape the package
    body is dead weight — tests always go through the conftest-loaded real package — so the
    ImportError is swallowed only there and re-raised everywhere else, keeping genuine load
    failures noisy in the host."""


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", _pre_tool_call)
    ctx.register_hook("post_tool_call", _post_tool_call)
    ctx.register_hook("pre_verify", stop_gate.check)
    ctx.register_hook("transform_tool_result", _transform_tool_result)
    wakes.register(ctx)
    active_pr_unblock.register(ctx)


_PRE_TOOL_CALL_CHECKS = (
    ("checkpoint_gate", lambda tool_name, args, kwargs: checkpoint_gate.check(tool_name, args, **kwargs)),
    ("kanban_policy", lambda tool_name, args, kwargs: kanban_policy.check(tool_name, args)),
    ("github_contact", lambda tool_name, args, kwargs: github_contact.check(tool_name, args)),
    ("pr_link_gate", lambda tool_name, args, kwargs: pr_link_gate.check(tool_name, args)),
    ("review_gate", lambda tool_name, args, kwargs: review_gate.check(tool_name, **kwargs)),
    ("handoff_gate", lambda tool_name, args, kwargs: handoff_gate.check(tool_name, args, **kwargs)),
    ("completion_gate", lambda tool_name, args, kwargs: completion_gate.check(tool_name, args)),
    ("backup_copy_gate", lambda tool_name, args, kwargs: backup_copy_gate.check(tool_name, args)),
    ("schedule_gate", lambda tool_name, args, kwargs: schedule_gate.check(tool_name, args)),
)


_PRE_TOOL_CALL_CHECK_ORDER = tuple(name for name, _ in _PRE_TOOL_CALL_CHECKS)


def _pre_tool_call_checks(tool_name, args, kwargs):
    """The live call's (blocked-message, name) pairs, in the pinned order.

    Each check is isolated: a guard that raises is treated as not blocking, so a
    single broken guard can never take the whole chain down (the pre-split root
    ``_pre_tool_call`` wrapped every ``check()`` call the same way).
    """
    pairs = []
    for name, check in _PRE_TOOL_CALL_CHECKS:
        try:
            blocked = check(tool_name, args, kwargs)
        except Exception:
            blocked = None
        pairs.append((blocked, name))
    return tuple(pairs)


def _pre_tool_call(tool_name: str = "", args: Optional[dict] = None, **kwargs) -> Optional[dict]:
    def stamp(message: str, name: str) -> str:
        marker = message.find(": ")
        if not message.startswith("agent-guards blocked") or marker < 0:
            return message
        return message[: marker] + " [" + name + "]" + message[marker:]
    for blocked, name in _pre_tool_call_checks(tool_name, args, kwargs):
        if blocked:
            return {"action": "block", "message": stamp(blocked, name)}
    return None


def _post_tool_call(**kwargs) -> None:
    for guard in (review_gate, stop_gate, checkpoint_gate):
        try:
            guard.record(**kwargs)
        except Exception:
            pass


def _transform_tool_result(**kwargs) -> Optional[str]:
    transformed = None
    try:
        transformed = completion_gate.transform(**kwargs)
    except Exception:
        transformed = None
    if transformed is None:
        try:
            transformed = workspace_scope_gate.transform(
                kwargs.get("tool_name") or "", kwargs.get("args"), kwargs.get("result"))
        except Exception:
            transformed = None
    if transformed is None:
        try:
            transformed = kanban_show_slim_gate.transform(
                kwargs.get("tool_name") or "", kwargs.get("args"), kwargs.get("result"))
        except Exception:
            transformed = None
    result = transformed if transformed is not None else kwargs.get("result")
    try:
        nudged = lead_work_nudge.hook(
            tool_name=kwargs.get("tool_name") or "", args=kwargs.get("args"), result=result,
            session_id=kwargs.get("session_id") or "", turn_id=kwargs.get("turn_id") or "")
    except Exception:
        return transformed
    return nudged if nudged is not None else transformed
