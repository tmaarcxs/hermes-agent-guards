
"""Block recurring agent mistakes before a tool runs, normalize known-bad arguments and keep work moving.

Blocks (terminal, foreground or background as noted):
- foreground ``sleep`` of 30 s or more, or a ``for``/``while``/``until`` polling
  loop holding a ``sleep`` of 15 s or more: run the wait with ``background=true``
  and a notify channel, or poll once and re-issue without sleeping; heredoc bodies
  and quoted strings are exempt (a script file's own sleeps are not the agent blocking);
- ``gh pr merge`` without ``--match-head-commit``: merges must pin the verified head;
- ``osascript``: desktop automation goes through ego-browser;
- ``git push --force``/``-f`` without ``--force-with-lease``;
- recursive deletion of a live Hermes home (``hermes_home_gate``, a submodule chained
  into ``terminal_violations``): ``rm -r``, ``find … -delete``, a ``--delete`` sync or a
  python delete call aimed at ``$HERMES_HOME``, ``~/.hermes`` or its ``profiles`` subtree
  (any user's literal absolute form too), an ancestor of these, or the same stage through a
  runner chain after a pipe; deeper nests and quoted/heredoc prose stay allowed; the fix:
  create test homes with ``mktemp -d`` inside the scratch workspace and run the command as
  ``env HERMES_HOME="$(mktemp -d "$PWD/.hermes-test-home.XXXXXX")" <test cmd>``,
  then delete the printed directory by its literal path;
- in a worker process (``github_contact``): commands that contact people on GitHub —
  review requests, issue assignees and comments whose body ``@mentions`` a user or
  org/team; for a CODEOWNERS-only gap the message names the setup's documented merge
  command when one exists (``AGENT_GUARDS_MERGE_HINT``).

Blocks (other tools):
- ``kanban_attach`` with more than 256 characters of inline base64: the model transcribes
  those bytes and corrupts them; attach by path instead.

Normalizes (modify directive, never blocks):
- optional Kanban arguments sent as empty strings (``assignee`` in kanban_list,
  ``completion_contract`` in kanban_create) become absent;
- ``skill_view`` names of the form ``x:x`` become ``x``;
- a Kanban ``task_id`` sent as the literal ``$HERMES_KANBAN_TASK`` becomes absent, so
  the tool falls back to the worker's own task from the environment.

Matches commands at command-word position outside quoted strings, so mentions in
strings, grep patterns and python -c bodies pass. It catches slips, not deliberate evasion. Fails open.

Modules:
- ``hermes_home_gate`` recognizes and blocks recursive deletion of a live Hermes home inside
  terminal commands: the ``rm -r``/``find -delete``/``rsync --delete``/python ``rmtree``-family
  surface with ancestors, runner chains and payload quoting; ``__init__`` chains it into
  ``terminal_violations``;
- ``checkpoint_gate`` (same condition, worker process only) forces the budget checkpoint: once a
  dispatcher-owned worker run passes 400 API calls (read from the ``api_request_id`` tail) without a
  successful post-call-1 Kanban comment (or review/complete/block), non-checkpoint tool calls are
  blocked until one is written, and the duty re-arms every 25 calls so a long run keeps
  checkpointing instead of dying at the iteration cap holding everything in hand;
- ``skill_size`` blocks growing a skill markdown file past its size limit, and flags
  after the fact a ``terminal``/``execute_code``/unified-diff ``patch`` call that left one
  over its limit (post-hoc instruction appended to the tool result, no revert);
- ``kanban_policy`` (only with ``kanban.default_assignee`` set) keeps delegated cards on the
  worker profile, out of triage, without execution overrides and with a numbered Acceptance list;
- ``lead_delegation`` (same condition, lead process only) blocks the lead from editing files under
  repository roots and from ``git commit``/``git push``/``gh pr create`` there: delivery goes to workers;
- ``lead_work_nudge`` (same condition, lead process only) appends one nudge to the tool result of the
  call that crosses 5 work calls in a user turn when the lead has not called ``kanban_create`` in that
  turn: split the remaining work across as many parallel cards as possible instead of continuing itself;
- ``completion_gate`` (same condition, worker process only) blocks ``kanban_complete`` until
  ``metadata.acceptance`` maps every numbered Acceptance item of the card to its evidence;
- ``github_contact`` (same condition, worker process only) blocks terminal calls that contact
  people on GitHub: review requests, issue assignees and @mention comments; ``@me``/``@copilot``,
  removal flags and, on assignee flags, the authenticated login itself are exempt
  (self-assignment contacts nobody; unresolvable login fails closed); for a CODEOWNERS-only
  gap the message names the setup's documented merge command when one exists
  (``AGENT_GUARDS_MERGE_HINT``), and always offers ``kanban_block`` with ``kind=needs_input``;
- ``review_gate`` (same condition) blocks ``kanban_request_review`` until the eight pre-review
  checks are closed in ``todo_list``, each completed item carrying a receipt's anchor.
- ``handoff_gate`` (same condition) bounces, once per session, a ``kanban_block`` whose reason
  says the worker's part is finished and only lead-owned steps remain; that is a completion;
- ``schedule_gate`` (same condition, worker process only) blocks ``kanban_schedule``: it ends
  the run without a timer, so a self-scheduled card silently stops;
- ``completion_gate`` (same condition, worker process only) keeps the published-PR completion
  duty visible: a ``kanban_show`` of the card with a GitHub ``completion_contract``
  gains the ``metadata.published_pr`` reminder, and a rejected ``kanban_complete`` ("PR acceptance
  missing") gains the exact fix on the rejection text;

- ``wakes`` (needs ``plugins.entries.agent-guards.allow_gateway_injection: true``) runs
  every Kanban wake — stale parks once per parked stretch (plain open-card stale wakes
  are gone; they acted 0/17 in the 14-day census), review-loop thresholds at 3/6/9...
  change requests, the churn corner at 10 change requests, waiting-on-external-gate
  runs, dispatcher stalls once per guard episode and at least 10 minutes apart, and
  unreconciled delivery notices — on one shared chassis over ``board_watch``;
- ``board_watch`` makes the wake sweeps event-driven: each sweeps on the first
  dispatcher tick after wake-relevant board activity (or any successful Kanban
  mutation, via a shared poke file) instead of on fixed ceilings. The stale-park
  wake, whose trigger ripens with time rather than events, also stores the next
  card's 5 min crossing in its cursor and sweeps on the first tick at or past
  it, so a quiet board cannot hold a wake past its age gate. Watermark and dedup
  semantics are unchanged.
- ``workspace_scope_gate`` (lead process with a messaging session origin only) scopes a
  lead ``kanban_list`` result to the cards the calling chat thread is subscribed to
  (``kanban_notify_subs`` platform/chat_id/thread_id): other chats' cards are hidden,
  ``count`` follows and ``hidden_foreign`` reports how many were hidden; worker processes
  and origin-less calls (CLI, cron, TUI) pass through unchanged.
- ``kanban_show_slim_gate`` (lead process only) deterministically slims a lead
  ``kanban_show`` result: ``worker_context`` (a byte-duplicate of the structured fields)
  is removed and fixed text-free event kinds (heartbeat and dispatcher bookkeeping)
  are dropped; ``runs`` and every other text-bearing field stay byte-identical, so
  summaries, review verdicts and errors are preserved; the result adverts
  ``removed_fields``/``dropped_event_rows``; worker processes and non-``kanban_show``
  results pass through unchanged.
- ``wakes`` runs every Kanban wake on one shared chassis (board_watch cursors,
  subscriber lookup, gateway injection): the stale-park wake (a parked card with a
  block_kind record whose last non-ignored event is at least 5 min old, once per
  parked stretch, minus the capability/triage parks whose own park notification
  already reached the chats), the review-loop threshold wake (3, 6, 9... change
  requests, once per threshold, durable-write-before-send), the churn corner
  warning (a non-archived, non-done card with at least 10 change requests, once
  per ungated crossing), the waiting-on-external-gate wake (a running card whose
  heartbeat wait streak is at least 15 min old, once per run), the dispatcher-stall
  wake (last 30 events all active_pr respawn guards on a todo/ready card, once per
  guard episode, at least 10 minutes apart), and the unreconciled-notice delivery
  net (a terminal state event no subscription confirmed delivered). The plain
  open-card stale wake is gone: its 17 wakes in 14 days acted zero times.
"""


