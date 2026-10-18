
"""Block recurring agent mistakes before a tool runs, normalize known-bad arguments and keep work moving.

Blocks (terminal, foreground or background as noted):
- foreground ``sleep`` of 30 s or more, or a ``for``/``while``/``until`` polling
  loop holding a ``sleep`` of 15 s or more: run the wait with ``background=true``
  and a notify channel, or poll once and re-issue without sleeping; heredoc bodies
  and quoted strings are exempt (a script file's own sleeps are not the agent blocking);
- ``gh pr merge`` without ``--match-head-commit``: merges must pin the verified head;
- ``osascript``: desktop automation goes through ego-browser;
- ``git push --force``/``-f`` without ``--force-with-lease``;
- recursive deletion of a live Hermes home (``hermes_home_guard``): ``rm -r``,
  ``find … -delete`` or a ``--delete`` sync whose target is ``$HERMES_HOME`` /
  ``~/.hermes`` / ``$HOME/.hermes`` (trailing slash optional), ``~/.hermes/profiles``
  or any ``~/.hermes/profiles/<name>``, or the same paths literally absolute; targets
  nested deeper (``~/.hermes/cache/scratch/x``) stay allowed, quoted mention in a
  grep pass and heredoc prose passes too; the fix: create test homes with
  ``mktemp -d`` inside the scratch workspace and pass them via
  ``env HERMES_HOME=<path> <cmd>`` only, delete them by literal path;
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
- ``repeat_guard`` blocks an identical call that already failed twice in the session with
  nothing changed since;
- ``checkpoint_gate`` (same condition, worker process only) forces the budget checkpoint: once a
  dispatcher-owned worker run passes 400 API calls (read from the ``api_request_id`` tail) without a
  successful post-call-1 Kanban comment (or review/complete/block), non-checkpoint tool calls are
  blocked until one is written, and the duty re-arms every 25 calls so a long run keeps
  checkpointing instead of dying at the iteration cap holding everything in hand;
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
  (self-assignment contacts nobody; unresolvable login fails closed); for a CODEOWNERS-only
  gap the message names the setup's documented merge command when one exists
  (``AGENT_GUARDS_MERGE_HINT``), and always offers ``kanban_block`` with ``kind=needs_input``;
- ``approval_wait_gate`` (same condition, worker process only) blocks the silent hours-long wait
  on an approval-only pull request: when the exact head of a polled PR is green on every
  automated check and the review approval is the only open gate, readiness polls of it
  (``gh pr checks``, ``gh pr view --json statusCheckRollup|mergeStateStatus|...``,
  ``gh api .../pulls/<n>``, poller helper scripts) are blocked with the
  authorized moves (merge the verified head via the documented merge command when the
  setup names one (``AGENT_GUARDS_MERGE_HINT``), or ``kanban_block(kind="needs_input")``), and a repeating ``kanban_heartbeat`` note that admits
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
- ``pr_acceptance_gate`` (same condition, worker process only) keeps the published-PR completion
  duty visible and capped: a ``kanban_show`` of the card with a GitHub ``completion_contract``
  gains the ``metadata.published_pr`` reminder, a rejected ``kanban_complete`` ("PR acceptance
  missing") gains the exact fix, and after two counted rejections a retry still lacking a
  matching ``published_pr`` is blocked with the contract restated and the ``kanban_block`` escape;
- ``stale_wake`` wakes a card's subscribed chat once when the card has not moved for 5 min
  (needs ``plugins.entries.agent-guards.allow_gateway_injection: true``);
- ``waiting_wake`` wakes the same chats when a ``running`` card's heartbeat notes admit
  waiting on an external gate (a merge bot, an approval, a deploy or rollout, a CI run)
  and that first admission is 15 min old — once per run, quoting the wait — so the owner
  decides: intervene, take the card over, or have the worker block it (same setting);
- ``review_loop`` wakes the same chats when an open card reaches 3, 6, 9... review rounds
  with changes requested, with a four-item decision menu: narrow the contract (the
  integrity-only convergence contract rides verbatim), continue over the real defects,
  split the card, or take it over (same injection setting);
- ``churn_wake`` warns the same chats when a non-archived, non-done card reaches 10/20/40
  worker runs or 5/10/20 change-requests — warning only, nothing is blocked — so the lead
  splits, narrows or confirms the card (same injection setting);
- ``pr_comment_gate`` (same condition, lead process only) blocks a lead ``kanban_comment`` that
  carries a full GitHub PR URL onto a todo/ready/blocked card: the dispatcher respawn guard reads
  any full PR URL from the last 24 h as "a worker already opened a PR" and holds the card out of
  the ready queue every tick (``respawn_guarded reason=active_pr``), so the comment is rejected
  with an ``owner/repo#N`` citation to use instead; worker comments are untouched.
- ``board_watch`` makes the three wakes event-driven: each sweeps on the first
  dispatcher tick after wake-relevant board activity (or any successful Kanban
  mutation, via a shared poke file) instead of on fixed hourly/5-min ceilings.
  The stale wake, whose trigger ripens with time rather than events, also
  stores the next card's 5 min crossing in its cursor and sweeps on the first
  tick at or past it, so a quiet board can no longer hold a wake past its age
  gate. Watermark and dedup semantics are unchanged.
- ``inventory_rule`` (lead process only): one ``pre_llm_call`` hook that
  injects the swarm-split instruction once per user turn at the lead's turn
  prologue: before delegating anything, write the inventory of the request's
  independent atomic units, then create one Kanban card per unit in the same
  turn, exactly one unit per card, as many as the work genuinely allows; child
  cards (non-empty ``parents``) are exempt. It never blocks — a worker-context
  call (``HERMES_KANBAN_TASK`` set) returns ``None`` and the only return shape
  is ``{"context": ...}``, which the host joins into the current user message.
- ``workspace_scope_gate`` (lead process with a messaging session origin only) scopes a
  lead ``kanban_list`` result to the cards the calling chat thread is subscribed to
  (``kanban_notify_subs`` platform/chat_id/thread_id): other chats' cards are hidden,
  ``count`` follows and ``hidden_foreign`` reports how many were hidden; worker processes
  and origin-less calls (CLI, cron, TUI) pass through unchanged.
"""


