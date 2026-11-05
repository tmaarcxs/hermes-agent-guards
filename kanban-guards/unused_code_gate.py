"""Block a review request (or completion) whose diff adds unused private Python code.

The repo rule is 'unused code is not allowed, even if it does nothing'. The real
incident: a worker added ``_PYTH_DELETE`` and ``_PYTH_ABS_DELETE`` as module-level
constants, review approved the PR, and only then the lead had them removed — the
detection existed as prose but nothing enforced it before the handoff. This gate
enforces it at ``kanban_request_review`` and ``kanban_complete``.

Changed files are the diff between the merge-base with the remote default branch
and the working tree (so both committed and uncommitted edits count). Among names
the diff ADDS, it flags:

- module-level private names (a single leading underscore: assignments, annotated
  assignments, ``def``/``async def``/``class``) that nothing in the repo references
  (one ``git grep -w`` per batch; a reference in a test counts; names in ``__all__``
  and dunders are exempt);
- imports the module never uses (re-exports via a package ``__init__.py`` and names
  in ``__all__`` are exempt).

Scope: Python only. The message names each ``file:line:name`` and the exact fix —
delete them and rerun the tests. Worker setups only (``kanban_policy``), cards with
a git worktree workspace only. Fails open: missing git, a timeout, an unparsable
file or any internal error returns ``None``.
"""

import ast
import os
import subprocess
from pathlib import Path
from typing import Any, Optional

from . import kanban_policy

_GIT_TIMEOUT_SECONDS = 10
_MESSAGE_HEAD = (
    "agent-guards blocked {tool}: the diff adds unused private Python code (unused code is not "
    "allowed, even if it does nothing). Delete {entries} and rerun the tests, then request review "
    "again. "
)


def _workspace_path() -> Optional[str]:
    """The card's workspace directory, or ``None`` when there is none."""
    try:
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc

        task_id = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
        if not task_id:
            return None
        conn = kbc.connect(board=None)
        try:
            task = kb.get_task(conn, task_id)
            path = str(task.workspace_path or "") if task is not None else ""
        finally:
            conn.close()
        return path or None
    except Exception:
        return None


def _which_git() -> Optional[str]:
    from shutil import which

    return which("git")


def _run(cmd: list, cwd: str) -> Optional[str]:
    """stdout of ``cmd`` in ``cwd``, ``None`` on any failure (missing git, timeout, error)."""
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=_GIT_TIMEOUT_SECONDS)
        if proc.returncode != 0:
            return None
        return proc.stdout.decode("utf-8", "replace")
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        return None


def _remote_default_branch(cwd: str) -> Optional[str]:
    """``origin/HEAD``'s branch name (the part after ``origin/``), ``main`` as fallback."""
    out = _run(["git", "symbolic-ref", "refs/remotes/origin/HEAD"], cwd)
    if out and out.strip():
        return out.strip().removeprefix("refs/remotes/origin/")
    return None if _run(["git", "rev-parse", "--verify", "origin/main"], cwd) is None else "main"


def _changed_files(cwd: str) -> set[str]:
    """Every file the whole-worktree diff (merge-base → working tree) touches.

    Both committed and uncommitted changes count: the diff endpoint is the working
    tree, not ``HEAD``. This one command also replaces scanning per file, keeping
    the whole check at two git invocations per card. Deleted files may still be
    listed but drop out later on the on-disk existence check.
    """
    base = _merge_base(cwd)
    if not base:
        return set()
    out = _run(["git", "diff", "--name-only", "-z", base, "--"], cwd)
    if out is None:
        return set()
    return {path for path in out.split("\0") if path}


def _runs_git(cwd: str) -> bool:
    """True only when git exists AND the probe run answers; anything else is fail-open."""
    if _which_git() is None:
        return False
    try:
        return _run(["git", "rev-parse", "--is-inside-work-tree"], cwd) == "true\n"
    except Exception:
        return False