import re
import shlex
from typing import Any, Optional

try:
    from . import background_notify_gate, backup_copy_gate, checkpoint_gate, completion_gate, github_contact, handoff_gate, hermes_home_gate, kanban_policy, kanban_show_slim_gate, lead_delegation, lead_work_nudge, paas_promote_gate, review_gate, schedule_gate, skill_size, stop_gate, workspace_convention, workspace_scope_gate, workspace_write_gate, wakes
except ImportError:
    if __name__ != "__init__":
        raise
    """Bare ``pytest`` (no test path, at the repo root) forms Package nodes from
    ``__init__.py`` files and imports this file as the top-level module ``__init__``,
    where the relative import above has no parent package. In that synthetic shape the
    package body is dead weight — tests always go through the conftest-loaded real
    package — so the ImportError is swallowed only there and re-raised everywhere else,
    keeping genuine load failures noisy in the host."""


_START = r"(?:^|[;&|\n(`{]|\$\(|\bthen\b|\bdo\b|\belse\b)\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*(?:sudo\s+|command\s+)?"
_SEGMENT_END = re.compile(r"[;\n]|&&|\|\|")
_QUOTED = re.compile(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"")
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w[\w.]*)\1")
_LOOP = re.compile(r"\b(?:for|while|until)\b")
_SLEEP = re.compile(_START + r"sleep\s+(\d+(?:\.\d+)?)([smh]?)\b")
_MERGE = re.compile(_START + r"gh\s+pr\s+merge\b")
_OSASCRIPT = re.compile(_START + r"osascript\b")
_PUSH = re.compile(_START + r"git\s+push\b")
_BARE_FORCE = re.compile(r"\s(?:--force|-f)(?=\s|$)")
_RUNNER_RM = re.compile(_START + r"(?:(?:sudo|command|env)\s+)*xargs\b(?:\s+-\S+)*\s+(?:(?:sudo|command)\s+)?rm\b(?=[^;&|\n]*(?:-[a-zA-Z]*[rR][a-zA-Z]*|--recursive)\b)")

