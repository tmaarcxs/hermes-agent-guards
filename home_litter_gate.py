"""Block Kanban workers from creating new top-level entries directly under ``~/``.

Marcos found litter directly in his home dir (``~/kanban.db``, ``~/kanban``,
``~/scratch``, ``~/wt``, ``~/ad-hoc`` …) written by agent/worker processes. The
workspace fence in :mod:`workspace_write_gate` guards the live Hermes home and
the task workspace, but a command that runs with its cwd at the real home — a
bare ``sqlite3 kanban.db``, a ``mkdir scratch``, a ``> ~/note`` redirect — lands
a brand-new entry at the top of ``~`` and nothing stops it.

This gate blocks ``terminal``/``write_file``/``patch`` calls from worker
processes when the write target would create a **not-yet-existing top-level
entry under the real home**. That includes a direct child (``mkdir scratch``)
and a nested target whose first component under ``~`` does not exist yet
(``mkdir -p scratch/f3`` from cwd ``~`` creates ``~/scratch`` too). It is
deliberately narrow:

- An entry that already exists is allowed (edits to ``~/.config``,
  ``~/git_repos`` and every existing file keep working).
- Dotted names are allowed (tool dirs: ``~/.cache``, ``~/.codegraph`` …).
- ``git_repos``, ``Downloads``, ``Documents`` and ``Desktop`` are allowed as
  creation targets.
- ``~/git_repos/**`` and ``~/.hermes/**`` are never direct children of ``~``,
  so they are out of scope by construction.

The lead is never affected; only worker processes (``HERMES_KANBAN_TASK`` set)
are checked. Unknown command shapes fail open.
"""

import os
import re
from pathlib import Path
from typing import Any, Optional

from . import workspace_write_gate as _wwg

ALLOWED_NEW_TOP_LEVEL = frozenset({"git_repos", "Downloads", "Documents", "Desktop"})
"""Non-dotted top-level entries a worker may create under the real home.

Anything else that does not exist yet is litter.
"""

_MESSAGE = (
    "agent-guards blocked {tool} [home_litter_gate]: this would create a new "
    "top-level entry directly under the home directory ({entry}). Workers must "
    "not litter ~/. Keep work inside the task workspace (HERMES_KANBAN_WORKSPACE), "
    "the Hermes scratch dir (~/.hermes/cache/scratch), TMPDIR/tmp or the board's "
    "kanban attachments directory; repo work goes in "
    "<main-repo>/.worktrees/<task>/ or ~/git_repos/ad-hoc/<task>/, and the kanban "
    "DB lives at ~/.hermes/kanban.db (never a bare relative 'kanban.db')."
)

_SQLITE3 = re.compile(r"\bsqlite3\s+(?:-\S+\s+)*(?P<path>\S+)")
"""``sqlite3 <db>`` creates the file when missing; the shared target extractor
does not treat it as a write head."""

_CREATE_HEADS = ("cp", "mv", "rsync", "install", "touch", "mkdir", "tee")
"""Creation-capable shell heads only.

``rm``/``rmdir`` are excluded on purpose: deleting an entry that does not exist
is a no-op, not litter, and the lead's own cleanup uses ``rm -rf``.
"""
_SED_IN_PLACE = re.compile(r"(?:^|\s)-i(?:\s|$)|--in-place")
_REDIRECT = re.compile(r"(?:>>|>)\s*(\S+)")
_FD_REDIRECT = re.compile(r"^\d?&[\d()]*$|^\d?&|^[&]|\)$")
_PY_CREATE = re.compile(
    r"\b(?:makedirs|mkdir|copytree|move|rename|replace)\s*\(|"
    r"\bopen\s*\([^()]*,\s*['\"][waxu+]|"
    r"\b(?:write_text|write_bytes)\s*\(|\brates?_file\(|\bto_csv\s*\(|\bto_json\s*\(|\bsavefig\s*\(|\bdump\s*\("
)
_PY_STRINGS = re.compile(r"(['\"])(.*?)\1")
_DOWNLOAD = re.compile(r"\b(?:curl|wget)\b[^\n;|&]*?\s(?:-o|-O|--output|-O\s)(?:\s+)?(?P<path>\S+)")
"""``curl -o <file>`` / ``wget -O <file>`` create the output file; covered here
because the shared target extractor does not treat them as write heads."""


def _home() -> Path:
    return _wwg._home()