def _repo_references(cwd: str, names: list[str]) -> dict[str, set[tuple[str, int]]]:
    """``{name: {(file, line), ...}}`` of every whole-word occurrence in the tree.

    One ``git grep -w -o -n`` per batch of names (the card's requirement: one grep
    per batch). The caller subtracts each name's own definition site, so a constant
    that only ever names itself is unreferenced while a reference in a test file
    counts as a use. A git failure returns every name carrying no occurrence rows,
    which fails open (nothing gets flagged).
    """
    if not names:
        return {name: set() for name in names}
    out = _run(["git", "grep", "-o", "-n", "-w", "-F", "-e", *names, "--", "."], cwd)
    rows: dict[str, set[tuple[str, int]]] = {name: set() for name in names}
    if out is None:
        return rows
    for row in out.splitlines():
        parts = row.split(":", 2)
        if len(parts) != 3:
            continue
        path, line, match = parts
        if match in rows:
            rows[match].add((path, int(line) if line.isdigit() else -1))
    return rows


def _diff_added_names(cwd: str, path: str, base: str) -> tuple[set[str], set[str]]:
    """``(private module-level names the diff added in ``path``, imports it added)``.

    "Added" is decided by the unified diff against the merge-base (``git diff -U0
    <base> -- <path>``): a name counts when its definition or import sits on a ``+``
    line of this file's hunk — the merge-base endpoint makes committed and
    uncommitted edits identical, so no separate index walk is needed.
    """
    out = _run(["git", "diff", "-U0", "--no-color", base, "--", path], cwd)
    if out is None:
        return set(), set()
    added_lines: set[int] = set()
    line = 0
    for raw in out.splitlines():
        if raw.startswith("@@"):
            try:
                line = int(raw.split()[2].split(",")[0].lstrip("+"))
            except (IndexError, ValueError):
                line = 0
            continue
        if raw.startswith("+++") or raw.startswith("---") or raw.startswith("diff "):
            continue
        if raw.startswith("+"):
            added_lines.add(line)
            line += 1
        elif raw.startswith("-"):
            pass
        elif raw.startswith(" ") or raw == "":
            line += 1
    private: set[str] = set()
    imports: set[str] = set()
    source = (Path(cwd) / path).read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set(), set()
    for node in tree.body:
        name, lineno = _definition_name(node)
        if name and lineno in added_lines:
            private.add(name)
        for alias, lineno in _import_aliases(node):
            bound = alias.asname or alias.name.split(".")[0]
            if lineno in added_lines:
                imports.add(bound)
    return private, imports


def _merge_base(cwd: str) -> Optional[str]:
    """The merge-base of HEAD with the remote default branch, ``None`` when undeterminable."""
    default = _remote_default_branch(cwd)
    out = _run(["git", "merge-base", "HEAD", f"origin/{default}"], cwd) if default else None
    return out.strip() if (out or "").strip() else None


def check(tool_name: str, args: Any = None, **kwargs: Any) -> Optional[str]:
    """``pre_tool_call``: block ``kanban_request_review`` (or ``kanban_complete``) when the diff adds unused code."""
    if tool_name not in ("kanban_request_review", "kanban_complete") or not isinstance(args, dict) or not kanban_policy.configured_worker():
        return None
    if not (os.environ.get("HERMES_KANBAN_TASK") or "").strip():
        return None
    cwd = _workspace_path()
    if not cwd or not Path(cwd).is_dir():
        return None
    if not _runs_git(cwd):
        return None
    findings = _findings(cwd)
    if not findings:
        return None
    entries = ", ".join(f"{path}:{line}:{name}" for path, line, name in findings)
    return _MESSAGE_HEAD.format(tool=tool_name, entries=entries) + kanban_policy.RETRY_INSTRUCTION


def _findings(cwd: str) -> list[tuple[str, int, str]]:
    """Unused-code findings across the diff, as sorted ``(file, line, name)`` triples."""
    try:
        changed = _changed_files(cwd)
        python_files = sorted(
            path for path in changed
            if path.endswith(".py") and (Path(cwd) / path).is_file()
        )
        if not python_files:
            return []
        base = _merge_base(cwd)
        if not base:
            return []
        trees: dict[str, tuple[str, ast.Module]] = {}
        for path in python_files:
            source = (Path(cwd) / path).read_text(encoding="utf-8", errors="replace")
            trees[path] = (source, ast.parse(source))
        added = {path: _diff_added_names(cwd, path, base) for path in python_files}
        candidate_names = sorted({name for path in python_files for name in added[path][0]})
        refs = _repo_references(cwd, candidate_names)
        reexported = _reexported_names(cwd, python_files)
        findings: list[tuple[str, int, str]] = []
        for path in python_files:
            source, tree = trees[path]
            findings.extend(_unused_private_names(path, tree, added[path][0], refs))
            findings.extend(_unused_imports(path, tree, added[path][1], refs, reexported))
        return sorted(findings)
    except Exception:
        return []


