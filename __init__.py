"""Block recurring agent mistakes before a tool runs, normalize known-bad arguments and keep work moving.

Blocks (terminal, foreground or background as noted):
- foreground ``sleep`` of 60 s or more: use background=true with notify instead;
- ``gh pr merge`` without ``--match-head-commit``: merges must pin the verified head;
- ``osascript``: desktop automation goes through ego-browser;
- ``git push --force``/``-f`` without ``--force-with-lease``;
- in a worker process (``github_contact``): commands that contact people on GitHub —
  review requests, issue assignees and comments whose body ``@mentions`` a user or
  org/team; a CODEOWNERS-only gap goes through ``pr_merge_verified`` instead.

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
- ``repeat_guard`` blocks an identical call that already failed twice in the session with
  nothing changed since;
- ``skill_size`` blocks growing a skill markdown file past its size limit;
- ``kanban_policy`` (only with ``kanban.default_assignee`` set) keeps delegated cards on the
  worker profile, out of triage, without execution overrides and with a numbered Acceptance list;
- ``lead_delegation`` (same condition, lead process only) blocks the lead from editing files under
  repository roots and from ``git commit``/``git push``/``gh pr create`` there: delivery goes to workers;
- ``completion_gate`` (same condition, worker process only) blocks ``kanban_complete`` until
  ``metadata.acceptance`` maps every numbered Acceptance item of the card to its evidence;
- ``github_contact`` (same condition, worker process only) blocks terminal calls that contact
  people on GitHub: review requests, issue assignees and @mention comments; ``@me``/``@copilot``,
  removal flags and, on assignee flags, the authenticated login itself are exempt
  (self-assignment contacts nobody; unresolvable login fails closed); the authorized
  alternatives for a CODEOWNERS-only gap are ``github-agent-ops/scripts/pr_merge_verified``
  or ``kanban_block`` with ``kind=needs_input``;
- ``approval_wait_gate`` (same condition, worker process only) blocks the silent 17-hour wait
  on an approval-only pull request: when the exact head of a polled PR is green on every
  automated check and the review approval is the only open gate, readiness polls of it
  (``gh pr checks``, ``gh pr view --json statusCheckRollup|mergeStateStatus|...``,
  ``gh api .../pulls/<n>``, ``pr_status``/``pr_wait_checks``) are blocked with the two
  authorized moves (merge the verified head via ``pr_merge_verified``, or
  ``kanban_block(kind="needs_input")``), and a repeating ``kanban_heartbeat`` note that admits
  the approval-only wait is blocked from the second one on. Pending or failing checks, drafts
  and dirty states are not verdict names, so their polls flow through; verdicts are cached
  for 15 minutes and fail open on errors;
- ``review_gate`` (same condition) blocks ``kanban_request_review`` until the eight pre-review
  checks are closed in ``todo_list``, each completed item carrying a receipt's anchor.
- ``handoff_gate`` (same condition) bounces, once per session, a ``kanban_block`` whose reason
  says the worker's part is finished and only lead-owned steps remain; that is a completion;
- ``schedule_gate`` (same condition, worker process only) blocks ``kanban_schedule``: it ends
  the run without a timer, so a self-scheduled card silently stops;
- ``published_head_gate`` (same condition, worker process only) blocks ``kanban_request_review``
  when the claimed ``metadata.commit``/``head`` is not the live head of the named pull request;
- ``stale_wake`` wakes a card's subscribed chat once when the card has not moved for 1 h
  (needs ``plugins.entries.agent-guards.allow_gateway_injection: true``);
- ``review_loop`` wakes the same chats when an open card reaches 3, 6, 9... review rounds
  with changes requested (same injection setting).
"""

import re
from typing import Any, Optional

from . import approval_wait_gate, backup_copy_gate, completion_gate, github_contact, handoff_gate, kanban_policy, lead_delegation, published_head_gate, repeat_guard, review_gate, review_loop, schedule_gate, skill_size, stale_wake
from . import stop_gate