import re
from typing import Any, Optional

try:
    from . import approval_wait_gate, background_notify_gate, backup_copy_gate, checkpoint_gate, churn_wake, completion_gate, github_contact, handoff_gate, inventory_rule, kanban_policy, lead_delegation, notification_reconcile, paas_promote_gate, pr_acceptance_gate, pr_comment_gate, published_head_gate, repeat_guard, review_gate, review_loop, schedule_gate, skill_size, stale_wake, stop_gate, waiting_wake, workspace_scope_gate
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
_RECURSIVE_RM = re.compile(_START + r"rm\b(?=[^;&|\n]*\s-[a-zA-Z]*r[a-zA-Z]*\b)")
_HERMES_HOME_TOKENS = (
    r"\$\{?HERMES_HOME\}?",
    r"~/?\.hermes",
    r"\$\{?HOME\}?/\.hermes",
    r"/home/u/\.hermes",
    r"/Users/u/\.hermes",
    r"/root/\.hermes",
)
_HERMES_HOME_PATH = re.compile(
    r"(?:" + "|".join(_HERMES_HOME_TOKENS) + r")(?:/profiles(?:/[A-Za-z0-9_.-]+)?|/)?(?=\s|$|['\";)&|])"
)
_FIND_DELETE = re.compile(_START + r"find\b")
_RSYNC_DELETE = re.compile(_START + r"rsync\b")
_PYTH_DELETE = re.compile(
    r"(?:shut(?:il)?\.)?rmtree\s*\(\s*(?:['\"])?(?:os\.environ(?:\.get)?\[?\s*['\"]HERMES_HOME|HERMES_HOME)"
)
_PYTH_ABS_DELETE = re.compile(
    r"rmtree\s*\(\s*['\"][^'\"]*(?:\.hermes(?:/profiles(?:/[A-Za-z0-9_.-]+)?)?/?)['\"]"
)
_HERMES_HOME_DELETE_MESSAGE = (
    "hermes_home_guard: this would recursively delete a live Hermes home. For tests, create a throwaway "
    "home with `mktemp -d` inside the scratch workspace and run the command as `env HERMES_HOME=<path> <cmd>` "
    "only; delete such directories by their literal path."
)
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
    ctx.register_hook("pre_llm_call", inventory_rule.hook)
    stale_wake.register(ctx)
    ctx.register_hook("on_kanban_dispatch_tick", stale_wake.stall_wake_on_tick)
    ctx.register_hook("on_kanban_dispatch_tick", waiting_wake.on_tick)
    ctx.register_hook("on_kanban_dispatch_tick", review_loop.on_tick)
    ctx.register_hook("on_kanban_dispatch_tick", churn_wake.on_tick)
    ctx.register_hook("on_kanban_dispatch_tick", notification_reconcile.on_tick)


def _segments(command: str, pattern: re.Pattern) -> list[str]:
    found = []
    for match in pattern.finditer(command):
        end = _SEGMENT_END.search(command, match.end())
        found.append(command[match.start() : end.start() if end else len(command)])
    return found


