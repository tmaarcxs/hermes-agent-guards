"""Block ``git stash`` writes for workers.

The stash stack lives in the repository, not in a worktree: every ``git worktree`` of the
repo shares it. A ``pop`` or ``drop`` can therefore restore or destroy another lane's
changes or one's own. Observed on spa#578: three ``git stash``, a ``git stash drop`` and a
``git stash pop`` in one worktree erased the diff and dropped foreign files into it.
``list`` and ``show`` only read the stack and stay allowed.

Worker setups only (``kanban.default_assignee`` set). Matches at command-word position
outside quoted strings and heredocs. Fails open.
"""

import re
from typing import Any, Optional

from . import _policy, hermes_home_gate, terminal_policy

_STASH = re.compile(
    terminal_policy._START + r"git\s+(?:(?:-C|-c)\s+\S+\s+)*stash\b(?:[ \t]+(?P<op>\S+))?"
)
_READ_ONLY = {"list", "show"}

MESSAGE = (
    "agent-guards blocked terminal: git stash is not allowed. The stash stack is shared by every "
    "worktree of the repository, so a pop or drop can restore or destroy another lane's changes "
    "or your own. To compare against the base use `git show HEAD:<file>`, `git diff`, or "
    "`git worktree add /tmp/<name> HEAD`."
)


def check(tool_name: str, args: Any) -> Optional[str]:
    if tool_name != "terminal" or not isinstance(args, dict):
        return None
    command = args.get("command")
    if not isinstance(command, str) or "stash" not in command:
        return None
    blanked = terminal_policy._QUOTED.sub("''", hermes_home_gate._strip_heredocs(command))
    if not any((m.group("op") or "") not in _READ_ONLY for m in _STASH.finditer(blanked)):
        return None
    return MESSAGE if _policy.configured_worker() else None
