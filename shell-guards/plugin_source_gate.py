"""Plugin-source policy: a plugin is only activated from a pushed revision of its repository.

The rule (Marcos, 2026-10-30): a change to one of our plugins is made in the
repository, pushed, and only then installed. The order is always:

1. edit in the repository clone,
2. commit and push,
3. ``hermes plugins enable`` / ``update`` to install the pushed revision.

Why the gate is on activation and not on the edit: the installed plugin directory
*is* the checkout of its repository, so an edit and a repo edit are the same file.
Blocking the edit would block the commit that is supposed to follow it. What must
never happen is a plugin going live from a revision the repository does not have —
that is the drift this gate closes.

So ``hermes plugins enable`` and ``hermes plugins update`` are refused while the
target plugin's work tree has uncommitted changes, or its HEAD is ahead of its
upstream branch. Commit and push, then activate.

Read-only commands, plugins that are not git checkouts, and any error leave the
call untouched. The gate fails open.
"""

import os
import re
import subprocess
from pathlib import Path
from typing import Any, Optional

_ACTIVATE = re.compile(r"\bhermes\s+plugins\s+(enable|update|install)\b")


def plugin_roots() -> list[Path]:
    """Directories Hermes installs plugins into."""
    roots = []
    try:
        from hermes_constants import get_hermes_home

        roots.append(Path(get_hermes_home()) / "plugins")
    except Exception:
        home = os.environ.get("HERMES_HOME") or os.path.join(os.path.expanduser("~"), ".hermes")
        roots.append(Path(home) / "plugins")
    try:
        from hermes_cli.plugins import get_bundled_plugins_dir

        roots.append(Path(get_bundled_plugins_dir()))
    except Exception:
        pass
    return roots


def _git(plugin: Path, *args: str) -> Optional[str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(plugin), *args], capture_output=True, text=True, timeout=10
        )
    except Exception:
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def is_git_checkout(plugin: Path) -> bool:
    return _git(plugin, "rev-parse", "--is-inside-work-tree") == "true"


def uncommitted_paths(plugin: Path) -> Optional[list[str]]:
    """Paths with uncommitted changes in the plugin's work tree, or None when unknown."""
    out = _git(plugin, "status", "--porcelain", "--untracked-files=all")
    if out is None:
        return None
    return [line[3:].strip().strip('"') for line in out.splitlines() if len(line) > 3]


def head_is_pushed(plugin: Path) -> Optional[bool]:
    """True when HEAD matches the upstream branch; None when that cannot be read."""
    upstream = _git(plugin, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    if not upstream:
        return None
    local = _git(plugin, "rev-parse", "HEAD")
    remote = _git(plugin, "rev-parse", upstream)
    if local is None or remote is None:
        return None
    return local == remote


def find_plugin(name: str) -> Optional[Path]:
    """The installed plugin directory called *name*, or None."""
    for root in plugin_roots():
        candidate = root / name
        try:
            if candidate.is_dir() and is_git_checkout(candidate):
                return candidate
        except Exception:
            continue
    return None


def activation_block(command: str) -> Optional[str]:
    """The block message for activating a plugin that is not clean and pushed, or None."""
    text = str(command or "")
    match = _ACTIVATE.search(text)
    if not match:
        return None
    for root in plugin_roots():
        try:
            entries = [e for e in root.iterdir() if e.is_dir()]
        except Exception:
            continue
        for entry in entries:
            if str(entry) not in text and entry.name not in text:
                continue
            if not is_git_checkout(entry):
                continue
            dirty = uncommitted_paths(entry)
            if dirty:
                preview = ", ".join(dirty[:5])
                return (
                    f"agent-guards blocked {match.group(0)} [plugin_source_gate]: plugin "
                    f"'{entry.name}' has UNCOMMITTED changes ({preview}). Our plugin rule: make the "
                    "change in the repository, COMMIT it, PUSH it, then activate the pushed revision. "
                    "Commit and push first, then retry."
                )
            if head_is_pushed(entry) is False:
                return (
                    f"agent-guards blocked {match.group(0)} [plugin_source_gate]: plugin "
                    f"'{entry.name}' has local commits that are not pushed. Our plugin rule: edit in the "
                    "repository, COMMIT, PUSH, then activate. Push first, then retry."
                )
    return None


def check(tool_name: str = "", args: Any = None, **kwargs: Any) -> Optional[str]:
    """``pre_tool_call``: the activation gate, applied to the shell commands that activate."""
    try:
        if str(tool_name or "") != "terminal" or not isinstance(args, dict):
            return None
        return activation_block(str(args.get("command") or ""))
    except Exception:
        return None


__all__ = [
    "activation_block", "check", "find_plugin", "head_is_pushed", "is_git_checkout",
    "plugin_roots", "uncommitted_paths",
]