def _strip_heredocs(command: str) -> str:
    """Blank out heredoc bodies: a script's own `sleep` (bash or `time.sleep`
    in Python) is not the agent blocking, and scripting text that merely says
    `sleep 600` must not be read as a command (measured: 3 false blocks)."""
    delim = None
    kept = []
    for line in command.split("\n"):
        if delim is not None:
            if line.strip() == delim:
                delim = None
            kept.append("")
            continue
        kept.append(line)
        match = _HEREDOC.search(line)
        if match:
            delim = match.group(2)
    return "\n".join(kept)


def hermes_home_violations(command: str) -> list[str]:
    """Recursive deletion of the live Hermes home: `$HERMES_HOME`, `~/.hermes`
    (`$HOME/.hermes`, literal absolute forms) and its `profiles` subtree itself,
    not nests below them. `rm`/`find -delete`/`rsync --delete` plus `shutil.rmtree`
    inside `python -c`/heredocs; single-quoted mention and heredoc prose are
    exempt, while double-quoted content expands and stays visible."""
    found = []
    stripped = _strip_heredocs(command)
    stripped = re.compile(r"'[^']*'").sub("''", stripped)
    for segment in _SEGMENT_END.split(stripped):
        if (_RECURSIVE_RM.search(segment) or _FIND_DELETE.search(segment)) and _HERMES_HOME_PATH.search(segment):
            found.append(_HERMES_HOME_DELETE_MESSAGE)
            continue
        if _RSYNC_DELETE.search(segment) and "--delete" in segment and _HERMES_HOME_PATH.search(segment):
            found.append(_HERMES_HOME_DELETE_MESSAGE)
            continue
    if _PYTH_DELETE.search(command) or _PYTH_ABS_DELETE.search(command):
        found.append(_HERMES_HOME_DELETE_MESSAGE)
    return found


def terminal_violations(command: str, background: bool) -> list[str]:
    found = []
    home = hermes_home_violations(command)
    if home:
        found.extend(home)
    command = _strip_heredocs(command)
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
    for guard in (repeat_guard, review_gate, stop_gate, checkpoint_gate, pr_acceptance_gate):
        try:
            guard.record(**kwargs)
        except Exception:
            pass


def _transform_tool_result(**kwargs) -> Optional[str]:
    try:
        transformed = pr_acceptance_gate.transform(**kwargs)
        if transformed is not None:
            return transformed
    except Exception:
        pass
    try:
        return workspace_scope_gate.transform(
            kwargs.get("tool_name") or "", kwargs.get("args"), kwargs.get("result"))
    except Exception:
        return None


def _pre_tool_call(tool_name: str = "", args: Optional[dict] = None, **kwargs) -> Optional[dict]:
    def stamp(message: str, name: str) -> str:
        marker = message.find(": ")
        if not message.startswith("agent-guards blocked") or marker < 0:
            return message
        return message[: marker] + " [" + name + "]" + message[marker:]
    for check, name in (
        (lambda: repeat_guard.check(tool_name, args, **kwargs), "repeat_guard"),
        (lambda: checkpoint_gate.check(tool_name, args, **kwargs), "checkpoint_gate"),
        (lambda: kanban_policy.check(tool_name, args), "kanban_policy"),
        (lambda: lead_delegation.check(tool_name, args), "lead_delegation"),
        (lambda: github_contact.check(tool_name, args), "github_contact"),
        (lambda: paas_promote_gate.check(tool_name, args), "paas_promote_gate"),
        (lambda: pr_acceptance_gate.check(tool_name, args, **kwargs), "pr_acceptance_gate"),
        (lambda: approval_wait_gate.check(tool_name, args, **kwargs), "approval_wait_gate"),
        (lambda: review_gate.check(tool_name, **kwargs), "review_gate"),
        (lambda: published_head_gate.check(tool_name, args), "published_head_gate"),
        (lambda: pr_comment_gate.check(tool_name, args), "pr_comment_gate"),
        (lambda: handoff_gate.check(tool_name, args, **kwargs), "handoff_gate"),
        (lambda: completion_gate.check(tool_name, args), "completion_gate"),
        (lambda: backup_copy_gate.check(tool_name, args), "backup_copy_gate"),
        (lambda: schedule_gate.check(tool_name, args), "schedule_gate"),
        (lambda: skill_size.check(tool_name, args), "skill_size"),
        (lambda: background_notify_gate.check(tool_name, args), "background_notify_gate"),
        (lambda: pr_comment_gate.check(tool_name, args), "pr_comment_gate"),
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
                return {"action": "block", "message": "agent-guards blocked terminal [__init__]: " + "; ".join(found) + "."}
            return None
        if tool_name == "kanban_attach" and len(str(args.get("content_base64") or "")) > _MAX_INLINE_ATTACH:
            return {"action": "block", "message": stamp(_INLINE_ATTACH_MESSAGE, "__init__")}
        changes = normalized_args(tool_name, args)
        return {"action": "modify", "args": changes} if changes else None
    except Exception:
        return None
