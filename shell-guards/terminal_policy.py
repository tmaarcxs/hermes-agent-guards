"""Terminal command policy for shell-guards: the sleep/merge/osascript/force-push/gh-auth/home-delete blocks, the inline-attach cap and the argument normalization.

Behaviour-identical move of the terminal block, ``normalized_args`` and the
inline-attach cap from the old monolithic ``agent-guards`` root ``__init__.py``,
plus the ``gh auth`` shared-account block (see ``SPLIT_EQUIVALENCE.md`` §10).
"""

import re
from typing import Any, Optional

from . import hermes_home_gate

_START = r"(?:^|[;&|\n(`{]|\$\(|\bthen\b|\bdo\b|\belse\b)\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*(?:sudo\s+|command\s+)?"
_SEGMENT_END = re.compile(r"[;\n]|&&|\|\|")
_QUOTED = re.compile(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"")
_LOOP = re.compile(r"\b(?:for|while|until)\b")
_SLEEP = re.compile(_START + r"sleep\s+(\d+(?:\.\d+)?)([smh]?)\b")
_READ_T = re.compile(_START + r"read\s+(?:-[A-Za-z]+\s+)*-t\s+(\d+(?:\.\d+)?)([smh]?)\b")
_PY_WAIT = re.compile(r"\btime\.sleep\s*\(([^)]*)\)")
_SELECT = re.compile(r"\b(?:select\.select|select)\s*\(([^()]*(?:\([^()]*\)[^()]*)*)\)")
_BACKGROUNDED_INTERP = re.compile(
    r"(?:^|\n|[;&|(]\s*)(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*(?:sudo\s+|env\s+(?:[-\w]+=?\S*\s+)*)?"
    r"(?:[A-Za-z0-9_/.~-]*python[0-9.]*|perl\d?)\b[^;\n|&]*?[\"'][^\"']*[\"'][^;\n|&]*&(?=\s|$)"
)
_MERGE = re.compile(_START + r"gh\s+pr\s+merge\b")
_OSASCRIPT = re.compile(_START + r"osascript\b")
_PUSH = re.compile(_START + r"git\s+push\b")
_BARE_FORCE = re.compile(r"\s(?:--force|-f)(?=\s|$)")
_PGREP_FULL = re.compile(r"\b(?:pgrep|pkill)\b[^\n;&|]*?\s(?:--full|-{1,2}[a-zA-Z]*f[a-zA-Z]*)\b")
"""Matches a ``pgrep``/``pkill`` full-command-line match inside a loop.

A ``pgrep -f``/``pkill -f`` in a loop matches the loop's own shell: the pattern is
part of that shell's command line, so the loop can never exit (observed 2026-10-30:
two waiters held 8h on ``while pgrep -f "migrate.py"``). No ``_START`` anchor: the
common shape is ``while pgrep -f X; do ...``, where pgrep follows the loop keyword
rather than a command separator. ``-f``/``-fl``/``-lf`` all enable full-command-line
matching.
"""
_GH_AUTH = re.compile(
    _START
    + r"(?:env\s+(?:[-\w]+(?:=\S*)?\s+)*)?(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*"
    + r"gh\s+auth\s+(switch|login|logout|refresh)\b"
)
_SHELL_C = re.compile(r"(?:bash|sh)\s+-c\s+")

_UNIT_SECONDS = {"": 1, "s": 1, "m": 60, "h": 3600}
_MAX_FOREGROUND_SLEEP = 5
_MAX_LOOP_SLEEP = 3

_WAIT_FOREGROUND_MESSAGE = (
    "foreground wait of >= 5s (read -t / time.sleep / select) in a foreground terminal call: "
    "run the wait as terminal(command=<the same command>, background=true, notify=true) "
    "instead - its completion message wakes you, so never sleep or poll"
)
_WAIT_LOOP_MESSAGE = (
    "polling loop with a wait of >= 3s (read -t / time.sleep / select): "
    "run the whole wait as terminal(command=<the same loop>, background=true, notify=[<ready pattern>]) "
    "instead - the ready-pattern match wakes you; never sleep or poll"
)


def _wait_seconds(token: str) -> Optional[float]:
    try:
        return float(token)
    except (TypeError, ValueError):
        return None


