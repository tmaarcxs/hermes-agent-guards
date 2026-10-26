"""Remind the lead to update Jot before a work turn ends.

Marcos keeps one generic line per outcome on his weekly Jot note, so a turn that ends
without a Jot call leaves the board showing the old state. This rides the ``pre_verify``
gate (``hermes_cli.plugins.get_pre_verify_continue_message``: once per turn-end after file
edits, bounded by ``agent.max_verify_nudges``) and stays silent as soon as a mutating Jot
tool ran in that turn.

Scope, so the reminder does not fire on every turn: a turn counts as work only when it
changed a file inside a git work tree. Agent upkeep (skills, plugins, config, memory under
``~/.hermes``) edits no repository, so it stays silent. Extend or narrow the exclusion with
``plugins.entries.jot-guards.settings.jot_nudge_skip_roots`` and switch the reminder off
with ``settings.jot_nudge: false``.

Hermes-side gap this cannot close: ``pre_verify`` only fires on a turn that mutated
files through a file tool. A turn that only runs commands or queries gets no reminder;
covering those needs a core stop gate, not a plugin hook.

Two invariants sit below: read-only Jot tools never count (``jot_read`` answers "what does
the board say", so only a mutating call marks the turn as updated), and ``_state`` maps a
session to ``(last turn seen, a mutating Jot tool ran in that turn)``. The turn id arrives
through ``post_tool_call`` and opens a fresh window, because ``pre_verify`` sends no turn id
of its own.
"""

from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_PLUGIN_KEY = "jot-guards"
MUTATING_TOOLS = frozenset({"jot_create", "jot_update", "jot_set_task", "jot_delete"})
DEFAULT_SKIP_ROOTS = ("~/.hermes",)
_MAX_SESSIONS = 200

MESSAGE = (
    "[System: jot-guards. No Jot tool ran this turn, and this turn changed files in a work "
    "repository, so the weekly board still shows the old state. Update it now, in this same "
    "response: `jot_read` first, then `jot_update`/`jot_set_task`, one generic line per outcome "
    "with its state and next step, per the `jot-weekly-board` skill. Then finish.]"
)

_state: "OrderedDict[str, Tuple[str, bool]]" = OrderedDict()
_settings_cache: Optional[Dict[str, Any]] = None


def reset() -> None:
    """Clear per-session and config state (used by the test suite)."""
    global _settings_cache
    _state.clear()
    _settings_cache = None


def settings() -> Dict[str, Any]:
    """``plugins.entries.jot-guards.settings``, cached."""
    global _settings_cache
    if _settings_cache is None:
        try:
            from hermes_cli.config import load_config_readonly

            entries = ((load_config_readonly() or {}).get("plugins") or {}).get("entries") or {}
            entry = entries.get(_PLUGIN_KEY) or {}
            found = entry.get("settings") or entry.get("config") or {}
            _settings_cache = found if isinstance(found, dict) else {}
        except Exception:
            _settings_cache = {}
    return _settings_cache


def skip_roots() -> List[Path]:
    """Directories whose edits are never professional work (``~/.hermes`` by default)."""
    configured = settings().get("jot_nudge_skip_roots")
    raw = configured if isinstance(configured, list) and configured else list(DEFAULT_SKIP_ROOTS)
    roots = []
    for item in raw:
        try:
            roots.append(Path(str(item)).expanduser().resolve())
        except Exception:
            continue
    return roots


def is_work_path(path: str, roots: Optional[List[Path]] = None) -> bool:
    """True when ``path`` sits in a git work tree outside every skip root."""
    try:
        target = Path(str(path)).expanduser().resolve()
    except Exception:
        return False
    for root in skip_roots() if roots is None else roots:
        if target == root or root in target.parents:
            return False
    return any((parent / ".git").exists() for parent in (target, *target.parents))


def record(tool_name: str = "", session_id: str = "", turn_id: str = "", **kwargs) -> None:
    """``post_tool_call``: open the turn's window and remember a mutating Jot call."""
    session = str(session_id or kwargs.get("task_id") or "")
    if not session:
        return
    turn = str(turn_id or "")
    seen_turn, updated = _state.get(session, ("", False))
    if turn and seen_turn and turn != seen_turn:
        updated = False
    _state[session] = (turn or seen_turn, updated or tool_name in MUTATING_TOOLS)
    _state.move_to_end(session)
    while len(_state) > _MAX_SESSIONS:
        _state.popitem(last=False)


def check(
    session_id: str = "", attempt: int = 0, changed_paths: Optional[list] = None, **kwargs
) -> Optional[dict[str, str]]:
    """``pre_verify``: keep the turn going once per turn while no Jot update happened.

    Worker processes are skipped: their handoff is the card, and the lead keeps the Jot line.
    """
    from os import getenv

    if attempt:
        return None
    if (getenv("HERMES_KANBAN_TASK") or "").strip():
        return None
    if settings().get("jot_nudge") is False:
        return None
    roots = skip_roots()
    if not any(is_work_path(path, roots) for path in (changed_paths or [])):
        return None
    if _state.get(str(session_id or kwargs.get("task_id") or ""), ("", False))[1]:
        return None
    return {"action": "continue", "message": MESSAGE}


__all__ = [
    "MESSAGE",
    "MUTATING_TOOLS",
    "check",
    "is_work_path",
    "record",
    "reset",
    "settings",
    "skip_roots",
]
