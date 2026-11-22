"""text-guards: keep the em dash and the section sign out of the reply.

The operator forbids both characters in anything the agent says. ``transform_llm_output`` rewrites
the final reply before delivery and before the assistant row is persisted, so the cleaned text is
the text the user sees and the text replayed next turn.

Only the reply is rewritten, never a tool argument. An earlier version also cleaned arguments, and
a ``patch`` or ``write_file`` call that carried the character as the content of an existing file no
longer matched or silently lost it. Fenced blocks and inline code spans are data and are left
alone for the same reason.

Rewrites: a spaced em dash becomes a comma, an em dash that opens a line becomes a hyphen bullet,
an em dash between digits becomes a hyphen, an em dash before closing punctuation or a line end is
dropped, and the section sign is replaced by a single space. Commentary sent while a turn is still
running never reaches ``transform_llm_output``; only a plugin that registers the hook with
``hold_stream=True`` would cover it, at the cost of streaming.
"""

import re

EM_DASH = "\u2014"
SECTION_SIGN = "\u00a7"

_CODE = re.compile(r"```.*?```|`[^`\n]*`", re.S)
_DASH = re.compile(r"[ \t]*" + EM_DASH + r"[ \t]*")
_LINE_DASH = re.compile(r"(?m)^([ \t]*)" + EM_DASH + r"[ \t]*")
_SECTION = re.compile(r"[ \t]*" + SECTION_SIGN + r"+[ \t]*")
_CLOSERS = ".,;:!?)]}"
_MASKS = "\ue000\ue001\ue002"


def _cjk(char):
    code = ord(char) if char else 0
    return 0x2E80 <= code <= 0x9FFF or 0xAC00 <= code <= 0xD7AF or 0xFF00 <= code <= 0xFFEF


def _dash(match):
    text, start, end = match.string, match.start(), match.end()
    before = text[start - 1] if start else ""
    after = text[end] if end < len(text) else ""
    if before.isdigit() and after.isdigit() and match.group(0) == EM_DASH:
        return "-"
    if not after or after == "\n" or after in _CLOSERS or before == ",":
        return ""
    if _cjk(before) or _cjk(after):
        return ""
    return ", "


def _section(match):
    text, start = match.string, match.start()
    return "" if not start or text[start - 1] == "\n" else " "


def clean(text):
    """Return ``text`` with both forbidden characters removed outside code, else ``text`` itself.

    Code spans are masked with placeholders and cleaned prose is stitched back around them, so a
    span boundary is never mistaken for the start of a line.
    """
    if EM_DASH not in text and SECTION_SIGN not in text:
        return text
    mark = next((c for c in _MASKS if c not in text), None)
    if mark is None:
        return text
    spans = []

    def _mask(match):
        spans.append(match.group(0))
        return f"{mark}{len(spans) - 1}{mark}"

    masked = _CODE.sub(_mask, text)
    cleaned = _SECTION.sub(_section, _DASH.sub(_dash, _LINE_DASH.sub(r"\1- ", masked)))
    return re.sub(f"{mark}(\\d+){mark}", lambda m: spans[int(m.group(1))], cleaned)


def strip_forbidden(response_text=None, **_kwargs):
    """``transform_llm_output``: the cleaned reply, or ``None`` to leave it untouched.

    ``None`` rather than an unchanged copy: a non-empty return replaces the reply, so echoing it
    back would take the slot from any later plugin.
    """
    if not isinstance(response_text, str):
        return None
    cleaned = clean(response_text)
    return cleaned if cleaned != response_text else None


def register(ctx) -> None:
    ctx.register_hook("transform_llm_output", strip_forbidden)


__all__ = ["register", "clean", "strip_forbidden"]