def _matches_wait(source: str) -> list[tuple[int, float]]:
    """(offset, seconds) of every wait-class literal in *source*.

    `read -t N`, `time.sleep(N)` and `select(..., N)` all idle the interpreter
    for N seconds; select's timeout is its LAST numeric argument. Non-numeric
    offsets (a variable) fail open — the shape that matters is the literal
    a model writes while intending to wait.
    """
    found: list[tuple[int, float]] = []
    for match in _READ_T.finditer(source):
        seconds = _wait_seconds(match.group(1))
        unit = match.group(2) or "s"
        if seconds is not None:
            found.append((match.start(1), seconds * _UNIT_SECONDS.get(unit, 1)))
    for match in _PY_WAIT.finditer(source):
        for token in match.group(1).split(","):
            seconds = _wait_seconds(token.strip())
            if seconds is not None:
                found.append((match.start(), seconds))
                break
    for match in _SELECT.finditer(source):
        args = [token.strip() for token in match.group(1).split(",")]
        seconds = _wait_seconds(args[-1]) if args else None
        if seconds is not None:
            found.append((match.start(), seconds))
    line_end = re.compile(r"[;\n|]")
    backgrounded = re.compile(r"(?:^|\s)&(?=\s|$)")
    kept = []
    for offset, seconds in found:
        rest = source[offset:]
        stop = line_end.search(rest)
        rest = rest[: stop.start()] if stop else rest
        if backgrounded.search(rest):
            continue
        kept.append((offset, seconds))
    return kept


_EMPTY_TO_ABSENT = {"kanban_list": ("assignee", "status", "tenant"), "kanban_create": ("completion_contract",)}
_ENV_TASK_LITERAL = re.compile(r"^\s*\$?\{?HERMES_KANBAN_TASK\}?\s*$")
_MAX_INLINE_ATTACH = 256
_INLINE_ATTACH_MESSAGE = (
    "agent-guards blocked kanban_attach: inline base64 over 256 characters arrives "
    "corrupted; attach by path: artifacts=[absolute path], then sha256-compare the stored file."
)


def attach_cap_block(args: Optional[dict]) -> Optional[str]:
    """The inline-attach cap message for a ``kanban_attach`` call, or ``None``."""
    if args is not None and len(str(args.get("content_base64") or "")) > _MAX_INLINE_ATTACH:
        return _INLINE_ATTACH_MESSAGE
    return None


def _loop_starts(command: str) -> list[int]:
    """Offsets where a ``for``/``while``/``until`` loop keyword begins."""
    return [m.start() for m in _LOOP.finditer(command)]


def _script_and_payload_bodies(source: str) -> list[str]:
    """Interpreter payload texts the agent actually runs: heredoc bodies and
    ``python -c``/``perl -e`` string payloads.

    The caller passes the RAW command: heredoc bodies and quoted payloads are
    exactly what the quote/blanking steps erase from the scanned shell text,
    so they survive only here. Mention-in-strings stays exempt: the -c payload
    must be a real quoted argument of an interpreter invocation.
    """
    bodies: list[str] = []
    delim = None
    body: list[str] = []
    for line in source.split("\n"):
        if delim is not None:
            if line.strip() == delim:
                if body:
                    bodies.append("\n".join(body))
                delim = None
                body = []
            else:
                body.append(line)
            continue
        match = re.search(r"<<-?\s*['\"]?(\w[\w.]*)['\"]?", line)
        if match:
            delim = match.group(1)
    for match in re.finditer(
            r"(?:python\d?(?:\d(?:\.\d+)?)?|perl\d?)\s+(?:-[A-Za-z]+\s+)*-c\s*|perl\s+(?:-[A-Za-z]+\s+)*-e\s*",
            source):
        start = match.end()
        if start < len(source) and source[start] in ("'", '"'):
            quote = source[start]
            end = source.find(quote, start + 1)
            while end > 0 and quote == '"' and source[end - 1] == "\\":
                end = source.find(quote, end + 1)
            if end > 0:
                bodies.append(source[start + 1:end])
    return bodies


def _segments(command: str, pattern: re.Pattern) -> list[str]:
    found = []
    for match in pattern.finditer(command):
        end = _SEGMENT_END.search(command, match.end())
        found.append(command[match.start() : end.start() if end else len(command)])
    return found


_GH_AUTH_MESSAGE = (
    "gh auth switch (also login/logout/refresh) is not allowed: it changes the shared "
    "active account for every Hermes process. To act as another account, set it in the "
    "command itself: GH_TOKEN=$(gh auth token -u <user>) <command>. The default active "
    "account is acme-bot."
)


