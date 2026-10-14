"""Reject a handoff that declares a kept backup/rollback copy of prior state.

The owner forbids backup/rollback/snapshot artifacts of prior state anywhere
(rollback is git, registries or re-applying the prior value). Cards t_00374bb1
and t_541fe4f0 still delivered "rollback copy preserved" notes, so the failure
repeats. This gate catches the handoff itself: a worker ``kanban_complete`` or
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

# The violation is a KEEP/PRESERVE claim over a backup-class artifact:
#   "backup kept", "rollback copy preserved", "pristine tree retained",
#   "before-worker-manifest.py preserved", "defective versions backed up".
# Merely naming a backup ("no backup copies were kept", "rollback via git")
# without the artifact claim stays allowed.
_FAMILY = r"(?:backup|backups?|back-ups?|rollback|pre-task|pristine|snapshot|before[-_][\w.-]+|prior[-_]state)"
_VERB = (
    r"(?:kept|keep|preserv\w+|retain\w*|sav(?:e|ed|es|ing)|stor(?:e|ed|es|ing|age)"
    r"|backed?[- ]?ups?|backup(?:ped)?|backs?\s?ups?|left(?:\s+in\s+place)?|maintain\w*|hold(?:s|ing)?|archiv\w*)"
)
_GENERIC = r"(?:versions?|copies?|artifacts?|files?|states?|trees?|manifests?|scripts?|source|baseline)"
_QUALIFIER = r"(?:defective|old|prior|previous|failing|earlier|original)"

_KEPT = re.compile(
    rf"(?is)"
    rf"\b{_FAMILY}\b[^\n.;]{{0,48}}?\b{_VERB}\b"
    rf"|\b{_VERB}\b[^\n.;]{{0,48}}?\b{_FAMILY}\b"
    rf"|\b{_QUALIFIER}\s+\w{{0,20}}\s+{_GENERIC}[^\n.;]{{0,48}}?\b(?:backed?[- ]?ups?|kept|stored|saved|preserved|retained)\b"
    rf"|\b{_GENERIC}\s+backed?[- ]?ups?\b"
    rf"|\b{_GENERIC}\s+stored\s+(?:under|in|at)\b"
)

_MESSAGE = (
    "agent-guards blocked the handoff: the summary or metadata keeps or preserves a backup/rollback/"
    "pristine/before copy of prior state. Rollback is git: delete the copy and, if needed, recover the "
    "prior state from git history (or the originating external system, e.g. the GitHub API), then re-run "
    "the handoff without the retained copy."
)


def _sentences(text: str) -> list[str]:
    # Literal parentheses are segment boundaries too: a keep claim inside a
    # "(backup kept)" paren must not stay glued to a preceding removal clause.
    parts = re.split(r"(?<=[;.\n])\s+|[()]", text)
    return [part for part in (sentence.strip() for sentence in parts) if part]


def keeps_backup_copy(text: str) -> bool:
    for sentence in _sentences(text):
        match = _KEPT.search(sentence)
        if not match:
            continue
        # Drop only sentence-scoped negation evidence: a negator immediately before
        # the claim ("no backup copies were kept"), a negator scoped to the artifact
        # noun, or a removal verb AFTER the claim ("backed up, then deleted").
        # A removal BEFORE the claim must not neutralize it: "test files removed,
        # backup kept" and "purged 77 entries (backup kept)" still keep the copy.
        if re.search(rf"(?i)\b(?:no|not|never|none|without|nor|neither)\b[^\n.;]{{0,60}}$", sentence[: match.start()]):
            continue
        if re.search(r"(?i)\b(?:removed?|deleted?|dropped|cleaned?|purged|erased|gone)\b", sentence[match.end() :]):
            continue
        if re.search(
            r"(?i)\b(?:no|not|never|none)\b[^\n.;]{0,60}?\b(?:backup|rollback|pristine|snapshot|cop(?:y|ies)|versions?)\b",
            sentence,
        ):
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
