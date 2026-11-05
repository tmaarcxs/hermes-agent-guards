"""Block the lead's ``hermes kanban create`` CLI and name the tool alternative.

The ``kanban_create`` tool subscribes the creating chat to the new card
(``kanban_notify_subs``), so its completion wakes the chat. ``hermes kanban
create`` from a terminal writes the card row but no subscription row, so the
card runs and finishes in silence — the four guard cards of 2026-11-05 sat
approved for hours because of it (``kanban_notify_subs`` had zero rows for each,
verified by SQL; the ``kanban_create`` tool carries one ``notify+wake`` row).

Scope: lead-like processes only, per the card's identity formula — block when
no ``HERMES_KANBAN_TASK`` is pinned AND the process profile is not the
configured worker profile. So a worker run on a card keeps the CLI (the skills'
documented worker fallback relies on it), a worker-profile process without a
card keeps it too, the reviewer only when on a card; everything else — the lead
chats, cron, the default profile — is steered to the tool.

Shape: a ``hermes [-p <profile>] [--board <slug>] kanban [--board <slug>]
create`` head, also as ``python -m hermes_cli[.main] ... kanban create``, in any
shell statement — after ``&&``/``;``/pipes, inside ``bash -c``/``sh -c`` or a
subshell, under environment prefixes like ``sudo``/``env KEYS=VALUE``. Command
segments split like the shell does (``github_contact._scan``), so a quoted title
or ``--body`` value never opens a new segment. ``create --help`` stays allowed
and every other ``hermes kanban`` subcommand (show, comment, list,
notify-subscribe, ...) passes untouched.

The block message assembles the exact ``kanban_create`` tool call from the
blocked command's own flags — title, assignee, workspace kind/path, project,
idempotency key, parents, skills, goal mode. Workspace values map
``scratch``/``worktree``/``worktree:<path>``/``dir:<path>`` onto
``workspace_kind``/``workspace_path`` (a bare non-scratch value reads as
``dir:<value>``); the last occurrence of a single-value flag wins. ``--branch``
is read and mapped honestly: the tool has no branch argument, the worktree
branch is the tool's own. With ``--body-file``, the message says to pass the
file text as the tool call's ``body`` argument. Unparsable commands and every
internal error fail open.
"""

import os
import re
from typing import Any, Optional

from . import github_contact
from . import kanban_policy

MESSAGE = (
    "agent-guards blocked terminal: the lead's terminal must not create cards — "
    "`hermes kanban create` writes the card while leaving no chat subscribed "
    "(no kanban_notify_subs row), so the card runs and finishes in silence. "
    "Create it with the kanban_create tool instead: {call}"
)

_BODY_FILE = (
    " With --body-file, read the file's text and pass it as the tool call's "
    "body argument instead of the flag."
)

_CREATE_HEAD = re.compile(
    r"(?:python3? -m hermes_cli(?:\.main)?|(?<![\w./-])hermes(?: -p \S+)?)"
    r"(?: --board \S+)? kanban(?: --board \S+)? create(?:\s|$)"
)

_WRAPPER = re.compile(r"^(?:bash|sh|zsh)$")

_X_VALUE_FLAGS = frozenset({"--assignee", "--workspace", "--project", "--branch", "--idempotency-key"})
_REPEAT_FLAGS = {"--parent": "parents", "--skill": "skills"}

_SWITCH = "--goal"
_KNOWN_KINDS = frozenset({"scratch", "worktree", "dir"})


def _create_arg_tokens(tokens: list[tuple[str, bool]]) -> Optional[list[tuple[str, bool]]]:
    """Tokens after the ``create`` head word, when the segment opens with one."""
    for i, (text, quoted) in enumerate(tokens):
        if quoted or text != "create":
            continue
        if _CREATE_HEAD.search(" ".join(t for t, q in tokens[:i] if not q) + " create"):
            return tokens[i + 1:]
    return None


def _segment_call(tokens: list[tuple[str, bool]]) -> Optional[str]:
    """The exact ``kanban_create`` call this segment's create command meant."""
    args = _create_arg_tokens(tokens)
    if args is None:
        return None
    title: Optional[str] = None
    single: dict[str, str] = {}
    repeat: dict[str, list[str]] = {}
    switches: list[str] = []
    body_file = False
    i = 0
    while i < len(args):
        text, quoted = args[i]
        if not quoted and text == "--help":
            return None
        if not quoted and text in _X_VALUE_FLAGS and i + 1 < len(args):
            single[text] = args[i + 1][0]
            i += 1
        elif not quoted and text in _REPEAT_FLAGS and i + 1 < len(args):
            repeat.setdefault(_REPEAT_FLAGS[text], []).append(args[i + 1][0])
            i += 1
        elif not quoted and text == _SWITCH:
            switches.append(text)
        elif not quoted and text == "--body-file":
            body_file = True
        elif title is None and not text.startswith("-"):
            title = text
        i += 1
    if not single.get("--assignee"):
        return None  # no dispatchable assignee in the command: do not guess one
    workspace = single.get("--workspace") or ""
    head, sep, raw = workspace.partition(":")
    kind = head if head in _KNOWN_KINDS else "dir"
    path = raw if sep else ("" if head in ("scratch", "worktree") else workspace)
    pieces = [
        'title="' + (title or "<title>") + '"',
        'assignee="' + single["--assignee"] + '"',
        'workspace_kind="' + kind + '"',
    ]
    if path:
        pieces.append('workspace_path="' + path + '"')
    if single.get("--project"):
        pieces.append('project="' + single["--project"] + '"')
    if single.get("--branch"):
        pieces.append("(the worktree branch is derived by the tool itself or from "
                      "project; the CLI's --branch has no tool-call equivalent) ")
    if single.get("--idempotency-key"):
        pieces.append('idempotency_key="' + single["--idempotency-key"] + '"')
    if repeat.get("parents"):
        pieces.append("parents=[" + ", ".join('"' + p + '"' for p in repeat["parents"]) + "]")
    if repeat.get("skills"):
        pieces.append("skills=[" + ", ".join('"' + s + '"' for s in repeat["skills"]) + "]")
    if switches:
        pieces.append("goal_mode=True")
    call = "kanban_create(" + ", ".join(pieces) + ")"
    if body_file:
        call += _BODY_FILE
    return call


def _decision(command: str, depth: int = 0) -> Optional[str]:
    for tokens in github_contact._scan(command):
        heads = [text for text, quoted in tokens if not quoted]
        if depth < 3 and len(heads) >= 3 and _WRAPPER.match(heads[0]) and heads[1] == "-c":
            nested = _decision(tokens[2][0], depth + 1)
            if nested:
                return nested
            if tokens[2][1]:
                continue
        if not _CREATE_HEAD.search(" ".join(heads)):
            continue
        call = _segment_call(tokens)
        if not call:
            continue
        return MESSAGE.format(call=call)
    return None


def check(tool_name: str, args: Any) -> Optional[str]:
    """``pre_tool_call``: steer the lead terminal to the ``kanban_create`` tool."""
    if tool_name != "terminal" or not isinstance(args, dict):
        return None
    if not kanban_policy.configured_worker():
        return None
    if (os.environ.get("HERMES_KANBAN_TASK") or "").strip():
        return None  # worker or reviewer run on a card: the CLI fallback is legitimate
    if (os.environ.get("HERMES_PROFILE") or "").strip() == kanban_policy.configured_worker():
        return None  # the worker profile's own process: a worker run, not a lead
    try:
        return _decision(str(args.get("command") or ""))
    except Exception:
        return None


__all__ = ["MESSAGE", "check"]
