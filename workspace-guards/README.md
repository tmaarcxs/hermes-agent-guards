# workspace-guards

Self-contained Hermes plugin: **where new entries and plugin edits may go.**

Moved out of the monolithic `agent-guards` plugin (base commit `90456b2`) as
part of the plugin monorepo split.

## What it answers

| Module | Question it answers |
|---|---|
| `home_litter_gate` | Is this terminal command about to drop a new top-level entry directly under the home directory? Block it with the allowed write locations spelled out (the task workspace `HERMES_KANBAN_WORKSPACE`, the Hermes scratch dir `~/.hermes/cache/scratch`, TMPDIR/tmp, the kanban attachments dir, `<main-repo>/.worktrees/<task>/` or `~/git_repos/ad-hoc/<task>/` for repo work) AND the exact workspace-targeted rewrite of the offending call itself (`mkdir -p $HERMES_KANBAN_WORKSPACE/<name>`, derived from the real call's flags and operands), so the retry is a copy instead of a re-invented path — resolves the real cwd from the `workdir` argument and the session's recorded cwd, not only an in-string `cd`. |
| `plugin_local_edit_gate` | Is this write about to land inside an INSTALLED plugin checkout (`~/.hermes/plugins/<name>/` or `~/.hermes/profiles/<profile>/plugins/<name>/` whose nearest git work tree carries an origin remote)? Block it and name the exact cycle instead: the matching working clone under `~/git_repos` (found by origin URL), its `git -C <clone> worktree add .worktrees/<slug> -b <branch> origin/main` command, and `hermes -p <profile> plugins update <name>` for every installed copy. Terminal writes covered include redirections, `tee`, in-place `sed` **and in-place `perl` (`-i`/`-pi`/`-i.bak` plus flag clusters)**, python and perl inline writes (`open(path, 'w')` and perl `open(F, '>', path)`, both handle and lexical-handle forms), `cp`/`mv`/`rsync`/`install`, `touch`/`mkdir`/`rm`, and `git checkout -- <file>`/`git restore`/`git apply` there. Any mutating git call (`reset`, `switch`, `branch -D`, `stash`, `clean`, `merge`, `commit`, `rm`, `worktree add`...) whose repository is an installed checkout is a write too, whether it is reached through `-C`, an in-string `cd` or the terminal tool's `workdir`. `sh`/`bash -c` scripts are scanned as nested commands and python heredoc bodies (`python3 - <<'EOF'`) contribute the paths their creation calls write. Reads, sanctioned `hermes plugins` verbs (update/install/enable/disable/list/doctor), read-only git shapes (status, log, diff, fetch, `pull`/`merge --ff-only`, `branch` listing, `worktree list`, `stash list`, `config --get`, rev-list, reflog, cat-file, ls-remote, grep...) and writes inside a worktree of the working clone pass; detection is dynamic, so a locally developed non-git plugin stays writable. |

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
- `_paths.py` path and command-segment helpers `home_litter_gate` uses.
- `tests/conftest.py` registers the package under its canonical module name
  (`sys.modules["workspace_guards"]`), so the moved tests run verbatim under the
  new package name `workspace-guards`. Run with
  `PYTHONPATH="$HOME/.hermes/hermes-agent"` and the Hermes venv python (3.10+):

  ```
  cd workspace-guards && ~/.hermes/hermes-agent/venv/bin/python -m pytest -q
  ```
