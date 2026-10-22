# hermes-agent-guards

A monorepo of three self-contained Hermes Agent plugins that block recurring
agent mistakes before a tool runs and keep delegated Kanban work moving. Every
check fails open: an internal error never blocks a tool call.

These guards used to ship as one monolithic `agent-guards` plugin. They are now
three independent folders, each with its own `plugin.yaml`, its own
`register()`, and no imports into a sibling folder. Install and enable the ones
you want; they do not depend on each other.

## The three plugins

### `shell-guards` — terminal command hygiene

Answers: *is this shell command safe to run?*

- Blocks a foreground `sleep` of 30 s or more, or a polling loop holding a
  `sleep` of 15 s or more (run the wait in the background with a notification
  channel, or poll once and re-issue without sleeping).
- Blocks `gh pr merge` without `--match-head-commit` (merges must pin the
  verified head), `osascript`, and a bare `git push --force`/`-f` (use
  `--force-with-lease`).
- Blocks recursive deletion of a live Hermes home (`rm` with a recursive flag,
  `find -delete`, `rsync --delete` on a protected destination, python
  `rmtree`-family calls), including through a runner chain after a pipe.
- Requires a bounded `background=true` terminal job to declare an outcome
  channel (`notify=true`, a notify pattern list, or a positive `heartbeat`).
- Blocks a `paas-promote` dispatch to `des` without the `auto-approval` label.
- Blocks `kanban_attach` with more than 256 characters of inline base64
  (attach by path instead) and normalizes known-bad optional arguments.
- Keeps skill markdown files under their size limit (blocks a call that would
  push one over; flags after the fact one that landed over).

### `workspace-guards` — where writes and card workspaces may live

Answers: *is this path one the agent may write to?*

- Blocks writes (`write_file`, `patch`, terminal commands, git mutations,
  inline python file writes) that target anything outside the card's own task
  workspace: the protected Hermes tree (`~/.hermes` config, plugins, kanban
  state), the host's main checkouts, other workers' workspaces.
- Blocks creating a new top-level entry directly under the home directory,
  resolving the command's real cwd from the terminal tool's `workdir` argument
  and the session's recorded cwd, not only an in-string `cd`.
- When the profile config sets `kanban.default_assignee`, rejects a
  `kanban_create` whose `dir`/`worktree` workspace_path leaves the placement
  convention.

### `kanban-guards` — Kanban worker, lead and wake discipline

Answers: *is this lifecycle call, board read or wake legitimate?*

- Worker gates: force a budget checkpoint comment before a run is cut at the
  iteration cap; keep delegated cards on the configured worker profile; block
  `kanban_complete` until `metadata.acceptance` maps every numbered Acceptance
  item to evidence; keep the published-PR completion duty visible; block
  contacts with people on GitHub from worker terminals; block
  `kanban_request_review` until the eight pre-review checks are closed; bounce
  a narrated stop after a blocked lifecycle call; block `kanban_schedule`;
  refuse a handoff that preserves a backup copy of the delivered work.
- Lead gates: block the lead from editing repository files or running
  `git commit`/`git push`/`gh pr create` there; nudge the lead once per user
  turn after more than 5 exploration calls without a `kanban_create`; scope a
  lead chat's `kanban_list` result to the cards its own chat thread is
  subscribed to; slim the lead's `kanban_show` deterministically.
- Wakes: run every Kanban wake on one shared event-driven chassis over
  `board_watch` — stale parked cards, review-loop thresholds, the churn corner,
  waiting-on-external-gate runs, dispatcher stalls and unreconciled delivery
  notices — injected into the card's subscribed chat within one dispatcher tick.
  Wakes need `plugins.entries.kanban-guards.allow_gateway_injection: true`.

Each folder's `README.md` documents its own rules in full.

## Install

```
hermes plugins install tmaarcxs/hermes-agent-guards#shell-guards
hermes plugins install tmaarcxs/hermes-agent-guards#workspace-guards
hermes plugins install tmaarcxs/hermes-agent-guards#kanban-guards
```

The `#<subdir>` fragment is the CLI's subdirectory syntax (see
`_resolve_git_url` in `hermes_cli/plugins_cmd.py`): the repository is cloned
once and only the named folder is installed. The `owner/repo/<subdir>` form
resolves the same way.

## Layout

```
shell-guards/       one self-contained plugin (modules + plugin.yaml + tests)
workspace-guards/   one self-contained plugin
kanban-guards/      one self-contained plugin
tests/              cross-plugin checks (helper-copy divergence, transform winner)
pytest.ini          runs all three folders plus tests/ in one command
CUTOVER.md          ordered activation steps for replacing the old plugin
```

The two small shared-library helpers (`RETRY_INSTRUCTION`, `configured_worker()`)
are deliberately duplicated per folder instead of shared: a common plugin would
re-introduce a load-order dependency across three otherwise independent plugins.
`tests/test_helper_copies_agree.py` fails when the copies drift apart.

## Tests

```
PYTHONPATH="$HOME/.hermes/hermes-agent" ~/.hermes/hermes-agent/venv/bin/python -m pytest
```

Run from the repo root; the suite needs Python 3.10+ and the Hermes checkout on
`PYTHONPATH`. A `pytest.ini` at the root collects all three plugin folders plus
`tests/` in one command.
