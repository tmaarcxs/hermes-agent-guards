"""Block Kanban workers from creating new top-level entries directly under ``~/``.

Marcos found litter directly in his home dir (``~/kanban.db``, ``~/kanban``,
``~/scratch``, ``~/wt``, ``~/ad-hoc`` …) written by agent/worker processes. The
workspace fence in :mod:`workspace_write_gate` guards the live Hermes home and
the task workspace, but a command that runs with its cwd at the real home — a
bare ``sqlite3 kanban.db``, a ``mkdir scratch``, a ``> ~/note`` redirect — lands
a brand-new entry at the top of ``~`` and nothing stops it.

This gate blocks ``terminal``/``write_file``/``patch`` calls from worker
processes when the write target would create a **not-yet-existing top-level
entry under the real home**. That includes a direct child (``mkdir scratch``),
a nested target whose first component under ``~`` does not exist yet
(``mkdir -p scratch/f3`` from cwd ``~`` creates ``~/scratch`` too), and a
copy-style head (``cp``/``mv``/``rsync``/``install``) whose destination is ``~``
itself — ``cp a.md ~/`` lands ``~/a.md``. It is deliberately narrow:

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
    "agent-guards blocked {tool}: this would create a new "
    "top-level entry under the home directory ({entry}). Workers must "
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

_COPY_HEADS = ("cp", "mv", "rsync", "install")
"""Copy-style heads. ``cp <src> ~/`` lands ``~/<basename(src)>`` — a brand-new
top-level entry — while the destination resolves to the home itself, so the
per-target check sees no child to name. The sources are re-expressed as home
children so the same create/allow decision applies.
"""
_REDIRECT = re.compile(r"(?:>>|>)\s*(\S+)")
_FD_REDIRECT = re.compile(r"^\d?&[\d()]*$|^\d?&|^[&]|\)$")
_PY_CREATE = re.compile(
    r"\b(?:makedirs|mkdir|copytree|copyfile|copy|move|rename|replace)\s*\(|"
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


def _copy_into_home_children(cwd: str, sources: list[str], dest: str) -> list[str]:
    """Children ``cp``/``mv``/``rsync``/``install`` would create when the
    destination is the home directory itself.

    ``cp a.md ~/`` writes ``~/a.md``; the destination resolves to ``~`` so the
    per-target check has no child to name. Re-express each source as its
    would-be home child so the same create/allow decision applies. Unknowable
    shapes (globs, variables, ``.``/``..``) fail open.
    """
    home = _home()
    joined = _wwg._join(cwd, dest)
    if not joined:
        return []
    try:
        resolved = Path(joined).expanduser().resolve(strict=False)
    except (OSError, ValueError):
        return []
    if resolved != home:
        return []
    children: list[str] = []
    for source in sources:
        name = source.rstrip("/").rsplit("/", 1)[-1].strip("\"'")
        if not name or name in (".", "..") or name.startswith("$") or "*" in name or "?" in name:
            continue
        children.append(str(home / name))
    return children


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
            if head.lower() in ("mkdir", "touch"):
                for dest in operands:
                    targets.append(_wwg._join(cwd, dest))
            else:
                dest = operands[-1]
                targets.append(_wwg._join(cwd, dest))
                if head.lower() in _COPY_HEADS:
                    targets.extend(_copy_into_home_children(cwd, operands[:-1], dest))
            break
        if head == "sed":
            for dest in _sed_destinations(tokens, index):
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


def _sed_destinations(tokens: list[str], index: int) -> list[str]:
    """File operands of an in-place ``sed`` — never the script or the ``-i`` argument.

    ``sed -i '' 's/a/b/' existing.txt`` passes an empty ``-i`` suffix argument and
    then the script, so the old ``tokens[index + 2:]`` read the *script* as a path
    and reported ``~/s`` for an edit to an existing file. The operands are parsed
    instead: options first (``-i`` plus its optional empty suffix, ``-e``/``-f``
    which consume the script), then the first remaining operand is the script
    unless ``-e``/``-f`` already supplied one, and the rest are the files.
    """
    rest = tokens[index + 1:]
    in_place = False
    script_consumed = False
    operands: list[str] = []
    position = 0
    while position < len(rest):
        token = rest[position]
        if token == "--in-place" or token.startswith("--in-place="):
            in_place = True
        elif token == "-i":
            in_place = True
            if position + 1 < len(rest) and rest[position + 1] == "":
                position += 1
        elif token.startswith("-i"):
            in_place = True
        elif token in ("-e", "--expression", "-f", "--file"):
            script_consumed = True
            position += 1
        elif token.startswith("-e") or token.startswith("-f"):
            script_consumed = True
        elif not token.startswith("-") or token == "-":
            operands.append(token)
        position += 1
    if not in_place:
        return []
    return operands if script_consumed else operands[1:]


def _py_receiver(code: str, start: int) -> str:
    """Text of the receiver expression immediately left of a matched call.

    ``pathlib.Path('<home>/newfile').write_text('x')`` matches only
    ``write_text('x')``, so the path lives in the receiver, not in the call
    body. Return the receiver (``pathlib.Path('<home>/newfile').``) or ``""``
    when the receiver is a bare name / unresolvable, so the caller falls back to
    the body literals alone.
    """
    window = code[max(0, start - 200):start]
    tail = re.split(r"[;\n]|\b(?:return|yield)\b|(?<![=!<>])=(?!=)", window)[-1].strip()
    if not tail.endswith("."):
        return ""
    if re.search(r"\b(?:or|and|not|if|else|for|in)\b", tail):
        return ""
    if tail.count("(") != tail.count(")"):
        return ""
    return tail


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
        text = _py_receiver(code, match.start()) + "".join(body)
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


def _resolved_cwd(args: dict, kwargs: dict) -> Optional[str]:
    """The cwd the terminal tool will actually run *command* in.

    The tool resolves it as explicit ``workdir`` > the session's recorded cwd >
    its own default (``tools/terminal_tool.py:_resolve_command_cwd``). Reading
    only the in-string ``cd`` let a bare ``sqlite3 kanban.db`` run with the cwd
    at the real home — the exact litter this gate exists for — pass unchecked.
    Unresolvable (no ``workdir``, no record, tools not importable) returns
    ``None`` and the caller falls back to the workspace seed.
    """
    workdir = str(args.get("workdir") or "").strip()
    if workdir:
        return workdir
    try:
        from tools.approval_context import get_current_session_key
        from tools.terminal_tool import get_session_cwd
    except Exception:
        return None
    try:
        key = (get_current_session_key(default="") or str(kwargs.get("task_id") or "")
               or str(kwargs.get("session_id") or ""))
        return get_session_cwd(key)
    except Exception:
        return None


def terminal_violations(command: str, base_cwd: Optional[str] = None) -> list[str]:
    blanked = _wwg._strip_heredoc_bodies(command)
    for cwd, segment in _wwg._segments(blanked, base_cwd):
        entry = _offending(_creation_targets(cwd, segment))
        if entry:
            return [_MESSAGE.format(tool="terminal", entry="~/" + entry)]
    for body in _wwg._script_bodies(command):
        entry = _offending(_py_creation_literals(body))
        if entry:
            return [_MESSAGE.format(tool="terminal", entry="~/" + entry)]
    return []


def check(tool_name: str, args: Any, **kwargs: Any) -> Optional[str]:
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
    found = terminal_violations(str(args.get("command") or ""), _resolved_cwd(args, kwargs))
    return found[0] if found else None
