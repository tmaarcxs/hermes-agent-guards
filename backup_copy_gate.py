"""Reject a handoff that declares a kept backup/rollback copy of prior state.

The owner forbids backup/rollback/snapshot artifacts of prior state anywhere
(rollback is git, registries or re-applying the prior value). This gate catches the handoff itself: a worker
``kanban_complete`` or
``kanban_request_review`` whose ``summary``, ``result`` or metadata values state
that a backup, rollback, pristine or before-copy was kept or preserved is
blocked with instructions to delete it.

Active only in worker setups (``kanban.default_assignee`` set; see
``kanban_policy``) and only in a worker process, mirroring ``completion_gate``.
Fails open.
"""

import re
from typing import Any, Optional

from . import kanban_policy
from .kanban_policy import RETRY_INSTRUCTION

_FAMILY = r"(?:backup|backups?|back-ups?|rollback|pre-task|pristine|snapshot|before[-_][\w.-]+|prior[-_]state)"
_VERB = (
    r"(?:kept|keep|preserv\w+|retain\w*|sav(?:e|ed|es|ing)|stor(?:e|ed|es|ing|age)"
    r"|backed?[- ]?ups?|backup(?:ped)?|backs?\s?ups?|left(?:\s+in\s+place)?|maintain\w*|hold(?:s|ing)?|archiv\w*)"
)
_GENERIC = r"(?:versions?|copies?|artifacts?|files?|states?|trees?|manifests?|scripts?|source|baseline)"
_QUALIFIER = r"(?:defective|old|prior|previous|failing|earlier|original)"

_FAM_ALT = ("backup", "backups", "back-ups", "rollback", "pre-task", "pristine", "snapshot",
            "before[-_][\\w.-]+", "prior[-_]state")

_KEPT = re.compile(
    rf"(?is)"
    rf"\b{_FAMILY}\b[^\n.;]{{0,32}}?\b{_VERB}\b"
    rf"|\b{_VERB}\b[^\n.;]{{0,32}}?\b{_FAMILY}\b"
    rf"|\b{_QUALIFIER}\s+\w{{0,20}}\s+{_GENERIC}[^\n.;]{{0,32}}?\b(?:backed?[- ]?ups?|kept|stored|saved|preserved|retained)\b"
    rf"|\b{_GENERIC}\s+backed?[- ]?ups?\b"
    rf"|\b{_GENERIC}\s+stored\s+(?:under|in|at)\b"
)

_NEG = r"(?:no|not|never|none|zero|without)"

_MESSAGE = (
    "agent-guards blocked the handoff: the summary or metadata keeps or preserves a backup/rollback/"
    "pristine/before copy of prior state. Rollback is git: delete the copy and, if needed, recover the "
    "prior state from git history (or the originating external system, e.g. the GitHub API), then re-run "
    "the handoff without the retained copy."
)


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[;.\n])\s+|[()]", text)
    return [part for part in (sentence.strip() for sentence in parts) if part]