_UNIT_SECONDS = {"": 1, "s": 1, "m": 60, "h": 3600}
_MAX_FOREGROUND_SLEEP = 30
_MAX_LOOP_SLEEP = 15
_EMPTY_TO_ABSENT = {"kanban_list": ("assignee", "status", "tenant"), "kanban_create": ("completion_contract",)}
_ENV_TASK_LITERAL = re.compile(r"^\s*\$?\{?HERMES_KANBAN_TASK\}?\s*$")
_MAX_INLINE_ATTACH = 256
_INLINE_ATTACH_MESSAGE = (
    "agent-guards blocked kanban_attach: inline base64 over 256 characters arrives "
    "corrupted; attach by path: artifacts=[absolute path], then sha256-compare the stored file."
)


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", _pre_tool_call)
    ctx.register_hook("post_tool_call", _post_tool_call)
    ctx.register_hook("pre_verify", stop_gate.check)
    ctx.register_hook("transform_tool_result", _transform_tool_result)
    wakes.register(ctx)


def _segments(command: str, pattern: re.Pattern) -> list[str]:
    found = []
    for match in pattern.finditer(command):
        end = _SEGMENT_END.search(command, match.end())
        found.append(command[match.start() : end.start() if end else len(command)])
    return found


def terminal_violations(command: str, background: bool) -> list[str]:
    found = []
    home = hermes_home_gate.hermes_home_violations(command)
    if home:
        found.extend(home)
    command = hermes_home_gate._strip_heredocs(command)
    command = _QUOTED.sub("''", command)
    if not background:
        for match in _SLEEP.finditer(command):
            seconds = float(match.group(1)) * _UNIT_SECONDS[match.group(2)]
            if seconds >= _MAX_FOREGROUND_SLEEP:
                found.append(
                    "foreground sleep >= 30s: run the wait with background=true and notify=true, "
                    "or poll once now and re-issue without sleeping"
                )
                break
        if not found:
            loop_starts = [m.start() for m in _LOOP.finditer(command)]
            if loop_starts:
                for match in _SLEEP.finditer(command):
                    seconds = float(match.group(1)) * _UNIT_SECONDS[match.group(2)]
                    if seconds >= _MAX_LOOP_SLEEP and any(start < match.start() for start in loop_starts):
                        found.append(
                            "polling loop with sleep >= 15s: run the whole wait with background=true "
                            "and notify=[<ready pattern>], or poll once without sleeping"
                        )
                        break
    if any("--match-head-commit" not in segment for segment in _segments(command, _MERGE)):
        found.append("gh pr merge needs --match-head-commit <verified head sha>")
    if _OSASCRIPT.search(command):
        found.append("osascript is not allowed: use ego-browser for browser work")
    if any(_BARE_FORCE.search(segment) for segment in _segments(command, _PUSH)):
        found.append("git push --force/-f is not allowed: use --force-with-lease")
    return found


