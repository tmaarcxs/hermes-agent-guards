# kanban-guards

Everything Kanban in one self-contained plugin. Worker gates (checkpoint, completion
acceptance, review checklist, handoff, stop, schedule, backup-copy, scratch-artifact,
GitHub contact, delegation policy), the lead gate (work nudge, board list scoping) and
the dispatcher wake chassis (`wakes` + `board_watch`), plus `active_pr_unblock`: a
dispatcher-tick observer that auto-lifts the `active_pr` respawn guard when a human
unblocked a card after its PR comment.

## unused_code_gate — no unused private Python code crosses a review handoff

The diff a card hands to review cannot add unused private Python code ('unused code is
not allowed, even if it does nothing'). Changed files are the diff between the
merge-base with the remote default branch (`origin/HEAD`, falling back to `main`) and
the working tree, so committed and uncommitted edits both count, and the gate answers
on `kanban_request_review` and `kanban_complete` of a card whose workspace is a git
worktree in a worker setup. Among the names the diff ADDS it flags, Python only:

- module-level private names — a single leading underscore on assignments, annotated
  assignments, `def`/`async def`/`class` — that nothing in the repo references. One
  `git grep -w` per batch of names decides; its `file:line:match` rows let the gate
  subtract each name's own definition site, so a constant that only names itself is
  unused while a reference anywhere (a test file counts) keeps the name. Names listed
  in `__all__` and dunder names are exempt;
- imports the module never uses. A re-export through a package `__init__.py` and a
  name listed in the module's `__all__` are deliberate re-exports, not dead imports.

The block message lists every `file:line:name` and the exact fix: delete them, rerun
the tests, and request review again. It fails open: no git binary, a git timeout, a
syntax-error file or any internal error leaves the call untouched — an unparsable file
is never a block.

Extracted from the monolithic `agent-guards` plugin; registration order inside this
plugin reproduces the Kanban part of the old root wiring verbatim (see the seam note in
`__init__.py`). Terminal hygiene (sleep/polling, PR-merge pin, osascript, force push,
home deletion), the inline-attach cap and Kanban argument normalization live in
`shell-guards`; write-location policy lives in `workspace-guards`; this plugin imports
nothing from them.

Run the tests:

```bash
PYTHONPATH="$HOME/.hermes/hermes-agent" ~/.hermes/hermes-agent/venv/bin/python -m pytest -q tests
```

## cli_create_gate — no lead-side CLI card creation

`kanban_create` (the tool) subscribes the calling chat to the new card;
`hermes kanban create` (the CLI) does not, so cards the lead opens from the
terminal finish without waking anyone — four guard cards sat approved for hours
that way on 2026-11-05. This gate (``pre_tool_call`` on ``terminal``) blocks any
``hermes [-p <profile>] [--board <slug>] kanban [--board <slug>] create`` in
processes that are neither a run on a card (``HERMES_KANBAN_TASK`` set) nor a
process of the worker profile itself (``HERMES_PROFILE`` == the configured
worker); the CLI stays the documented worker fallback there. It sees through
``python -m hermes_cli[.main]``, ``bash -c``, subshells, ``&&``/``;``/pipe
statements and ``KEY=VALUE``/``env`` prefixes, and stays silent for every other
``hermes kanban`` subcommand and for ``create --help`` (a quoted title or body
value can never open a new segment). The block message rebuilds the exact
``kanban_create`` tool call from the blocked command's own flags — title,
assignee, ``--workspace scratch|worktree|worktree:<p>|dir:<p>`` mapped onto
``workspace_kind``/``workspace_path``, project, idempotency key,
parents, skills, goal mode, last flag repetition winning (``--branch`` is read
and mapped honestly: the tool has no branch argument). With
``--body-file``, names the file text as the tool call's ``body`` argument. It
fails open on any parse error and needs ``kanban.default_assignee`` to know who
the worker profile is.

Run the tests:

```bash
PYTHONPATH="$HOME/.hermes/hermes-agent" ~/.hermes/hermes-agent/venv/bin/python -m pytest -q tests
```

## scratch_artifact_gate

Blocks `kanban_complete` and `kanban_request_review` (pre_tool_call, worker setups only)
on a card whose workspace kind is `scratch` — the workspace is deleted on completion, so
anything still only inside it is lost:

- the handoff text (summary, result, metadata values, nested) names an absolute path
  inside the card's scratch workspace that `metadata.artifacts` does not cover: covered
  means the exact path, or an artifact that is that path, a directory containing it, or
  an archive whose members include it (relative to the workspace or the archive stem);
- a git repository under the workspace has commits unreachable from any remote-tracking
  ref while `artifacts` holds no `.bundle`/`.patch` file.

The block message names every uncovered path (or the branch and head SHA of the unpushed
repo) and the exact fix: pass the absolute paths in `metadata.artifacts`
(`git bundle create <abs>.bundle <branch>`, or `git format-patch` for commits), and
states that the scratch workspace is deleted on completion. Fails open on any internal
error, on non-scratch workspaces and outside worker setups.
