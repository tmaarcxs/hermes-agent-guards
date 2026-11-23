"""Clip runaway single lines in successful terminal output before core truncation.

A single minified JS/CSS line, base64 source map, or multi-kilobyte ``ps`` command line
can blow past Hermes core's capture window and trigger head+tail truncation that hides
useful surrounding output, while adding pure noise to every subsequent turn's context.

This transform runs on ``transform_terminal_output`` (before core truncation), fails
open, and stays conservative so structured outputs and deliberate reads are never
corrupted:

- skips non-zero exit codes (error diagnostics and tracebacks stay verbatim);
- skips commands that explicitly slice by characters or bytes (``cut -c``, ``head -c``,
  ``tail -c``);
- skips pure JSON documents and individual complete JSON object/array lines;
- applies a tighter threshold (head 360 + tail 80, clipping lines > 560 chars) to
  search/process/env commands (``grep``, ``rg``, ``ps``, ``pgrep``, ``lsof``, ``env``)
  checked outside heredoc bodies, and a wider threshold (head 800 + tail 120, clipping
  lines > 1040 chars) to all other successful outputs.
"""

import re
from typing import Optional

_SEARCH_OR_PROC = re.compile(
    r"(?:^|[;&|\n]\s*)(?:grep|egrep|fgrep|rg|ps|pgrep|lsof|env|printenv)(?=[\s;&|]|$)"
)
_EXPLICIT_CHAR_SLICE = re.compile(
    r"(?:^|[;&|\n]\s*)(?:cut\s+[^;&|\n]*-c|(?:head|tail)\s+[^;&|\n]*-c)(?=[\s0-9]|$)"
)
_MIN_NET_SAVING = 120
_TIGHT_HEAD, _TIGHT_TAIL = 360, 80
_WIDE_HEAD, _WIDE_TAIL = 800, 120


def _command_header(command: str) -> str:
    """Return the part of *command* before any heredoc body so embedded script text does not
    masquerade as a top-level ``grep``/``ps`` or ``cut -c`` command."""
    if "<<" not in command:
        return command
    return command.splitlines()[0]


def _is_complete_json_shape(text: str) -> bool:
    """True when trimmed *text* looks like a single complete JSON object or array."""
    stripped = text.strip()
    if len(stripped) < 2:
        return False
    return (stripped[0] == "{" and stripped[-1] == "}") or (
        stripped[0] == "[" and stripped[-1] == "]"
    )


def clip(output: object, command: str = "", returncode: Optional[int] = 0) -> Optional[str]:
    """Return *output* with runaway lines clipped, or ``None`` when untouched."""
    if not isinstance(output, str) or returncode not in (0, None):
        return None
    header = _command_header(command or "")
    if _EXPLICIT_CHAR_SLICE.search(header):
        return None
    tight = bool(_SEARCH_OR_PROC.search(header))
    head, tail = (_TIGHT_HEAD, _TIGHT_TAIL) if tight else (_WIDE_HEAD, _WIDE_TAIL)
    kept = head + tail
    min_line = kept + _MIN_NET_SAVING
    if len(output) <= min_line or _is_complete_json_shape(output):
        return None

    changed = False
    rebuilt = []
    for raw_line in output.splitlines(keepends=True):
        if raw_line.endswith("\r\n"):
            eol = "\r\n"
            body = raw_line[:-2]
        elif raw_line.endswith("\n"):
            eol = "\n"
            body = raw_line[:-1]
        else:
            eol = ""
            body = raw_line
        if len(body) > min_line and not _is_complete_json_shape(body):
            dropped = len(body) - kept
            rebuilt.append(f"{body[:head]} ...[+{dropped} chars clipped]... {body[-tail:]}{eol}")
            changed = True
        else:
            rebuilt.append(raw_line)
    return "".join(rebuilt) if changed else None
