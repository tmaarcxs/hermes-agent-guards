
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
- ``kanban_show_slim_gate`` (lead process only) deterministically slims a lead
  ``kanban_show`` result: ``worker_context`` (a byte-duplicate of the structured fields)
  is removed and fixed text-free event kinds (heartbeat and dispatcher bookkeeping)
  are dropped; ``runs`` and every other text-bearing field stay byte-identical, so
  summaries, review verdicts and errors are preserved; the result adverts
  ``removed_fields``/``dropped_event_rows``; worker processes and non-``kanban_show``
  results pass through unchanged.
"""


import re
import shlex
from typing import Any, Optional

try:
    from . import approval_wait_gate, background_notify_gate, backup_copy_gate, checkpoint_gate, churn_wake, completion_gate, github_contact, handoff_gate, inventory_rule, kanban_policy, kanban_show_slim_gate, lead_delegation, notification_reconcile, paas_promote_gate, pr_acceptance_gate, review_gate, review_loop, schedule_gate, skill_size, stale_wake, stop_gate, waiting_wake, workspace_scope_gate, workspace_write_gate
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
_RECURSIVE_RM_FLAGS = re.compile(r"(?:-[a-zA-Z]*[rR][a-zA-Z]*|--recursive)\b")
_RUNNER_RM = re.compile(_START + r"(?:(?:sudo|command|env)\s+)*xargs\b(?:\s+-\S+)*\s+(?:(?:sudo|command)\s+)?rm\b(?=[^;&|\n]*" + _RECURSIVE_RM_FLAGS.pattern + r")")
_RECURSIVE_RM = re.compile(_START + r"rm\b(?=[^;&|\n]*" + _RECURSIVE_RM_FLAGS.pattern + r")")
_FIND_DELETE = re.compile(_START + r"find\b")
_FIND_DELETE_FLAGS = re.compile(r"\s-delete\b")
_FIND_NAME_FILTER = re.compile(r"-name\b")
_FIND_EXEC_RM = re.compile(r"-exec\s+(?:sudo\s+)?rm\s(?:[^;{}])*-[a-zA-Z]*r")
_RMTREE_ARG = re.compile(r"rmtree\s*\(\s*([^),]+)")
_RSYNC_DELETE = re.compile(_START + r"rsync\b")
_RSYNC_DELETE_FLAG = re.compile(r"(?:^|[\s/])--delete(?=[\s'=\"]|$)")
_PYTH_HERMES_ENV_RE = re.compile(r"os\.environ(?:\.get)?\[?\s*\(??\s*['\"]([^'\"]+)['\"]")
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


_PROTECTED_LEVELS = ("home", "ancestor", "root")


def _level_below_anchor(rest: str) -> str:
    """Classify the path that follows an anchor (a home root or `HERMES_HOME`).

    `.hermes` itself and `.hermes/profiles` (`/profile-name`) are the live
    home; `profiles/<name>/cache/...` and any other child are nests below."""
    if rest in ("", "/"):
        return "home"
    parts = rest.rstrip("/").split("/")
    if parts[0] != ".hermes":
        return "none"
    if len(parts) == 1:
        return "home"
    if parts[1] == "profiles" and len(parts) in (2, 3):
        return "home"
    return "below"


def _path_level(token: str) -> str:
    """Map one shell/Python path token to its protection level (`home`,
    `ancestor`, `root`, `below`, `none`). `~<user>` stays out of scope: `~u`
    never maps onto `/home/u` without knowing the OS."""
    t = token.strip().strip('"').strip("'")
    if t.startswith("~"):
        body = t[1:]
        user = body.split("/", 1)[0]
        if not user:
            if body in ("", "/"):
                return "ancestor"
            return _level_below_anchor(body[1:] if body.startswith("/") else body)
        return "none"
    if t in ("$HOME", "${HOME}", "$HOME/", "${HOME}/"):
        return "ancestor"
    if t.startswith("$HOME"):
        rest = t[len("$HOME"):]
        if rest in ("", "/"):
            return "ancestor"
        if rest.startswith("/"):
            return _level_below_anchor(rest[1:])
        return "none"
    if t in ("$HERMES_HOME", "${HERMES_HOME}", "$HERMES_HOME/", "${HERMES_HOME}/"):
        return "home"
    if t.startswith("$HERMES_HOME"):
        rest = t[len("$HERMES_HOME"):]
        if rest in ("", "/"):
            return "home"
        if rest.startswith("/"):
            return _level_below_anchor(rest[1:])
        return "none"
    if t.startswith("/root"):
        rest = t[5:]
        if rest in ("", "/"):
            return "ancestor"
        if rest.startswith("/"):
            return _level_below_anchor(rest[1:])
        return "none"
    for prefix in ("/home/", "/Users/"):
        if t.startswith(prefix):
            rest = t[len(prefix):]
            user = rest.split("/", 1)[0]
            if not user:
                return "none"
            rest = rest[len(user):]
            if rest in ("", "/"):
                return "ancestor"
            return _level_below_anchor(rest[1:] if rest.startswith("/") else rest)
    if t == "/":
        return "root"
    return "none"


def _segment_operand_levels(segment: str) -> list[str]:
    """Protection levels of the segment's operand tokens (shlex view)."""
    try:
        tokens = shlex.split(segment)
    except ValueError:
        tokens = segment.split()
    return [level for token in tokens for level in (_path_level(token),) if level != "none"]