def normalized_args(tool_name: str, args: dict[str, Any]) -> Optional[dict[str, Any]]:
    changes: dict[str, Any] = {key: None for key in _EMPTY_TO_ABSENT.get(tool_name, ()) if args.get(key) == ""}
    if tool_name.startswith("kanban_") and _ENV_TASK_LITERAL.match(str(args.get("task_id") or "")):
        changes["task_id"] = None
    if tool_name == "skill_view":
        name = args.get("name")
        if isinstance(name, str) and ":" in name:
            prefix, _, rest = name.partition(":")
            if prefix == rest:
                changes["name"] = rest
    return changes or None


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
    if transformed is None:
        try:
            transformed = skill_size.transform(
                kwargs.get("tool_name") or "", kwargs.get("args"), kwargs.get("result"),
                tool_call_id=kwargs.get("tool_call_id") or "")
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


def _pre_tool_call(tool_name: str = "", args: Optional[dict] = None, **kwargs) -> Optional[dict]:
    def stamp(message: str, name: str) -> str:
        marker = message.find(": ")
        if not message.startswith("agent-guards blocked") or marker < 0:
            return message
        return message[: marker] + " [" + name + "]" + message[marker:]
    for check, name in (
        (lambda: checkpoint_gate.check(tool_name, args, **kwargs), "checkpoint_gate"),
        (lambda: workspace_write_gate.check(tool_name, args), "workspace_write_gate"),
        (lambda: kanban_policy.check(tool_name, args), "kanban_policy"),
        (lambda: workspace_convention.check(tool_name, args), "workspace_convention"),
        (lambda: lead_delegation.check(tool_name, args), "lead_delegation"),
        (lambda: github_contact.check(tool_name, args), "github_contact"),
        (lambda: paas_promote_gate.check(tool_name, args), "paas_promote_gate"),
        (lambda: review_gate.check(tool_name, **kwargs), "review_gate"),
        (lambda: handoff_gate.check(tool_name, args, **kwargs), "handoff_gate"),
        (lambda: completion_gate.check(tool_name, args), "completion_gate"),
        (lambda: backup_copy_gate.check(tool_name, args), "backup_copy_gate"),
        (lambda: schedule_gate.check(tool_name, args), "schedule_gate"),
        (lambda: skill_size.check(tool_name, args), "skill_size"),
        (lambda: skill_size.record(tool_name, args, **kwargs), "skill_size"),
        (lambda: background_notify_gate.check(tool_name, args), "background_notify_gate"),
    ):
        try:
            blocked = check()
        except Exception:
            blocked = None
        if blocked:
            return {"action": "block", "message": stamp(blocked, name)}
    try:
        args = args if isinstance(args, dict) else {}
        if tool_name == "terminal":
            found = terminal_violations(str(args.get("command") or ""), bool(args.get("background")))
            if found:
                return {"action": "block", "message": "agent-guards blocked terminal [__init__]: " + "; ".join(found)}
            return None
        if tool_name == "kanban_attach" and len(str(args.get("content_base64") or "")) > _MAX_INLINE_ATTACH:
            return {"action": "block", "message": stamp(_INLINE_ATTACH_MESSAGE, "__init__")}
        changes = normalized_args(tool_name, args)
        return {"action": "modify", "args": changes} if changes else None
    except Exception:
        return None