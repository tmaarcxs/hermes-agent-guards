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

_FAMILY = r"(?:backup|backups?|back-ups?|rollback|pre-task|pristine|snapshot|before[-_][\w.-]+)"
_RETAIN = (
    r"(?:kept|keep|preserv\w+|retain\w*|sav(?:e|ed|es|ing)|stor(?:e|ed|es|ing|age)"
    r"|backed?[- ]?ups?|backup(?:ped)?|backs?\s?ups?|maintain\w*|archiv\w*)"
)
_VERB = rf"(?:{_RETAIN}|left(?:\s+in\s+place)?)"
_FAM_ALT = ("backup", "backups", "back-ups", "rollback", "pre-task", "pristine", "snapshot", "before[-_][\\w.-]+")
_GENERIC = r"(?:versions?|copies?|artifacts?|files?|states?|trees?|manifests?|scripts?|source|baseline)"
_QUALIFIER = r"(?:defective|old|prior|previous|failing|earlier|original)"

_KEPT = re.compile(
    rf"(?is)"
    rf"\b{_FAMILY}\b[^\n.;,]{{0,32}}?{_VERB}\b"
    rf"|\b{_VERB}(?=[^a-z])[^\n.;,]{{0,32}}?\b{_FAMILY}\b"
    rf"|\b{_QUALIFIER}\s+\w{{0,20}}\s+{_GENERIC}[^\n.;]{{0,32}}?\b(?:backed?[- ]?ups?|kept|stored|saved|preserved|retained)\b"
    rf"|\b{_GENERIC}\s+backed?[- ]?ups?\b"
    rf"|\b{_GENERIC}\s+stored\s+(?:under|in|at)\b"
)

_NEG = r"(?:no|not|never|none|zero|without)"
_REMOVAL = r"(?:removed?|deleted?|dropped|purged|cleaned|erased|gone|removal|untouched)"
_PRE_REMOVAL = re.compile(
    rf"(?i)\b{_REMOVAL}\b"
    rf"(?:\s+(?:of|and|a|the|any|all|this|that|its|old|prior|previous|stale|retained))*"
    rf"\s*$"
)
_NEGATED_LEAD = re.compile(rf"(?i)(?:^|\s)(?:{_NEG}|\d+)\s+[^\s]{{0,40}}(?=\s*$|[\s,)])")

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

    A keep claim is a backup/rollback/pristine/pre-task family word adjacent to a retention verb inside one
    clause or a bare two-word claim ("backup kept"); quoting does not excuse it. A family lexeme that lives
    inside a path segment (adjacent to ``/`` or ```` ` ```, or starting the fragment like a filename) is
    locator-gated: it counts only with a family-or-verb word followed by ``at/under/in/on/to`` — a
    natural-language locator, not the path itself. A keep fragment isolated inside parentheses is negated by
    a removal verb in the following 120 chars of the original text ("(backup kept) and then deleted").
    Negation before the match, a removal elsewhere in the fragment, a removal named after a paren keep
    fragment, or a keep scoped by "nowhere" vetoes the claim.
    """
    for sentence in _sentences(text):
        match = _KEPT.search(sentence)
        if match is None:
            continue
        prefix = sentence[: match.start()]
        if _NEGATED_LEAD.search(prefix) and not re.search(rf"(?i)\b{_VERB}\b", prefix):
            continue
        if _PRE_REMOVAL.search(prefix):
            continue
        if re.search(rf"(?i)\b{_REMOVAL}\b", sentence[match.end():]):
            continue
        origin = _fragment_origin(text, sentence)
        if origin is not None:
            if re.search(
                rf"(?i)\b{_REMOVAL}\b",
                text[origin + match.end(): origin + match.end() + 120],
            ):
                continue
            if re.search(
                rf"(?i)\b{_NEG}\b[^\n.;]{{0,60}}?\b(?:backup|rollback|pristine|snapshot|cop(?:y|ies)|versions?|artifacts?)\b",
                sentence,
            ):
                continue
        span = match.group(0)
        locator = (
            rf"(?i)\b(?:{'|'.join(_FAM_ALT)}|{_VERB})\b[^\n.;]{{0,16}}\b(?:at|under|in|on|to)\b"
        )
        path_like = _path_like(sentence, span, match.start()) or bool(
            re.search(r"(?i)\bbefore[-_][\w.-]+", span)
        )
        if path_like and not re.search(locator, sentence):
            continue
        if _dropped_head(prefix) and not re.search(locator, sentence):
            continue
        if re.search(rf"(?i)\b{_VERB}\b[^\n.;]{{0,20}}\b(?:nowhere|nothing|nobody)\b", sentence):
            continue
        return True
    return False


def _fragment_origin(text: str, sentence: str) -> Optional[int]:
    parts = re.split(r"([()])", text)
    scan = 0
    in_paren = False
    for part in parts:
        if part == "(":
            in_paren = True
        elif part == ")":
            in_paren = False
        elif sentence in part and in_paren:
            return scan + part.index(sentence)
        scan += len(part)
    return None


def _dropped_head(prefix: str) -> bool:
    head = prefix.strip().rsplit(" ", 1)[-1].strip("'\"") if prefix.strip() else ""
    return bool(re.fullmatch(r"(?i)prior[-_ ]?state|before[-_][\w.-]+", head))


def _path_like(sentence: str, token: str, start: int) -> bool:
    """Whether a family lexeme in ``token`` is glued into a path or compound token.

    A family word joined to a path character or a ``-``/``_`` (``report/snapshot``,
    ``archive-backup``, ``*-snapshot``) is a filename or compound fragment, not a retained copy of
    prior state; it counts only with a natural-language locator (the caller's ``locator`` gate).
    Only the ``-``/``_``-joined run directly before the family word is followed back, so
    ``archive-backup`` is one token while ``pristine version`` stays two words.
    """
    for fam in re.finditer(rf"(?i)(?:{'|'.join(_FAM_ALT)})", token):
        absolute = start + fam.start()
        after = token[fam.end(): fam.end() + 1]
        seg = re.split(r"[\s/,;:()'\"`]", token[: fam.start()])[-1] or token[: fam.start()]
        head = re.search(r"[\w.~]*[-_][\w.~-]*$", seg)
        if head and head.group(0).endswith("-"):
            return True
        token_start = absolute - len(head.group(0)) if head else absolute
        j = token_start
        while j > 0 and (sentence[j - 1].isalnum() or sentence[j - 1] in "_.-"):
            j -= 1
        seg_before = sentence[j - 1: j] if j > 0 else ""
        seg_start = (j == 0) or seg_before in " \t/`("
        adjacent = (j < token_start and seg_before in "/`-") or (len(after) == 1 and after in "/`-")
        if seg_start and adjacent:
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