def _segment_tokens(segment: str) -> list[str]:
    """shlex token view of one segment with `punctuation_chars=True`: a pipe
    comes back as its own token while quoted payloads stay whole, so a
    quoted `|` can never split the operand side off a deletion verb.
    Unbalanced quotes return no tokens instead of a word-split fallback —
    a `str.split` rebuilds the very pipe/quote confusion (R1) this view
    exists to avoid."""
    lexer = shlex.shlex(segment, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return []


def _pipe_left_tokens(segment: str) -> list[str]:
    """Operand tokens piped into a deletion verb from an earlier pipe stage
    of the same segment (`echo <home> | xargs rm -rf`): every token before
    the LAST `|` token (`||` parses as one token, so it never splits left
    from right)."""
    tokens = _segment_tokens(segment)
    if "|" not in tokens:
        return []
    pipe_index = len(tokens) - 1 - tokens[::-1].index("|")
    return tokens[:pipe_index]


_RUNNER_WORDS = ("sudo", "command", "env", "nohup", "nice", "exec", "time", "parallel")
_RUNNER_XARGS_FLAGS = re.compile(r"xargs(?:\s+-\S+)*")


def _composed_runner_levels(segment: str) -> Optional[set]:
    """Levels a runner-composed deletion would delete (`echo <home> | xargs
    rm -rf`), or `None` when this segment is not one. The runner chain
    (`sudo`, `env`, `nohup`, `nice`, `exec`,
    `time`, `parallel`, `xargs` with its flags) appends the piped-in
    operands to the verb, so the composed command's targets are the verb's
    own operands plus everything left of the last pipe. Semantics stay
    verb-local (a filtered `find` below an ancestor walks only matches; an
    rsync deletes on its destination), mirroring the direct-shape checks."""
    verbs = ("rm", "find", "rsync")
    tokens = _segment_tokens(segment)
    verb_index = None
    for index in range(len(tokens) - 1, -1, -1):
        if tokens[index] in verbs:
            verb_index = index
            break
    if verb_index is None:
        return None
    if (
        tokens[verb_index] == "rm"
        and verb_index >= 2
        and tokens[verb_index - 1] == "-exec"
        and tokens[verb_index - 2] == "find"
    ):
        verb_index -= 2
    runner_seen = False
    for token in reversed(tokens[:verb_index]):
        if token == "|":
            break
        if token in _RUNNER_WORDS or token == "--" or (token.startswith("-") and token != "--") or _RUNNER_XARGS_FLAGS.fullmatch(token):
            runner_seen = True
            continue
        if verb_index + 1 < len(tokens) and tokens[verb_index + 1].startswith("-"):
            break
        if (
            '"' in token or "'" in token or "=" in token
            or token.endswith(",") or token.endswith("[") or token.endswith("]")
        ):
            break
        return None
    if not runner_seen:
        return None
    if verb_index + 1 >= len(tokens) or not tokens[verb_index + 1].startswith("-"):
        return None
    stage = tokens[verb_index:]
    if any(('"' in token and token != '"') for token in stage[2:]):
        return None
    stage_text = " ".join(stage)
    stage_levels = set(_segment_operand_levels(stage_text))
    left_levels = set(
        level for token in tokens[:verb_index] for level in (_path_level(token),) if level != "none"
    )
    levels = stage_levels | left_levels
    if stage[0] == "find":
        if not (_FIND_DELETE_FLAGS.search(stage_text) or _FIND_EXEC_RM.search(stage_text)):
            return None
        if "home" in (levels & set(_PROTECTED_LEVELS) | stage_levels):
            return levels
        if _FIND_NAME_FILTER.search(stage_text):
            return None
        return levels if levels & set(_PROTECTED_LEVELS) else None
    if stage[0] == "rsync":
        if not _RSYNC_DELETE_FLAG.search(stage_text):
            return None
        operands = [token for token in stage[1:] if not token.startswith("-")]
        if operands and _path_level(operands[-1]) in _PROTECTED_LEVELS:
            return levels
        if left_levels & set(_PROTECTED_LEVELS):
            return levels
        return None
    if not _RECURSIVE_RM_FLAGS.search(stage_text):
        return None
    return levels if levels & set(_PROTECTED_LEVELS) else None


def _rsync_delete_target_protected(segment: str) -> bool:
    """True when the `--delete` sync's DESTINATION (last non-option operand,
    the side that loses files) is a protected target; a home as SOURCE syncs
    out to a safe copy and stays allowed."""
    try:
        tokens = shlex.split(segment)
    except ValueError:
        tokens = segment.split()
    operands = []
    for token in tokens[1:]:
        if token.startswith("-"):
            if token in ("--",):
                continue
            continue
        operands.append(token)
    if not operands:
        return False
    return _path_level(operands[-1]) in _PROTECTED_LEVELS


def hermes_home_violations(command: str) -> list[str]:
    """Recursive deletion of the live Hermes home: `$HERMES_HOME`, `~/.hermes`
    (`$HOME/.hermes`, literal absolute forms for any user) and its `profiles`
    subtree itself, an ancestor of it (`~`, `$HOME`, `/home/<user>`,
    `/Users/<user>`, `/root`, `/`), not nests below an ancestor that do not
    include the home. `rm -r/-R/--recursive`, `find -delete`,
    `rsync --delete` (destination side only) plus `shutil.rmtree` inside
    `python -c`/heredocs; single-quoted mention and heredoc prose are exempt,
    while double-quoted content expands and stays visible."""
    found = []
    blanked = _strip_heredocs(command)
    jointed = blanked.replace("\\\n", "")
    for phrase in _python_delete_phrases(command, jointed):
        if _python_rmtree_protected(phrase):
            found.append(_HERMES_HOME_DELETE_MESSAGE)
            return found
    for segment in _SEGMENT_END.split(jointed):
        composed = _composed_runner_levels(segment)
        if _RECURSIVE_RM.search(segment):
            levels = set(_segment_operand_levels(segment))
        elif composed is not None:
            levels = composed
        else:
            levels = set()
        protected = bool(levels & set(_PROTECTED_LEVELS))
        if _RECURSIVE_RM.search(segment) and protected:
            found.append(_HERMES_HOME_DELETE_MESSAGE)
            continue
        if composed is not None:
            found.append(_HERMES_HOME_DELETE_MESSAGE)
            continue
        left_levels = set(
            level for token in _pipe_left_tokens(segment) for level in (_path_level(token),) if level != "none"
        )
        if _FIND_DELETE.search(segment) and _find_delete_activates(segment, left_levels):
            found.append(_HERMES_HOME_DELETE_MESSAGE)
            continue
        if _RSYNC_DELETE.search(segment) and _RSYNC_DELETE_FLAG.search(segment):
            if _rsync_delete_target_protected(segment):
                found.append(_HERMES_HOME_DELETE_MESSAGE)
    return found


_PY_CODE_ARG = re.compile(r"(?:^|[;&|\n(`{]|\$\(|\bthen\b|\bdo\b|\belse\b)\s*python\d?(?:\d\.\d+)?\s+(?:-[^\s-]\S*\s+)*-c\s*")
_PY_LOADER_LINE = re.compile(r"^\s*(?:sudo\s+)?(?:env\s+\S+=\S+\s+)*python\d?(?:\d\.\d+)?(?:\s|$)")


def _python_delete_phrases(command: str, blanked: str) -> list[str]:
    """`rmtree(...)` occurrences in real python execution contexts: the
    `python -c <code>` payload captured THROUGH its closing quote, and the
    body of a python heredoc read from the original command (the blanked
    text no longer carries bodies). Prose in echo/grep arguments never
    enters those contexts and is skipped."""
    phrases = []
    for match in _PY_CODE_ARG.finditer(blanked):
        phrases.append(_capture_code_payload(blanked, match.end()))
    if _HEREDOC.search(command):
        body = _heredoc_python_body(command)
        if body and _PY_LOADER_LINE.match(command.strip()):
            phrases.append(body)
    return phrases


def _capture_code_payload(text: str, start: int) -> str:
    if start < len(text):
        quote = text[start] if text[start] in ("'", '"') else None
        if quote:
            end_quote = text.find(quote, start + 1)
            while end_quote > 0 and quote == '"' and text[end_quote - 1] == "\\":
                end_quote = text.find(quote, end_quote + 1)
            if end_quote > 0:
                return text[start + 1 : end_quote]
            return text[start + 1 :]
    end = _SEGMENT_END.search(text, start)
    return text[start : end.start() if end else len(text)]


def _heredoc_python_body(command: str) -> Optional[str]:
    lines = command.split("\n")
    delim = None
    collecting = False
    body = []
    for line in lines:
        if delim is not None:
            if line.strip() == delim:
                delim = None
                collecting = False
                continue
            if collecting:
                body.append(line)
            continue
        match = _HEREDOC.search(line)
        if match:
            delim = match.group(2)
            collecting = True
    return "\n".join(body) if body else None


def _python_rmtree_protected(phrase: str) -> bool:
    for match in _RMTREE_ARG.finditer(phrase):
        raw = match.group(1).strip()
        if "os.environ" in raw or "os.getenv" in raw:
            literal = _PYTH_HERMES_ENV_RE.search(raw)
            if literal and "HERMES_HOME" in literal.group(1):
                return True
            continue
        literal = re.match(r"(['\"])(.*?)\1", raw)
        token = literal.group(2) if literal else raw
        if _path_level(token) in _PROTECTED_LEVELS:
            return True
    return False


def _find_delete_activates(segment: str, left_levels: Optional[set] = None) -> bool:
    """`find <operand>` deletes recursively with `-delete` or
    `-exec rm -r...`; a home operand always blocks, an ancestor or root
    operand only when no `-name` filter narrows the walk below it, a nest
    below never blocks."""
    levels = set(_segment_operand_levels(segment)) | set(left_levels or ())
    if not any(level in _PROTECTED_LEVELS for level in levels):
        return False
    if not (_FIND_DELETE_FLAGS.search(segment) or _FIND_EXEC_RM.search(segment)):
        return False
    if "home" in levels:
        return True
    return _FIND_NAME_FILTER.search(segment) is None


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
    for guard in (review_gate, stop_gate, checkpoint_gate, pr_acceptance_gate):
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
        scoped = workspace_scope_gate.transform(
            kwargs.get("tool_name") or "", kwargs.get("args"), kwargs.get("result"))
        if scoped is not None:
            return scoped
    except Exception:
        pass
    try:
        return kanban_show_slim_gate.transform(
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
        (lambda: checkpoint_gate.check(tool_name, args, **kwargs), "checkpoint_gate"),
        (lambda: workspace_write_gate.check(tool_name, args), "workspace_write_gate"),
        (lambda: kanban_policy.check(tool_name, args), "kanban_policy"),
        (lambda: lead_delegation.check(tool_name, args), "lead_delegation"),
        (lambda: github_contact.check(tool_name, args), "github_contact"),
        (lambda: paas_promote_gate.check(tool_name, args), "paas_promote_gate"),
        (lambda: pr_acceptance_gate.check(tool_name, args, **kwargs), "pr_acceptance_gate"),
        (lambda: approval_wait_gate.check(tool_name, args, **kwargs), "approval_wait_gate"),
        (lambda: review_gate.check(tool_name, **kwargs), "review_gate"),
        (lambda: handoff_gate.check(tool_name, args, **kwargs), "handoff_gate"),
        (lambda: completion_gate.check(tool_name, args), "completion_gate"),
        (lambda: backup_copy_gate.check(tool_name, args), "backup_copy_gate"),
        (lambda: schedule_gate.check(tool_name, args), "schedule_gate"),
        (lambda: skill_size.check(tool_name, args), "skill_size"),
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
                return {"action": "block", "message": "agent-guards blocked terminal [__init__]: " + "; ".join(found) + "."}
            return None
        if tool_name == "kanban_attach" and len(str(args.get("content_base64") or "")) > _MAX_INLINE_ATTACH:
            return {"action": "block", "message": stamp(_INLINE_ATTACH_MESSAGE, "__init__")}
        changes = normalized_args(tool_name, args)
        return {"action": "modify", "args": changes} if changes else None
    except Exception:
        return None
