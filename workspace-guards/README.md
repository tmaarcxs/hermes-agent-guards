# workspace-guards

Self-contained Hermes plugin: **where new entries and card workspaces may live.**

Moved out of the monolithic `agent-guards` plugin (base commit `90456b2`) as
part of the three-plugin monorepo split (`shell-guards`, `workspace-guards`,
`kanban-guards`).

## What it answers

| Module | Question it answers |
|---|---|
| `home_litter_gate` | Is this terminal command about to drop a new top-level entry directly under the home directory? Block it with the allowed write locations spelled out (the task workspace `HERMES_KANBAN_WORKSPACE`, the Hermes scratch dir `~/.hermes/cache/scratch`, TMPDIR/tmp, the kanban attachments dir, `<main-repo>/.worktrees/<task>/` or `~/git_repos/ad-hoc/<task>/` for repo work) AND the exact workspace-targeted rewrite of the offending call itself (`mkdir -p $HERMES_KANBAN_WORKSPACE/<name>`, derived from the real call's flags and operands), so the retry is a copy instead of a re-invented path — resolves the real cwd from the `workdir` argument and the session's recorded cwd, not only an in-string `cd`. |
| `plugin_local_edit_gate` | Is this write about to land inside an INSTALLED plugin checkout (`~/.hermes/plugins/<name>/` or `~/.hermes/profiles/<profile>/plugins/<name>/` whose nearest git work tree carries an origin remote)? Block it and name the exact cycle instead: the matching working clone under `~/git_repos` (found by origin URL), its `git -C <clone> worktree add .worktrees/<slug> -b <branch> origin/main` command, and `hermes -p <profile> plugins update <name>` for every installed copy. Terminal writes covered include redirections, `tee`, in-place `sed` **and in-place `perl` (`-i`/`-pi`/`-i.bak` plus flag clusters)**, python and perl inline writes (`open(path, 'w')` and perl `open(F, '>', path)`, both handle and lexical-handle forms), `cp`/`mv`/`rsync`/`install`, `touch`/`mkdir`/`rm`, and `git checkout -- <file>`/`git restore`/`git apply` there. Reads, sanctioned `hermes plugins` verbs (update/install/enable/disable/list/doctor), read-only git shapes and writes inside a worktree of the working clone pass; detection is dynamic, so a locally developed non-git plugin stays writable. |
| `workspace_convention` | Does this `kanban_create` place its `dir`/`worktree` workspace outside the convention (`<main-checkout>/.worktrees/<task>/` or `~/git_repos/ad-hoc/`), or point any kind — even `scratch` or no kind — at a main checkout, the home dir or a direct child of it? Reject it. Active only when `kanban.default_assignee` is configured. |
| `repeat_block_gate` | Did the same guard just reject this exact call (same tool, same guard, same argument hash) for the second+ time in this session with no different call in between? Rewrite the repeated block into an escalation that says repetition cannot pass and names the two permitted ways out — make the change the original block states, or (worker sessions only, `HERMES_KANBAN_TASK` set) call `kanban_block(reason="<guard>: ...")` — keeping the original block text with the exact corrected call below it. Acts only on the outcome (rides `transform_tool_result`): every call still reaches the real guards, so a veto that flips between two identical attempts is never punished. `kanban_block`/`kanban_heartbeat` results are never escalated (they are the escape hatch). State is one (key, count) pair per session; any non-block result resets it; every error fails open (the original block text is delivered unchanged). Limit, stated honestly: the core's agent-loop paths deliver `terminal`/`write_file`/`patch` blocked results without firing transform hooks, so the gate's rewrite reaches guard blocks of registry-dispatched tools (`kanban_*`, skill, MCP) — the majority of measured repeat events — but not terminal-class repeats; fixing that needs a Hermes-core change, which is out of this plugin's scope. |

## Install

```
hermes plugins install tmaarcxs/hermes-agent-guards#workspace-guards
```

Registers two hooks: `pre_tool_call`, `transform_tool_result`.

## Layout

- `_policy.py` — local copy of the two shared helpers these modules need
  (`configured_worker()`, `RETRY_INSTRUCTION`); the monorepo root's
  `tests/test_helper_copies_agree.py` fails if this copy drifts from its
  siblings. This folder imports nothing from sibling plugins.
- `_paths.py` path and command-segment helpers `home_litter_gate` uses.
- `tests/conftest.py` registers the package under its canonical module name
  (`sys.modules["workspace_guards"]`), so the moved tests run verbatim under the
  new package name `workspace-guards`. Run with
  `PYTHONPATH="$HOME/.hermes/hermes-agent"` and the Hermes venv python (3.10+):

  ```
  cd workspace-guards && ~/.hermes/hermes-agent/venv/bin/python -m pytest -q
  ```
