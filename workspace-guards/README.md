# workspace-guards

Self-contained Hermes plugin: **where writes and card workspaces may live.**

Moved out of the monolithic `agent-guards` plugin (base commit `90456b2`) as
part of the three-plugin monorepo split (`shell-guards`, `workspace-guards`,
`kanban-guards`).

## What it answers

| Module | Question it answers |
|---|---|
| `workspace_write_gate` | Is this write (`write_file`, `patch`, a terminal command, an inline python file write, a git mutation) aimed at something outside this card's own task workspace — the protected Hermes tree, another worker's workspace, a main checkout? Block it. |
| `home_litter_gate` | Is this terminal command about to drop a new top-level entry directly under the home directory? Block it (resolves the real cwd from the `workdir` argument and the session's recorded cwd, not only an in-string `cd`). |
| `workspace_convention` | Does this `kanban_create` place its `dir`/`worktree` workspace outside the convention (`<main-checkout>/.worktrees/<task>/` or `~/git_repos/ad-hoc/`), or point any kind — even `scratch` or no kind — at a main checkout, the home dir or a direct child of it? Reject it. Active only when `kanban.default_assignee` is configured. |

## Install

```
hermes plugins install tmaarcxs/hermes-agent-guards#workspace-guards
```

Registers one hook: `pre_tool_call`.

## Layout

- `_policy.py` — local copy of the two shared helpers these modules need
  (`configured_worker()`, `RETRY_INSTRUCTION`); the monorepo root's
  `tests/test_helper_copies_agree.py` fails if this copy drifts from its
  siblings. This folder imports nothing from sibling plugins.
- `workspace_write_gate` and `home_litter_gate` stay in one folder on purpose:
  `home_litter_gate` reuses nine private helpers of `workspace_write_gate`.
- `tests/conftest.py` registers the package under its canonical module name
  (`sys.modules["workspace_guards"]`), so the moved tests run verbatim under the
  new package name `workspace-guards`. Run with
  `PYTHONPATH="$HOME/.hermes/hermes-agent"` and the Hermes venv python (3.10+):

  ```
  cd workspace-guards && ~/.hermes/hermes-agent/venv/bin/python -m pytest -q
  ```