def _module_all(tree: ast.Module) -> Optional[set[str]]:
    """The module's ``__all__`` names, ``None`` when the module declares none."""
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = [t for t in node.targets if isinstance(t, ast.Name) and t.id == "__all__"]
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target] if isinstance(node.target, ast.Name) and node.target.id == "__all__" else []
        else:
            continue
        if not targets:
            continue
        try:
            values = ast.literal_eval(node.value)
        except Exception:
            return None
        return {str(item) for item in values}
    return None


def _unused_private_names(path: str, tree: ast.Module, added: set[str],
                          refs: dict[str, set[tuple[str, int]]]) -> list[tuple[str, int, str]]:
    """Module-level ``_name`` definitions added by the diff that nothing references."""
    all_names = _module_all(tree) or set()
    used = _names_used_in_module(tree)
    findings: list[tuple[str, int, str]] = []
    for node in tree.body:
        name, lineno = _definition_name(node)
        if not name or name not in added:
            continue
        if name.startswith("__") or name in all_names:
            continue
        if name in used:
            continue
        occurrences = refs.get(name, set())
        if occurrences - {(path, lineno)}:
            continue
        findings.append((path, lineno, name))
    return findings


def _definition_name(node: ast.stmt) -> tuple[Optional[str], int]:
    """``(name, line)`` of a module-level private definition, ``(None, 0)`` otherwise."""
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        target = node.targets[0] if isinstance(node, ast.Assign) else node.target
        if isinstance(target, ast.Name) and target.id.startswith("_") and not target.id.startswith("__"):
            return target.id, node.lineno
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        if node.name.startswith("_") and not node.name.startswith("__"):
            return node.name, node.lineno
    return None, 0


def _names_used_in_module(tree: ast.Module) -> set[str]:
    """Names loaded anywhere in the module (a ``Load`` context; the def line is a Store)."""
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            used.add(node.id)
    return used


def _unused_imports(path: str, tree: ast.Module, added: set[str],
                    refs: dict[str, set[tuple[str, int]]],
                    reexported: set[str]) -> list[tuple[str, int, str]]:
    """Imports added by the diff that the module never uses.

    A package ``__init__.py`` that re-exports the same name (``from pkg.thing import
    getcwd``) keeps ``thing``'s import alive, and a name listed in ``__all__`` is a
    deliberate re-export too.
    """
    all_names = _module_all(tree) or set()
    used = _names_used_in_module(tree)
    findings: list[tuple[str, int, str]] = []
    for node in tree.body:
        for alias, lineno in _import_aliases(node):
            bound = alias.asname or alias.name.split(".")[0]
            if bound not in added or bound in all_names or bound in used or bound in reexported:
                continue
            if refs.get(bound, set()) - {(path, lineno)}:
                continue
            findings.append((path, lineno, bound))
    return findings


def _reexported_names(cwd: str, changed_paths: list[str]) -> set[str]:
    """Names a changed package ``__init__.py`` re-exports (from <module> import <name>)."""
    reexported: set[str] = set()
    for init_path in changed_paths:
        if not init_path.endswith("__init__.py"):
            continue
        source = Path(cwd, init_path)
        if not source.is_file():
            continue
        try:
            tree = ast.parse(source.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            for alias in node.names:
                reexported.add(alias.asname or alias.name)
    return reexported


def _import_aliases(node: ast.stmt) -> list[tuple[ast.alias, int]]:
    if isinstance(node, ast.Import):
        return [(alias, node.lineno) for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        return [(alias, node.lineno) for alias in node.names]
    return []


__all__ = ["check"]