def _quoted_spans(text: str) -> list[tuple[int, int]]:
    return [(match.start(), match.end()) for match in _QUOTED.finditer(text)]


def _shell_c_argument(text: str, start: int) -> Optional[str]:
    """The quoted argument of a top-level ``bash -c``/``sh -c``, or ``None``.

    Only the quoted form is a real spelling: an unquoted ``bash -c gh auth switch``
    runs ``gh`` with ``auth``/``switch`` as ``$0``/``$1``, not the gh subcommand.
    """
    quote = text[start] if start < len(text) and text[start] in ("'", '"') else None
    if quote is None:
        return None
    end = text.find(quote, start + 1)
    while end > 0 and quote == '"' and text[end - 1] == "\\":
        end = text.find(quote, end + 1)
    return text[start + 1 : end] if end > 0 else text[start + 1 :]


def _gh_auth_blocked(blanked: str, source: str) -> bool:
    """A ``gh auth`` account-mutating verb at command-word position, or inside a
    quoted ``bash -c``/``sh -c`` argument.

    ``blanked`` has quoted content removed, so prose in ``echo``/``grep`` and a
    ``gh auth`` mention inside any string stay exempt; a ``bash -c``/``sh -c``
    argument is re-scanned from the original text because the blanking removes it.
    ``python -c`` bodies and heredoc bodies stay exempt, exactly as for the other
    terminal blocks.
    """
    if _GH_AUTH.search(blanked):
        return True
    spans = _quoted_spans(source)
    for match in _SHELL_C.finditer(source):
        if any(start <= match.start() < end for start, end in spans):
            continue
        inner = _shell_c_argument(source, match.end())
        if inner is not None and _GH_AUTH.search(_QUOTED.sub("''", inner)):
            return True
    return False


def terminal_violations(command: str, background: bool) -> list[str]:
    found = []
    home = hermes_home_gate.hermes_home_violations(command)
    if home:
        found.extend(home)
    source = hermes_home_gate._strip_heredocs(command)
    raw_command = command
    command = _QUOTED.sub("''", source)
    if not background:
        for match in _SLEEP.finditer(command):
            seconds = float(match.group(1)) * _UNIT_SECONDS[match.group(2)]
            if seconds >= _MAX_FOREGROUND_SLEEP:
                found.append(
                    "foreground sleep >= 5s in a foreground terminal call: run the wait as "
                    "terminal(command=<the same command>, background=true, notify=true) "
                    "instead - its completion message wakes you, so never sleep or poll"
                )
                break
        if not found:
            loop_starts = [m.start() for m in _LOOP.finditer(command)]
            if loop_starts:
                for match in _SLEEP.finditer(command):
                    seconds = float(match.group(1)) * _UNIT_SECONDS[match.group(2)]
                    if seconds >= _MAX_LOOP_SLEEP and any(start < match.start() for start in loop_starts):
                        found.append(
                            "polling loop with sleep >= 3s: run the whole wait as "
                            "terminal(command=<the same loop>, background=true, notify=[<ready pattern>]) "
                            "instead - the ready-pattern match wakes you; never sleep or poll"
                        )
                        break
        if not found:
            waits = _matches_wait(command)
            if waits:
                loops = [m.start() for m in _LOOP.finditer(command)]
                if not loops and any(seconds >= _MAX_FOREGROUND_SLEEP for _, seconds in waits):
                    found.append(_WAIT_FOREGROUND_MESSAGE)
                elif loops and any(seconds >= _MAX_LOOP_SLEEP and any(start < offset for start in loops)
                                   for offset, seconds in waits):
                    found.append(_WAIT_LOOP_MESSAGE)
        if not found:
            for body in _script_and_payload_bodies(raw_command):
                waits = _matches_wait(_QUOTED.sub("''", body))
                if not waits:
                    continue
                if _BACKGROUNDED_INTERP.search(raw_command):
                    continue
                loops = [m.start() for m in _LOOP.finditer(_QUOTED.sub("''", body))]
                if not loops and any(seconds >= _MAX_FOREGROUND_SLEEP for _, seconds in waits):
                    found.append(_WAIT_FOREGROUND_MESSAGE)
                    break
                if loops and any(seconds >= _MAX_LOOP_SLEEP and any(start < offset for start in loops)
                                 for offset, seconds in waits):
                    found.append(_WAIT_LOOP_MESSAGE)
                    break
    if not found and _loop_starts(command):
        starts = _loop_starts(command)
        for match in _PGREP_FULL.finditer(command):
            if any(start < match.start() for start in starts):
                found.append(
                    "pgrep/pkill -f inside a loop self-matches the loop's own command line, "
                    "so the loop never exits: match the exact process name (`pgrep -x name`) "
                    "or test the target directly (e.g. a pidfile or a port check)"
                )
                break
    if any("--match-head-commit" not in segment for segment in _segments(command, _MERGE)):
        found.append(
            "gh pr merge without a pinned head: add --match-head-commit <head-sha-of-the-pull-request> "
            "to this exact call (read the sha from `gh pr view <number> --json headRefOid`), so the "
            "merge applies to the verified head"
        )
    if _OSASCRIPT.search(command):
        found.append("osascript is not allowed: use ego-browser for browser work")
    if any(_BARE_FORCE.search(segment) for segment in _segments(command, _PUSH)):
        found.append("git push --force/-f is not allowed: use --force-with-lease")
    if _gh_auth_blocked(command, source):
        found.append(_GH_AUTH_MESSAGE)
    return found


