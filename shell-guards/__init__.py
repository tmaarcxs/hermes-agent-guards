"""Force an outcome channel on bounded background terminal jobs and other shell hygiene.

Self-contained agent-guards plugin: terminal command hygiene (foreground
sleeps/polling loops, pinned PR merges, osascript, bare force pushes, recursive
deletion of a live Hermes home), the dispatch-label guard for paas-promote, the
background-jobs outcome-channel requirement, the inline-attach cap, argument
normalization and the skill-file size guard.

Intent (behaviour identical to the monolithic plugin):
- blocks: recursive ``find``/``grep -r``/``rg`` over the whole Hermes home, ``installs`` or
  ``benchmarks`` (about 2.5 million files; minutes per call);
  ``git stash`` writes (push, pop, drop, apply, clear; ``list``/``show`` pass) for
  workers, because the stash stack is shared by every worktree of the repo;
  ``process_manage`` ``poll`` (``wait`` returns at the process exit, ``log``
  reads output); foreground ``sleep`` of 5 s or more, or a ``for``/``while``/``until``
  polling loop holding a ``sleep`` of 3 s or more (run the wait as
  ``terminal(command=<the same command/loop>, background=true, notify=true)``,
  loops with ``notify=[<ready pattern>]``);
  a ``pgrep -f``/``pkill -f`` inside a loop in the foreground *and* the
  background (the pattern is part of the loop shell's own command line, so it
  self-matches and never exits — use ``pgrep -x`` or test the target directly);
  ``gh pr merge`` without ``--match-head-commit`` (add
  ``--match-head-commit <head-sha-of-the-pull-request>`` to the same call, the
  sha comes from ``gh pr view <number> --json headRefOid``); ``osascript`` (use
  ego-browser); ``git push``
  with a bare ``--force``/``-f``; ``gh auth switch``/``login``/``logout``/
  ``refresh`` (they change the shared active account for every Hermes process -
  act as another account with ``GH_TOKEN=$(gh auth token -u <user>) <command>``,
  default ``acme-bot``); recursive deletion of a live Hermes home
  (``rm -r``, ``find … -delete``, a ``--delete`` sync, a python delete call);
  a ``paas-promote`` dispatch to ``des`` without the ``auto-approval`` label;
  an unsigned ``git commit`` (or ``--amend``) in a repo that requires signed
  commits (a github.com origin whose ``origin/<default>`` last-20 history is
  >= 90% signed decides it from local data only; Azure remotes exempt);
  ``kanban_attach`` with more than 256 characters of inline base64 (the bytes
  get transcribed and corrupted — attach by path);
- turns a short ``process_manage`` ``wait`` timeout into the configured limit, so a wait runs to
  the process exit;
- clamps a foreground ``terminal`` ``timeout`` or a ``process_manage`` ``wait`` timeout that
  exceeds the call deadline (``timeouts.tools.sequential_call``, else 420 s) to deadline minus
  15 s (405 by default), before dispatch — a call above the deadline is otherwise killed by the
  host and returns nothing; and appends a deadline note to a ``terminal`` result whose own
  rewritten timeout killed it (405 s marker), naming the background=true/notify=true
  replacement call - the host's own deadline kill itself produces a result no transform hook
  sees, so those deaths stay silent;
- requires: a ``terminal`` call with ``background=true`` that is not a
  long-lived server declares an outcome channel (``notify=true``, a notify
  pattern list, or a positive ``heartbeat``);
- rewrites a ``terminal`` command: unsets a ``PYTHONPATH`` that holds a Hermes install
  site-packages entry, and adds ``set -o pipefail`` to a test runner piped to ``tail``/``head``
  so the real exit status shows (no "re-run without the pipe" turn);
- rewrites: a ``transform_tool_result`` turns Hermes core's poll-only promoted-process
  note ("poll it with process(action=\"poll\", session_id=...)") into the event-driven
  instruction consistent with the guards: ``process_manage(action="wait", session_id=...)``
  or ``process_manage(action="log", session_id=...)``, and an interrupted wait means end
  the turn and wait again in a later turn - never poll, never sleep. The note variant that
  already carries ``notify_on_complete=true`` keeps its poll hint;
- normalizes: empty-string optional Kanban arguments become absent, a
  ``task_id`` of the literal ``$HERMES_KANBAN_TASK`` becomes absent, and a
  doubled ``skill_view`` name ``x:x`` becomes ``x``;
- sizes: blocks ``skill_manage``/``write_file``/``patch`` calls that would push
  a skill markdown file over its limit (SKILL.md 6000, other skill ``.md``
  12000 characters), flags after the fact a ``terminal``/``execute_code``/
  ``patch`` call that left one over its limit (required action + tree-split
  instruction appended to the result, no revert), and refuses on ``pre_verify``
  to end the turn while a file this session flagged is still over its limit
  (re-measured at check time, at most two continuations per file per session).

Matches commands at command-word position outside quoted strings, so mentions
in strings, grep patterns and python -c bodies pass. It catches slips, not
deliberate evasion. Every gate fails open.
"""

