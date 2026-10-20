"""Reject ``kanban_create`` calls that would litter a home directory.

Active whenever the worker setup is configured (see ``kanban_policy``): the same
``kanban.default_assignee`` switch gates it in the lead and in worker processes.
A card that names its own ``workspace_kind=dir`` or ``workspace_kind=worktree``
must place ``workspace_path`` inside the shared convention, whose single source
of truth is the ``git-ops`` skill (``references/skill-detail.md``, "Worktree
placement (user-mandated)"):

- repo work: ``<main-checkout>/.worktrees/<task>/`` — a linked worktree inside
  the verified main checkout, never the main checkout itself, never nested
  inside another ``.worktrees``, and never a central sibling directory;
- work with no repo: ``~/git_repos/ad-hoc/<name>/``;
- the default card scratch stays ``~/.hermes/kanban/workspaces/<task>``
  (``workspace_kind=scratch``), which needs no path.

The check rejects a ``dir`` or ``worktree`` card whose explicit path falls
outside those roots. Independently of the kind, any ``kanban_create`` that
carries an explicit path which is a main checkout (``<path>/.git`` is a
directory), the home directory itself, or one of its direct children is rejected
too: the host uses an explicit ``scratch`` path verbatim and defaults an omitted
kind to ``scratch``, so those three shapes are the same home-litter threat for
every kind. Scratch cards and cards that let the board derive their path (no
``workspace_path``) stay untouched, so the deterministic check never blocks the
default flow. The ad-hoc root is home-relative and overridable
with ``AGENT_GUARDS_ADHOC_ROOT`` for tests; the worktree rule is derived from the
path itself (its ``.worktrees`` segment and the main-checkout prefix above it),
so it cannot drift from the layout the skill mandates. The check fails open: any
internal error leaves the call untouched.
"""

import os
from pathlib import Path
from typing import Any, Optional

from . import kanban_policy

CONVENTIONAL_KINDS = ("dir", "worktree")
ADHOC_DEPTH = 1
WORKTREE_SEGMENT = ".worktrees"
CANONICAL_WORKTREE = "<main-checkout>/" + WORKTREE_SEGMENT + "/<task>/"


def _home() -> Path:
    return Path(os.environ.get("HERMES_REAL_HOME") or os.path.expanduser("~")).expanduser()


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


def _worktree_base(path: Path) -> Optional[Path]:
    """Return the main checkout prefix of ``<main-checkout>/.worktrees/<task>/``.

    ``None`` unless the path has exactly one ``.worktrees`` segment, at least one
    segment below it, and the prefix above it is a verified git main checkout
    (its ``.git`` is a directory). A path nested inside another ``.worktrees``
    therefore never qualifies.
    """
    parts = path.parts
    if parts.count(WORKTREE_SEGMENT) != 1:
        return None
    index = parts.index(WORKTREE_SEGMENT)
    if index == 0 or index >= len(parts) - 1:
        return None
    base = Path(*parts[:index])
    return base if _is_main_checkout(base) else None


def _unconditional_reasons(path: Path) -> list[str]:
    """Reasons that hold for every kind, including ``scratch`` and an omitted one.

    The host uses an explicit ``scratch`` ``workspace_path`` verbatim and
    defaults an omitted ``workspace_kind`` to ``scratch``, so a home directory,
    a direct child of it, or a main checkout is the same home-litter threat no
    matter which kind the call names.
    """
    home = _home()
    reasons: list[str] = []
    if path == home:
        reasons.append("workspace_path is the home directory itself")
    elif path.parent == home:
        reasons.append("workspace_path is a direct child of the home directory")
    if _is_main_checkout(path):
        reasons.append("workspace_path is a main checkout (its .git is a directory)")
    return reasons


def _kind_reasons(kind: str, path: Path) -> list[str]:
    """Root rules for the conventional kinds only (``dir`` / ``worktree``)."""
    adhoc = adhoc_root()
    reasons: list[str] = []
    below_adhoc = _levels_below(adhoc, path)
    in_adhoc = below_adhoc is not None and below_adhoc >= ADHOC_DEPTH
    in_worktree = _worktree_base(path) is not None
    if kind == "worktree" and not in_worktree:
        reasons.append(
            f"workspace_kind=worktree must live at {CANONICAL_WORKTREE} inside the "
            "repository's verified main checkout"
        )
    elif kind == "dir" and not (in_worktree or in_adhoc):
        reasons.append(
            f"workspace_kind=dir must live at {CANONICAL_WORKTREE} or "
            f"{_display(adhoc)}/<name>/"
        )
    return reasons


def violations(args: dict[str, Any]) -> list[str]:
    path = _resolve(str(args.get("workspace_path") or ""))
    if path is None:
        return []
    kind = str(args.get("workspace_kind") or "").strip()
    reasons = _unconditional_reasons(path)
    if kind in CONVENTIONAL_KINDS:
        reasons += _kind_reasons(kind, path)
    return reasons


def message(found: list[str], args: dict[str, Any]) -> str:
    adhoc = _display(adhoc_root())
    scratch = _display(_home() / ".hermes" / "kanban" / "workspaces")
    allowed = (
        f"Allowed: repo work at {CANONICAL_WORKTREE} (a linked worktree under the "
        f"repository's verified main checkout), work with no repo at {adhoc}/<name>/, "
        f"and the default card scratch {scratch}/<task> "
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
