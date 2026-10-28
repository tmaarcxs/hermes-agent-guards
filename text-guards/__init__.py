"""text-guards: cut the forbidden section-sign character out of the final reply.

The operator forbids the section sign in anything the agent writes. One hook does one thing:
``transform_llm_output`` returns the reply with every occurrence replaced by a single space,
before delivery and before the assistant row is persisted. Nothing else is touched — no tool
blocking, no argument rewriting, no other text normalization — and any error lets the reply
through unchanged.
"""

import re

FORBIDDEN = "\u00a7"

_PATTERN = re.compile(r"[ \t]*" + FORBIDDEN + r"[ \t]*")


def strip_forbidden(response_text, **_kwargs):
    """Return ``response_text`` without the forbidden character, or ``None`` to leave it alone.

    ``None`` (not the unchanged string) is what the hook contract needs: a non-empty return
    value replaces the reply, so passing an unchanged copy back would take the slot from any
    later plugin. A single space stays where the character was, which keeps words separated.
    """
    if not isinstance(response_text, str) or FORBIDDEN not in response_text:
        return None
    return _PATTERN.sub(" ", response_text)


def register(ctx) -> None:
    ctx.register_hook("transform_llm_output", strip_forbidden)


__all__ = ["register", "strip_forbidden"]
