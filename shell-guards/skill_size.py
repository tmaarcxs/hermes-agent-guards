"""Keep skill files under a size limit so skills stay a small index plus a reference tree.

``check`` runs on ``pre_tool_call`` for ``skill_manage``, ``write_file`` and ``patch``
(replace mode). It computes the size each skill markdown file would have after the
call and blocks when that size is over the limit and larger than the current file:
``SKILL.md`` may hold at most ``SKILL_LIMIT`` characters and any other ``.md`` file
inside a skill at most ``REFERENCE_LIMIT``. Shrinking an over-limit file is always
allowed, so an oversized skill can be split step by step.

Writes that ``check`` cannot see coming are caught after the fact: ``record`` runs
on ``pre_tool_call`` for ``terminal``, ``execute_code`` and ``patch`` and takes a
snapshot of every markdown file under the skill roots — its byte size by ``stat``,
and its character count only for a file already over its limit in bytes, since a
file under the limit in bytes cannot be over it in characters. The flag is
delivered by one of two seams, both built on ``_flag``: a ``terminal``
call is flagged on ``transform_terminal_output``, which the terminal tool runs
INSIDE the tool, before ``transform_tool_result``; every other watched tool
(``execute_code``, unified-diff ``patch``) is flagged on ``transform_tool_result``.
The first seam to run pops the call's snapshot, so a call is never flagged twice.
Re-snapshotting there catches the tree-split instruction once per skill markdown
file that call left over its limit in characters and larger than (or new since)
the snapshot. Unified-diff ``patch`` calls and writes through
``terminal``/``execute_code`` (shell redirects, python scripts, ``sed -i``,
``cp``/``mv``) are therefore measured too. Nothing is reverted, deleted or backed
up: the write stands and the model is told to fix it in the same turn.

The flag message alone was not enough: a model can read it as someone else's
task, because the snapshot attributes any growth to the current call even when
another session or worker wrote the file concurrently. So the message leads with
the required action and says outright that the cause does not matter and that the
file is over its limit NOW. And the flag is enforced deterministically: ``verify_check``
runs on ``pre_verify`` and refuses to end the turn while a file flagged in THIS
session is still over its limit, returning a continue directive that names the
file and the action. It re-measures at check time (a file already back under its
limit clears the flag silently) and caps at ``_MAX_CONTINUATIONS`` continuations
per file per session, so a model that cannot fix it cannot loop forever.

The terminal seam exists because the host keeps only the FIRST non-``None``
``transform_tool_result`` string across plugins (``model_tools.py``
``_apply_transform_tool_result_hook``), and ``kanban-guards`` loads before
``shell-guards``. A flag that rode ``transform_tool_result`` alone would be
silently dropped whenever a Kanban lead's threshold-crossing ``terminal`` call
also left an oversized skill file — the nudge would win and the flag would
vanish. Delivering the terminal flag one hook earlier puts it inside the result
the nudge appends to, so both messages reach the model.

Only markdown inside the skill roots is measured; scripts and data files are not.
Both surfaces catch slips, not deliberate evasion; ``skill_tree_check.py`` in
skill-library-audit audits the whole library.
"""

import os
from collections import OrderedDict
from pathlib import Path
from typing import Any, Optional

SKILL_LIMIT = 6000
REFERENCE_LIMIT = 12000
_MAX_CONTINUATIONS = 2
_MAX_FLAGGED_SESSIONS = 200

_WATCHED_TOOLS = frozenset({"terminal", "execute_code", "patch"})
_MAX_PENDING = 8
_MAX_TREE = 4096
_PENDING: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
_TREE: "dict[str, tuple[float, list[str], list[str]]]" = {}
_FLAGGED: "OrderedDict[str, dict[str, int]]" = OrderedDict()

_TREE_SPLIT = (
    "Distribute the content as a tree instead: keep SKILL.md as the index (frontmatter, when to use, "
    "the few rules needed on every load, and one line per reference saying when to read it); put each "
    "topic in references/<topic>.md; when a reference outgrows its limit, keep that file as a short "
    "index pointing to references/<topic>-<subtopic>.md files. Move text verbatim, keep existing file "
    "paths, then retry."
)

_CONTINUE = (
    "[System: agent-guards. This turn cannot end yet: {files} is still over its size limit. "
    "Shrink it now, in this same response — move text verbatim into references/<topic>.md files and "
    "keep SKILL.md as the short index. Shrinking an over-limit file is always allowed. "
    "Do not reply that the file is unrelated to your task.]"
)


def reset() -> None:
    """Clear per-session state (used by the test suite)."""
    _PENDING.clear()
    _FLAGGED.clear()


def _hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


_HOME = _hermes_home()
ROOTS = [_HOME / "skills", Path.home() / ".agents" / "skills", *(_HOME / "profiles").glob("*/skills")]
_SKIPPED_PARTS = {".archive", ".curator_backups", ".hub", ".locks"}


