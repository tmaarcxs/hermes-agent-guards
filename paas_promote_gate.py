"""Block a paas-promote dispatch to des that lacks the auto-approval label.

Applies in every process (lead and workers): a ``terminal`` command whose statement
dispatches the ``paas-promote`` workflow (``gh workflow run paas-promote`` or
``paas-promote.yml``) and whose inputs name ``des`` while ``LABELS`` does not contain
``auto-approval`` is blocked. The motivating incident: a dispatch without the label
produced a deployment pull request that stayed open for 45 minutes because only
``auto-approval`` makes the bot merge it, and nobody noticed because the worker only
heartbeated.

Inputs are read from ``-f``/``--field`` KEY=VALUE flags (value single-quoted or bare)
and from leading environment assignments of the dispatch statement
(``SUPRAENVS=des gh ...``; ``env``-wrapped dispatches are out of scope). ``SUPRAENVS``
is the comma/space-separated target list and defaults to ``des`` when absent or empty,
so the incident shape is blocked without naming it; a PRE-only or PRO-only dispatch is
a different promotion path and passes. ``LABELS`` items are compared case-insensitively
as whole tokens, so ``auto-approval,x`` or any case variant passes while a lookalike
substring inside another token does not. The block message names the missing label and
gives the exact corrected ``-f LABELS=...`` flag built from the dispatch's own value;
later flags win on a duplicated key.

Statements are the text between ``&&``, ``;``, ``|``, newlines and ``(``, stripped; a
statement blocks only when its head, after optional ``KEY=VALUE`` assignments, is the
dispatch, so ``echo``ing the workflow name or mentioning it inside a quoted string
passes. Deliberately out of scope (it catches slips, not deliberate evasion): shell
expansion in any value or outside quotes (``$``, backticks), unbalanced quotes, and
dispatches wrapped in ``env`` or another program — those fail open, as does any
unexpected parsing error.
"""

import re
from typing import Optional

LABEL = "auto-approval"
DEFAULT_ENV = "des"

MESSAGE = (
    "agent-guards blocked terminal: a paas-promote dispatch that targets des without the "
    "'auto-approval' label produces a deployment pull request that stays open until someone "
    "notices — only 'auto-approval' makes the bot merge it (the release path always carries "
    "it). Add the label to the dispatch: {flag}."
)

_SINGLE = "\x01"
_DOUBLE = "\x02"
_QUOTED = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")
_DYNAMIC = re.compile(r"[$`]")
_STATEMENTS = re.compile(r"&&|[;&|\n(]")
_LEAD = re.compile(rf"(?:[A-Za-z_][A-Za-z0-9_]*=(?:\x01[^\x01]*\x01|\x02[^\x02]*\x02|[^\s;&|]*)[ \t]+)*")
_ASSIGN = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=([^\s;&|]*)")
_FIELD = re.compile(r"(?:-f|--field)[ =]([A-Za-z_][A-Za-z0-9_]*)=(\x01[^\x01]*\x01|\x02[^\x02]*\x02|[^\s;&|]*)")
_BLOB_FIELD = re.compile(r"(?:-f|--field)[ =](\x01[^\x01]*\x01|\x02[^\x02]*\x02)")
_DISPATCH = re.compile(r"gh\s+workflow\s+run\s+(?:\S+[ \t]+)*?paas-promote(?:\.ya?ml)?(?![\w.-])")


class _Unanalyzable(Exception):
    """A command shape this gate deliberately does not interpret."""


def _mask(command: str) -> str:
    """Quoted spans wrapped in sentinels so shell syntax inside them cannot act as code."""
    opener = lambda match: (_SINGLE if match.group(0)[0] == "'" else _DOUBLE) + match.group(0)[1:-1] + (_SINGLE if match.group(0)[0] == "'" else _DOUBLE)
    return _QUOTED.sub(opener, command)


def _unmask(raw: str) -> str:
    """The literal value of a sentinel-wrapped or bare token, before expansion checks."""
    if raw[:1] in (_SINGLE, _DOUBLE) and raw[-1:] == raw[:1]:
        return raw[1:-1]
    return raw


def _decode(raw: str) -> str:
    """The literal value of one field: strip sentinels, reject expansion shapes.

    An empty value means the flag carried none (``-f LABELS=``): treated as empty,
    not as a parse failure.
    """
    value = _unmask(raw)
    if raw.startswith(_DOUBLE) and _DYNAMIC.search(value):
        raise _Unanalyzable("shell expansion inside the double-quoted value")
    if not raw.startswith((_SINGLE, _DOUBLE)) and _DYNAMIC.search(value):
        raise _Unanalyzable("shell expansion in an unquoted value")
    return value


def _inputs(masked: str) -> dict[str, str]:
    """Every KEY=VALUE input of the dispatch: leading assignments, then ``-f`` flags."""
    fields: dict[str, str] = {}
    lead = _LEAD.match(masked)
    if lead:
        for key, raw in _ASSIGN.findall(lead.group(0)):
            fields[key.upper()] = _decode(raw)
    quoted_masked = _BLOB_FIELD.sub(" ", masked)
    for key, raw in _FIELD.findall(quoted_masked):
        fields[key.upper()] = _decode(raw)
    for blob in _BLOB_FIELD.findall(masked):
        pair = _decode(blob)
        name, sep, value = pair.partition("=")
        if sep and name.isidentifier():
            fields[name.upper()] = value
    return fields


def _decision(fields: dict[str, str]) -> Optional[str]:
    raw_envs = fields.get("SUPRAENVS", "").strip()
    envs = [item.lower() for item in re.split(r"[,\s]+", raw_envs) if item] if raw_envs else [DEFAULT_ENV]
    if "des" not in envs:
        return None
    labels = [item.lower() for item in re.split(r"[,\s]+", fields.get("LABELS", "")) if item]
    if LABEL in labels:
        return None
    given = fields.get("LABELS", "").strip()
    corrected = f"{given},{LABEL}" if given else LABEL
    return MESSAGE.format(flag=f"-f LABELS={corrected}")


def _statement_block(statement: str) -> Optional[str]:
    masked = _mask(statement.strip())
    stripped_of_quotes = re.sub(r"[\x01\x02]", "", masked)
    if "'" in stripped_of_quotes or '"' in stripped_of_quotes:
        raise _Unanalyzable("unbalanced quote")
    if _DYNAMIC.search(stripped_of_quotes):
        raise _Unanalyzable("shell expansion outside a quoted value")
    lead = _LEAD.match(masked)
    if not _DISPATCH.match(masked, len(lead.group(0)) if lead else 0):
        return None
    return _decision(_inputs(masked))


def check(tool_name: str, args) -> Optional[str]:
    """``pre_tool_call``: block the incident dispatch shape, fail open on everything else."""
    if tool_name != "terminal" or not isinstance(args, dict):
        return None
    try:
        for statement in _STATEMENTS.split(str(args.get("command") or "")):
            blocked = _statement_block(statement)
            if blocked:
                return blocked
    except _Unanalyzable:
        return None
    except Exception:
        return None
    return None
