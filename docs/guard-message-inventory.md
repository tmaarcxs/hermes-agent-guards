# Guard-message inventory: shell-guards + workspace-guards (card t_d295d26a)

Every block/refusal message in the two plugins' scope, with the alternative it names.
Base for the diff: `origin/main` commit `47239d8`. Messages marked CHANGED were
reworded by this card; messages marked OK already name a concrete alternative and
were left unchanged (their wording is pinned by tests).

## shell-guards

| Gate (source) | Trigger | Before text (short) | After text | Alternative named |
|---|---|---|---|---|
| `__init__` foreground sleep (terminal_policy) | foreground `sleep` >= 5s | CHANGED | "foreground sleep >= 5s in a foreground terminal call: run the wait as `terminal(command=<the same command>, background=true, notify=true)` instead - its completion message wakes you, so never sleep or poll" | exact `terminal(background=true, notify=true)` call with the same command |
| `__init__` polling loop (terminal_policy) | loop holding `sleep` >= 3s | CHANGED | "polling loop with sleep >= 3s: run the whole wait as `terminal(command=<the same loop>, background=true, notify=[<ready pattern>])` instead - the ready-pattern match wakes you; never sleep or poll" | exact call shape with a ready-pattern notify list |
| `__init__` gh pr merge (terminal_policy) | `gh pr merge` without `--match-head-commit` | CHANGED (was "gh pr merge needs --match-head-commit <verified head sha>") | "gh pr merge without a pinned head: add `--match-head-commit <head-sha-of-the-pull-request>` to this exact call (read the sha from `gh pr view <number> --json headRefOid`), so the merge applies to the verified head" | exact flag + how to obtain the sha |
| `__init__` pgrep loop (terminal_policy) | `pgrep -f`/`pkill -f` inside a loop | OK (unchanged) | "pgrep/pkill -f inside a loop self-matches the loop's own command line, so the loop never exits: match the exact process name (`pgrep -x name`) or test the target directly (e.g. a pidfile or a port check)" | `pgrep -x` / pidfile / port check |
| `__init__` osascript (terminal_policy) | `osascript` | OK (unchanged) | "osascript is not allowed: use ego-browser for browser work" | tool name |
| `__init__` force-push (terminal_policy) | bare `git push --force`/`-f` | OK (unchanged) | "git push --force/-f is not allowed: use --force-with-lease" | exact flag |
| `_GH_AUTH_MESSAGE` (terminal_policy) | `gh auth switch/login/logout/refresh` | OK (unchanged) | "...set it in the command itself: `GH_TOKEN=$(gh auth token -u <user>) <command>`. The default active account is acme-bot." | exact command template |
| hermes_home_gate | recursive deletion of a live Hermes home | OK (unchanged) | "For tests, create a throwaway home with `mktemp -d` inside the scratch workspace and run the command as `env HERMES_HOME=\"$(mktemp -d ...)\" <test cmd>`; delete the printed directory by its literal path." | exact recipe |
| hermes_home_search_gate | recursive search over the whole Hermes home | OK (unchanged) | "Search the directory that holds what you need (skills, memories, plugins, profiles/<name>, kanban, hermes-agent), use search_files, or recall with hindsight_recall." | three concrete alternatives |
| process_poll_gate | `process_manage action=poll` | CHANGED | "poll is not allowed - it costs a full turn and returns nothing to act on. For the same process now: `process_manage(action=\"wait\", session_id=...)` returns the moment it exits, `process_manage(action=\"log\", session_id=...)` reads its output, and a job launched with `terminal(background=true, notify=true)` sends its completion as a message. If the wait comes back interrupted with no new output, end the turn and re-issue the wait in a later turn - never poll, never sleep." | two exact calls + the wait-again loop exit |
| background_notify_gate | bounded `background=true` job with no outcome channel (block path) | CHANGED (compactness cap 320 bytes kept) | "this background=true job has no outcome channel. Add one: `terminal(command=<same command>, background=true, notify=true).`" (+ RETRY_INSTRUCTION) | exact call shape |
| git_stash_gate | `git stash` write | OK (unchanged) | "To compare against the base use `git show HEAD:<file>`, `git diff`, or `git worktree add /tmp/<name> HEAD`." | three exact commands |
| `__init__` attach cap (terminal_policy) | `kanban_attach` inline base64 > 256 chars | OK (unchanged) | "inline base64 over 256 characters arrives corrupted; attach by path: artifacts=[absolute path], then sha256-compare the stored file." | exact argument + verification |
| paas_promote_gate | paas-promote to des without auto-approval | OK (unchanged) | "Add the label to the dispatch: `-f LABELS=<given>,auto-approval`." | exact corrected flag built from the call |
| plugin_source_gate | enable/update with dirty or unpushed plugin tree | OK (unchanged) | "Our plugin rule: make the change in the repository, COMMIT it, PUSH it, then activate the pushed revision. Commit and push first, then retry." | the two commands in order |
| skill_size.check | skill .md over size limit | OK (unchanged) | "Trim at least N characters, or take the cheaper route below." + tree-split instruction (references/<topic>.md route) | exact action + split route |
| **core rewrite** `rewrite_poll_instructions` (terminal_policy, new transform) | Hermes core's poll-only promoted-process note attached to an over-cap foreground call promoted to background | NEW | "Requested foreground timeout {requested}s exceeds the {cap}s cap ... Do NOT re-run it. Do not poll it either: this session cannot receive completion notifications, so retrieve the outcome when you need it with `process_manage(action=\"wait\", session_id=<session_id>)` (returns the moment the process exits) or `process_manage(action=\"log\", session_id=<session_id>)` (reads the output so far). If the wait comes back interrupted with no new output, END the turn and re-issue the wait in a later turn - never sleep, never poll." | replaces core's "poll it with process(action=\"poll\", session_id=...) until it exits" (which process_poll_gate blocks, looping the worker); the notify-carrying variant of the core note is left untouched |

## workspace-guards

| Gate | Trigger | Status | Alternative named |
|---|---|---|---|
| home_litter_gate | new top-level entry under `~` | OK (unchanged; the wording already names every location exactly) | task workspace `HERMES_KANBAN_WORKSPACE`, Hermes scratch `~/.hermes/cache/scratch`, TMPDIR/tmp, kanban attachments, `<main-repo>/.worktrees/<task>/` or `~/git_repos/ad-hoc/<task>/` for repo work, `~/.hermes/kanban.db` for the DB |
| workspace_convention | `kanban_create` workspace_path outside the convention | OK (unchanged) | `<main-checkout>/.worktrees/<task>/` (linked worktree), `~/git_repos/ad-hoc/<name>/` for no-repo work, default scratch `~/.hermes/kanban/workspaces/<task>` (kind=scratch, no path); "Use scratch by default." |

Note: the card's top-blocked gate `workspace_write_gate` was already removed upstream
(commit `5584b65`, PR #96) before this card branched, so its two message shapes (git
mutation outside the workspace / write to a protected path) no longer exist; the README
and the `git-ops` skill document the removal. The 27 empty-reason blocks claimed in the
card did not reproduce: a scan of all worker tool results containing `agent-guards
blocked` (6,235 rows in the worker profile state.db plus agent.log.1-3) found zero
blocks with an empty reason text; every block carries a gate stamp and a reason.