def _roots() -> list[Path]:
    return [Path(os.path.realpath(root)) for root in ROOTS if root.exists()]


def _limit(path: Path) -> Optional[int]:
    if path.suffix != ".md" or _SKIPPED_PARTS & set(path.parts):
        return None
    real = Path(os.path.realpath(path))
    if not any(real.is_relative_to(root) for root in _roots()):
        return None
    return SKILL_LIMIT if path.name == "SKILL.md" else REFERENCE_LIMIT


def _snapshot_limit(path: str) -> Optional[int]:
    """``_limit`` for a path ``_snapshot`` produced: it walked the resolved roots already, so
    the per-file ``realpath`` syscall (the bulk of the post-call cost) is skipped."""
    name = path.rsplit("/", 1)[-1]
    if not name.endswith(".md"):
        return None
    if _SKIPPED_PARTS & set(path.split("/")):
        return None
    return SKILL_LIMIT if name == "SKILL.md" else REFERENCE_LIMIT


def _size(path: Path) -> int:
    try:
        return len(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return 0


def _skill_dir(name: str) -> Optional[Path]:
    for root in _roots():
        direct = root / name
        if (direct / "SKILL.md").is_file():
            return direct
        for skill_md in root.rglob("SKILL.md"):
            if skill_md.parent.name == name and not _SKIPPED_PARTS & set(skill_md.parts):
                return skill_md.parent
    return None


def _replaced(current: int, old: Any, new: Any, replace_all: bool, text: str) -> int:
    old, new = str(old or ""), str(new or "")
    count = text.count(old) if replace_all and old else 1
    return current + (len(new) - len(old)) * max(count, 1)


def _skill_manage_targets(args: dict) -> list[tuple[Path, int, int]]:
    operations = args.get("operations")
    if not isinstance(operations, list):
        operations = [args]
    simulated: dict[Path, int] = {}
    results: list[tuple[Path, int, int]] = []
    for op in operations:
        if not isinstance(op, dict):
            continue
        action = op.get("action") or args.get("action")
        name = str(op.get("name") or args.get("name") or "")
        if action not in ("create", "edit", "patch", "write_file") or not name:
            continue
        skill_dir = _skill_dir(name)
        if skill_dir is None:
            base = _roots()[0] if _roots() else _HOME / "skills"
            skill_dir = base / name
        file_path = op.get("file_path") if action in ("patch", "write_file") else None
        target = skill_dir / (file_path or "SKILL.md")
        before = simulated.get(target, _size(target))
        if action == "write_file":
            after = len(str(op.get("file_content") or ""))
        elif action == "patch" and not op.get("content"):
            text = target.read_text(encoding="utf-8") if target.is_file() else ""
            after = _replaced(before, op.get("old_string"), op.get("new_string"), bool(op.get("replace_all")), text)
        else:
            after = len(str(op.get("content") or ""))
        simulated[target] = after
        results.append((target, before, after))
    return results


def _file_tool_targets(tool_name: str, args: dict) -> list[tuple[Path, int, int]]:
    raw = args.get("path")
    if not raw:
        return []
    target = Path(os.path.expanduser(str(raw)))
    before = _size(target)
    if tool_name == "write_file":
        return [(target, before, len(str(args.get("content") or "")))]
    if (args.get("mode") or "replace") != "replace" or not target.is_file():
        return []
    text = target.read_text(encoding="utf-8")
    return [(target, before, _replaced(before, args.get("old_string"), args.get("new_string"),
                                       bool(args.get("replace_all")), text))]


def _scan(directory: str) -> tuple[list[str], list[str]]:
    """Child markdown paths and child directories of one directory, one ``scandir``.

    A child directory is only descended into when its resolved path stays inside a skill
    root, so a symlink pointing out of the roots cannot smuggle outside files in.
    """
    files: list[str] = []
    directories: list[str] = []
    roots = _roots()
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return files, directories
    for entry in entries:
        try:
            if entry.is_dir(follow_symlinks=True):
                if entry.name in _SKIPPED_PARTS:
                    continue
                if any(Path(os.path.realpath(entry.path)).is_relative_to(root) for root in roots):
                    directories.append(entry.path)
            elif entry.name.endswith(".md"):
                if entry.is_symlink() and not any(
                    Path(os.path.realpath(entry.path)).is_relative_to(root) for root in roots
                ):
                    continue
                files.append(entry.path)
        except OSError:
            continue
    return files, directories


def _children(directory: str, mtime: float) -> tuple[list[str], list[str]]:
    """``_scan`` memoized on the directory's own mtime: a cached directory whose mtime still
    matches is reused without a ``scandir``, since any added or removed entry bumps it. An
    in-place rewrite keeps the directory mtime and is caught by the per-file ``stat`` below.
    """
    hit = _TREE.get(directory)
    if hit is not None and hit[0] == mtime:
        return hit[1], hit[2]
    files, directories = _scan(directory)
    if len(_TREE) >= _MAX_TREE:
        _TREE.clear()
    _TREE[directory] = (mtime, files, directories)
    return files, directories


def _chars(path: str, byte_size: int, limit: Optional[int]) -> Optional[int]:
    """Character count of ``path``, read only when its byte size exceeds ``limit``.

    UTF-8 never uses fewer bytes than characters, so a file under the limit in bytes is
    under it in characters too; only the handful of byte-over-limit files pay a read.
    """
    if limit is None or byte_size <= limit:
        return None
    try:
        return len(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return None


def _snapshot() -> dict[str, tuple[int, Optional[int], float]]:
    """Byte size, over-limit character count and mtime of every skill markdown file.

    The size is ``stat``-only; the character count is read only for a file already over
    its limit in bytes, so a snapshot stays cheap on a large tree.
    """
    found: dict[str, tuple[int, Optional[int], float]] = {}
    stack = [str(root) for root in _roots()]
    while stack:
        directory = stack.pop()
        try:
            mtime = os.stat(directory).st_mtime
        except OSError:
            continue
        files, directories = _children(directory, mtime)
        stack.extend(directories)
        for path in files:
            try:
                info = os.stat(path)
            except OSError:
                continue
            found[path] = (info.st_size, _chars(path, info.st_size, _snapshot_limit(path)), info.st_mtime)
    return found


def _session_of(**kwargs) -> str:
    """The session key the flag and the ``pre_verify`` check both key on.

    ``pre_tool_call`` (``record``) and ``transform_tool_result`` carry ``session_id``;
    ``pre_verify`` carries only ``session_id``. The ``task_id`` fallback keeps a worker
    whose hook payload lacks a session id from losing its flags.
    """
    return str(kwargs.get("session_id") or kwargs.get("task_id") or "")


def _snapshot_pending(key: str, session: str) -> None:
    _PENDING[key] = {"session": session, "before": _snapshot()}
    _PENDING.move_to_end(key)
    while len(_PENDING) > _MAX_PENDING:
        _PENDING.popitem(last=False)


def record(tool_name: str = "", args: Any = None, **kwargs) -> None:
    """``pre_tool_call``: remember the skill-markdown sizes before a call that can write them.

    Keyed by ``tool_call_id`` so concurrent tool calls in one turn cannot shadow each
    other; the oldest entries are dropped once ``_MAX_PENDING`` is exceeded. The session
    key travels with the snapshot because the ``transform_terminal_output`` seam does not
    receive a session id — only ``pre_tool_call`` does.
    """
    try:
        if tool_name not in _WATCHED_TOOLS:
            return
        _snapshot_pending(str(kwargs.get("tool_call_id") or ""), _session_of(**kwargs))
    except Exception:
        return


def _post_message(tool_name: str, target: Path, chars: int, limit: int) -> str:
    kind = "SKILL.md" if limit == SKILL_LIMIT else "a skill reference file"
    return (
        f"Shrink {target} now, in this turn: agent-guards: {tool_name} left it at {chars:,} characters, "
        f"over the limit for {kind} ({limit:,}; SKILL.md {SKILL_LIMIT:,}, other skill .md files "
        f"{REFERENCE_LIMIT:,}). It is over the limit NOW, regardless of what caused the growth — who "
        "caused it does not matter. Shrinking an over-limit file is always allowed. Do not reply that "
        "the file is unrelated to your task. " + _TREE_SPLIT
    )


def _flag(tool_name: str, result: str, key: str, session: str = "") -> Optional[str]:
    """Append one tree-split instruction per skill markdown this call left oversized.

    Pops the call's ``_PENDING`` snapshot, so the first seam to run wins and the
    second sees ``None`` — one call is never flagged twice. Every flagged file is
    remembered against the session so ``pre_verify`` can refuse to end the turn
    while it is still over its limit.
    """
    pending = _PENDING.pop(key, None)
    if pending is None:
        return None
    before = pending["before"]
    session = session or pending["session"]
    messages = []
    for path, (_size, chars, _mtime) in _snapshot().items():
        limit = _snapshot_limit(path)
        if limit is None or chars is None or chars <= limit:
            continue
        previous = before.get(path)
        if previous is not None and previous[1] is not None and chars <= previous[1]:
            continue
        messages.append(_post_message(tool_name, Path(path), chars, limit))
        _remember_flag(session, path)
    if not messages:
        return None
    return result + "\n\n" + "\n\n".join(messages)


def _remember_flag(session: str, path: str) -> None:
    if not session:
        return
    flags = _FLAGGED.setdefault(session, {})
    flags.setdefault(path, 0)
    _FLAGGED.move_to_end(session)
    while len(_FLAGGED) > _MAX_FLAGGED_SESSIONS:
        _FLAGGED.popitem(last=False)


def _over_limit(path: str) -> bool:
    """Is ``path`` still over its limit right now? Re-measured, never cached."""
    limit = _limit(Path(path))
    if limit is None:
        return False
    try:
        chars = len(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return False
    return chars > limit


def verify_check(**kwargs) -> Optional[dict[str, str]]:
    """``pre_verify``: refuse to end the turn while a flagged file is still over its limit.

    Re-measures every file flagged in THIS session: a file already back under its limit
    (or gone) clears its flag silently. For a file still over, one continue directive
    naming it is returned — at most ``_MAX_CONTINUATIONS`` times per file per session, so
    a model that cannot fix it cannot loop forever. Fails open.

    The host consults ``pre_verify`` only after a ``write_file``/``patch`` landed in the
    turn (``agent/turn_stop_gates.py`` ``_pre_verify_nudge``: ``_edited and has_hook(...)``)
    and bounds the directives itself by ``agent.max_verify_nudges``; this check is not
    worker-scoped, so a main chat session gets it too.
    """
    try:
        session = _session_of(**kwargs)
        flags = _FLAGGED.get(session)
        if not flags:
            return None
        outstanding = []
        for path in list(flags):
            if not _over_limit(path):
                flags.pop(path, None)
                continue
            if flags[path] >= _MAX_CONTINUATIONS:
                continue
            flags[path] += 1
            outstanding.append(path)
        if not flags:
            _FLAGGED.pop(session, None)
        if not outstanding:
            return None
        return {"action": "continue", "message": _CONTINUE.format(files=", ".join(outstanding))}
    except Exception:
        return None


def transform(tool_name: str = "", args: Any = None, result: Any = None, **kwargs) -> Optional[str]:
    """``transform_tool_result``: flag every skill markdown this call pushed over its limit.

    Returns the tool result with one tree-split instruction appended per offending file,
    or ``None`` to leave it untouched. Fails open: an internal error never changes the
    result.

    A ``terminal`` call is normally flagged one hook earlier, by
    ``transform_terminal_output``; this path covers the calls that hook never saw (a
    ``background=true`` or yielded call returns before the terminal tool finalizes its
    output, and an environment without the hook at all).
    """
    try:
        if tool_name not in _WATCHED_TOOLS or not isinstance(result, str):
            return None
        return _flag(tool_name, result, str(kwargs.get("tool_call_id") or ""), _session_of(**kwargs))
    except Exception:
        return None


def transform_terminal_output(command: str = "", output: Any = None, **kwargs) -> Optional[str]:
    """``transform_terminal_output``: flag a ``terminal`` call that left a skill file oversized.

    The terminal tool runs this hook INSIDE the tool, before ``transform_tool_result``
    (``tools/terminal_tool_result.py`` ``finalize_foreground_result`` applies the output
    transform, then ``model_tools._apply_transform_tool_result_hook`` runs after the tool
    returns). The host keeps only the FIRST non-``None`` ``transform_tool_result`` string
    across plugins, and ``kanban-guards`` loads before ``shell-guards``, so a flag that
    rode the later hook alone would be silently dropped whenever a Kanban lead's
    threshold-crossing ``terminal`` call also left an oversized skill file. Delivering it
    here puts it inside the output the nudge appends to, so both messages reach the model.

    Fails open: a non-string output or an internal error leaves the output untouched.

    An empty ``tool_call_id`` is skipped: the process-registry result path runs the same
    hook for ``process_manage`` output, and an unkeyed snapshot must not be stolen by a
    call that never wrote it. Such a terminal call is still flagged by the
    ``transform_tool_result`` seam.
    """
    try:
        if not isinstance(output, str):
            return None
        key = str(kwargs.get("tool_call_id") or "")
        if not key:
            return None
        return _flag("terminal", output, key)
    except Exception:
        return None


def check(tool_name: str, args: Any = None, **_kwargs) -> Optional[str]:
    if not isinstance(args, dict):
        return None
    if tool_name == "skill_manage":
        targets = _skill_manage_targets(args)
    elif tool_name in ("write_file", "patch"):
        targets = _file_tool_targets(tool_name, args)
    else:
        return None
    for target, before, after in targets:
        limit = _limit(target)
        if limit is None or after <= limit or after < before:
            continue
        kind = "SKILL.md" if limit == SKILL_LIMIT else "a skill reference file"
        return (
            f"agent-guards blocked {tool_name}: {target.name} would be {after:,} characters; the limit for {kind} "
            f"is {limit:,} (SKILL.md {SKILL_LIMIT:,}, other skill .md files {REFERENCE_LIMIT:,}). " + _TREE_SPLIT
        )
    return None