def keeps_backup_copy(text: str) -> bool:
    """Whether any sentence claims a kept/preserved backup of prior state.

    Paren-split fragments keep their offset into the original text so a
    removal verb landing outside an isolated fragment ('(backup kept) and
    then deleted ...') still negates the bare keep claim.

    Vetoes are scoped narrow: the snapshot consumption/parity/render/review
    window veto applies only when the matched family word itself is
    'snapshot' and no retention verb hugs it (a retention verb inside or
    right after the snapshot token stays a real claim); the cross-fragment
    removal and negation windows only apply when the match is itself a
    paren fragment, so a removal promised or performed elsewhere never
    erases a live keep claim.
    """
    for sentence in _sentences(text):
        match = _KEPT.search(sentence)
        if not match:
            continue
        origin = None
        scan = 0
        in_paren = False
        frag_paren = False
        for part in re.split(r"(?<=[;.\n])\s+|([()])", text):
            if part is None:
                continue
            if part == "(":
                in_paren = True
                scan += len(part)
                continue
            if part == ")":
                in_paren = False
                scan += len(part)
                continue
            stripped = part.strip()
            if stripped == sentence and (origin is None or scan <= origin):
                origin = scan + (part.index(sentence) if sentence in part else 0)
                frag_paren = in_paren
            scan += len(part)
        offset = origin if origin is not None else 0
        if re.search(rf"(?i)\b{_NEG}\b[^\n.;]{{0,60}}$", sentence[: match.start()]):
            continue
        if re.search(r"(?i)\b(?:removed?|deleted?|dropped|cleaned?|purged|erased|gone)\b", sentence[match.end():]):
            continue
        if frag_paren and re.search(r"(?i)\b(?:removed?|deleted?|dropped|cleaned?|purged|erased|gone)\b", text[offset + match.end(): offset + match.end() + 120]):
            continue
        if frag_paren and re.search(
            rf"(?i)\b{_NEG}\b[^\n.;]{{0,60}}?\b(?:backup|rollback|pristine|snapshot|cop(?:y|ies)|versions?|artifacts?)\b",
            sentence,
        ):
            continue
        span = sentence[max(0, match.start() - 40): match.end() + 40]
        span_off = max(0, match.start() - 40)
        token = match.group(0)
        snapped = list(re.finditer(r"(?i)\bsnapshot\b", token))
        family_snapshot = bool(snapped)
        match_keeps = any(
            re.search(
                r"(?i)\b(?:kept|keep|preserv\w*|retain\w*|sav\w+|stor(?:e|ed|es|ing|age)|backed?[- ]?ups?|backup(?:ped)?|backs?\s?ups?|left(?:\s+in\s+place)?|maintain\w*|hold(?:s|ing)?|archiv\w*)\b",
                snippet,
            )
            is not None
            for snippet in (
                token[max(0, s_.start() - 4): s_.end() + 10] for s_ in snapped
            )
        )
        consumed = False
        if family_snapshot and not match_keeps:
            for word in re.finditer(r"(?i)\b(?:consum\w+|parity|render\w*|reviewed|verified against|replay\w*|preview)\b", span):
                abs_pos = span_off + word.start()
                word_before = sentence[max(0, abs_pos - 4): abs_pos]
                if abs_pos > match.end() and re.search(r"(?i)(?:[/\\]|\bat\b|\bin\b|\bunder\b|\bto\b)\W{0,2}$", word_before):
                    continue
                consumed = True
        if consumed:
            continue
        start = match.start()
        path_like = False
        for fam in re.finditer(rf"(?i)(?:{'|'.join(_FAM_ALT)})", token):
            absolute = start + fam.start()
            after = token[fam.end(): fam.end() + 1]
            j = absolute
            while j > 0 and (sentence[j - 1].isalnum() or sentence[j - 1] in "_.-"):
                j -= 1
            seg_before = sentence[j - 1: j] if j > 0 else ""
            seg_start = (j == 0) or seg_before in " \t/`"
            adjacent = (j < absolute and seg_before in "/`") or (len(after) == 1 and after in "/`")
            if seg_start and adjacent:
                path_like = True
        if path_like:
            locator_runs = list(re.finditer(
                rf"(?i)\b(?:{'|'.join(_FAM_ALT)}|{_VERB[3:-1]})\b[^\n.;]{{0,16}}\b(?:at|under|in|on|to)\b",
                sentence))
            if not locator_runs:
                continue
        if re.search(rf"(?i)^\W*{_NEG}\b[^\n.;]{{0,80}}\b{_FAMILY}\b", sentence):
            continue
        if re.search(r"(?i)\b(?:kept|preserv\w+|retain\w+|stor\w+|sav\w+)\b[^\n.;]{0,20}\b(?:nowhere|nothing|nobody)\b", sentence):
            continue
        if re.search(rf"(?i)\b{_NEG}\b[^\n.;]{{0,12}}\b(?:kept|preserv\w+|retain\w+|stor\w+|sav\w+)\b", sentence):
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