def _new_top_level_entry(path: str) -> Optional[str]:
    """Return the offending entry name when *path* would create a not-yet-existing
    top-level entry under the real home (and it is not allowlisted), else ``None``.

    The check is on the first component under the home, not only a direct child:
    ``mkdir -p scratch/f3`` from cwd ``~`` creates ``~/scratch`` as well, so the
    absent first component is the litter. A path whose first component already
    exists (``~/git_repos/newrepo``, ``~/.cache/x``) stays allowed.
    """
    candidate = _wwg._normalize(path)
    if not candidate:
        return None
    try:
        resolved = Path(candidate).expanduser().resolve(strict=False)
    except (OSError, ValueError):
        return None
    home = _home()
    if resolved == home or home not in resolved.parents:
        return None
    name = resolved.relative_to(home).parts[0]
    if not name or name.startswith("."):
        return None
    if name in ALLOWED_NEW_TOP_LEVEL:
        return None
    try:
        if (home / name).exists():
            return None
    except OSError:
        return None
    return name


def _creation_targets(cwd: str, segment: str) -> list[str]:
    """Shell write targets that CREATE a path (never ``rm``/``rmdir``)."""
    unquoted = re.sub(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"", "''", segment)
    targets: list[str] = []
    for match in _REDIRECT.finditer(unquoted):
        if not _FD_REDIRECT.match(match.group(1)):
            targets.append(_wwg._join(cwd, match.group(1)))
    for match in re.finditer(r"\btee\b(?:\s+-\S+)*\s+(\S+)", unquoted):
        targets.append(_wwg._join(cwd, match.group(1)))
    tokens = _wwg._tokens(segment)
    for index, head in enumerate(tokens):
        if head.lower() in _CREATE_HEADS:
            operands = [dest for dest in tokens[index + 1:] if not dest.startswith("-")]
            if not operands:
                break
            for dest in operands if head.lower() in ("mkdir", "touch") else operands[-1:]:
                targets.append(_wwg._join(cwd, dest))
            break
        if head == "sed":
            if _SED_IN_PLACE.search(" ".join(tokens)):
                for dest in tokens[index + 2:]:
                    if not dest.startswith("-"):
                        targets.append(_wwg._join(cwd, dest))
            break
    for match in _SQLITE3.finditer(unquoted):
        dest = match.group("path")
        if not dest.startswith("-"):
            targets.append(_wwg._join(cwd, dest))
    for match in _DOWNLOAD.finditer(unquoted):
        dest = match.group("path")
        if not dest.startswith("-"):
            targets.append(_wwg._join(cwd, dest))
    return targets


def _py_creation_literals(code: str) -> list[str]:
    literals: list[str] = []
    for match in _PY_CREATE.finditer(code):
        depth = 1
        body: list[str] = []
        for char in code[match.start():match.start() + 200]:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    break
            body.append(char)
        text = "".join(body)
        if "expanduser(" in text:
            text = text[text.index("expanduser(") + len("expanduser("):]
        literals.extend(literal for _, literal in _PY_STRINGS.findall(text))
    return literals


def _offending(targets: list[str]) -> Optional[str]:
    for target in targets:
        if not target:
            continue
        entry = _new_top_level_entry(target)
        if entry:
            return entry
    return None


def terminal_violations(command: str) -> list[str]:
    blanked = _wwg._strip_heredoc_bodies(command)
    for cwd, segment in _wwg._segments(blanked):
        entry = _offending(_creation_targets(cwd, segment))
        if entry:
            return [_MESSAGE.format(tool="terminal", entry="~/" + entry)]
    for body in _wwg._script_bodies(command):
        entry = _offending(_py_creation_literals(body))
        if entry:
            return [_MESSAGE.format(tool="terminal", entry="~/" + entry)]
    return []


def check(tool_name: str, args: Any) -> Optional[str]:
    if not _wwg.is_worker() or not isinstance(args, dict):
        return None
    if tool_name in ("write_file", "patch"):
        path = str(args.get("path") or "")
        workspace = (os.environ.get("HERMES_KANBAN_WORKSPACE") or "").strip()
        if path and workspace and not path.startswith(("/", "~", "$")):
            path = workspace.rstrip("/") + "/" + path
        entry = _new_top_level_entry(path)
        return _MESSAGE.format(tool=tool_name, entry="~/" + entry) if entry else None
    if tool_name != "terminal":
        return None
    found = terminal_violations(str(args.get("command") or ""))
    return found[0] if found else None
