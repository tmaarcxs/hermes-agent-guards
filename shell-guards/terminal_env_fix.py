"""Rewrite a ``terminal`` command so two Hermes-side traps cost no extra turn.

1. A Hermes launcher can leak its own install site-packages into ``PYTHONPATH`` (a
   previous generation under ``.hermes/installs/`` is not recognised as Hermes-owned and
   is not stripped). A project venv on another Python version then imports Hermes'
   ``pydantic`` and fails to collect its tests. Unset it when it holds such an entry.
2. ``<test runner> ... | tail`` returns the exit status of ``tail``. Hermes then asks the
   model to re-run the command without the pipe. ``set -o pipefail`` gives the real
   status up front, so no re-run is needed. Only test runners: ``cmd | head`` would
   otherwise turn a harmless SIGPIPE into a failure.

The child shell can inherit the leak even when this process env is clean (Hermes builds
the child env itself), so the rewrite keys on the command (python-like tools), not on
``os.environ``. Fails open; idempotent.
"""

import re
from typing import Optional

_MARK = "case \":$PYTHONPATH:\" in *\"/.hermes/installs/\"*)"
_UNSET = _MARK + " unset PYTHONPATH;; esac; "
_RUNNER = re.compile(r"\b(?:pytest|unittest|ruff|mypy)\b")
_PIPE_TO_VIEWER = re.compile(r"(?<!\|)\|(?!\|)\s*(?:tail|head)\b[^|]*$")
_PYTHONISH = re.compile(r"(?:^|[\s;&|(/])(?:python[0-9.]*|pytest|pip[0-9.]*|uv|tox|nox|poetry|coverage)(?=[\s;&|)]|$)")


def fix(tool_name: str, args: dict) -> Optional[dict]:
    command = args.get("command")
    if tool_name != "terminal" or not isinstance(command, str) or not command.strip() or command.startswith(_MARK):
        return None
    prefix = _UNSET if _PYTHONISH.search(command) else ""
    if _RUNNER.search(command) and _PIPE_TO_VIEWER.search(command) and "pipefail" not in command:
        prefix += "set -o pipefail; "
    return {"command": prefix + command} if prefix else None
