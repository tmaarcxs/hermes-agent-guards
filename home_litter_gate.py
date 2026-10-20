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
- ``~/git_repos/**`` and ``~/.hermes/**`` never make a new first component
  under ``~``, so they are out of scope by construction.

The lead is never affected; only worker processes (``HERMES_KANBAN_TASK`` set)
are checked. A creating head counts only in command position — ``grep -rn
mkdir src`` reads, it does not create. The command word is the first token that
is not a leading wrapper, a ``VAR=value`` word, or a wrapper's own option (and
that option's argument, where it takes one), so ``sudo -u root mkdir x``,
``nice -n 5 mkdir x``, ``env -i mkdir x`` and ``timeout 5 mkdir x`` are all
recognised. A python ``-c``/heredoc creating call contributes only its write
target — never a copy source — resolved against the command's own cwd, so
``python3 -c "os.makedirs('brandnew')"`` with the cwd at ``~`` is caught.
Unknown command shapes fail open.
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

_CREATE_HEADS = ("cp", "mv", "rsync", "install", "touch", "mkdir")
"""Creation-capable shell heads only.

``rm``/``rmdir`` are excluded on purpose: deleting an entry that does not exist
is a no-op, not litter, and the lead's own cleanup uses ``rm -rf``. ``tee`` and
``sqlite3`` create files too but need their own operand parsing, so they are
handled separately below.
"""

_COPY_HEADS = ("cp", "mv", "rsync", "install")
"""Copy-style heads. ``cp <src> ~/`` lands ``~/<basename(src)>`` — a brand-new
top-level entry — while the destination resolves to the home itself, so the
per-target check sees no child to name. The sources are re-expressed as home
children so the same create/allow decision applies.
"""

_WRAPPERS = frozenset({"sudo", "command", "nohup", "time", "exec", "env", "nice", "ionice", "stdbuf", "timeout"})
"""Words that may precede the real command word without being it.

``sudo mkdir x`` still creates ``~/x``; a create head is only recognised in
command position, so these wrappers — and the options each may carry — are
skipped when locating it.
"""

_ARG_OPTIONS = {
    "sudo": frozenset({
        "-u", "--user", "-g", "--group", "-p", "--prompt", "-C", "--close-from",
        "-h", "--host", "-r", "--role", "-t", "--type", "-U", "--other-user",
        "-D", "--chdir", "-R", "--chroot", "-T", "--command-timeout",
    }),
    "env": frozenset({"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "ionice": frozenset({
        "-c", "--class", "-n", "--classdata", "-p", "--pid", "-P", "--pgid", "-u", "--uid",
    }),
    "stdbuf": frozenset({"-i", "--input", "-o", "--output", "-e", "--error"}),
    "timeout": frozenset({"-s", "--signal", "-k", "--kill-after"}),
    "time": frozenset({"-o", "--output", "-f", "--format"}),
    "exec": frozenset({"-a", "--argv0"}),
}
"""Wrapper options that consume the *next* token as their argument, per wrapper.

``sudo -u root mkdir x`` names ``root`` as ``-u``'s argument, so the head is the
token after it; without this ``-u`` reads as the command word and the create
head behind it is missed. The sets are per wrapper because the same letter
differs: ``sudo -n`` takes no argument while ``nice -n 5`` does. An attached
form (``-c2``, ``--user=root``) consumes nothing extra.
"""

_WRAPPER_OPERANDS = {"timeout": 1}
"""Wrappers whose first positional operand is not the command.

``timeout DURATION COMMAND`` puts the duration before the command, so one
non-option token is skipped after the wrapper's options.
"""
_REDIRECT = re.compile(r"(?:>>|>)\s*(\S+)")
_FD_REDIRECT = re.compile(r"^\d?&[\d()]*$|^\d?&|^[&]|\)$")
_PY_CREATE = re.compile(
    r"\b(?P<name>makedirs|mkdir|copytree|copyfile|copy|move|rename|replace)\s*\(|"
    r"\bopen\s*\([^()]*,\s*['\"][waxu+]|"
    r"\b(?:write_text|write_bytes)\s*\(|"
    r"\brates?_file\(|\bto_csv\s*\(|\bto_json\s*\(|\bsavefig\s*\(|\bdump\s*\("
)
_PY_STRINGS = re.compile(r"(['\"])(.*?)\1")
_COPY_LITERAL_CALLS = frozenset({"copytree", "copyfile", "copy", "move", "rename", "replace"})
"""Calls whose *last* literal is the write target; the earlier ones are sources.

``shutil.copy('a', 'brandnew')`` writes ``brandnew``; treating ``a`` as a
created path would report a nonexistent source as litter (and block a copy into
an existing destination).
"""
_MULTI_TARGET_CALLS = frozenset({"makedirs", "mkdir"})
"""Calls that create every literal operand (a multi-operand ``mkdir a b``)."""
_SQLITE3_ARG_OPTIONS = frozenset({"-cmd", "-separator", "-nullvalue", "-newline", "-init"})
"""sqlite3 options that consume a separate argument, so the DB operand is after them."""


def _home() -> Path:
    return _wwg._home()


def _new_top_level_entry(path: str, cwd: Optional[str] = None) -> Optional[str]:
    """Return the offending entry name when *path* would create a not-yet-existing
    top-level entry under the real home (and it is not allowlisted), else ``None``.

    The check is on the first component under the home, not only a direct child:
    ``mkdir -p scratch/f3`` from cwd ``~`` creates ``~/scratch`` as well, so the
    absent first component is the litter. A path whose first component already
    exists (``~/git_repos/newrepo``, ``~/.cache/x``) stays allowed.

    A relative *path* (a python ``-c``/heredoc literal, say) resolves against
    *cwd* — the command's own cwd — rather than the guard process's cwd, so
    ``python3 -c "os.makedirs('brandnew')"`` with ``workdir=~`` is caught even
    though the process cwd is elsewhere. ``None`` keeps the process-cwd
    resolution for callers that already carry an absolute path.
    """
    candidate = _wwg._normalize(path)
    if not candidate:
        return None
    if cwd and not candidate.startswith(("/", "~", "$")):
        candidate = _wwg._join(cwd, candidate) or candidate
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


def _head_index(tokens: list[str]) -> int:
    """Index of the command word: skip leading wrappers, their options and
    ``VAR=value`` words.

    ``sudo -u root mkdir x``, ``nice -n 5 mkdir x`` and ``env -i mkdir x`` all
    create ``~/x``; the create head must be recognised in command position, not
    at whatever index the word happens to appear (``grep -rn mkdir src`` is a
    read, not a create). A wrapper may carry its own options — some consuming
    the next token as an argument (``sudo -u root``), some attached
    (``nice -n5``, ``--user=root``) — and ``timeout`` also takes a positional
    duration before the command; all of those are skipped so the head is the
    real command word. Unknown tokens before a known head stop the scan: the
    shape is then unanalyzable and fails open downstream.
    """
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in _WRAPPERS:
            index += 1
            options = _ARG_OPTIONS.get(token, frozenset())
            while index < len(tokens):
                current = tokens[index]
                if not current.startswith("-"):
                    break
                consumes_next = current in options
                index += 1
                if consumes_next and index < len(tokens):
                    index += 1
            index += _WRAPPER_OPERANDS.get(token, 0)
            continue
        if "=" in token and not token.startswith("-") and not token.startswith("/"):
            index += 1
            continue
        break
    return index


def _creation_targets(cwd: str, segment: str) -> list[str]:
    """Shell write targets that CREATE a path (never ``rm``/``rmdir``)."""
    unquoted = re.sub(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"", "''", segment)
    targets: list[str] = []
    for match in _REDIRECT.finditer(unquoted):
        if not _FD_REDIRECT.match(match.group(1)):
            targets.append(_wwg._join(cwd, match.group(1)))
    tokens = _wwg._tokens(segment)
    head_index = _head_index(tokens)
    head = tokens[head_index].lower() if head_index < len(tokens) else ""
    if head in _CREATE_HEADS:
        operands = [dest for dest in tokens[head_index + 1:] if not dest.startswith("-")]
        if operands:
            if head in ("mkdir", "touch"):
                for dest in operands:
                    targets.append(_wwg._join(cwd, dest))
            else:
                dest = operands[-1]
                targets.append(_wwg._join(cwd, dest))
                if head in _COPY_HEADS:
                    targets.extend(_copy_into_home_children(cwd, operands[:-1], dest))
    elif head == "tee":
        operands = [dest for dest in tokens[head_index + 1:] if not dest.startswith("-")]
        if operands:
            targets.append(_wwg._join(cwd, operands[0]))
    elif head == "sed":
        for dest in _sed_destinations(tokens, head_index):
            targets.append(_wwg._join(cwd, dest))
    elif head == "sqlite3":
        operands = _operands_after(tokens, head_index, _SQLITE3_ARG_OPTIONS)
        if operands:
            targets.append(_wwg._join(cwd, operands[0]))
    elif head in ("curl", "wget"):
        for dest in _download_destinations(head, tokens, head_index):
            targets.append(_wwg._join(cwd, dest))
    return targets


def _operands_after(tokens: list[str], index: int, arg_options: frozenset) -> list[str]:
    """Non-option operands after *index*, skipping options that consume a value.

    ``sqlite3 -cmd '.timer on' kanban.db`` names ``kanban.db`` as the database;
    the option and its separate argument must not be read as the path. An
    attached form (``--separator=,``) consumes nothing extra.
    """
    operands: list[str] = []
    position = index + 1
    while position < len(tokens):
        token = tokens[position]
        if token in arg_options:
            position += 2
            continue
        if token.startswith("-") and token != "-":
            position += 1
            continue
        operands.append(token)
        position += 1
    return operands


def _download_destinations(head: str, tokens: list[str], index: int) -> list[str]:
    """Output file of a ``curl``/``wget`` head, taken from its own operands.

    The old unanchored ``\\bcurl\\b`` regex matched the word anywhere in the
    segment, so ``echo curl -o foo`` (which writes nothing) was blocked. The
    head is only the command word, as for every other create shape.

    ``curl`` and ``wget`` disagree on the flags, so they are parsed separately:
    curl's ``-o``/``--output`` and wget's ``-O``/``--output-document`` name the
    file; curl's ``-O``/``--remote-name`` takes it from the URL basename
    (``curl -O http://x/f.tar`` writes ``./f.tar``, not ``./http:``). A bare
    ``wget <url>`` (no output option) also writes the URL basename, but telling
    that apart from ``wget --help``/``-i <list>`` needs more context than one
    segment carries, so it stays a documented fail-open.
    """
    rest = tokens[index + 1:]
    outputs: list[str] = []
    urls: list[str] = []
    remote_name = False
    position = 0
    while position < len(rest):
        token = rest[position]
        takes_argument = ("-o", "--output") if head == "curl" else ("-O", "--output-document")
        remote_flag = ("-O", "--remote-name") if head == "curl" else ()
        if token in takes_argument:
            if position + 1 < len(rest):
                outputs.append(rest[position + 1])
                position += 1
        elif token in remote_flag:
            remote_name = True
        elif token.startswith("--output=") or token.startswith("--output-document="):
            outputs.append(token.split("=", 1)[1])
        elif len(token) > 2 and token.startswith(takes_argument[0]):
            outputs.append(token[2:])
        elif "://" in token:
            urls.append(token)
        position += 1
    if remote_name and urls:
        basename = urls[-1].rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0]
        if basename:
            outputs.append(basename)
    return outputs


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


def _py_call_body(code: str, start: int) -> str:
    """The matched call's argument text, parens balanced up to 200 chars."""
    depth = 1
    body: list[str] = []
    for char in code[start:start + 200]:
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
    return text


def _py_creation_literals(code: str) -> list[str]:
    """Path literals that a matched creating call would actually write.

    Only the *target* of each call is returned, so a copy source is not
    mistaken for litter: ``shutil.copy('a', 'brandnew')`` writes ``brandnew``,
    and ``pathlib.Path('brandnew').write_text('x')`` puts its path in the
    receiver, not the payload. An unrecognised shape contributes all its
    literals (fail-open on precision, never silently dropping a candidate).
    """
    literals: list[str] = []
    for match in _PY_CREATE.finditer(code):
        name = match.group("name") or ""
        receiver = _py_receiver(code, match.start())
        receiver_literals = [literal for _, literal in _PY_STRINGS.findall(receiver)]
        body_literals = [literal for _, literal in _PY_STRINGS.findall(_py_call_body(code, match.start()))]
        if receiver_literals:
            literals.extend(receiver_literals)
        elif name in _COPY_LITERAL_CALLS:
            literals.extend(body_literals[-1:])
        elif name in _MULTI_TARGET_CALLS:
            literals.extend(body_literals)
        elif name == "open":
            literals.extend(body_literals[:1])
        else:
            literals.extend(body_literals)
    return literals


def _offending(targets: list[str], cwd: Optional[str] = None) -> Optional[str]:
    for target in targets:
        if not target:
            continue
        entry = _new_top_level_entry(target, cwd)
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
        return _config_cwd()
    try:
        key = (get_current_session_key(default="") or str(kwargs.get("task_id") or "")
               or str(kwargs.get("session_id") or ""))
        recorded = get_session_cwd(key)
    except Exception:
        recorded = None
    return recorded or _config_cwd()


def _config_cwd() -> Optional[str]:
    """The terminal tool's own default cwd when no session record exists yet.

    ``tools/terminal_tool_config._resolve_config_cwd`` uses
    ``TERMINAL_CWD`` (config ``terminal.cwd``, ``"."`` in this install) and
    otherwise ``os.getcwd()`` — the *process* cwd. A worker process launched
    with its cwd at the home directory therefore runs a bare
    ``sqlite3 kanban.db`` in ``~`` even on its first command, before any cwd
    has been recorded. Returning that cwd here keeps the gate's view equal to
    the tool's.
    """
    try:
        from tools.terminal_scope import terminal_env
        configured = (terminal_env("TERMINAL_CWD", "") or "").strip()
    except Exception:
        configured = (os.environ.get("TERMINAL_CWD") or "").strip()
    try:
        process_cwd = os.getcwd()
    except OSError:
        process_cwd = ""
    if not configured or configured == ".":
        return process_cwd or None
    try:
        return os.path.abspath(os.path.expanduser(configured))
    except (OSError, ValueError):
        return process_cwd or None


def terminal_violations(command: str, base_cwd: Optional[str] = None) -> list[str]:
    blanked = _wwg._strip_heredoc_bodies(command)
    for cwd, segment in _wwg._segments(blanked, base_cwd):
        entry = _offending(_creation_targets(cwd, segment), cwd)
        if entry:
            return [_MESSAGE.format(tool="terminal", entry="~/" + entry)]
    for body in _wwg._script_bodies(command):
        entry = _offending(_py_creation_literals(body), base_cwd)
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
