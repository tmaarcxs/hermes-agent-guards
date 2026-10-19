"""Reject a handoff that declares a kept backup/rollback copy of prior state.

The owner forbids backup/rollback/snapshot artifacts of prior state anywhere
(rollback is git, registries or re-applying the prior value). This gate catches the handoff itself: a worker
``kanban_complete`` or
``kanban_request_review`` whose ``summary``, ``result`` or metadata values state
that a backup, rollback, pristine or pre-task copy was kept or preserved is
blocked with instructions to delete it. Quoting the claim does not excuse it:
a keep claim inside single or double quotes still blocks.

Active only in worker setups (``kanban.default_assignee`` set; see
``kanban_policy``) and only in a worker process, mirroring ``completion_gate``.
Fails open.
"""

import re
from typing import Any, Optional

from . import kanban_policy
from .kanban_policy import RETRY_INSTRUCTION

_FAMILY = r"(?:backup|backups?|back-ups?|rollback|pre-task|pristine|snapshot)"
_VERB = (
    r"(?:kept|keep|preserv\w+|retain\w*|sav(?:e|ed|es|ing)|stor(?:e|ed|es|ing|age)"
    r"|backed?[- ]?ups?|backup(?:ped)?|backs?\s?ups?|left(?:\s+in\s+place)?|maintain\w*|archiv\w*)"
)
_GENERIC = r"(?:versions?|copies?|artifacts?|files?|states?|trees?|manifests?|scripts?|source|baseline)"
_QUALIFIER = r"(?:defective|old|prior|previous|failing|earlier|original)"

_KEPT = re.compile(
    rf"(?is)"
    rf"\b{_FAMILY}\b[^\n.;]{{0,32}}?\b{_VERB}\b"
    rf"|\b(?!{_FAMILY}){_VERB}\b[^\n.;]{{0,32}}?\b{_FAMILY}\b"
    rf"|\b{_QUALIFIER}\s+\w{{0,20}}\s+{_GENERIC}[^\n.;]{{0,32}}?\b(?:backed?[- ]?ups?|kept|stored|saved|preserved|retained)\b"
    rf"|\b{_GENERIC}\s+backed?[- ]?ups?\b"
    rf"|\b{_GENERIC}\s+stored\s+(?:under|in|at)\b"
    rf"|\b(?:rollback|snapshot|pristine)\b[^\n.;]{{0,24}}?\b(?:at|in|under)\s+\S*[/~.]"
)

_NEG = r"(?:no|not|never|none|zero|without)"
_REMOVAL = r"(?:removed?|deleted?|dropped|purged|cleaned|erased|gone|removal)"
_PRE_REMOVAL = re.compile(
    rf"(?i)\b{_REMOVAL}\b"
    rf"(?:\s+(?:of|and|a|the|any|all|this|that|its|old|prior|previous|stale|retained))*"
    rf"\s*$"
)
_DROPPED_COMPOUND = r"(?:backup|rollback|pre-task|pristine|snapshot|prior[-_]?state|prior)"

_MESSAGE = (
    "agent-guards blocked the handoff: the summary or metadata keeps or preserves a backup/rollback/"
    "pristine/pre-task copy of prior state. Rollback is git: delete the copy and, if needed, recover the "
    "prior state from git history (or the originating external system, e.g. the GitHub API), then re-run "
    "the handoff without the retained copy."
)


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[;.\n])\s+|[()]", text)
    return [part for part in (sentence.strip() for sentence in parts) if part]


def keeps_backup_copy(text: str) -> bool:
    """Whether any sentence claims a kept/preserved backup of prior state.

    A keep claim is a backup/rollback/pristine/pre-task/snapshot family word
    adjacent to a retention verb inside one clause; a bare two-word claim
    ("backup kept") is a claim. Negation before the match, or a removal in
    the same sentence before or after it, vetoes the claim, as does a bare
    keep statement scoped by "nowhere". Overreach compounds naming the copy
    only by a dropped family lexeme ("prior state tree backed up") need a
    locator to count; Quoted spans are not exempt: a summary that quotes a
    keep claim still states it.
    """
    for sentence in _sentences(text):
        match = _KEPT.search(sentence)
        if match is None:
            continue
        span = match.group(0)
        prefix = sentence[: match.start()]
        if re.search(rf"(?i)\b{_NEG}\b[^\n.;]{{0,60}}$", prefix):
            continue
        if _PRE_REMOVAL.search(prefix):
            continue
        if re.search(rf"(?i)\b{_REMOVAL}\b", sentence[match.end():]):
            continue
        if re.search(rf"(?i)\b{_VERB}\b[^\n.;]{{0,20}}\b(?:nowhere|nothing|nobody)\b", sentence):
            continue
        compound = (
            re.fullmatch(rf"(?i)\b{_GENERIC}\s+backed?[- ]?ups?\b", span) is not None
            or re.match(rf"(?i)^\b{_FAMILY}\b\s+(?:{_GENERIC})\s+\b{_VERB}\b", span) is not None
        )
        if compound:
            head = prefix.strip().rsplit(" ", 1)[-1].strip("'\"") if prefix.strip() else ""
            if re.match(rf"(?i)^\b{_FAMILY}\b\s+(?:{_GENERIC})\s+\b{_VERB}\b", span) or head and re.fullmatch(rf"(?i)(?:{_DROPPED_COMPOUND})", head):
                locator = re.search(
                    rf"(?i)\b(?:at|in|under|to|into|onto)\b|\S*[/~_-]\S*",
                    sentence[match.end():][:60],
                )
                if not locator:
                    continue
        return True
    return False


def _candidate_texts(args: Any) -> list[str]:
    texts = []
    for key in ("summary", "result"):
        value = (args or {}).get(key)
        if isinstance(value, str):
            texts.append(value)
    metadata = (args or {}).get("metadata")
    stack = [metadata]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            texts.append(item)
        elif isinstance(item, dict):
            stack.extend(str(key) for key in item if isinstance(key, str) and not str(key).startswith("_"))
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
    return [text for text in texts if text]


def check(tool_name: str, args: Any) -> Optional[str]:
    if tool_name not in ("kanban_complete", "kanban_request_review"):
        return None
    if not isinstance(args, dict) or not kanban_policy.configured_worker():
        return None
    if not any(keeps_backup_copy(text) for text in _candidate_texts(args)):
        return None
    return _MESSAGE + " " + RETRY_INSTRUCTION
