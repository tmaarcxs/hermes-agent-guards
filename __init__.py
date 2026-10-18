
"""Block recurring agent mistakes before a tool runs, normalize known-bad arguments and keep work moving.

Blocks (terminal, foreground or background as noted):
- foreground ``sleep`` of 30 s or more, or a ``for``/``while``/``until`` polling
  loop holding a ``sleep`` of 15 s or more: run the wait with ``background=true``
  and a notify channel, or poll once and re-issue without sleeping; heredoc bodies
  and quoted strings are exempt (a script file's own sleeps are not the agent blocking);
- ``gh pr merge`` without ``--match-head-commit``: merges must pin the verified head;
- ``osascript``: desktop automation goes through ego-browser;
- ``git push --force``/``-f`` without ``--force-with-lease``;
- forged terminal receipts: the pseudo-victory phrase ``verified working exactly
  match`` or a canned pytest receipt (``N failed``/``0 passed``) written as literal
  text into a command that also runs ``pytest`` — real receipts come from the tool
  result, never from replayed text; quoted text is exempt (mentioning the phrase in
  a grep or echo passes), and heredoc-planted payloads are handled by the
  ingestion rule below;
- ingesting content into the gateway-managed ``skins/`` tree: a
  copy/link/move/sync/tee command whose destination lands under ``skins/`` or a
  shell redirection writing into it plants unmanaged files under a managed
  namespace; reads out of ``skins/`` and ``git mv`` inside it stay allowed;
- ``git checkout`` restoring a path under the gateway-managed ``skins/`` tree with no
  committed-tree spec (``git checkout -- skins/...``, bare ``skins``, branch names): profile
  checkpoints are preserved with a sha/``refs/``/``HEAD``-pinned restore instead.
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
from . import approval_wait_gate, background_notify_gate, backup_copy_gate, checkpoint_gate, churn_wake, completion_gate, github_contact, handoff_gate, inventory_rule, kanban_policy, lead_delegation, notification_reconcile, paas_promote_gate, pr_acceptance_gate, pr_comment_gate, published_head_gate, repeat_guard, review_gate, review_loop, schedule_gate, skill_size, stale_wake, stop_gate, waiting_wake, workspace_scope_gate


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
_PYTEST = re.compile(_START + r"(?:python[\d.]*\s+-m\s+pytest|pytest)\b")
_CANNED_RECEIPT = re.compile(r"\b\d+ failed\b|\b0 passed\b")
_VICTORY_MARKER = re.compile(r"\bverified working exactly match\b")
_GIT_CHECKOUT = re.compile(_START + r"git\s+checkout\b")
_CHECKOUT_FLAGS_WITH_ARG = {"-b", "-B"}
_INGEST_VERB = re.compile(_START + r"(?:cp|rsync|ln|install|ditto|tee)\b")
_INGEST_MV = re.compile(r"(?:^|[;&|\n(`{(]|\$\()mv\b")
_GIT_MV = re.compile(r"(?:^|[;&|\n(`{(]|\$\()git\s+mv\b")
_REDIR_INTO_SKINS = re.compile(r">>?\s*(?:\./)?skins[/\\]")


def _skins_token(token: str) -> bool:
    bare = token.lstrip("./")
    return bare == "skins" or bare.startswith("skins/") or bare.startswith("skins\\")
_UNIT_SECONDS = {"": 1, "s": 1, "m": 60, "h": 3600}
_MAX_FOREGROUND_SLEEP = 30
_MAX_LOOP_SLEEP = 15
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


def _skins_ingestion(command: str) -> Optional[str]:
    command = _strip_heredocs(command)
    command = _QUOTED.sub("''", command)
    for segment in _SEGMENT_END.split(command):
        segment = segment.strip()
        if not segment:
            continue
        is_ingest = bool(_INGEST_VERB.search(segment)) or (
            bool(_INGEST_MV.match(segment)) and not _GIT_MV.search(segment)
        )
        if not is_ingest:
            if _REDIR_INTO_SKINS.search(segment):
                return (
                    "ingesting into skins/ is not allowed: the skins/ tree is gateway-managed; "
                    "profile checkpoints stay in the profile home"
                )
            continue
        tokens = [token for token in segment.split()[1:] if token.startswith("-") is False]
        destinations = tokens[-1:]
        for token in destinations:
            if _skins_token(token):
                return (
                    "ingesting into skins/ is not allowed: the skins/ tree is gateway-managed; "
                    "profile checkpoints stay in the profile home"
                )


def _pytest_forged_receipt(command: str) -> Optional[str]:
    if not _VICTORY_MARKER.search(command) and not any(
        _PYTEST.search(segment) and _CANNED_RECEIPT.search(segment)
        for segment in _SEGMENT_END.split(command)
    ):
        return None
    return (
        "forged receipt: 'verified working exactly match' / canned 'N failed' or '0 passed' "
        "text does not prove a run — real receipts come from the tool result; run pytest and let it report"
    )


def _skins_checkout_restore(command: str) -> Optional[str]:
    for match in _GIT_CHECKOUT.finditer(command):
        end = _SEGMENT_END.search(command, match.end())
        segment = command[match.start() : end.start() if end else len(command)]
        start = segment.find("checkout") + len("checkout")
        tokens = segment[start:].split()
        flags_with_arg = 0
        positional = []
        for token in tokens:
            if token in _CHECKOUT_FLAGS_WITH_ARG:
                flags_with_arg += 1
                continue
            if token.startswith("-"):
                continue
            positional.append(token)
        if flags_with_arg:
            continue
        paths = [token for token in positional if "/" in token or "\\" in token or token == "skins"]
        bare_spec = [token for token in positional if token != "skins" and "/" not in token and "\\" not in token]
        if bare_spec:
            continue
        if any(_skins_token(token) for token in paths):
            return (
                "git checkout restoring skins/ without a committed-tree spec discards profile checkpoints; "
                "preserve them with `git checkout <sha|refs/...|HEAD> -- skins/...` instead"
            )


def terminal_violations(command: str, background: bool) -> list[str]:
    command = _strip_heredocs(command)
    command = _QUOTED.sub("''", command)
    found = []
    forged = _pytest_forged_receipt(command)
    if forged:
        found.append(forged)
    skins = _skins_checkout_restore(command)
    if skins:
        found.append(skins)
    ingestion = _skins_ingestion(command)
    if ingestion:
        found.append(ingestion)
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
    for check in (
        lambda: repeat_guard.check(tool_name, args, **kwargs),
        lambda: checkpoint_gate.check(tool_name, args, **kwargs),
        lambda: kanban_policy.check(tool_name, args),
        lambda: lead_delegation.check(tool_name, args),
        lambda: github_contact.check(tool_name, args),
        lambda: paas_promote_gate.check(tool_name, args),
        lambda: pr_acceptance_gate.check(tool_name, args, **kwargs),
        lambda: approval_wait_gate.check(tool_name, args, **kwargs),
        lambda: review_gate.check(tool_name, **kwargs),
        lambda: published_head_gate.check(tool_name, args),
        lambda: pr_comment_gate.check(tool_name, args),
        lambda: handoff_gate.check(tool_name, args, **kwargs),
        lambda: completion_gate.check(tool_name, args),
        lambda: backup_copy_gate.check(tool_name, args),
        lambda: schedule_gate.check(tool_name, args),
        lambda: skill_size.check(tool_name, args),
        lambda: background_notify_gate.check(tool_name, args),
        lambda: pr_comment_gate.check(tool_name, args),
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
