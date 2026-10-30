"""text-guards: cut the forbidden characters out of anything the agent emits.

The operator forbids the section sign and the em dash. Two seams cover every path where text
leaves the agent:

- ``transform_llm_output`` cleans the final reply, before delivery and before the assistant row
  is persisted.
- ``pre_tool_call`` rewrites the tool arguments, so a comment body, a file written by
  ``write_file``/``patch``, or a shell command never carries the characters either.

Both replace each occurrence, with its surrounding spaces, by a single space, and both leave
everything else alone. Any error lets the original value through.
"""

import re

FORBIDDEN = "\u00a7\u2014"

_PATTERN = re.compile(r"[ \t]*[" + FORBIDDEN + r"][ \t]*")


def clean(text):
    """Return ``text`` with every forbidden character replaced by a single space."""
    return _PATTERN.sub(" ", text)


def sanitize(value):
    """Walk strings inside ``value`` (dicts, lists, scalars) and clean each one."""
    if isinstance(value, str):
        return clean(value) if _PATTERN.search(value) else value
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, dict):
        return {key: sanitize(item) for key, item in value.items()}
    return value


def strip_forbidden(response_text=None, **_kwargs):
    """``transform_llm_output``: return the cleaned reply, or ``None`` to leave it untouched.

    ``None`` rather than an unchanged copy: a non-empty return replaces the reply, so echoing it
    back would take the slot from any later plugin.
    """
    if not isinstance(response_text, str) or not _PATTERN.search(response_text):
        return None
    return clean(response_text)


def rewrite_arguments(tool_name="", args=None, **_kwargs):
    """``pre_tool_call``: hand the host cleaned arguments when any string carries the character."""
    if not isinstance(args, dict):
        return None
    cleaned = sanitize(args)
    return {"action": "modify", "args": cleaned} if cleaned != args else None


def register(ctx) -> None:
    ctx.register_hook("transform_llm_output", strip_forbidden)
    ctx.register_hook("pre_tool_call", rewrite_arguments)


__all__ = ["register", "clean", "sanitize", "rewrite_arguments", "strip_forbidden"]
