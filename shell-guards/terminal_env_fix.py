"""Rewrite a ``terminal`` command so two Hermes-side traps cost no extra turn.

1. A Hermes launcher can leak its own install site-packages into ``PYTHONPATH`` (a
   previous generation under ``.hermes/installs/`` is not recognised as Hermes-owned and
   is not stripped). A project venv on another Python version then imports Hermes'
   ``pydantic`` and fails to collect its tests. Unset it when it holds such an entry.
2. ``<test runner> ... | tail`` returns the exit status of ``tail``. Hermes then asks the
   model to re-run the command without the pipe. ``set -o pipefail`` gives the real
   status up front, so no re-run is needed. Only test runners: ``cmd | head`` would
   otherwise turn a harmless SIGPIPE into a failure.
3. A recursive ``grep`` without ``--exclude-dir`` walks ``node_modules``, ``.git`` and ``.venv``.
   Over 30 days of worker runs, recursive searches of 30 s or more were 25 % of the long
   foreground terminal time and most had no exclusion. The three directories are excluded
   unless the command already names ``--exclude-dir`` or ``node_modules``.

The child shell can inherit the leak even when this process env is clean (Hermes builds
the child env itself), so the rewrite keys on the command (python-like tools), not on
``os.environ``. Fails open; idempotent.
"""

import re
import shutil
from typing import Optional

from . import terminal_policy

_MARK = "case \":$PYTHONPATH:\" in *\"/.hermes/installs/\"*)"
_UNSET = _MARK + " unset PYTHONPATH;; esac; "
_RUNNER = re.compile(r"\b(?:pytest|unittest|ruff|mypy)\b")
_PIPE_TO_VIEWER = re.compile(r"(?<!\|)\|(?!\|)\s*(?:tail|head)\b[^|]*$")
_GREP = re.compile(terminal_policy._START + r"(?P<tool>grep|egrep|fgrep)(?=\s)(?P<rest>[^;|&\n]*)")
_RECURSIVE = re.compile(r"(?:^|\s)(?:--recursive|-[A-Za-z]*[rR][A-Za-z]*)(?=\s|$)")
_EXCLUDES = " --exclude-dir=node_modules --exclude-dir=.git --exclude-dir=.venv"
_PYTHONISH = re.compile(r"(?:^|[\s;&|(/])(?:python[0-9.]*|pytest|pip[0-9.]*|uv|tox|nox|poetry|coverage)(?=[\s;&|)]|$)")


_TIMEOUT = re.compile(
    terminal_policy._START + r"(?P<tool>timeout)(?P<flags>(?:\s+(?:--foreground|--preserve-status|-[ks]\s+\S+|--(?:kill-after|signal)=\S+))*)"
    r"\s+(?P<dur>\d+(?:\.\d+)?[smhd]?)(?=\s)"
)
_PERL_TIMEOUT = (
    "perl -e '$d=shift;$d*={s=>1,m=>60,h=>3600,d=>86400}->{$1} if $d=~s/([smhd])$//;"
    "$p=fork;if(!$p){exec @ARGV;exit 127}$SIG{ALRM}=sub{kill 15,$p;exit 124};alarm $d;waitpid $p,0;exit($?>>8)'"
)


def _shim_timeout(command: str) -> str:
    """macOS has no ``timeout`` (145 "command not found" in 30 days): same contract, exit 124 on expiry.

    shortcut: ignores -k/-s (always TERM); upgrade if a call needs them.
    """
    if "<<" in command or "timeout" not in command or shutil.which("timeout"):
        return command
    masked = terminal_policy._QUOTED.sub(lambda m: m.group()[0] + "x" * (len(m.group()) - 2) + m.group()[-1], command)
    for m in reversed(list(_TIMEOUT.finditer(masked))):
        command = command[: m.start("tool")] + _PERL_TIMEOUT + " " + m.group("dur") + command[m.end("dur"):]
    return command


def _exclude_bulk_dirs(command: str) -> str:
    """Keep a recursive ``grep`` out of ``node_modules``, ``.git`` and ``.venv`` unless it names them."""
    if "<<" in command or "grep" not in command:
        return command
    masked = terminal_policy._QUOTED.sub(lambda m: m.group()[0] + "x" * (len(m.group()) - 2) + m.group()[-1], command)
    cuts = [
        m.end("tool")
        for m in _GREP.finditer(masked)
        if _RECURSIVE.search(m.group("rest")) and "--exclude-dir" not in command[m.end("tool"): m.end()]
        and "node_modules" not in command[m.end("tool"): m.end()]
    ]
    for cut in reversed(cuts):
        command = command[:cut] + _EXCLUDES + command[cut:]
    return command


def fix(tool_name: str, args: dict) -> Optional[dict]:
    command = args.get("command")
    if tool_name != "terminal" or not isinstance(command, str) or not command.strip() or command.startswith(_MARK):
        return None
    prefix = _UNSET if _PYTHONISH.search(command) else ""
    if _RUNNER.search(command) and _PIPE_TO_VIEWER.search(command) and "pipefail" not in command:
        prefix += "set -o pipefail; "
    rewritten = prefix + _exclude_bulk_dirs(_shim_timeout(command))
    return {"command": rewritten} if rewritten != command else None
