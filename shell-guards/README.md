# shell-guards

Self-contained agent-guards plugin for terminal command hygiene.

## Blocks

- Foreground `sleep` of 30s or more, or a `for`/`while`/`until` polling loop holding a `sleep` of 15s or more. Run the wait with `background=true` and a notify channel, or poll once and re-issue without sleeping. Heredoc bodies and quoted strings are exempt.
- `gh pr merge` without `--match-head-commit` — merges must pin the verified head.
- `osascript` — desktop automation goes through ego-browser.
- `git push` with a bare `--force`/`-f` — use `--force-with-lease`.
- Recursive deletion of a live Hermes home (`rm` with a recursive flag, `find -delete`, `rsync --delete` on a protected destination, python `rmtree`-family calls in `-c` payloads or heredocs) aimed at `$HERMES_HOME`, `~/.hermes`, any user's literal absolute form, the `profiles` subtree or an ancestor of these, including stages reached through a runner chain after a pipe. Deeper nests and quoted/heredoc prose stay allowed; for tests create a throwaway home with `mktemp -d` inside the scratch workspace and delete the printed directory by its literal path.
- A `paas-promote` workflow dispatch targeting `des` without the `auto-approval` label in `LABELS` — the deployment pull request would stay open until someone notices. The block message names the corrected `-f LABELS=...` flag.
- `kanban_attach` with more than 256 characters of inline base64 — the model transcribes those bytes and corrupts them; attach by path (`artifacts=[absolute path]`).

## Requires

- A `terminal` call with `background=true` that is not a long-lived server (vite, http.server, uvicorn, gunicorn, local proxies, databases, containers — detected conservatively from the final statement) declares an outcome channel: `notify=true`, a non-empty notify pattern list, or a positive `heartbeat`. Pty sessions are exempt.

## Normalizes

- Empty-string optional Kanban arguments (`assignee` in `kanban_list`, `completion_contract` in `kanban_create`) become absent.
- A Kanban `task_id` sent as the literal `$HERMES_KANBAN_TASK` becomes absent, so the tool falls back to the worker's own task.
- A `skill_view` name of the form `x:x` becomes `x`.

## Skill sizes

- Blocks `skill_manage`/`write_file`/`patch`(replace) calls that would push a skill markdown file over its limit (SKILL.md 6000, other skill `.md` 12000 characters; shrinking an over-limit file is always allowed).
- Flags after the fact a `terminal`/`execute_code`/unified-diff `patch` call that left one over its limit: the tree-split instruction is appended to the tool result, nothing is reverted.

Commands are matched at command-word position outside quoted strings, so mentions in strings, grep patterns and python bodies pass. The guards catch slips, not deliberate evasion, and fail open. `post_tool_call` is registered as a no-op placeholder hook; lifecycle and board gates live in the sibling kanban-guard plugins.
