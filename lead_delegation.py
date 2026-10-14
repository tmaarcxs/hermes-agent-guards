"""Keep repository delivery work in Kanban workers, not in the lead chat.

Active only in worker setups (``kanban.default_assignee`` set; see ``kanban_policy``) and
only in the lead process, i.e. when ``HERMES_KANBAN_TASK`` is absent. There:
- ``write_file`` and ``patch`` on a path under a repository root are blocked;
- terminal ``git commit``, ``git push`` and ``gh pr create`` aimed at a repository root
  (named in the command or the workdir) are blocked.
The lead investigates, writes issues and cards, reviews and integrates; implementation goes
to a worker card. Repository roots default to ``~/git_repos`` and can be overridden with the
``AGENT_GUARDS_REPO_ROOTS`` environment variable (``os.pathsep``-separated).
"""

import os
import re
from pathlib import Path
from typing import Any, Optional

from . import kanban_policy

_START = r"(?:^|[;&|\n(`{]|\$\(|\bthen\b|\bdo\b|\belse\b)\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*"
_DELIVERY = re.compile(_START + r"(?:git\s+(?:-C\s+\S+\s+)?(?:commit|push)\b|gh\s+pr\s+create\b)")
_QUOTED = re.compile(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"")
MESSAGE = (
    "agent-guards blocked {tool}: repository implementation and delivery belong to a Kanban worker, not the lead "
    "chat. Create (or reuse) a worker card with kanban_create naming the worktree, the contract and a numbered "
    "Acceptance list, then review and integrate its result."
)


def repo_roots() -> list[Path]:
    raw = os.environ.get("AGENT_GUARDS_REPO_ROOTS") or "~/git_repos"
    return [Path(part).expanduser().resolve() for part in raw.split(os.pathsep) if part.strip()]


def _under_root(path: str) -> bool:
    if not path:
        return False
    resolved = Path(path).expanduser().resolve()
    return any(resolved == root or root in resolved.parents for root in repo_roots())


def _mentions_root(text: str) -> bool:
    expanded = text.replace("~", str(Path.home()))
    return any(str(root) in expanded for root in repo_roots())


def is_lead() -> bool:
    return not os.environ.get("HERMES_KANBAN_TASK") and bool(kanban_policy.configured_worker())


def check(tool_name: str, args: Any) -> Optional[str]:
    if tool_name not in ("write_file", "patch", "terminal") or not isinstance(args, dict) or not is_lead():
        return None
    if tool_name in ("write_file", "patch"):
        return MESSAGE.format(tool=tool_name) if _under_root(str(args.get("path") or "")) else None
    command = str(args.get("command") or "")
    if not _DELIVERY.search(_QUOTED.sub("''", command)):
        return None
    if _mentions_root(command) or _under_root(str(args.get("workdir") or "")):
        return MESSAGE.format(tool="terminal")
    return None
