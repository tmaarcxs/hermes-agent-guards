# hermes-agent-guards

A monorepo of five self-contained Hermes Agent plugins: three that block
recurring agent mistakes before a tool runs and keep delegated Kanban work
moving, plus two smaller guards. Every guard check fails open: an internal
error never blocks a tool call.

The three guards used to ship as one monolithic `agent-guards` plugin. They are
now independent folders, each with its own `plugin.yaml`, its own
`register()`, and no imports into a sibling folder. Install and enable the ones
you want; they do not depend on each other.

## The plugins

### `shell-guards` — terminal command hygiene

Answers: *is this shell command safe to run?*

- Blocks `git stash` writes for workers (the stash stack is shared by every worktree), `process_manage` `poll`, a foreground `sleep` of 5 s or more, and a polling loop holding a
  `sleep` of 3 s or more (run the wait in the background with a notification
  channel; `wait` returns at the process exit). The same thresholds cover the
  equivalent idle-wait shapes: `read -t N`, and `time.sleep(N)`/`select(..., N)`
  inside a `python -c`/`perl -e` payload or heredoc script.
- Blocks a `pgrep -f`/`pkill -f` inside a loop, in the foreground *and* in the
  background: the pattern is part of the loop shell's own command line, so the
  loop matches itself and never exits (observed: two waiters held 8 h on
  `while pgrep -f "migrate.py"`). Use `pgrep -x <name>` or test the target
  directly.
- Blocks `gh pr merge` without `--match-head-commit` (merges must pin the
  verified head), `osascript`, and a bare `git push --force`/`-f` (use
  `--force-with-lease`).
- Blocks `gh auth switch`/`login`/`logout`/`refresh`: they change the shared
  active gh account for every Hermes process, so act as another account with
  `GH_TOKEN=$(gh auth token -u <user>) <command>` instead (default
  `acme-bot`); `gh auth status`/`token` stay allowed.
- Blocks an unsigned `git commit`/`git commit --amend` in a repo that requires
  signed commits: a `github.com` origin whose `origin/<default>` last-20 history
  is >= 90% signed decides it, from local data only (Azure remotes never
  require signing); the block message names the exact command with `-S` and the
  `git config` lines that make signing permanent. Verdict cached per repo for
  10 minutes, no network, fails open.
- Blocks recursive deletion of a live Hermes home (`rm` with a recursive flag,
  `find -delete`, `rsync --delete` on a protected destination, python
  `rmtree`-family calls), including through a runner chain after a pipe.
