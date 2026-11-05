"""workspace-guards: where writes and card workspaces may live.

Self-contained plugin holding three modules (two moved from the monolithic
``agent-guards`` plugin, base commit ``90456b2``, plus the local-edit gate):

- ``home_litter_gate`` blocks creating new top-level entries directly under the
  home directory (``~/kind_of长长的``, ``cd ~ && touch x``, ``> ~/notes``),
  resolving the command's real cwd from the terminal tool's ``workdir``
  argument and the session's recorded cwd, not only an in-string ``cd``.
  Reuses the path-parsing helpers in ``_paths``.
- ``workspace_convention`` (only with ``kanban.default_assignee`` configured)
  rejects a ``kanban_create`` whose ``dir``/``worktree`` workspace_path leaves
  the placement convention (outside ``<main-checkout>/.worktrees/<task>/`` or
  ``~/git_repos/ad-hoc/``) and, for every kind including an explicit
  ``scratch`` path or an omitted kind, a workspace_path that is a main
  checkout, the home dir or a direct child of it.
- ``plugin_local_edit_gate`` blocks writes into an installed plugin checkout
  (``~/.hermes/plugins/<name>/`` or ``~/.hermes/profiles/<profile>/plugins/<name>/``
  whose nearest git work tree carries an origin remote): the change belongs in
  the working clone under ``~/git_repos``, pushed, then synced with
  ``hermes -p <profile> plugins update <name>``. The message names the matching
  clone, the ``git worktree add`` command to start from it and every sync call.
  Reads, sanctioned ``hermes plugins`` verbs, read-only git shapes and writes
  inside a worktree of the working clone pass. detection is dynamic, never a
  name list, so a locally developed non-git plugin stays writable.

The small shared-library helpers these modules use (``configured_worker()``,
``RETRY_INSTRUCTION``) travel in a local ``_policy.py`` copy; this folder
imports nothing from sibling plugins.
"""

try:
    from . import home_litter_gate, plugin_local_edit_gate, workspace_convention
except ImportError:
    if __name__ != "__init__":
        raise
    """Bare ``pytest`` (no test path) can import this file as the top-level
    module ``__init__`` where the relative import has no parent package; the
    package body is dead weight there, so swallow the ImportError only in
    that synthetic shape and re-raise everywhere else."""


_MODULES = None  # bound lazily so the bare-pytest synthetic shape stays inert


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", _pre_tool_call)


def _modules():
    global _MODULES
    if _MODULES is None:
        from . import home_litter_gate, plugin_local_edit_gate, workspace_convention

        _MODULES = (home_litter_gate, plugin_local_edit_gate, workspace_convention)
    return _MODULES


def _pre_tool_call(tool_name: str = "", args=None, **kwargs):
    home_litter_gate, plugin_local_edit_gate, workspace_convention = _modules()

    def stamp(message: str, name: str) -> str:
        marker = message.find(": ")
        if not message.startswith("agent-guards blocked") or marker < 0:
            return message
        return message[: marker] + " [" + name + "]" + message[marker:]

    for check, name in (
        (lambda: home_litter_gate.check(tool_name, args, **kwargs), "home_litter_gate"),
        (lambda: plugin_local_edit_gate.check(tool_name, args, **kwargs), "plugin_local_edit_gate"),
        (lambda: workspace_convention.check(tool_name, args), "workspace_convention"),
    ):
        try:
            blocked = check()
        except Exception:
            blocked = None
        if blocked:
            return {"action": "block", "message": stamp(blocked, name)}
    return None