from typing import Optional

try:
    from . import (
        _policy,
        background_notify_gate,
        base_branch_gate,
        git_stash_gate,
        hermes_home_gate,
        hermes_home_search_gate,
        paas_promote_gate,
        plugin_source_gate,
        process_poll_gate,
        signed_commit_gate,
        skill_size,
        terminal_env_fix,
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
    ctx.register_hook("pre_verify", _pre_verify)
    ctx.register_hook("transform_tool_result", _transform_tool_result)
    ctx.register_hook("transform_terminal_output", _transform_terminal_output)


def _post_tool_call(**kwargs) -> None:
    """Placeholder for the lane's shared ``record`` surface.

    The shell-lane guards need no post-call bookkeeping today; the hook stays
    registered so the plugin's hook surface is stable across the split.
    """
    return None


def _pre_verify(**kwargs) -> Optional[dict]:
    """Refuse to end the turn while a file this session flagged is still over its limit.

    See ``skill_size.check``: re-measures at check time, clears a fixed file silently,
    caps at two continuations per file per session, fails open.
    """
    try:
        return skill_size.verify_check(**kwargs)
    except Exception:
        return None


def _transform_tool_result(**kwargs) -> Optional[str]:
    """Rewrite core's poll-only promoted note first; the skill-size flag rides after.

    The host keeps the FIRST non-``None`` string across plugins (``model_tools.py``
    ``_apply_transform_tool_result_hook``), and callbacks do not chain — every callback
    gets the ORIGINAL result. The core note rewrite and the skill-size flag can therefore
    never both fire on one result (a promotion note result is not a skill write), so
    first-wins is safe: try the note rewrite, then skill_size's flag on the original.
    The deadline-completion note lives on ``transform_terminal_output`` instead: the
    host's own deadline kill produces ``_ToolTimeoutResult``, which this hook never
    sees (``_publish_sequential_result`` skips transforms for timed-out results).
    """
    try:
        rewritten = terminal_policy.rewrite_poll_instructions(kwargs.get("result"))
        if rewritten is not None:
            return rewritten
    except Exception:
        pass
    try:
        return skill_size.transform(
            kwargs.get("tool_name") or "", kwargs.get("args"), kwargs.get("result"),
            tool_call_id=kwargs.get("tool_call_id") or "")
    except Exception:
        return None


def _transform_terminal_output(**kwargs) -> Optional[str]:
    """Deliver the skill-size flag for a ``terminal`` call before the result hook runs.

    See ``skill_size.transform_terminal_output``: the host keeps only the first
    non-``None`` ``transform_tool_result`` string across plugins, so the flag must ride
    the earlier ``transform_terminal_output`` seam to survive alongside the
    ``kanban-guards`` delegation nudge. The deadline clamp rides the same seam: the
    terminal tool runs it INSIDE the tool, so the tool's own-timeout death of the
    rewritten 405 s call gets the ``background=true`` note the later result hooks
    cannot deliver.
    """
    if kwargs.get("returncode") == 124:
        try:
            note = terminal_policy.tool_note(
                kwargs.get("output"), kwargs.get("command") or "",
                kwargs.get("returncode"))
            if note is not None:
                return note
        except Exception:
            pass
    try:
        return skill_size.transform_terminal_output(
            kwargs.get("command") or "", kwargs.get("output"),
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
        (lambda: plugin_source_gate.check(tool_name, args), "plugin_source_gate"),
        (lambda: skill_size.check(tool_name, args), "skill_size"),
        (lambda: skill_size.record(tool_name, args, **kwargs), "skill_size"),
        (lambda: process_poll_gate.check(tool_name, args), "process_poll_gate"),
        (lambda: git_stash_gate.check(tool_name, args), "git_stash_gate"),
        (lambda: base_branch_gate.check(tool_name, args), "base_branch_gate"),
        (lambda: hermes_home_search_gate.check(tool_name, args), "hermes_home_search_gate"),
        (lambda: signed_commit_gate.check(tool_name, args), "signed_commit_gate"),
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
            changes = {**(background_notify_gate.fix(tool_name, args) or {}), **(terminal_env_fix.fix(tool_name, args) or {})}
            changes = {**(changes or {}), **(terminal_policy.rewrite_deadline_args(tool_name, args) or {})}
            return {"action": "modify", "args": changes} if changes else None
        blocked = terminal_policy.attach_cap_block(args)
        if blocked:
            return {"action": "block", "message": stamp(blocked, "__init__")}
        changes = terminal_policy.normalized_args(tool_name, args)
        if tool_name == "process_manage":
            changes = {**(changes or {}), **(process_poll_gate.fix(tool_name, args) or {}),
                       **(terminal_policy.rewrite_deadline_args(tool_name, args) or {})}
        return {"action": "modify", "args": changes} if changes else None
    except Exception:
        return None