- Blocks a `gh pr create --base` or `az repos pr create --target-branch`/`-t`
  (and a `git checkout -b`/`switch -c`/`worktree add -b`/`branch` start point)
  that names a long-lived integration branch (`main`, `master`, `develop`,
  `trunk`, `dev`) different from the repo's real default branch — read from
  `refs/remotes/origin/HEAD` in the command's repo, never hard-coded; a stacked
  PR on a feature or release branch and an omitted base pass, and so does a
  promotion PR whose head (`--head`/`-H`, Azure `--source-branch`, or the
  branch checked out in the call's repo) is the default branch; the block
  message gives the corrected command with the real default branch, swapping
  only the flagged branch so quoted `--title`/`--body` text is preserved
  verbatim (and naming the passing call shape when no single edit works).
- Turns `notify=true` on for a bounded `background=true` terminal job that declares no outcome
  channel (`notify=true`, a notify pattern list, or a positive `heartbeat`), so its completion
  event wakes the agent. Workers get the insert directly; the lead gets a block whose message
  names the exact call shape.
- Blocks a `paas-promote` dispatch to `des` without the `auto-approval` label, in both dispatch
  shapes (`gh workflow run` and the raw JSON `curl .../workflows/dispatches` POST) and after
  stripping whitespace around `LABELS` items.
- Blocks `kanban_attach` with more than 256 characters of inline base64
  (attach by path instead) and normalizes known-bad optional arguments.
- Rewrites a `terminal`/`process_manage wait` `timeout` above the call
  deadline (`timeouts.tools.sequential_call`, else 420 s) down to 15 s under
  it (405 by default) so the tool's own timeout returns partial output
  instead of the host killing the whole slot; a `terminal` result whose own
  rewritten timeout killed it gets a note naming the
  `background=true, notify=true` replacement call (the host's own deadline
  kill produces a result no transform hook sees, so those deaths stay
  silent).
- Tells the model when it moved a foreground wait to the background: that call's
  `Background process started` result gains a `rewritten_to_background` field
  (not finished, do not re-run, how to collect the outcome), so the model
  knows its wait moved.
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
  and the session's recorded cwd, not only an in-string `cd`; the block message
  names the exact workspace-targeted rewrite of the offending call
  (`mkdir -p $HERMES_KANBAN_WORKSPACE/<name>`).
- Blocks writes (`write_file`, `patch`, terminal redirect/tee/sed -i/perl -i,
  inline python/perl writes, `cp`/`mv`/`rm`/`touch`/`mkdir`, `git checkout --`/
  `restore`/`apply`) that target an installed plugin checkout
  (`~/.hermes/plugins/<name>/` or `~/.hermes/profiles/<profile>/plugins/<name>/`
  whose nearest git work tree carries an origin remote) and names the exact
  cycle instead: the matching working clone under `~/git_repos`, its
  `git worktree add` command, and `hermes -p <profile> plugins update <name>`
  for every installed copy.
- When the profile config sets `kanban.default_assignee`, rejects a
  `kanban_create` whose `dir`/`worktree` workspace_path leaves the placement
  convention.
- Escalates a call the same guard already blocked twice in one session: the
  repeated block result is REWRITTEN (never blocked in advance — every call
  still reaches the real guards) into a verdict that says repetition cannot
  pass and names the way out — the exact change the original block states, or
  `kanban_block(reason="...")` in a worker session — with the original block
  text kept below it. `kanban_block`/`kanban_heartbeat` results are never
  escalated; every error fails open. Reaches registry-dispatched tools
  (`kanban_*`, skill, MCP); terminal-class blocked results do not fire
  transform hooks in the core, so repeats of those stay out of reach without
  a core change.

### `kanban-guards` — Kanban worker, lead and wake discipline

Answers: *is this lifecycle call, board read or wake legitimate?*

- Worker gates: force a budget checkpoint comment before a run is cut at the
  iteration cap; keep delegated cards on the configured worker profile; block
  `kanban_complete` until `metadata.acceptance` maps every numbered Acceptance
  item to evidence; block a `kanban_request_review`/`kanban_complete` whose diff
  added unused private Python names or imports (leading-underscore module-level
  names nothing references — one `git grep -w` per batch decides, test references
  count, `__all__` names and dunders are exempt — and imports the module never
  uses, with `__init__.py` re-exports exempt); keep the published-PR completion
  duty visible; block
  contacts with people on GitHub from worker terminals; bounce
  a narrated stop after a blocked lifecycle call; block `kanban_schedule`;
  refuse a handoff that preserves a backup copy of the delivered work; block a
  scratch-workspace handoff that names deliverables only inside the workspace
  (uncovered paths in the handoff text, or unpushed git commits with no bundle
  or patch artifact) — the workspace is deleted on completion.
- Lead gates: steer a lead terminal away from `hermes kanban create` (the CLI
  leaves the new card without a chat subscription) toward the `kanban_create`
  tool, naming the exact call rebuilt from the blocked command's flags;
  scope a lead chat's `kanban_list` result to the cards its own chat
  thread is subscribed to.
- Wakes: run every Kanban wake on one shared event-driven chassis over
  `board_watch` — stale parked cards, review-loop thresholds, the churn corner,
  waiting-on-external-gate runs, dispatcher stalls and unreconciled delivery
  notices — injected into the card's subscribed chat within one dispatcher tick.
  Wakes need `plugins.entries.kanban-guards.allow_gateway_injection: true`.

### `text-guards` — banned characters in the reply

Answers: *does anything the agent emits contain a character the operator forbids?*

- `transform_llm_output` cleans the final reply; `pre_tool_call` cleans the tool arguments, so
  comment bodies, files written by `write_file`/`patch` and shell commands are covered too.
- Replaces every occurrence of the section sign, with its surrounding spaces, by a single space.
- Never blocks a call or touches other text; returns `None` when there is nothing to clean.

### `provider-guards` upstream provider failures

Answers: *does a provider failure that the core calls permanent get retried?*

- `transform_api_error_classification` classifies HTTP 424 `byok_upstream_provider_failed` as a retryable `server_error`, so the core retries in the same session instead of ending the turn.
- Any other status, code or message is left to the core. Fails open.

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

Do not pass `--ref <sha>` for a plugin that must keep receiving updates: the
flag writes `pinned: true` in the install metadata and `hermes plugins update`
then refuses to move it. Install without `--ref`, and `hermes plugins update
<name>` tracks the default branch. Reinstalling with `--force` keeps the old
`pinned` flag, so remove and install again instead.

## Layout

```
shell-guards/       one self-contained plugin (modules + plugin.yaml + tests)
workspace-guards/   one self-contained plugin
kanban-guards/      one self-contained plugin
jot-guards/         placeholder; its nudge gate was removed (shaping, not enforcement)
recall-guards/      one self-contained plugin
goal-waker/         one self-contained plugin (resumes parked goals on messaging chats; one wake
                    per goal-row advance)
text-guards/        one self-contained plugin
provider-guards/    one self-contained plugin (retry an upstream HTTP 424)
tests/              cross-plugin checks (helper-copy divergence, transform winner)
pytest.ini          runs all folders plus tests/ in one command
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

## Privacy guard

This repository is public. `tests/test_public_privacy.py` fails the root suite
when any tracked file carries an employer identifier, and `tools/check_privacy.sh`
applies the same pattern as a pre-push hook so a leak never leaves the machine.
A fresh clone has no hook installed; link the versioned script into the local
hooks directory:

```
ln -s ../../tools/check_privacy.sh .git/hooks/pre-push
```

The hook rejects any push whose added lines match, so never bypass it with
`git push --no-verify`; fix the content instead.