def rewrite_poll_instructions(result: Any) -> Optional[str]:
    """Rewrite core's poll-only promoted-process note to the instruction the guards enforce.

    Hermes core promotes an over-cap foreground ``terminal`` call to a tracked background
    process and attaches a note telling the model to poll it with ``process(action="poll")``
    (``tools/terminal_tool.py`` ``_PROMOTED_NOTE_POLL_ONLY``); ``process_poll_gate`` blocks
    ``poll`` for workers, so a worker following that note loops between core's text and the
    guard. The note rides the result JSON's ``promoted_from_foreground`` field, which a
    ``transform_tool_result`` hook still sees after the tool returns. The note that already
    carries ``notify_on_complete=true`` (a notification will arrive) keeps its poll hint:
    there ``poll`` is a "sooner" convenience the guard does not need to fight. Fails open:
    a non-string, non-JSON or unmatchable result returns ``None`` untouched.
    """
    if not isinstance(result, str) or "promoted_from_foreground" not in result:
        return None
    try:
        import json

        data = json.loads(result)
        note = data.get("promoted_from_foreground") if isinstance(data, dict) else None
    except Exception:
        return None
    if not isinstance(note, str) or "poll it with process" not in note:
        return None
    data["promoted_from_foreground"] = _POLL_NOTE_REPLACEMENT
    return json.dumps(data, ensure_ascii=False)


_POLL_NOTE_REPLACEMENT = (
    "Requested foreground timeout {requested}s exceeds the {cap}s cap, so this command was started as a "
    "tracked background process instead of being refused. Do NOT re-run it. Do not poll it either: "
    "this session cannot receive completion notifications, so retrieve the outcome when you need it with "
    'process_manage(action="wait", session_id=<session_id>) (returns the moment the process exits) or '
    'process_manage(action="log", session_id=<session_id>) (reads the output so far). If the wait comes '
    "back interrupted with no new output, END the turn and re-issue the wait in a later turn — never "
    "sleep, never poll."
)


def _tool_deadline() -> Optional[float]:
    """Current per-call deadline, resolved the way the host resolves it (config, then env, then 420)."""
    try:
        from agent.deadline import resolve_timeout
    except Exception:
        return None
    try:
        return resolve_timeout("tools.sequential_call", default=None) or resolve_timeout(
            "tools.concurrent_batch", default=420.0, env_var="HERMES_CONCURRENT_TOOL_TIMEOUT_S"
        )
    except Exception:
        return None


def _wait_cap() -> Optional[int]:
    """A call at or above the deadline races it and dies without partial output.

    A foreground ``terminal`` ``timeout`` above the deadline is killed by the host before
    the tool's own 600 s promotion can run, and a ``process_manage`` ``wait`` at or above
    it behaves the same. Measured over 7 days: ``timeout=420`` errored 649 times out of
    1,275 reads, ``timeout=400`` never (787 reads). Keep a 5 s margin under the deadline
    so the tool's own timeout returns partial output.
    """
    deadline = _tool_deadline()
    if not deadline or deadline <= 6:
        return None
    return int(deadline) - 5


