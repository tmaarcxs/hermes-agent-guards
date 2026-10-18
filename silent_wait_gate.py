"""Correct foreground waits that outlive the liveness clock to the background form.

The crash audit found 10 worker runs and 8.9 h lost to a foreground ``terminal``
call that stays silent past the tool deadline (``sleep 480; gh run list`` being
the anchor) and then dies on the 600 s liveness watchdog. The sleep and loop
ceilings already live in ``terminal_violations`` (they keep blocking from 30 s /
15 s with their own message, and the combined-message contract of
``test_all_violations_in_one_message`` is untouched); ``silent_wait_gate`` covers
the shapes they miss at the card's 120 s threshold — though in practice the two
reinforce each other, because the pre-existing ceiling fires first on every
``sleep`` shape. Each block names the exact corrected call — the background form
with ``notify=true``, or the bounded form — and ends with the lifecycle retry
instruction.

Detected shapes:

- a foreground ``sleep`` of 120 seconds or more (``sleep 480``, ``sleep 2m``,
  ``sleep 5h``, ``sleep infinity``; bare ``inf`` passes — ``sleep`` rejects it
  and parsing stays exact);
- ``tail -f`` / ``tail -F`` / ``tail --follow[=name]``, including as the tail of
  a pipeline (``vite build ... 2>&1 | tail -f log`` — the pipe does not make an
  endless stream bounded), and the follow-mode log streams ``docker compose
  logs --follow``, ``docker logs -f`` and ``journalctl -f``;
- a conditionless ``while true`` / ``while :`` / ``until :`` loop or an
  open-ended ``for ((;;))`` / ``for ;;``.

A ``sleep`` that ``terminal_violations`` already blocks (foreground >= 30 s, or
>= 15 s inside its loop check) keeps its own existing message there; a shorter
one passes both. Quoted strings, heredoc bodies and ``python -c``/``python -``/
``bash -c`` bodies are blanked before matching (a script's own waits are not
the agent's), the module is consulted for a ``terminal`` call that is foreground
(background falsy) and not a ``pty`` session, fails open on any doubt, and is
active in worker processes only.
"""

import os
import re
from typing import Any, Optional

from . import kanban_policy

MESSAGE = (
    "agent-guards blocked terminal: foreground wait would outlive the liveness watchdog ({found}). "
    "Run the wait in the background instead: terminal(command=<the same command>, background=true, "
    "notify=true) — its completion notice carries the output tail — or use the bounded form right "
    "away (run the polling step once now and re-issue without waiting, or cap the command with a timeout)."
)

_INTERP_BODY = re.compile(
    r"(?:[A-Za-z0-9_/.~-]*python[0-9.]*|node|bash|sh|zsh)[ \t]+(?:-\w+ )*-c\b[^;&|`]*\Z"
)
_HEREDOC_BODY = re.compile(r"(<<-?\s*(?:['\"]?\w[\w.]*['\"]?)).*\Z", re.S)
_QUOTED = re.compile(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"")
_SEGMENT_START = r"(?:^|[;&|\n(`{]|\$\(|\bthen\b|\bdo\b|\belse\b)\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*(?:sudo\s+|command\s+|exec\s+|timeout\s+\S+\s+|nice\s+)*"
_SLEEP = re.compile(_SEGMENT_START + r"sleep\s+(?:(\d+(?:\.\d+)?)([smh]?)\b|(inf(?:inity)?))")
_LOOP = re.compile(r"\b(?:while[ \t]+(?:true|:)\b|until[ \t]+:[ \t;]|for[ \t]*\(\(;;\)\)|for[ \t]*;;)")
_TAIL_FOLLOW = re.compile(_SEGMENT_START + r"tail\s+(?:-\S+\s+)*(?:-f\b|-F\b|--follow(?:=?\S+)?)")
_STREAM_FOLLOW = re.compile(_SEGMENT_START + r"(?:docker\s+(?:compose\s+)?logs|docker\s+logs|journalctl)\b[^\n;&|`]*?(?:(?<=\s)--follow\b|(?<=\s)-f\b)")

_UNIT = {"": 1, "s": 1, "m": 60, "h": 3600}


def detect(command: str) -> str:
    """The first unbounded-wait shape in the command, as shown in the message."""
    children = _strip_quoted_and_heredocs(command)
    if _LOOP.search(children):
        return "unbounded poll loop"
    for match in _SLEEP.finditer(children):
        word = match.group(3)
        if word:
            if word == "infinity":
                return "sleep infinity"
            continue
        if float(match.group(1)) * _UNIT[match.group(2)] >= 120:
            return "sleep " + match.group(0).split()[-1]
    if _TAIL_FOLLOW.search(children):
        return "tail -f"
    if _STREAM_FOLLOW.search(children):
        return "follow-mode log stream"
    return ""


def _strip_quoted_and_heredocs(command: str) -> str:
    text = _INTERP_BODY.split(command)[0] if _INTERP_BODY.search(command) else command
    text = _HEREDOC_BODY.sub(r"\1''", text)
    return _QUOTED.sub("''", text)


def check(tool_name: str, args: Any) -> Optional[str]:
    if tool_name != "terminal" or not isinstance(args, dict) or args.get("background") or args.get("pty"):
        return None
    if not kanban_policy.configured_worker():
        return None
    found = ""
    try:
        found = detect(str(args.get("command") or ""))
    except Exception:
        found = ""
    if not found:
        return None
    return MESSAGE.format(found=found) + " " + kanban_policy.RETRY_INSTRUCTION
