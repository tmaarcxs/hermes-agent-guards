"""Force an outcome channel on bounded background terminal jobs and other shell hygiene.

Self-contained agent-guards plugin: terminal command hygiene (foreground
sleeps/polling loops, pinned PR merges, osascript, bare force pushes, recursive
deletion of a live Hermes home), the dispatch-label guard for paas-promote, the
background-jobs outcome-channel requirement, the inline-attach cap, argument
normalization and the skill-file size guard.

Intent (behaviour identical to the monolithic plugin):
- blocks: foreground ``sleep`` of 30 s or more, or a ``for``/``while``/``until``
  polling loop holding a ``sleep`` of 15 s or more (run the wait with
  ``background=true`` and a notify channel, or poll once without sleeping);
  ``gh pr merge`` without ``--match-head-commit``; ``osascript``; ``git push``
  with a bare ``--force``/``-f``; recursive deletion of a live Hermes home
  (``rm -r``, ``find … -delete``, a ``--delete`` sync, a python delete call);
  a ``paas-promote`` dispatch to ``des`` without the ``auto-approval`` label;
  ``kanban_attach`` with more than 256 characters of inline base64 (the bytes
  get transcribed and corrupted — attach by path);
- requires: a ``terminal`` call with ``background=true`` that is not a
  long-lived server declares an outcome channel (``notify=true``, a notify
  pattern list, or a positive ``heartbeat``);
- normalizes: empty-string optional Kanban arguments become absent, a
  ``task_id`` of the literal ``$HERMES_KANBAN_TASK`` becomes absent, and a
  doubled ``skill_view`` name ``x:x`` becomes ``x``;
- sizes: blocks ``skill_manage``/``write_file``/``patch`` calls that would push
  a skill markdown file over its limit (SKILL.md 6000, other skill ``.md``
  12000 characters), and flags after the fact a ``terminal``/``execute_code``/
  ``patch`` call that left one over its limit (instruction appended to the
  result, no revert).

Matches commands at command-word position outside quoted strings, so mentions
in strings, grep patterns and python -c bodies pass. It catches slips, not
deliberate evasion. Every gate fails open.
"""

from typing import Optional

try:
    from . import (
        _policy,
        background_notify_gate,
        hermes_home_gate,
        paas_promote_gate,
        skill_size,
        terminal_policy,
    )
except ImportError:
    if __name__ != "__init__":
        raise
    """Bare ``pytest`` (no test path) can import this file as the top-level
    module ``__init__`` where the relative import has no parent package; the
    package body is dead weight there, so swallow the ImportError only in
    that synthetic shape and re-raise everywhere else."""


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", _pre_tool_call)
    ctx.register_hook("post_tool_call", _post_tool_call)
    ctx.register_hook("transform_tool_result", _transform_tool_result)


def _post_tool_call(**kwargs) -> None:
    """Placeholder for the lane's shared ``record`` surface.

    The shell-lane guards need no post-call bookkeeping today; the hook stays
    registered so the plugin's hook surface is stable across the split.
    """
    return None


def _transform_tool_result(**kwargs) -> Optional[str]:
    try:
        return skill_size.transform(
            kwargs.get("tool_name") or "", kwargs.get("args"), kwargs.get("result"),
            tool_call_id=kwargs.get("tool_call_id") or "")
    except Exception:
        return None


def _pre_tool_call(tool_name: str = "", args: Optional[dict] = None, **kwargs) -> Optional[dict]:
    def stamp(message: str, name: str) -> str:
        marker = message.find(": ")
        if not message.startswith("agent-guards blocked") or marker < 0:
            return message
        return message[: marker] + " [" + name + "]" + message[marker:]

    for check, name in (
        (lambda: paas_promote_gate.check(tool_name, args), "paas_promote_gate"),
        (lambda: skill_size.check(tool_name, args), "skill_size"),
        (lambda: skill_size.record(tool_name, args, **kwargs), "skill_size"),
        (lambda: background_notify_gate.check(tool_name, args), "background_notify_gate"),
    ):
        try:
            blocked = check()
        except Exception:
            blocked = None
        if blocked:
            return {"action": "block", "message": stamp(blocked, name)}
    try:
        args = args if isinstance(args, dict) else {}
        if tool_name == "terminal":
            found = terminal_policy.terminal_violations(str(args.get("command") or ""), bool(args.get("background")))
            if found:
                return {"action": "block", "message": "agent-guards blocked terminal [__init__]: " + "; ".join(found)}
            return None
        blocked = terminal_policy.attach_cap_block(args)
        if blocked:
            return {"action": "block", "message": stamp(blocked, "__init__")}
        changes = terminal_policy.normalized_args(tool_name, args)
        return {"action": "modify", "args": changes} if changes else None
    except Exception:
        return None