def _deadline_margin() -> Optional[int]:
    """Deadline minus a 15 s margin: the rewrite target for an over-deadline ``timeout``.

    15 s so the model still sees a bounded error surface (a failed command is fine; a
    deadline death that returns nothing is not).
    """
    deadline = _tool_deadline()
    if not deadline or deadline < 16:
        return None
    return int(deadline) - 15


def _rewrite_timeout(value: Any, cap: Optional[int]) -> bool:
    """True when ``value`` is a clamped candidate (an ``int`` above ``cap``)."""
    return (
        cap is not None
        and isinstance(value, int)
        and not isinstance(value, bool)
        and value > cap
    )


def rewrite_deadline_args(tool_name: str, args: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Rewrite a ``timeout`` that exceeds the call deadline to deadline minus 15 s.

    ``terminal`` only in the foreground (a ``background=true`` call is not run under this
    hook's deadline) and ``process_manage`` ``wait`` (``poll``/``log``/``kill`` do not
    block). Merged after the existing default-deadline wait clamp (deadline minus 5 s), so
    an over-deadline wait lands on deadline minus 15 s; the deadline-minus-5 default clamp
    stays authoritative for the band between the two margins. A rewritten value is below
    the deadline, so re-entry finds nothing to change. Fails open: a non-integer or
    non-positive ``timeout``, a missing cap (unresolvable deadline) and any internal error
    all leave the call untouched.
    """
    if tool_name == "terminal" and not bool(args.get("background")):
        key = "timeout"
    elif tool_name == "process_manage" and args.get("action") == "wait":
        key = "timeout"
    else:
        return None
    cap = _deadline_margin()
    if not _rewrite_timeout(args.get(key), cap):
        return None
    return {key: cap}


def _tool_note_text(command: str, cap: int) -> str:
    """The note for the tool's own timeout death of a (rewritten) over-deadline call:
    what happened and the event-driven way to run the same work."""
    return (
        f"Note: guarded - this foreground terminal call was rewritten from a timeout above the "
        f"{cap + 15:.0f}s call deadline to {cap}s and its own timeout then killed it with partial "
        f"output above. The next time this command may run that long, declare the outcome channel "
        f"up front: terminal(command=<the same command>, background=true, notify=true) returns at "
        f"once and its completion arrives as a message - never re-issue this same foreground call."
    )


def tool_note(output: Any, command: str, returncode: Any, **kwargs) -> Optional[str]:
    """``transform_terminal_output``: annotate the tool's own-timeout death of a clamped call.

    This seam runs INSIDE the terminal tool (``finalize_foreground_result``), so it also
    sees the death of the rewritten 405 s call — the reachable complement to the silent
    pre-call rewrite, because the host's own deadline kill produces ``_ToolTimeoutResult``,
    which no transform hook sees (``_publish_sequential_result`` skips transforms for
    timed-out results). Fires only when the output carries the terminal tool's own timeout
    marker exactly, ``[Command timed out after Ns]`` with N equal to the rewrite cap (so
    only calls the clamp touched, not an arbitrary ``exit 124`` or the configured default
    timeout), with ``returncode`` 124 and the marker at the end. Fails open: a non-string
    output, another exit code or an unresolvable deadline leave the output untouched.
    """
    if returncode != 124 or not isinstance(output, str) or not output:
        return None
    cap = _deadline_margin()
    if cap is None:
        return None
    marker = f"[Command timed out after {cap}s]"
    if not output.rstrip().endswith(marker):
        return None
    try:
        return output + "\n\n" + _tool_note_text(command, cap)
    except Exception:
        return None


def normalized_args(tool_name: str, args: dict[str, Any]) -> Optional[dict[str, Any]]:
    changes: dict[str, Any] = {key: None for key in _EMPTY_TO_ABSENT.get(tool_name, ()) if args.get(key) == ""}
    if tool_name.startswith("kanban_") and _ENV_TASK_LITERAL.match(str(args.get("task_id") or "")):
        changes["task_id"] = None
    if tool_name == "process_manage" and args.get("action") == "wait":
        timeout = args.get("timeout")
        cap = _wait_cap()
        if cap and isinstance(timeout, int) and not isinstance(timeout, bool) and timeout > cap:
            changes["timeout"] = cap
    if tool_name == "skill_view":
        name = args.get("name")
        if isinstance(name, str) and ":" in name:
            prefix, _, rest = name.partition(":")
            if prefix == rest:
                changes["name"] = rest
    return changes or None
