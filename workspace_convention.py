"""Reject ``kanban_create`` calls that would litter a home directory.

Active whenever the worker setup is configured (see ``kanban_policy``): the same
``kanban.default_assignee`` switch gates it in the lead and in worker processes.
A card that names its own ``workspace_kind=dir`` or ``workspace_kind=worktree``
must place ``workspace_path`` inside the shared convention, whose single source
of truth is the ``git-ops`` skill:

- repo work: ``~/git_repos/worktrees/<repo>/<task>/`` — a linked worktree, never
  the main checkout and never ``<repo>/.worktrees``;
- spikes and work with no repo: ``~/git_repos/ad-hoc/<name>/``;
- the default card scratch stays ``~/.hermes/kanban/workspaces/<task>``
  (``workspace_kind=scratch``), which needs no path.

The check rejects a ``dir`` or ``worktree`` card whose explicit path falls
outside those roots, is a main checkout (``<path>/.git`` is a directory), or is
the home directory itself or one of its direct children. Scratch cards and cards
that let the board derive their path stay untouched, so the deterministic check
never blocks the default flow. Roots are home-relative and overridable with
``AGENT_GUARDS_WORKTREE_ROOT`` and ``AGENT_GUARDS_ADHOC_ROOT`` for tests. The
check fails open: any internal error leaves the call untouched.
"""

import os
from pathlib import Path
from typing import Any, Optional

from . import kanban_policy

CONVENTIONAL_KINDS = ("dir", "worktree")
WORKTREE_DEPTH = 2
ADHOC_DEPTH = 1


def _home() -> Path:
    return Path(os.environ.get("HERMES_REAL_HOME") or os.path.expanduser("~")).expanduser()


def worktree_root() -> Path:
    override = (os.environ.get("AGENT_GUARDS_WORKTREE_ROOT") or "").strip()
    return Path(override).expanduser() if override else _home() / "git_repos" / "worktrees"


def adhoc_root() -> Path:
    override = (os.environ.get("AGENT_GUARDS_ADHOC_ROOT") or "").strip()
    return Path(override).expanduser() if override else _home() / "git_repos" / "ad-hoc"


def _display(root: Path) -> str:
    """Render a root as ``~/...`` when it lives under the home directory."""
    home = _home()
    try:
        return "~/" + str(root.relative_to(home))
    except ValueError:
        return str(root)


def _resolve(raw: str) -> Optional[Path]:
    raw = str(raw).strip()
    if not raw or not (raw.startswith(("/", "~"))):
        return None
    try:
        return Path(raw).expanduser().resolve(strict=False)
    except (OSError, ValueError):
        return None


def _levels_below(root: Path, path: Path) -> Optional[int]:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return None
    return len([part for part in relative.parts if part not in ("", ".")])


def _is_main_checkout(path: Path) -> bool:
    try:
        return (path / ".git").is_dir()
    except OSError:
        return False


def _reasons(kind: str, path: Path) -> list[str]:
    home = _home()
    worktree = worktree_root()
    adhoc = adhoc_root()
    reasons: list[str] = []
    if path == home:
        reasons.append("workspace_path is the home directory itself")
    elif path.parent == home:
        reasons.append("workspace_path is a direct child of the home directory")
    if _is_main_checkout(path):
        reasons.append("workspace_path is a main checkout (its .git is a directory)")
    below_worktree = _levels_below(worktree, path)
    below_adhoc = _levels_below(adhoc, path)
    in_worktree = below_worktree is not None and below_worktree >= WORKTREE_DEPTH
    in_adhoc = below_adhoc is not None and below_adhoc >= ADHOC_DEPTH
    if kind == "worktree" and not in_worktree:
        reasons.append(
            f"workspace_kind=worktree must live under {_display(worktree)}/<repo>/<task>/"
        )
    elif kind == "dir" and not (in_worktree or in_adhoc):
        reasons.append(
            f"workspace_kind=dir must live under {_display(worktree)}/<repo>/<task>/ "
            f"or {_display(adhoc)}/<name>/"
        )
    return reasons


def violations(args: dict[str, Any]) -> list[str]:
    kind = str(args.get("workspace_kind") or "").strip()
    if kind not in CONVENTIONAL_KINDS:
        return []
    path = _resolve(str(args.get("workspace_path") or ""))
    if path is None:
        return []
    return _reasons(kind, path)


def message(found: list[str], args: dict[str, Any]) -> str:
    worktree = _display(worktree_root())
    adhoc = _display(adhoc_root())
    scratch = _display(_home() / ".hermes" / "kanban" / "workspaces")
    allowed = (
        f"Allowed: repo work in {worktree}/<repo>/<task>/, spikes with no repo in "
        f"{adhoc}/<name>/, and the default card scratch {scratch}/<task> "
        "(workspace_kind=scratch, no workspace_path)."
    )
    return (
        "agent-guards blocked kanban_create: workspace_path "
        f"{str(args.get('workspace_path') or '')!r} violates the workspace convention: "
        + "; ".join(found)
        + ". "
        + allowed
        + " Use scratch by default. Fix the call and retry — "
        + kanban_policy.RETRY_INSTRUCTION
    )


def check(tool_name: str, args: Any) -> Optional[str]:
    if tool_name != "kanban_create" or not isinstance(args, dict):
        return None
    if not kanban_policy.configured_worker():
        return None
    found = violations(args)
    if not found:
        return None
    return message(found, args)