_START = r"(?:^|[;&|\n(`{]|\$\(|\bthen\b|\bdo\b|\belse\b)\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*(?:sudo\s+|command\s+)?"
_SEGMENT_END = re.compile(r"[;\n]|&&|\|\|")
_QUOTED = re.compile(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"")
_SLEEP = re.compile(_START + r"sleep\s+(\d+(?:\.\d+)?)([smh]?)\b")
_MERGE = re.compile(_START + r"gh\s+pr\s+merge\b")
_OSASCRIPT = re.compile(_START + r"osascript\b")
_PUSH = re.compile(_START + r"git\s+push\b")
_BARE_FORCE = re.compile(r"\s(?:--force|-f)(?=\s|$)")
_UNIT_SECONDS = {"": 1, "s": 1, "m": 60, "h": 3600}
_MAX_FOREGROUND_SLEEP = 60
_EMPTY_TO_ABSENT = {"kanban_list": ("assignee", "status", "tenant"), "kanban_create": ("completion_contract",)}
_ENV_TASK_LITERAL = re.compile(r"^\s*\$?\{?HERMES_KANBAN_TASK\}?\s*$")
_MAX_INLINE_ATTACH = 256
_INLINE_ATTACH_MESSAGE = (
    "agent-guards blocked kanban_attach: inline base64 over 256 characters is transcribed by the model "
    "and arrives corrupted. Attach by path: kanban_complete/kanban_request_review artifacts=[absolute path], "
    "or terminal `hermes kanban attach <task_id> <absolute path>`; then compare the stored file's sha256 with the source."
)


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", _pre_tool_call)
    ctx.register_hook("post_tool_call", _post_tool_call)
    ctx.register_hook("pre_verify", stop_gate.check)
    stale_wake.register(ctx)
    ctx.register_hook("on_kanban_dispatch_tick", review_loop.on_tick)


def _segments(command: str, pattern: re.Pattern) -> list[str]:
    found = []
    for match in pattern.finditer(command):
        end = _SEGMENT_END.search(command, match.end())
        found.append(command[match.start() : end.start() if end else len(command)])
    return found


def terminal_violations(command: str, background: bool) -> list[str]:
    command = _QUOTED.sub("''", command)
    found = []
    if not background:
        for match in _SLEEP.finditer(command):
            if float(match.group(1)) * _UNIT_SECONDS[match.group(2)] >= _MAX_FOREGROUND_SLEEP:
                found.append(
                    "foreground sleep >= 60s: run the wait with background=true and notify=true, "
                    "or wait on the process with process_manage"
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
    for guard in (repeat_guard, review_gate, stop_gate):
        try:
            guard.record(**kwargs)
        except Exception:
            pass


def _pre_tool_call(tool_name: str = "", args: Optional[dict] = None, **kwargs) -> Optional[dict]:
    for check in (
        lambda: repeat_guard.check(tool_name, args, **kwargs),
        lambda: kanban_policy.check(tool_name, args),
        lambda: lead_delegation.check(tool_name, args),
        lambda: github_contact.check(tool_name, args),
        lambda: approval_wait_gate.check(tool_name, args, **kwargs),
        lambda: review_gate.check(tool_name, **kwargs),
        lambda: published_head_gate.check(tool_name, args),
        lambda: handoff_gate.check(tool_name, args, **kwargs),
        lambda: completion_gate.check(tool_name, args),
        lambda: backup_copy_gate.check(tool_name, args),
        lambda: schedule_gate.check(tool_name, args),
        lambda: skill_size.check(tool_name, args),
    ):
        try:
            blocked = check()
        except Exception:
            blocked = None
        if blocked:
            return {"action": "block", "message": blocked}
    try:
        args = args if isinstance(args, dict) else {}
        if tool_name == "terminal":
            found = terminal_violations(str(args.get("command") or ""), bool(args.get("background")))
            if found:
                return {"action": "block", "message": "agent-guards blocked terminal: " + "; ".join(found) + "."}
            return None
        if tool_name == "kanban_attach" and len(str(args.get("content_base64") or "")) > _MAX_INLINE_ATTACH:
            return {"action": "block", "message": _INLINE_ATTACH_MESSAGE}
        changes = normalized_args(tool_name, args)
        return {"action": "modify", "args": changes} if changes else None
    except Exception:
        return None
